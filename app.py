"""
Streamlit UI — friend-group edition.

Adds (this session): auth gate, SQLite watchlist, hit-rate framing from
backtest, basket-not-stock-picker banner, data-as-of timestamps, onboarding
panel, misuse flags on every result row, error resilience around major paths.

    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from data_layer import MODALITIES
import engine
import mispricing
import valuations
import userdb
import misuse_flags
import custom_tickers as ct

ROOT = Path(__file__).parent
DATA = ROOT / "data"

st.set_page_config(page_title="Biotech Mispricing Engine", layout="wide")


# ===========================================================================
# Auth — shared password gate for the friend group
# ===========================================================================

userdb.ensure_schema()


def _gate() -> str | None:
    """Returns username if authenticated, else stops the app."""
    if st.session_state.get("auth_ok"):
        return st.session_state.get("username")

    st.title("Biotech Mispricing Engine")
    st.caption("Private build — invite-only.")
    if userdb.is_using_default_password():
        st.warning(
            "⚠️ **GROUP_PASSWORD env var not set** — this app is running with the "
            "default placeholder password (`change-me-before-sharing`). Anyone "
            "with the URL who guesses or reads the source can log in. **Set "
            "GROUP_PASSWORD in your hosting secrets before sharing.**"
        )
    with st.form("auth"):
        username = st.text_input("Pick a username (only used to label your watchlist)")
        password = st.text_input("Group password", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if userdb.check_group_password(password):
            if not username.strip():
                st.error("Pick a username.")
                st.stop()
            st.session_state.auth_ok = True
            st.session_state.username = username.strip()
            userdb.add_user(username.strip())
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


username = _gate()


# ===========================================================================
# Helpers
# ===========================================================================

def fmt_money(m: float | None) -> str:
    if m is None or pd.isna(m):
        return "—"
    if m >= 1000:
        return f"${m/1000:.2f}B"
    return f"${m:.0f}M"


def edgar_link(ticker: str) -> str:
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=10-K&dateb=&owner=include&count=40"


def file_age_human(p: Path) -> str:
    if not p.exists():
        return "never"
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    delta = datetime.now(timezone.utc) - mtime
    h = int(delta.total_seconds() / 3600)
    if h < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


@st.cache_data(show_spinner=False)
def cached_universe() -> pd.DataFrame:
    return engine.load_universe()


@st.cache_data(show_spinner=False)
def cached_rank(ticker: str, top_n: int, exclude_megas: bool, same_region: bool, year: int | None = None, date: str | None = None) -> pd.DataFrame:
    return engine.rank(ticker, top_n=top_n, exclude_megas=exclude_megas, same_region_only=same_region, year=year, date=date)


@st.cache_data(show_spinner=True)
def cached_anchor_screen(ticker: str, n_peers: int, top_n: int, same_region: bool, year: int | None = None, date: str | None = None) -> pd.DataFrame:
    return mispricing.anchor_screen(ticker, n_peers=n_peers, top_n=top_n, same_region_only=same_region, year=year, date=date)


@st.cache_data(show_spinner=False)
def cached_north_star_state(ticker: str, year: int | None = None, date: str | None = None) -> dict:
    if date:
        return engine.get_north_star_state_at_date(ticker, date)
    return engine.get_north_star_state(ticker, year)


@st.cache_data(show_spinner=False)
def cached_available_years(ticker: str) -> list[int]:
    from historical import available_years
    return available_years(ticker)


@st.cache_data(show_spinner=False, ttl=86400)
def cached_recent_filings(ticker: str, limit: int = 8) -> list[dict]:
    from historical import recent_filings
    return recent_filings(ticker, limit=limit)


def safe_call(fn, *args, **kwargs):
    """Wrap a UI-triggering call so a thrown exception shows a friendly message
    instead of a stack trace, but the trace still lands in the server log."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        st.error(f"Something went wrong while running this query: {type(e).__name__}: {e}")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
        st.stop()


# ===========================================================================
# Sidebar — data freshness, watchlist, disclaimer
# ===========================================================================

universe = cached_universe()

st.sidebar.title("Biotech Mispricing")
st.sidebar.caption(f"Logged in as **{username}**")
if st.sidebar.button("Sign out", use_container_width=True):
    st.session_state.clear()
    st.rerun()

# Data freshness — visible on every page so staleness can't hide
val_path = DATA / "valuations.parquet"
uni_path = DATA / "current_universe.parquet"
health_path = DATA / "_refresh_health.json"

st.sidebar.markdown("**Data freshness**")
st.sidebar.caption(
    f"Universe: {file_age_human(uni_path)}\n"
    f"\nValuations cache: {file_age_human(val_path)}\n"
    f"\nLast nightly refresh: {file_age_human(health_path)}"
)
if health_path.exists():
    try:
        h = json.loads(health_path.read_text())
        if not h.get("ok", True):
            st.sidebar.error("Last refresh had errors — see server logs.")
    except Exception:
        pass

st.sidebar.markdown("---")

# Watchlist (persisted via JSON)
wl_rows = userdb.list_watchlist(username)
st.sidebar.subheader(f"Watchlist ({len(wl_rows)})")
if wl_rows:
    wl_df = pd.DataFrame(wl_rows)
    st.sidebar.dataframe(wl_df[["ticker", "name", "added_at"]], hide_index=True, use_container_width=True)

    # === RICH CSV EXPORT ===
    # Joins universe + valuations + flags + cheapness signals so the export
    # is actually useful (current cash, mkt cap, EV, modality, warnings).
    @st.cache_data(show_spinner=False, ttl=300)
    def _build_rich_export(rows: tuple) -> bytes:
        """rows = tuple of (ticker, name, source, note, added_at) tuples for cache stability."""
        wl = pd.DataFrame([{"ticker": r[0], "name": r[1], "source": r[2], "note": r[3], "added_at": r[4]} for r in rows])
        # Pull current valuations + universe context
        try:
            val_cache = pd.read_parquet(val_path) if val_path.exists() else pd.DataFrame(columns=["ticker"])
        except Exception:
            val_cache = pd.DataFrame(columns=["ticker"])
        val_cache = val_cache.copy()
        if "marketCap" in val_cache.columns:
            val_cache["mkt_cap_m"] = val_cache["marketCap"] / 1e6
            val_cache["cash_m"] = val_cache["totalCash"] / 1e6
            val_cache["debt_m"] = val_cache["totalDebt"] / 1e6
            val_cache["ev_m"] = val_cache["enterpriseValue"] / 1e6
            val_cache["price"] = val_cache["currentPrice"]

        uni_lite = universe[["ticker", "primary_modality", "size_band", "region", "industry"]].copy()

        out = (wl
               .merge(uni_lite, on="ticker", how="left")
               .merge(val_cache[[c for c in ["ticker", "mkt_cap_m", "cash_m", "debt_m", "ev_m", "price", "fetched_at"] if c in val_cache.columns]], on="ticker", how="left"))

        # Friendly column ordering
        out["edgar_url"] = out["ticker"].apply(edgar_link)
        out = out.rename(columns={
            "name": "company",
            "mkt_cap_m": "mkt_cap_$M",
            "cash_m": "cash_$M",
            "debt_m": "debt_$M",
            "ev_m": "ev_$M",
            "fetched_at": "valuations_fetched_at",
        })
        ordered_cols = [c for c in [
            "ticker", "company", "primary_modality", "size_band", "region", "industry",
            "mkt_cap_$M", "cash_$M", "debt_$M", "ev_$M", "price",
            "source", "note", "added_at",
            "valuations_fetched_at", "edgar_url",
        ] if c in out.columns]
        return out[ordered_cols].to_csv(index=False).encode()

    rows_tuple = tuple((r["ticker"], r["name"], r.get("source") or "", r.get("note") or "", r["added_at"]) for r in wl_rows)
    st.sidebar.download_button(
        "📥 Download enriched CSV",
        _build_rich_export(rows_tuple),
        f"watchlist_{username}.csv",
        "text/csv",
        use_container_width=True,
        help="Includes ticker, company, modality, size, region, current mkt cap / cash / debt / EV, your note, added timestamp, and EDGAR link",
    )

    rm_pick = st.sidebar.selectbox("Remove a name", options=[""] + wl_df["ticker"].tolist())
    if rm_pick and st.sidebar.button(f"Remove {rm_pick}"):
        userdb.remove_watchlist(username, rm_pick)
        st.rerun()
else:
    st.sidebar.caption("No companies saved yet.")

st.sidebar.markdown("---")

# === REQUEST A TICKER — let users add stocks not in our universe ===
st.sidebar.subheader("➕ Request a ticker")
st.sidebar.caption(
    "Add a biotech/pharma ticker we missed. Validates against yfinance, "
    "then it shows up in the Anchor + Screener dropdowns."
)
new_ticker_input = st.sidebar.text_input("Ticker symbol", placeholder="e.g. ACAD, GH, SLN.L", key="new_ticker_input")
if st.sidebar.button("Add to universe", use_container_width=True, disabled=not new_ticker_input):
    with st.sidebar:
        with st.spinner(f"Validating {new_ticker_input.upper()}..."):
            result = ct.add_ticker(new_ticker_input.upper(), requested_by=username)
    if result["ok"]:
        rec = result["record"]
        st.sidebar.success(f"✓ Added **{rec['ticker']}** — {rec['name']}")
        st.sidebar.caption(f"Mkt cap: ${rec['mkt_cap_m']:,.0f}M · Region: {rec['region']} · Industry: {rec['industry']}")
        if rec.get("non_pharma_warning"):
            st.sidebar.warning(rec["non_pharma_warning"])
        # Clear the universe cache so the new ticker shows up
        cached_universe.clear()
        st.sidebar.info("Refresh the page (or pick a different anchor) to see it in the dropdowns.")
    else:
        st.sidebar.error(result["error"])

# Show current custom additions
custom_list = ct.list_custom_tickers()
if custom_list:
    with st.sidebar.expander(f"Currently added ({len(custom_list)})"):
        for c in custom_list[-10:]:  # show last 10
            st.markdown(f"- **{c['ticker']}** — {c.get('name', '')[:40]}  \n  *added by {c.get('requested_by', '?')}*")
        if st.button("Clear all custom additions", key="clear_custom"):
            for c in custom_list:
                ct.remove_custom_ticker(c["ticker"])
            cached_universe.clear()
            st.rerun()

st.sidebar.caption(
    "⚠️ Custom additions persist in this Streamlit container but may reset "
    "when Streamlit recycles the app (every ~10 min idle). Add Supabase "
    "for permanent storage."
)

st.sidebar.markdown("---")
st.sidebar.error(
    "**Not investment advice.** This is a screen, not a stock-picker. "
    "Single-name results are noisy. Diversify."
)


# ===========================================================================
# Main — Tabs
# ===========================================================================

tab_anchor, tab_screener, tab_backtest, tab_calibrate, tab_about = st.tabs(
    ["Anchor mode", "Free screener", "Backtest evidence", "Calibrate", "How to read this"]
)


# ============================ ANCHOR TAB =============================
with tab_anchor:
    st.header("Anchor mode")

    # Onboarding panel — defaults to expanded for new users
    with st.expander("📖 First time here? Read this first.", expanded=(len(wl_rows) == 0)):
        st.markdown(
            """
**What this does.** You pick a north-star company. We find its 30 closest peers
by financial shape and modality, then rank that peer set by cheapness signals
(net cash relative to market cap, EV/cash, peer-relative valuation, runway).

**Why a year picker?** "What looks like Vertex *today*" returns megacaps. "What
looks like Vertex in 2010 — pre-Kalydeco, $7B mid-cap with a clinical pipeline"
returns today's mid-cap clinical biotechs. That's usually the more interesting
question.

**Read the cheapness score as a percentile within the peer set, not an
absolute rating.** "95" means cheapest in this peer pool — not "this stock
is 95% off."

**Treat the output as a basket.** Our 2018 backtest showed cheap names had
a 50% hit rate but +287% mean 1Y return — meaning you win by owning the
basket, not picking one name. See the **Backtest evidence** tab.

**Try this first:** select VRTX, toggle time-machine mode, slide to 2010,
hit run. You'll see today's $5–8B mid-cap clinical biotechs ranked by cheapness.
"""
        )

    sorted_universe = universe.sort_values("mkt_cap_m", ascending=False, na_position="last")
    options = [f"{r['ticker']} — {r['name']}" for _, r in sorted_universe.iterrows()]
    default_idx = next((i for i, opt in enumerate(options) if opt.startswith("VRTX")), 0)
    ns_choice = st.selectbox("North-star company", options, index=default_idx, key="anchor_ns")
    ns_ticker = ns_choice.split(" — ")[0]

    # Time-machine mode — year OR exact date
    with st.spinner("Checking historical data availability..."):
        years_avail = safe_call(cached_available_years, ns_ticker)

    use_year = st.checkbox(
        "Time-machine mode — match against this company's HISTORICAL state",
        value=False,
        key="use_year",
        help="Replaces the north-star's financials with the chosen year-end (or exact-date) state from SEC EDGAR. Modality stays today's tag.",
    )

    year_value: int | None = None
    date_value: str | None = None
    if use_year:
        if not years_avail:
            st.warning(f"No XBRL data on EDGAR for {ns_ticker} (likely IFRS filer or pre-XBRL).")
        else:
            mode = st.radio(
                "Precision",
                options=["Year-end (default)", "Exact date (uses the most recent 10-Q)"],
                horizontal=True,
                key="time_mode",
            )
            if mode.startswith("Year"):
                year_value = st.select_slider(
                    "Year",
                    options=years_avail,
                    value=years_avail[len(years_avail) // 2],
                    key="anchor_year",
                )
            else:
                # Exact date mode — clamped to the XBRL-available range
                import datetime as _dt
                min_dt = _dt.date(min(years_avail), 1, 1)
                max_dt = _dt.date(max(years_avail) + 1, 12, 31)
                # Default to mid-range
                default_dt = _dt.date(years_avail[len(years_avail) // 2], 6, 30)
                d = st.date_input(
                    "Exact date (YYYY-MM-DD)",
                    value=default_dt,
                    min_value=min_dt,
                    max_value=max_dt,
                    key="anchor_date",
                    help="The engine pulls the most recent 10-Q/10-K filed on or before this date for fundamentals, plus that exact day's stock close from yfinance for market cap.",
                )
                date_value = d.isoformat()

    # Frozen-state card
    if date_value:
        card_label = f"at {date_value}"
    elif year_value:
        card_label = f"at FY {year_value}"
    else:
        card_label = "(today)"
    with st.expander(f"📌 North-star state {card_label}", expanded=use_year):
        ns_state = safe_call(cached_north_star_state, ns_ticker, year_value, date_value)
        if ns_state.get("available"):
            kc1, kc2, kc3, kc4, kc5 = st.columns(5)
            kc1.metric("Market cap", fmt_money(ns_state.get("mkt_cap_m")))
            kc2.metric("Revenue", fmt_money(ns_state.get("revenue_m")))
            if year_value or date_value:
                kc3.metric("Cash", fmt_money(ns_state.get("cash_m")))
            else:
                kc3.metric("Stage", "Has revenue" if ns_state.get("has_revenue") else "Pre-revenue")
            kc4.metric("Size band", ns_state.get("size_band", "—"))
            kc5.metric("Modality (today)", ns_state.get("primary_modality", "—"))
            st.caption(f"Source: {ns_state.get('source')}    •    [10-K filings on EDGAR]({edgar_link(ns_ticker)})")
            if year_value or date_value:
                st.warning(
                    "⚠️ **Look-ahead caveat:** modality / sub-sector tags come from today's xlsx. "
                    "M5 will pull modality from each year's actual 10-K Item 1 to close this gap."
                )
        else:
            st.error(f"Snapshot unavailable: {ns_state.get('unavailable_reason', '?')}")

    c1, c2, c3 = st.columns(3)
    n_peers = c1.number_input("Peer set size", min_value=10, max_value=80, value=30, step=5)
    top_n = c2.number_input("Show top-N cheapest", min_value=5, max_value=40, value=15, step=5)
    same_region = c3.checkbox("Same-region peers only", value=False)

    # Show calibration status — Anchor results pick up calibrated weights if present
    try:
        import calibration as _cal
        _cw = _cal.apply_calibrated_weights()
    except Exception:
        _cw = None
    if _cw is not None:
        st.success("✓ Using **calibrated weights** from your Calibrate tab session.")
    else:
        st.caption("Using default weights. Run the Calibrate tab to tune to your taste.")

    if date_value:
        spinner_msg = f"Loading {ns_ticker} state on {date_value} from EDGAR + scoring peers..."
    elif year_value:
        spinner_msg = f"Loading {ns_ticker} FY{year_value} 10-K from EDGAR + scoring peers..."
    else:
        spinner_msg = "Pulling valuations from yfinance + scoring..."
    with st.spinner(spinner_msg):
        result = safe_call(cached_anchor_screen, ns_ticker, int(n_peers), int(top_n), same_region, year_value, date_value)

    # === BASKET FRAMING — backtest-informed expectation ===
    st.info(
        "**Read this as a basket, not a tip-sheet.** In our 2018 backtest, the cheapest-quintile basket had "
        "a **50% hit rate** but **+287% mean 1Y return** — most cheap names were flat or down individually; "
        "the basket-level result came from one or two big winners in the right tail. **Diversify across the names you save.**"
    )

    # Compute misuse flags for the candidate names
    candidate_tickers = result["ticker"].tolist()
    mkt_caps = dict(zip(result["ticker"], result["mkt_cap_m_yf"]))
    flags_df = misuse_flags.compute_flags_batch(candidate_tickers, mkt_caps=mkt_caps, username=username)

    # Display
    display = result.copy()
    display["Mkt Cap"] = display["mkt_cap_m_yf"].apply(fmt_money)
    display["Cash"] = display["cash_m"].apply(fmt_money)
    display["EV"] = display["ev_m"].apply(fmt_money)
    display["Net cash / mc"] = display["net_cash_to_mc"].round(2)
    display["EV / Cash"] = display["ev_cash_ratio"].round(2)
    display["Runway (mo)"] = display["runway_months"].round(0)
    display["Cheapness"] = display["cheapness_score"].round(1)
    display["Anchor"] = display["is_anchor"].map({True: "★", False: ""})
    display["EDGAR"] = display["ticker"].apply(edgar_link)

    # Surface rich modalities (from 10-K extraction) when available — purely
    # informational column; the engine still ranks via xlsx tags until coverage
    # is universe-wide.
    rich_lookup = {
        r["ticker"]: r
        for _, r in universe[["ticker", "rich_modalities", "rich_therapeutic_areas", "modality_source"]].iterrows()
    }
    def _rich_str(tk: str) -> str:
        r = rich_lookup.get(tk)
        if r is None or r.get("modality_source") in (None, "xlsx"):
            return ""
        # parquet returns numpy arrays — convert defensively before truthiness checks
        rm = r.get("rich_modalities")
        ra = r.get("rich_therapeutic_areas")
        mods = list(rm) if rm is not None else []
        tas = list(ra) if ra is not None else []
        parts = []
        if mods:
            parts.append(" + ".join(mods[:2]))
        if tas:
            parts.append("(" + " / ".join(tas[:2]) + ")")
        return " ".join(parts)
    display["Pipeline (10-K)"] = display["ticker"].apply(_rich_str)

    # Merge flags
    display = display.merge(
        flags_df[["ticker", "any_warning", "warning_count", "fresh_ipo", "going_concern", "reverse_merger_shell", "sub_ten_mkt_cap", "near_term_catalyst"]],
        on="ticker", how="left",
    )
    display["Flags"] = display.apply(misuse_flags.short_flag_string, axis=1)

    # === COMPACT TABLE — 6 most-useful columns ===
    st.dataframe(
        display[["Anchor", "ticker", "name", "Mkt Cap", "Cash", "Cheapness", "Flags"]]
        .rename(columns={"ticker": "Ticker", "name": "Company", "Cash": "Cash on hand"}),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Anchor": st.column_config.TextColumn("★", width="small", help="★ marks the north-star company you anchored on"),
            "Cheapness": st.column_config.ProgressColumn("Cheapness", min_value=0, max_value=100, format="%.1f", help="Higher = cheaper relative to this peer set. Percentile within the pool."),
            "Cash on hand": st.column_config.TextColumn("Cash on hand", help="Most recent reported cash + equivalents from yfinance"),
            "Flags": st.column_config.TextColumn(
                "⚠️",
                help="🆕 fresh IPO (cash inflated)  •  🛑 going-concern  •  🐚 reverse-merger shell  •  ⚠️ sub-$10M mkt cap  •  📅 your catalyst note",
                width="small",
            ),
        },
    )

    # === COMPANY PEEK PANEL — full details for selected ticker ===
    st.markdown("---")
    st.subheader("🔍 Company peek")
    st.caption("Pick any ticker from the results to see full financials, 10-K extracted modalities, per-signal cheapness breakdown, and warnings.")

    peek_options = [f"{r['ticker']} — {r['name']}" for _, r in display.iterrows()]
    peek_default = next((i for i, opt in enumerate(peek_options) if not display.iloc[i]["is_anchor"]), 0)
    peek_choice = st.selectbox("Ticker to inspect", peek_options, index=peek_default, key=f"peek_{ns_ticker}_{year_value}_{date_value}")
    peek_ticker = peek_choice.split(" — ")[0]
    peek_row = display.loc[display["ticker"] == peek_ticker].iloc[0]
    peek_flags = flags_df.loc[flags_df["ticker"] == peek_ticker].iloc[0]
    universe_row = universe.loc[universe["ticker"] == peek_ticker]
    peek_universe = universe_row.iloc[0] if not universe_row.empty else None

    pk1, pk2 = st.columns([2, 1])

    with pk1:
        st.markdown(f"### {peek_row['ticker']} — {peek_row['name']}")
        anchor_note = " ★ (this is your anchor)" if peek_row["is_anchor"] else ""
        st.caption(f"Region: {peek_row['region']} · Size: {peek_row['size_band']} · Modality (xlsx): {peek_row['primary_modality']}{anchor_note}")

        # Financial grid
        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Market cap", peek_row["Mkt Cap"])
        f2.metric("Cash on hand", peek_row["Cash"])
        debt_val = peek_row.get("debt_m")
        f3.metric("Debt", fmt_money(debt_val) if pd.notna(debt_val) else "—")
        f4.metric("EV", peek_row["EV"])

        f5, f6, f7, f8 = st.columns(4)
        f5.metric("Net cash / mkt cap", f"{peek_row['Net cash / mc']:.2f}" if pd.notna(peek_row["Net cash / mc"]) else "—",
                  help=">1 means market values business below cash")
        f6.metric("EV / Cash", f"{peek_row['EV / Cash']:.2f}" if pd.notna(peek_row["EV / Cash"]) else "—",
                  help="<1 means EV is less than cash")
        runway_v = peek_row.get("Runway (mo)")
        f7.metric("Runway (mo)", f"{runway_v:.0f}" if pd.notna(runway_v) else "—",
                  help="Cash / monthly burn, capped at 60")
        f8.metric("Cheapness", f"{peek_row['Cheapness']:.1f}",
                  help="Percentile within this peer set; higher = cheaper")

        # 10-K extracted modalities
        if peek_universe is not None:
            rich_mods = list(peek_universe["rich_modalities"]) if peek_universe["rich_modalities"] is not None else []
            rich_tas = list(peek_universe["rich_therapeutic_areas"]) if peek_universe["rich_therapeutic_areas"] is not None else []
            if rich_mods or rich_tas:
                st.markdown("**🧬 Pipeline (extracted from latest 10-K Item 1):**")
                if rich_mods:
                    st.markdown(f"- Modalities: {' · '.join(rich_mods)}")
                if rich_tas:
                    st.markdown(f"- Therapeutic areas: {' · '.join(rich_tas)}")
                pipeline_filed = peek_universe.get("pipeline_filed")
                if pipeline_filed:
                    st.caption(f"Source: 10-K filed {pipeline_filed}")

        # Warnings detail
        if peek_flags.get("any_warning"):
            st.markdown("**⚠️ Warning flags:**")
            warning_lines = []
            if peek_flags.get("fresh_ipo"):
                warning_lines.append(f"🆕 **Fresh IPO** — {peek_flags.get('fresh_ipo_reason') or 'recently public'}")
            if peek_flags.get("going_concern"):
                warning_lines.append(f"🛑 **Going-concern flag** — {peek_flags.get('going_concern_reason') or 'auditor noted substantial doubt'}")
            if peek_flags.get("reverse_merger_shell"):
                warning_lines.append(f"🐚 **Reverse-merger shell** — {peek_flags.get('reverse_merger_shell_reason') or 'mostly cash, no real biz'}")
            if peek_flags.get("sub_ten_mkt_cap"):
                warning_lines.append(f"⚠️ **Sub-$10M market cap** — {peek_flags.get('sub_ten_mkt_cap_reason') or 'distressed zone'}")
            if peek_flags.get("near_term_catalyst"):
                warning_lines.append(f"📅 **Near-term catalyst (your note)** — {peek_flags.get('near_term_catalyst_reason') or 'see watchlist'}")
            for line in warning_lines:
                st.markdown(f"- {line}")

        # === What's happening — recent SEC filings ===
        with st.expander("📰 What's happening — recent SEC filings"):
            recent = safe_call(cached_recent_filings, peek_ticker, 8)
            if recent:
                form_emoji = {
                    "8-K": "⚡", "8-K/A": "⚡",
                    "10-K": "📄", "10-K/A": "📄",
                    "10-Q": "📊", "10-Q/A": "📊",
                    "S-1": "🚀", "S-1/A": "🚀",
                    "S-3": "💵", "S-3/A": "💵",
                    "424B5": "💵", "424B3": "💵",
                    "DEF 14A": "🗳", "PRE 14A": "🗳",
                    "SC 13G": "👥", "SC 13G/A": "👥", "SC 13D": "👥", "SC 13D/A": "👥",
                    "4": "🧑‍💼",  # insider buying/selling Form 4
                }
                form_label = {
                    "8-K": "Material event",
                    "10-K": "Annual report",
                    "10-Q": "Quarterly report",
                    "S-1": "IPO registration",
                    "S-3": "Shelf registration",
                    "424B5": "Prospectus supplement (offering)",
                    "424B3": "Prospectus supplement",
                    "DEF 14A": "Definitive proxy",
                    "SC 13G": "Passive 5%+ ownership",
                    "SC 13D": "Active 5%+ ownership",
                    "4": "Insider transaction",
                }
                for f in recent:
                    emoji = form_emoji.get(f["form"], "•")
                    label = form_label.get(f["form"], "")
                    desc = f.get("primary_doc_desc") or label
                    line = f"- {emoji} **{f['form']}** · {f['filing_date']}"
                    if desc:
                        line += f" — {desc}"
                    if f.get("edgar_url"):
                        line += f" · [view]({f['edgar_url']})"
                    st.markdown(line)
                st.caption("⚡ = material event (could be a Ph2/Ph3 readout, FDA action, M&A); 💵 = potential dilution; 🧑‍💼 = insider transactions")
            else:
                st.caption("No recent filings retrieved (could be a non-US filer or an EDGAR fetch error).")

    with pk2:
        # Save / EDGAR / catalyst note actions
        st.markdown("**Actions**")
        st.markdown(f"[📂 View 10-K filings on EDGAR]({edgar_link(peek_ticker)})")
        if not peek_row["is_anchor"]:
            peek_note = st.text_input(
                "Catalyst note",
                value=userdb.get_note(username, peek_ticker) or "",
                placeholder="e.g. Ph2 readout June 2026",
                key=f"peek_note_{peek_ticker}",
            )
            if st.button("💾 Save to watchlist", key=f"peek_save_{peek_ticker}", use_container_width=True):
                src = f"anchor:{ns_ticker}" + (f"@{year_value}" if year_value else (f"@{date_value}" if date_value else ""))
                userdb.add_watchlist(username, peek_ticker, name=peek_row["name"], source=src, note=peek_note)
                if peek_note:
                    userdb.set_note(username, peek_ticker, peek_note)
                st.success(f"Saved {peek_ticker}.")
                st.rerun()

        # Per-signal z-score breakdown
        st.markdown("---")
        st.markdown("**Cheapness signals (z-scored vs peer pool)**")
        sig_rows = []
        for sig_label, sig_col in [
            ("Net cash / mc", "z_net_cash_to_mc"),
            ("Inv EV / Cash", "z_inv_ev_cash"),
            ("Peer-rel EV", "z_peer_log_ev_resid"),
            ("Peer-rel EV/Cash", "z_peer_log_ev_cash_resid"),
            ("Runway", "z_runway_months"),
        ]:
            v = peek_row.get(sig_col)
            if pd.notna(v):
                sig_rows.append({"signal": sig_label, "z-score": round(float(v), 2)})
        if sig_rows:
            st.dataframe(pd.DataFrame(sig_rows), hide_index=True, use_container_width=True,
                         column_config={"z-score": st.column_config.ProgressColumn("z (-3 to +3)", min_value=-3, max_value=3, format="%.2f")})

    # ----- Bulk save (legacy, kept for power users) -----
    with st.expander("Bulk save multiple tickers"):
        save_choice = st.multiselect(
            "Pick names to save",
            [f"{r['ticker']} — {r['name']}" for _, r in result.iterrows() if not r["is_anchor"]],
            key="anchor_save",
        )
        note = st.text_input(
            "Shared note for all (optional)",
            key="anchor_note",
            placeholder="e.g. small Ph2 oncology, near-cash, watch for ASCO readout",
        )
        if st.button("Save all selected"):
            for s in save_choice:
                tk, name = s.split(" — ", 1)
                src = f"anchor:{ns_ticker}" + (f"@{year_value}" if year_value else "")
                userdb.add_watchlist(username, tk, name=name, source=src, note=note)
                if note:
                    userdb.set_note(username, tk, note)
            st.success(f"Saved {len(save_choice)} names.")
            st.rerun()


# ============================ SCREENER TAB =============================
with tab_screener:
    st.header("Free screener")
    st.caption(
        "Cheapness ranking across a slice of the universe — no anchor required. "
        "Only tickers we've already pulled from yfinance are scored. "
        "Run `python valuations.py --all` (or wait for nightly refresh) to pre-warm the full universe."
    )

    if val_path.exists():
        cache_size = len(pd.read_parquet(val_path))
        st.info(f"Valuations cache: {cache_size} tickers populated · refreshed {file_age_human(val_path)}.")
    else:
        st.warning("No valuations cache yet.")

    c1, c2, c3 = st.columns(3)
    region_pick = c1.selectbox("Region", [None, "US", "Hong Kong"], index=1, format_func=lambda x: x or "All")
    modality_pick = c2.selectbox("Modality", [None] + MODALITIES, index=0, format_func=lambda x: x or "All")
    size_pick = c3.selectbox("Size band", [None, "micro", "small", "mid", "large", "mega"], index=0, format_func=lambda x: x or "All")
    top_n_screen = st.number_input("Show top N", min_value=10, max_value=100, value=25, step=5)

    if st.button("Run screen") or st.session_state.get("screen_result_keep") is True:
        # Persist results across reruns so the peek panel works
        if "screen_result_cache" not in st.session_state or st.session_state.get("screen_result_keep") is not True:
            with st.spinner("Scoring slice..."):
                result = safe_call(mispricing.free_screen,
                                  modality=modality_pick, region=region_pick, size_band=size_pick,
                                  top_n=int(top_n_screen), fetch=False)
            st.session_state.screen_result_cache = result
            st.session_state.screen_result_keep = True
        else:
            result = st.session_state.screen_result_cache

        if result["cheapness_score"].notna().sum() == 0:
            st.warning("No valuations data for this slice. The nightly refresh job will populate the cache, or run `python valuations.py --all` locally.")
        else:
            # Compute misuse flags + display
            mkt_caps = dict(zip(result["ticker"], result["mkt_cap_m_yf"]))
            scr_flags_df = misuse_flags.compute_flags_batch(result["ticker"].tolist(), mkt_caps=mkt_caps, username=username)

            display = result.copy()
            display["Mkt Cap"] = display["mkt_cap_m_yf"].apply(fmt_money)
            display["Cash"] = display["cash_m"].apply(fmt_money)
            display["EV"] = display["ev_m"].apply(fmt_money)
            display["Net cash / mc"] = display["net_cash_to_mc"].round(2)
            display["EV / Cash"] = display["ev_cash_ratio"].round(2)
            display["Cheapness"] = display["cheapness_score"].round(1)
            display = display.merge(
                scr_flags_df[["ticker", "any_warning", "fresh_ipo", "going_concern", "reverse_merger_shell", "sub_ten_mkt_cap", "near_term_catalyst"]],
                on="ticker", how="left",
            )
            display["Flags"] = display.apply(misuse_flags.short_flag_string, axis=1)

            # === COMPACT TABLE ===
            st.dataframe(
                display[["ticker", "name", "Mkt Cap", "Cash", "Cheapness", "Flags"]]
                .rename(columns={"ticker": "Ticker", "name": "Company", "Cash": "Cash on hand"}),
                hide_index=True, use_container_width=True,
                column_config={
                    "Cheapness": st.column_config.ProgressColumn("Cheapness", min_value=0, max_value=100, format="%.1f", help="Higher = cheaper relative to this slice"),
                    "Cash on hand": st.column_config.TextColumn("Cash on hand", help="Most recent reported cash + equivalents"),
                    "Flags": st.column_config.TextColumn("⚠️", help="🆕 fresh IPO  •  🛑 going-concern  •  🐚 shell  •  ⚠️ sub-$10M  •  📅 catalyst note", width="small"),
                },
            )

            # === COMPANY PEEK PANEL ===
            st.markdown("---")
            st.subheader("🔍 Company peek")
            scr_peek_options = [f"{r['ticker']} — {r['name']}" for _, r in display.iterrows()]
            scr_peek_choice = st.selectbox("Ticker to inspect", scr_peek_options, key="scr_peek")
            scr_peek_ticker = scr_peek_choice.split(" — ")[0]
            scr_peek_row = display.loc[display["ticker"] == scr_peek_ticker].iloc[0]
            scr_peek_flags = scr_flags_df.loc[scr_flags_df["ticker"] == scr_peek_ticker].iloc[0]
            scr_peek_uni = universe.loc[universe["ticker"] == scr_peek_ticker]
            scr_peek_uni = scr_peek_uni.iloc[0] if not scr_peek_uni.empty else None

            sp1, sp2 = st.columns([2, 1])
            with sp1:
                st.markdown(f"### {scr_peek_row['ticker']} — {scr_peek_row['name']}")
                st.caption(f"Region: {scr_peek_row['region']} · Size: {scr_peek_row['size_band']} · Modality (xlsx): {scr_peek_row['primary_modality']}")

                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Market cap", scr_peek_row["Mkt Cap"])
                f2.metric("Cash on hand", scr_peek_row["Cash"])
                f3.metric("EV", scr_peek_row["EV"])
                f4.metric("Cheapness", f"{scr_peek_row['Cheapness']:.1f}")

                f5, f6 = st.columns(2)
                f5.metric("Net cash / mkt cap", f"{scr_peek_row['Net cash / mc']:.2f}" if pd.notna(scr_peek_row['Net cash / mc']) else "—")
                f6.metric("EV / Cash", f"{scr_peek_row['EV / Cash']:.2f}" if pd.notna(scr_peek_row['EV / Cash']) else "—")

                if scr_peek_uni is not None:
                    rich_mods = list(scr_peek_uni["rich_modalities"]) if scr_peek_uni["rich_modalities"] is not None else []
                    rich_tas = list(scr_peek_uni["rich_therapeutic_areas"]) if scr_peek_uni["rich_therapeutic_areas"] is not None else []
                    if rich_mods or rich_tas:
                        st.markdown("**🧬 Pipeline (from latest 10-K Item 1):**")
                        if rich_mods:
                            st.markdown(f"- Modalities: {' · '.join(rich_mods)}")
                        if rich_tas:
                            st.markdown(f"- Therapeutic areas: {' · '.join(rich_tas)}")

                if scr_peek_flags.get("any_warning"):
                    st.markdown("**⚠️ Warning flags:**")
                    if scr_peek_flags.get("fresh_ipo"):
                        st.markdown(f"- 🆕 Fresh IPO — {scr_peek_flags.get('fresh_ipo_reason') or ''}")
                    if scr_peek_flags.get("going_concern"):
                        st.markdown(f"- 🛑 Going-concern — {scr_peek_flags.get('going_concern_reason') or ''}")
                    if scr_peek_flags.get("reverse_merger_shell"):
                        st.markdown(f"- 🐚 Reverse-merger shell — {scr_peek_flags.get('reverse_merger_shell_reason') or ''}")
                    if scr_peek_flags.get("sub_ten_mkt_cap"):
                        st.markdown(f"- ⚠️ Sub-$10M market cap — {scr_peek_flags.get('sub_ten_mkt_cap_reason') or ''}")

                with st.expander("📰 What's happening — recent SEC filings"):
                    scr_recent = safe_call(cached_recent_filings, scr_peek_ticker, 8)
                    if scr_recent:
                        for f in scr_recent:
                            line = f"- **{f['form']}** · {f['filing_date']}"
                            desc = f.get("primary_doc_desc")
                            if desc:
                                line += f" — {desc}"
                            if f.get("edgar_url"):
                                line += f" · [view]({f['edgar_url']})"
                            st.markdown(line)
                    else:
                        st.caption("No recent filings retrieved.")

            with sp2:
                st.markdown("**Actions**")
                st.markdown(f"[📂 EDGAR filings]({edgar_link(scr_peek_ticker)})")
                scr_peek_note = st.text_input("Catalyst note", value=userdb.get_note(username, scr_peek_ticker) or "", key=f"scr_peek_note_{scr_peek_ticker}")
                if st.button("💾 Save to watchlist", key=f"scr_save_{scr_peek_ticker}", use_container_width=True):
                    userdb.add_watchlist(username, scr_peek_ticker, name=scr_peek_row["name"], source="screener", note=scr_peek_note)
                    if scr_peek_note:
                        userdb.set_note(username, scr_peek_ticker, scr_peek_note)
                    st.success(f"Saved {scr_peek_ticker}.")
                    st.rerun()


# ============================ BACKTEST TAB =============================
with tab_backtest:
    st.header("Backtest evidence")
    st.markdown(
        """
The cheapness signal was tested across two regimes by running the engine on
the universe-as-of FY2018 and FY2020 financial state, then comparing forward
1Y / 3Y total returns by quintile.

**Headline:** the cheap basket beat the expensive basket in *both* regimes —
offensively in the 2018 bull market and defensively in the 2020 bear market.

| Window | Cheap mean 1Y | Expensive mean 1Y | Cheap mean 3Y | Expensive mean 3Y |
|---|--:|--:|--:|--:|
| FY2018 → 2019 / 2021 | **+287%** | +3% | **+142%** | +1% |
| FY2020 → 2021 / 2023 | −23% | −25% | **−18%** | −40% |

**What the numbers actually tell you:**

- **Hit rates inside the cheap basket are 25–50%.** Most cheap names individually
  fail. The basket-level outperformance comes from the right tail (one AXSM
  in 2018 explains most of the +287%) and from defense in down markets.
- **The middle quintiles (q2/q3) underperform the most.** Deep discount looks
  safer than mild discount. Counterintuitive but consistent across both years.
- **Survivor bias is real.** Universe is today's living biotechs; names that
  delisted between the test year and now are missing — and those are
  disproportionately the value traps. So real cheap-bucket returns would be
  somewhat lower than measured here. The directional signal still holds.

See `BACKTEST_RESULTS.md` in the project folder for full per-ticker breakdowns.

---

**The practical implication for how to use this app:**

1. Treat the top-N cheapest list as a **shortlist**, not a tip-sheet.
2. Save a basket (5–15 names), not one.
3. The 🆕 / 🛑 / 🐚 / ⚠️ flags on the results table are there because the
   2018 backtest's cheap-bucket losers were almost all clinical-stage names
   with failed catalysts — the engine can't see the catalyst risk, so it
   flags everything that looks like a value trap by other signals.
"""
    )


# ============================ CALIBRATE TAB =============================
with tab_calibrate:
    import calibration
    st.header("Calibrate the engine to your taste")
    st.markdown(
        """
The default similarity weights came out of my head. This tab lets you **replace
intuition with empirical signal** — grade ~30 candidate (anchor, candidate)
pairs as **peers** or **not peers**, then run a grid search that finds the
weight combination best reproducing your judgments.

**The session takes 15–25 minutes.** Some pairs are obvious (you'll click
through quickly); some are borderline (your call here is what teaches the
engine your taste).
"""
    )

    labels = calibration.load_labels(username)
    n_graded = sum(1 for it in labels if it["label"] in ("peer", "not_peer"))
    n_peer = sum(1 for it in labels if it["label"] == "peer")
    n_not = sum(1 for it in labels if it["label"] == "not_peer")
    st.info(f"You've graded **{n_graded}** pairs so far ({n_peer} peer / {n_not} not-peer / {len(labels) - n_graded} skipped).")

    if "cal_pairs" not in st.session_state:
        st.session_state.cal_pairs = None

    cc1, cc2 = st.columns(2)
    if cc1.button("Sample 30 pairs to grade"):
        with st.spinner("Sampling pairs..."):
            st.session_state.cal_pairs = safe_call(calibration.sample_pairs, 30, 42)

    if st.session_state.cal_pairs:
        already = {it["pair_key"] for it in labels}
        unmatched = [
            p for p in st.session_state.cal_pairs
            if f"{p['anchor']}|{p['candidate']}" not in already
        ]
        if not unmatched:
            st.success("✓ All 30 pairs graded. You can run the calibration now.")
        else:
            st.markdown(f"**{len(unmatched)} pairs left to grade.**")
            current = unmatched[0]
            anchor_row = universe.loc[universe["ticker"] == current["anchor"]].iloc[0]
            cand_row = universe.loc[universe["ticker"] == current["candidate"]].iloc[0]

            colA, colB = st.columns(2)
            with colA:
                st.subheader(f"{anchor_row['ticker']}")
                st.caption(anchor_row["name"])
                st.metric("Mkt cap", fmt_money(anchor_row["mkt_cap_m"]))
                st.metric("Modality (xlsx)", anchor_row["primary_modality"])
                rich_mods_a = list(anchor_row["rich_modalities"]) if anchor_row["rich_modalities"] is not None else []
                rich_tas_a = list(anchor_row["rich_therapeutic_areas"]) if anchor_row["rich_therapeutic_areas"] is not None else []
                if rich_mods_a or rich_tas_a:
                    st.caption(f"10-K: {' + '.join(rich_mods_a[:3])} / {' / '.join(rich_tas_a[:2])}")
            with colB:
                st.subheader(f"{cand_row['ticker']}")
                st.caption(cand_row["name"])
                st.metric("Mkt cap", fmt_money(cand_row["mkt_cap_m"]))
                st.metric("Modality (xlsx)", cand_row["primary_modality"])
                rich_mods_c = list(cand_row["rich_modalities"]) if cand_row["rich_modalities"] is not None else []
                rich_tas_c = list(cand_row["rich_therapeutic_areas"]) if cand_row["rich_therapeutic_areas"] is not None else []
                if rich_mods_c or rich_tas_c:
                    st.caption(f"10-K: {' + '.join(rich_mods_c[:3])} / {' / '.join(rich_tas_c[:2])}")

            st.markdown(f"**Are these companies peers** (would you want them in the same basket)?")
            bc1, bc2, bc3 = st.columns(3)
            if bc1.button("✅ Yes, peers", use_container_width=True):
                calibration.save_label(username, current["anchor"], current["candidate"], "peer", current.get("bucket"))
                st.rerun()
            if bc2.button("❌ Not peers", use_container_width=True):
                calibration.save_label(username, current["anchor"], current["candidate"], "not_peer", current.get("bucket"))
                st.rerun()
            if bc3.button("⏭ Skip / unsure", use_container_width=True):
                calibration.save_label(username, current["anchor"], current["candidate"], "skip", current.get("bucket"))
                st.rerun()

    st.markdown("---")
    st.subheader("Run grid-search calibration")
    st.caption(
        "Needs ≥3 peer + ≥3 not-peer labels. The grid is 6 levels per weight × 5 "
        "weights = 7,776 combinations; takes ~30–90 seconds."
    )
    granularity = st.slider("Grid granularity (more = slower, more precise)", 4, 8, 6)

    if st.button("Run calibration"):
        with st.spinner("Running grid search..."):
            res = safe_call(calibration.run_grid_search, username, granularity=granularity)
        if not res.get("ok"):
            st.warning(res.get("reason", "Calibration failed."))
        else:
            st.success(f"Calibration complete. AUC: {res['default_auc']:.3f} → **{res['best_auc']:.3f}** (Δ {res['auc_delta']:+.3f})")
            wlabels = ["log mkt cap", "has revenue", "log revenue", "primary modality", "compound modality"]
            comp = pd.DataFrame({
                "weight": wlabels,
                "default": [round(x, 2) for x in res["default_weights"]],
                "calibrated": [round(x, 2) for x in res["best_weights"]],
            })
            st.table(comp)
            st.caption(
                "Calibrated weights are saved. The Anchor tab will pick them up "
                "on next ranking (after page reload)."
            )

    cw = calibration.apply_calibrated_weights()
    if cw is not None:
        st.markdown("---")
        st.markdown("**Currently active calibrated weights:**")
        st.code(str(cw))


# ============================ ABOUT TAB =============================
with tab_about:
    st.header("How to read this")
    st.markdown(
        """
**The product is the cheapness ranking inside a peer set, not the peer set itself.**
Similarity is the substrate; cheapness is the output.

**Cheapness signals (all signed so higher = cheaper):**
- `net_cash_to_mc` — `(cash − debt) / market cap`. >1 = market values whole business below net cash.
- `ev_cash_ratio` — `EV / cash`. <1 = enterprise value is less than cash on hand.
- `peer_log_ev_resid` — log-EV residual vs peer median. Negative residual = cheap.
- `runway_months` — `cash / monthly burn`. Capped at 60. Higher = funded longer.

**Time-machine mode** replaces the north-star's financials with that fiscal
year's state from SEC EDGAR XBRL. Modality tags are still today's (M4 fixes
this). XBRL coverage starts ~2010; pre-2010 needs HTML parsing (M5).

**Misuse flags on each result:**
- 🆕 fresh IPO — first 10-K within last 18 months, IPO cash makes everything look fake-cheap
- 🛑 going-concern — auditor flagged solvency risk *(M5: live from 10-K text indexer)*
- 🐚 reverse-merger shell — mostly cash, no real biz
- ⚠️ sub-$10M mkt cap — distressed zone
- 📅 your catalyst note — you've added a note containing trial/FDA keywords

**What's still missing** (in PLAN.md): pipeline-aware modality (M4 LLM extraction over 10-K Item 1),
calibrated weights (M3 peer-pair labeling tool), survivor-bias dead-company list (M5),
catalyst-feed integration, paid licensed data source.

**Not investment advice.** Use as a screen, not a buy list.
"""
    )

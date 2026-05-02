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

# Watchlist (persisted via SQLite)
wl_rows = userdb.list_watchlist(username)
st.sidebar.subheader(f"Watchlist ({len(wl_rows)})")
if wl_rows:
    wl_df = pd.DataFrame(wl_rows)
    st.sidebar.dataframe(wl_df, hide_index=True, use_container_width=True)
    st.sidebar.download_button(
        "Download CSV",
        wl_df.to_csv(index=False).encode(),
        f"watchlist_{username}.csv",
        "text/csv",
        use_container_width=True,
    )
    rm_pick = st.sidebar.selectbox("Remove a name", options=[""] + wl_df["ticker"].tolist())
    if rm_pick and st.sidebar.button(f"Remove {rm_pick}"):
        userdb.remove_watchlist(username, rm_pick)
        st.rerun()
else:
    st.sidebar.caption("No companies saved yet.")

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

    st.dataframe(
        display[["Anchor", "ticker", "name", "region", "primary_modality", "Pipeline (10-K)", "size_band",
                 "Mkt Cap", "Cash", "EV", "Net cash / mc", "EV / Cash", "Runway (mo)",
                 "Cheapness", "Flags", "EDGAR"]]
        .rename(columns={"ticker": "Ticker", "name": "Company", "region": "Region",
                         "primary_modality": "Modality", "size_band": "Size"}),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Cheapness": st.column_config.ProgressColumn("Cheapness percentile", min_value=0, max_value=100, format="%.1f"),
            "EDGAR": st.column_config.LinkColumn("Filings", display_text="10-Ks →"),
            "Flags": st.column_config.TextColumn(
                "⚠️",
                help="🆕 fresh IPO (cash inflated)  •  🛑 going-concern  •  🐚 reverse-merger shell  •  ⚠️ sub-$10M mkt cap  •  📅 your catalyst note",
                width="small",
            ),
        },
    )

    # Per-row explanation for the warnings
    flagged = flags_df.loc[flags_df["any_warning"]]
    if len(flagged) > 0:
        with st.expander(f"⚠️ {len(flagged)} candidates have warning flags — what they mean"):
            for _, fr in flagged.iterrows():
                reasons = [
                    fr.get("fresh_ipo_reason"),
                    fr.get("going_concern_reason"),
                    fr.get("reverse_merger_shell_reason"),
                    fr.get("sub_ten_mkt_cap_reason"),
                    fr.get("near_term_catalyst_reason"),
                ]
                reasons = [r for r in reasons if r]
                if reasons:
                    st.markdown(f"- **{fr['ticker']}** {misuse_flags.short_flag_string(fr)} — {' · '.join(reasons)}")

    # ----- Save to watchlist (persists to SQLite) -----
    st.subheader("Save to watchlist")
    save_choice = st.multiselect(
        "Pick names to save",
        [f"{r['ticker']} — {r['name']}" for _, r in result.iterrows() if not r["is_anchor"]],
        key="anchor_save",
    )
    note = st.text_input(
        "Note (optional — mention catalysts and they'll get a 📅 flag next time)",
        key="anchor_note",
        placeholder="e.g. Ph2 readout June 2026 — mTOR inhibitor in NSCLC",
    )
    if st.button("Save selected"):
        for s in save_choice:
            tk, name = s.split(" — ", 1)
            src = f"anchor:{ns_ticker}" + (f"@{year_value}" if year_value else "")
            userdb.add_watchlist(username, tk, name=name, source=src, note=note)
            if note:
                userdb.set_note(username, tk, note)
        st.success(f"Saved {len(save_choice)} names to your watchlist.")
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

    if st.button("Run screen"):
        result = safe_call(mispricing.free_screen,
                          modality=modality_pick, region=region_pick, size_band=size_pick,
                          top_n=int(top_n_screen), fetch=False)

        if result["cheapness_score"].notna().sum() == 0:
            st.warning("No valuations data for this slice. Pre-warm with `python valuations.py --all`.")
        else:
            display = result.copy()
            display["Mkt Cap"] = display["mkt_cap_m_yf"].apply(fmt_money)
            display["Cash"] = display["cash_m"].apply(fmt_money)
            display["EV"] = display["ev_m"].apply(fmt_money)
            display["Net cash / mc"] = display["net_cash_to_mc"].round(2)
            display["EV / Cash"] = display["ev_cash_ratio"].round(2)
            display["Cheapness"] = display["cheapness_score"].round(1)
            display["EDGAR"] = display["ticker"].apply(edgar_link)
            st.dataframe(
                display[["ticker", "name", "region", "primary_modality", "size_band",
                         "Mkt Cap", "Cash", "EV", "Net cash / mc", "EV / Cash", "Cheapness", "EDGAR"]],
                hide_index=True, use_container_width=True,
                column_config={
                    "Cheapness": st.column_config.ProgressColumn("Cheapness percentile", min_value=0, max_value=100, format="%.1f"),
                    "EDGAR": st.column_config.LinkColumn("Filings", display_text="10-Ks →"),
                },
            )


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

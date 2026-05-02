"""
Misuse flags — surface the patterns that produce *fake* cheapness or amplify
risk, so the user can manually filter the output before trusting it.

Council-pass + backtest insight: every losing name in the 2018 bucket-1 cohort
was a clinical-stage biotech where a Ph2/Ph3 readout went sideways. The
cheapness signal correctly identified them as cheap on paper; the user needs
catalyst awareness to filter the value traps.

Flags emitted:

  fresh_ipo
    Company filed its first 10-K within the last 18 months. IPO cash makes
    everything look fake-cheap. Derived from earliest fiscal year in XBRL.

  going_concern
    Latest 10-K text contains the phrase "going concern". Auditor flag —
    indicates real solvency risk regardless of cash position.

  near_term_catalyst
    A user-editable note exists for this ticker mentioning common catalyst
    keywords (Phase, readout, FDA, BLA, PDUFA, NDA, top-line, AdCom). This
    is a passive/manual flag for now; M5 wires it to a real catalyst feed.

  reverse_merger_shell
    Heuristic: cash > 0.7 × assets AND R&D < $5M AND no revenue. Indicates
    a shell that's mostly cash and waiting for a transaction.

  sub_ten_million_mkt_cap
    Real biotechs at <$10M mkt cap are usually distressed. Disclose, don't
    auto-exclude.

Public API:

    flags = compute_flags(ticker)  -> dict[str, bool|str]
    flags = compute_flags_batch([tickers])  -> DataFrame
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import historical
import userdb

ROOT = Path(__file__).parent

CATALYST_KEYWORDS = re.compile(
    r"\b(phase|readout|FDA|BLA|PDUFA|NDA|top-?line|AdCom|approval|trial|primary endpoint)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Individual flag computers
# ---------------------------------------------------------------------------

def _fresh_ipo_flag(ticker: str) -> tuple[bool, str | None]:
    years = historical.available_years(ticker)
    if not years:
        return False, None
    earliest = min(years)
    today_year = datetime.now(timezone.utc).year
    months_listed = (today_year - earliest) * 12  # rough — month-precision not needed
    if months_listed <= 18:
        return True, f"first 10-K filed FY{earliest} (~{months_listed}mo public)"
    return False, None


def _going_concern_flag(ticker: str) -> tuple[bool, str | None]:
    """Real implementation (M5 shipped): fetches the latest 10-K from EDGAR,
    searches for the auditor's "going concern" / "substantial doubt" language.
    Cached for 90 days per ticker."""
    try:
        import going_concern
        result = going_concern.check(ticker)
        if result.get("flagged"):
            ev = result.get("evidence") or ""
            # Compact for display
            if len(ev) > 200:
                ev = ev[:200].rsplit(" ", 1)[0] + "..."
            return True, f"flagged in 10-K filed {result.get('filed')}: {ev}"
    except Exception:
        pass
    return False, None


def _reverse_merger_shell_flag(ticker: str) -> tuple[bool, str | None]:
    """Heuristic: latest available year — cash > 70% of assets, no revenue, R&D < $5M."""
    years = historical.available_years(ticker)
    if not years:
        return False, None
    snap = historical.get_snapshot(ticker, max(years))
    if not snap or not snap.get("available"):
        return False, None
    cash = snap.get("cash_m") or 0
    assets = snap.get("assets_m") or 1
    rev = snap.get("revenue_m") or 0
    rd = snap.get("rd_m") or 0
    if cash > 0.7 * assets and rev == 0 and rd < 5:
        return True, f"cash ${cash:.0f}M / assets ${assets:.0f}M, no rev, R&D ${rd:.1f}M"
    return False, None


def _sub_ten_mkt_cap_flag(ticker: str, mkt_cap_m: float | None) -> tuple[bool, str | None]:
    if mkt_cap_m is not None and 0 < mkt_cap_m < 10:
        return True, f"mkt cap ${mkt_cap_m:.1f}M (distressed-zone)"
    return False, None


def _user_catalyst_note_flag(username: str | None, ticker: str) -> tuple[bool, str | None]:
    """If the user has a catalyst-keyword note for this ticker, flag it (positively)."""
    if not username:
        return False, None
    note = userdb.get_note(username, ticker)
    if not note:
        return False, None
    if CATALYST_KEYWORDS.search(note):
        return True, note[:80]
    return False, None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def compute_flags(ticker: str, mkt_cap_m: float | None = None, username: str | None = None) -> dict:
    """Compute all flags for one ticker. Returns dict with bool flags + reason strings."""
    fresh_b, fresh_r = _fresh_ipo_flag(ticker)
    rm_b, rm_r = _reverse_merger_shell_flag(ticker)
    tiny_b, tiny_r = _sub_ten_mkt_cap_flag(ticker, mkt_cap_m)
    cat_b, cat_r = _user_catalyst_note_flag(username, ticker)
    gc_b, gc_r = _going_concern_flag(ticker)

    return {
        "ticker": ticker,
        "fresh_ipo": fresh_b,
        "fresh_ipo_reason": fresh_r,
        "going_concern": gc_b,
        "going_concern_reason": gc_r,
        "reverse_merger_shell": rm_b,
        "reverse_merger_shell_reason": rm_r,
        "sub_ten_mkt_cap": tiny_b,
        "sub_ten_mkt_cap_reason": tiny_r,
        "near_term_catalyst": cat_b,
        "near_term_catalyst_reason": cat_r,
        "any_warning": any([fresh_b, rm_b, tiny_b, gc_b]),
        "warning_count": sum([fresh_b, rm_b, tiny_b, gc_b]),
    }


def compute_flags_batch(
    tickers: list[str],
    mkt_caps: dict[str, float] | None = None,
    username: str | None = None,
) -> pd.DataFrame:
    mkt_caps = mkt_caps or {}
    rows = [compute_flags(t, mkt_cap_m=mkt_caps.get(t), username=username) for t in tickers]
    return pd.DataFrame(rows)


def short_flag_string(flags_row: dict | pd.Series) -> str:
    """Compact warning emoji string for tables: 🆕 (fresh IPO), 🛑 (going concern),
    🐚 (shell), ⚠️ (sub-$10M), 📅 (catalyst note).
    """
    s = ""
    if flags_row.get("fresh_ipo"):
        s += "🆕"
    if flags_row.get("going_concern"):
        s += "🛑"
    if flags_row.get("reverse_merger_shell"):
        s += "🐚"
    if flags_row.get("sub_ten_mkt_cap"):
        s += "⚠️"
    if flags_row.get("near_term_catalyst"):
        s += "📅"
    return s


if __name__ == "__main__":
    # Smoke test
    for tk in ["VRTX", "KURA", "ALXO", "SMMT"]:
        f = compute_flags(tk)
        print(f"{tk}: warnings={f['warning_count']}  flags={short_flag_string(f) or 'clean'}  reasons={[v for k, v in f.items() if k.endswith('_reason') and v]}")

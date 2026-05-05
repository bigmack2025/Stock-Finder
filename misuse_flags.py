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

  upcoming_catalyst
    Company has a Phase 2/3 trial with a primary-completion date in the
    next 18 months (or just-completed in last 90 days). Source: clinicaltrials.gov.

  user_catalyst_note
    User has a free-text note for this ticker mentioning catalyst keywords
    (Phase, readout, FDA, BLA, PDUFA, NDA, top-line, AdCom). Manual override
    that complements the auto-pulled clinical-trial flag.

  insider_buying
    Insiders bought stock on the open market in the last 90 days at material
    size (≥2 distinct insiders, OR single buyer ≥$250K, OR total ≥$500K).
    Source: SEC Form 4 filings.

  reverse_merger_shell
    Heuristic: cash > 0.7 × assets AND R&D < $5M AND no revenue. Indicates
    a shell that's mostly cash and waiting for a transaction.

  sub_ten_million_mkt_cap
    Real biotechs at <$10M mkt cap are usually distressed. Disclose, don't
    auto-exclude.

Public API:

    flags = compute_flags(ticker, mkt_cap_m=..., username=..., company_name=...)
    flags = compute_flags_batch(tickers, mkt_caps=..., names=..., username=...)
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


def _upcoming_catalyst_flag(ticker: str, company_name: str | None, cached_only: bool = False) -> tuple[bool, str | None]:
    """Phase 2/3 trial with primary completion in next 18 months (or just-completed
    in last 90 days). Source: clinicaltrials.gov via catalysts.py."""
    if not company_name:
        return False, None
    try:
        import catalysts
        summary = catalysts.short_summary(ticker, company_name, cached_only=cached_only)
        if summary:
            return True, summary
    except Exception:
        pass
    return False, None


def _insider_buying_flag(ticker: str, cached_only: bool = False) -> tuple[bool, str | None]:
    """Insiders bought open-market shares in last 90 days at material size.
    Source: SEC Form 4 filings via insider_buying.py."""
    try:
        import insider_buying
        ev = insider_buying.short_evidence(ticker, cached_only=cached_only)
        if ev:
            return True, ev
    except Exception:
        pass
    return False, None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def compute_flags(
    ticker: str,
    mkt_cap_m: float | None = None,
    username: str | None = None,
    company_name: str | None = None,
    lazy_signals: bool = False,
) -> dict:
    """Compute all flags for one ticker. Returns dict with bool flags + reason strings.

    `lazy_signals=True` skips network calls for `insider_buying` and
    `upcoming_catalyst` — those are returned from cache only, or False if
    no cache exists yet. Used by the batch path so the screener table renders
    in <2s instead of ~3 minutes on a cold cache. The Company Peek panel
    populates them on demand (per-ticker = fast).
    """
    fresh_b, fresh_r = _fresh_ipo_flag(ticker)
    rm_b, rm_r = _reverse_merger_shell_flag(ticker)
    tiny_b, tiny_r = _sub_ten_mkt_cap_flag(ticker, mkt_cap_m)
    note_b, note_r = _user_catalyst_note_flag(username, ticker)
    gc_b, gc_r = _going_concern_flag(ticker)
    cat_b, cat_r = _upcoming_catalyst_flag(ticker, company_name, cached_only=lazy_signals)
    ins_b, ins_r = _insider_buying_flag(ticker, cached_only=lazy_signals)

    # Combined "any catalyst signal" — surfaces in tables / sorts.
    any_catalyst = bool(cat_b or note_b)
    if cat_b and note_b:
        catalyst_reason = f"{cat_r} · note: {note_r}"
    else:
        catalyst_reason = cat_r or note_r

    severity_warnings = [fresh_b, rm_b, tiny_b, gc_b]

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
        # New unified catalyst flags
        "upcoming_catalyst": cat_b,
        "upcoming_catalyst_reason": cat_r,
        "user_catalyst_note": note_b,
        "user_catalyst_note_reason": note_r,
        "any_catalyst": any_catalyst,
        "catalyst_reason": catalyst_reason,
        # Backwards-compat alias used by older app.py code paths
        "near_term_catalyst": any_catalyst,
        "near_term_catalyst_reason": catalyst_reason,
        # Insider buying (Form 4)
        "insider_buying": ins_b,
        "insider_buying_reason": ins_r,
        # Roll-ups
        "any_warning": any(severity_warnings),
        "warning_count": sum(severity_warnings),
        "any_positive_signal": any([cat_b, note_b, ins_b]),
    }


def compute_flags_batch(
    tickers: list[str],
    mkt_caps: dict[str, float] | None = None,
    names: dict[str, str] | None = None,
    username: str | None = None,
    lazy_signals: bool = True,  # default lazy — table render must be fast
) -> pd.DataFrame:
    """Compute flags for many tickers. Defaults to lazy-signal mode so the
    screener/anchor table renders fast even on a cold cache. The Company Peek
    panel populates insider_buying and upcoming_catalyst on demand."""
    mkt_caps = mkt_caps or {}
    names = names or {}
    rows = [
        compute_flags(
            t,
            mkt_cap_m=mkt_caps.get(t),
            username=username,
            company_name=names.get(t),
            lazy_signals=lazy_signals,
        )
        for t in tickers
    ]
    return pd.DataFrame(rows)


def short_flag_string(flags_row: dict | pd.Series) -> str:
    """Compact flag emoji string for tables.

    Severity (red flags first): 🛑 going concern, 🆕 fresh IPO, 🐚 shell, ⚠️ tiny.
    Positive signals after: 💰 insider buying, 📅 upcoming catalyst.
    """
    s = ""
    if flags_row.get("going_concern"):
        s += "🛑"
    if flags_row.get("fresh_ipo"):
        s += "🆕"
    if flags_row.get("reverse_merger_shell"):
        s += "🐚"
    if flags_row.get("sub_ten_mkt_cap"):
        s += "⚠️"
    if flags_row.get("insider_buying"):
        s += "💰"
    if flags_row.get("any_catalyst") or flags_row.get("near_term_catalyst"):
        s += "📅"
    return s


FLAGS_LEGEND = (
    "🛑 going-concern  ·  🆕 fresh IPO  ·  🐚 reverse-merger shell  ·  "
    "⚠️ sub-$10M mkt cap  ·  💰 insider buying (Form 4)  ·  📅 upcoming catalyst"
)


if __name__ == "__main__":
    # Smoke test
    samples = [
        ("VRTX", "Vertex Pharmaceuticals"),
        ("KURA", "Kura Oncology"),
        ("ALXO", "ALX Oncology"),
        ("SMMT", "Summit Therapeutics"),
    ]
    for tk, name in samples:
        f = compute_flags(tk, company_name=name)
        active = [k.replace("_reason", "") for k, v in f.items() if k.endswith("_reason") and v]
        print(f"{tk:6}  flags={short_flag_string(f) or 'clean':12}  active={active}")

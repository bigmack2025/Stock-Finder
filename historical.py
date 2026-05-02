"""
Historical-state fetcher.

Given (ticker, year), return a snapshot of what that company looked like at
the end of that fiscal year:
  - mkt_cap_m, cash_m, debt_m, ev_m
  - revenue_m, rd_m
  - has_revenue, log_mkt_cap, log_revenue, size_band
  - modalities (CARRIED OVER FROM CURRENT — see leakage caveat)

Sources:
  - SEC EDGAR XBRL companyfacts API for fundamentals (2010+)
  - yfinance for historical price → market cap = price × shares outstanding

XBRL coverage starts ~2010 for most filers (mandatory phase-in 2009–2011).
For earlier years we'd need to parse 10-K HTML — deferred to M2.

KNOWN LIMITATIONS (also surfaced in the UI):
  1. Modality at year T uses today's xlsx tag — look-ahead leakage. A company
     labeled "Oncology" today may have been "platform" or "general" at year T.
     Acceptable for v1 since modalities rarely flip; M2 will pull modality from
     each year's 10-K Item 1.
  2. Pipeline stage (preclin/Ph1/Ph2/Ph3) is NOT extracted — also M2.
     For now we proxy with `had_revenue_at_year`.
  3. Debt is approximated as `Liabilities − operating liabilities`. Pure debt
     concept (LongTermDebt) is sparse in XBRL.

Usage:
    from historical import get_snapshot
    snap = get_snapshot("VRTX", 2008)   # may return None if pre-XBRL
    snap = get_snapshot("VRTX", 2012)   # pre-Kalydeco approval
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
HIST_DIR = DATA_DIR / "historical"
HIST_DIR.mkdir(parents=True, exist_ok=True)

# SEC EDGAR requires a UA with contact info. The user can override the
# email via the EDGAR_USER_AGENT_EMAIL env var (set this in Streamlit secrets
# before deploying so your real email isn't baked into the public repo).
import os as _os
USER_AGENT = (
    f"Biotech-Mispricing-Engine/0.1 "
    f"({_os.environ.get('EDGAR_USER_AGENT_EMAIL', 'biotech-engine@example.com')})"
)

# XBRL coverage starts ~2010 for most filers
MIN_XBRL_YEAR = 2010

# Concept fallback chains — XBRL tag names changed in 2018 with ASC 606 etc.
CONCEPT_CHAINS = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
        "CashAndCashEquivalents",
    ],
    "shares": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    "rd": ["ResearchAndDevelopmentExpense"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "long_term_debt": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "DebtCurrent",
    ],
    "ocf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
}


# ---------------------------------------------------------------------------
# Plumbing — HTTP with rate-limit politeness
# ---------------------------------------------------------------------------

_last_request_at = 0.0


def _http_get_json(url: str) -> dict | None:
    """SEC EDGAR rate limit: 10 req/s. We pace at ~5 req/s."""
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < 0.2:
        time.sleep(0.2 - elapsed)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        _last_request_at = time.time()
        return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Ticker → CIK map (cached on disk)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _ticker_cik_map() -> dict[str, str]:
    cache = HIST_DIR / "_ticker_cik.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 24 * 3600:
        return json.loads(cache.read_text())
    data = _http_get_json("https://www.sec.gov/files/company_tickers.json") or {}
    out = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in data.values()}
    cache.write_text(json.dumps(out))
    return out


def get_cik(ticker: str) -> str | None:
    return _ticker_cik_map().get(ticker.upper())


# ---------------------------------------------------------------------------
# Companyfacts cache
# ---------------------------------------------------------------------------

def _facts_path(ticker: str) -> Path:
    return HIST_DIR / f"facts_{ticker.upper()}.json"


def _load_facts(ticker: str, max_age_days: int = 7) -> dict | None:
    p = _facts_path(ticker)
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_days * 86400:
        return json.loads(p.read_text())
    cik = get_cik(ticker)
    if not cik:
        return None
    data = _http_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    if data is None:
        # Mark as 'tried but unavailable' with a small file so we don't refetch hot
        p.write_text(json.dumps({"_unavailable": True, "ticker": ticker, "fetched_at": datetime.now(timezone.utc).isoformat()}))
        return None
    p.write_text(json.dumps(data))
    return data


# ---------------------------------------------------------------------------
# Concept extraction — pick the value AS OF end of fiscal year `year`
# ---------------------------------------------------------------------------

def _value_at_fy_end(facts_us_gaap: dict, concept: str, year: int) -> float | None:
    """For instant concepts (balance-sheet items): pick the FY end value.
    For duration concepts (revenue, R&D): pick the FY annual value.

    CRITICAL: A 10-K filed in year Y also includes prior-year comparison values
    tagged with `fy=Y` in XBRL — we must filter on the `end` date, not just `fy`,
    to avoid picking up an old comparison row.
    """
    if concept not in facts_us_gaap:
        return None
    units = facts_us_gaap[concept].get("units", {})
    if not units:
        return None
    unit_key = "USD" if "USD" in units else ("shares" if "shares" in units else next(iter(units)))
    rows = units[unit_key]

    def _end_year(r: dict) -> int | None:
        e = r.get("end") or ""
        try:
            return int(e[:4])
        except (TypeError, ValueError):
            return None

    # Filter to rows whose `end` date is in the target calendar year.
    in_year = [r for r in rows if _end_year(r) == year]

    # Prefer 10-K FY filings, then any FY, then anything in-year.
    candidates = [r for r in in_year if r.get("fp") == "FY" and (r.get("form") or "").startswith("10-K")]
    if not candidates:
        candidates = [r for r in in_year if r.get("fp") == "FY"]
    if not candidates:
        candidates = in_year
    if not candidates:
        return None

    # Prefer the most recent filing (latest restated number)
    candidates.sort(key=lambda r: r.get("filed", ""), reverse=True)
    return float(candidates[0]["val"])


def _first_available(facts_us_gaap: dict, concept_chain: list[str], year: int) -> float | None:
    for concept in concept_chain:
        v = _value_at_fy_end(facts_us_gaap, concept, year)
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Historical price → mkt cap
# ---------------------------------------------------------------------------

def _historical_mkt_cap(ticker: str, year: int, shares: float | None) -> float | None:
    """Mkt cap at end of fiscal year ≈ year-end close × shares outstanding."""
    if not shares:
        return None
    try:
        import numpy as np
        import yfinance as yf
        hist = yf.Ticker(ticker).history(
            start=f"{year}-12-15",
            end=f"{year+1}-01-15",
            auto_adjust=False,
        )
        if hist.empty:
            return None
        target = pd.Timestamp(f"{year}-12-31", tz=hist.index.tz)
        deltas_seconds = (hist.index - target).total_seconds().to_numpy()
        closest_idx = int(np.abs(deltas_seconds).argmin())
        price = float(hist.iloc[closest_idx]["Close"])
        return (price * shares) / 1e6  # in $M
    except Exception as e:
        # Silent fail is too sticky during dev; print once
        import sys
        print(f"[historical] mkt_cap fail {ticker} {year}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def get_snapshot(ticker: str, year: int) -> dict | None:
    """Return a single-year snapshot of company financials, or None if unavailable."""
    if year < MIN_XBRL_YEAR:
        return {
            "ticker": ticker,
            "year": year,
            "available": False,
            "reason": f"XBRL coverage starts {MIN_XBRL_YEAR}; pre-{MIN_XBRL_YEAR} years require 10-K HTML parsing (M2).",
        }

    facts = _load_facts(ticker)
    if not facts or facts.get("_unavailable"):
        return {
            "ticker": ticker,
            "year": year,
            "available": False,
            "reason": "No XBRL companyfacts on EDGAR (delisted, pre-IPO, or non-US filer).",
        }

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return {
            "ticker": ticker,
            "year": year,
            "available": False,
            "reason": "No us-gaap facts (likely IFRS filer).",
        }

    cash = _first_available(us_gaap, CONCEPT_CHAINS["cash"], year)
    shares = _first_available(us_gaap, CONCEPT_CHAINS["shares"], year)
    revenue = _first_available(us_gaap, CONCEPT_CHAINS["revenue"], year) or 0
    rd = _first_available(us_gaap, CONCEPT_CHAINS["rd"], year)
    assets = _first_available(us_gaap, CONCEPT_CHAINS["assets"], year)
    liabilities = _first_available(us_gaap, CONCEPT_CHAINS["liabilities"], year)
    long_term_debt = _first_available(us_gaap, CONCEPT_CHAINS["long_term_debt"], year)
    ocf = _first_available(us_gaap, CONCEPT_CHAINS["ocf"], year)

    if cash is None and shares is None and revenue == 0:
        return {
            "ticker": ticker,
            "year": year,
            "available": False,
            "reason": f"No financial data for fiscal year {year}.",
        }

    mkt_cap_m = _historical_mkt_cap(ticker, year, shares)

    # Sanity clip — protect against bad XBRL share-count values or yfinance
    # split-adjustment quirks. A real biotech can't have mkt cap > 100x its
    # total assets (and the largest US biotech ever was ~$330B at peak).
    if mkt_cap_m is not None:
        max_asset_anchor = max(assets or 0, cash or 0, 1) / 1e6
        if mkt_cap_m > 100 * max_asset_anchor or mkt_cap_m > 1_000_000:
            mkt_cap_m = None  # mark as unreliable; downstream signals will skip

    # Convert raw $ to $M
    cash_m = cash / 1e6 if cash else None
    revenue_m = revenue / 1e6
    rd_m = rd / 1e6 if rd else None
    debt_m = long_term_debt / 1e6 if long_term_debt else None
    ev_m = None
    if mkt_cap_m is not None and cash_m is not None:
        ev_m = mkt_cap_m - cash_m + (debt_m or 0)

    import math
    log_mkt_cap = math.log10(mkt_cap_m) if mkt_cap_m and mkt_cap_m > 0 else None
    log_revenue = math.log10(revenue_m + 1) if revenue_m >= 0 else 0

    from data_layer import size_band as _size_band

    return {
        "ticker": ticker.upper(),
        "year": year,
        "available": True,
        "fy_end_date": f"{year}-12-31",
        "mkt_cap_m": mkt_cap_m,
        "cash_m": cash_m,
        "debt_m": debt_m,
        "ev_m": ev_m,
        "revenue_m": revenue_m,
        "rd_m": rd_m,
        "assets_m": assets / 1e6 if assets else None,
        "liabilities_m": liabilities / 1e6 if liabilities else None,
        "shares_outstanding": shares,
        "operating_cash_flow": ocf,
        "has_revenue": int(revenue_m > 0),
        "log_mkt_cap": log_mkt_cap,
        "log_revenue": log_revenue,
        "size_band": _size_band(mkt_cap_m),
    }


def _value_at_date(facts_us_gaap: dict, concept_chain: list[str], query_date: str) -> tuple[float | None, dict | None]:
    """Find the most recent value for any concept in `concept_chain` that would
    have been publicly known on `query_date` (YYYY-MM-DD). Honors the SEC `filed`
    date so we never use info that wasn't yet released.

    Returns (value, source_filing_metadata).
    """
    best: tuple[float | None, dict | None] = (None, None)
    best_end = ""
    for concept in concept_chain:
        if concept not in facts_us_gaap:
            continue
        units = facts_us_gaap[concept].get("units", {})
        if not units:
            continue
        unit_key = "USD" if "USD" in units else ("shares" if "shares" in units else next(iter(units)))
        rows = units[unit_key]
        # Filter to rows filed on or before query date — no look-ahead
        available = [r for r in rows if (r.get("filed") or "") and r["filed"] <= query_date]
        if not available:
            continue
        # Prefer the row with the latest `end` date (most recent balance-sheet snapshot we'd know)
        available.sort(key=lambda r: ((r.get("end") or ""), r.get("filed") or ""), reverse=True)
        candidate = available[0]
        end = candidate.get("end") or ""
        if end > best_end:
            best = (float(candidate["val"]), candidate)
            best_end = end
    return best


def _ttm_value_at_date(facts_us_gaap: dict, concept_chain: list[str], query_date: str) -> float | None:
    """For duration concepts (revenue, R&D): sum the trailing 4 quarters publicly
    known on query_date. If only annual numbers exist, return the most recent
    annual value.
    """
    best_annual: float | None = None
    best_annual_end = ""
    for concept in concept_chain:
        if concept not in facts_us_gaap:
            continue
        units = facts_us_gaap[concept].get("units", {})
        unit_key = "USD" if "USD" in units else next(iter(units), None)
        if unit_key is None:
            continue
        rows = [r for r in units[unit_key] if (r.get("filed") or "") and r["filed"] <= query_date]
        if not rows:
            continue
        # Look for full-year filings (fp=FY) ending on or before query date
        annuals = [r for r in rows if r.get("fp") == "FY"]
        annuals.sort(key=lambda r: r.get("end") or "", reverse=True)
        for r in annuals:
            end = r.get("end") or ""
            if end and end <= query_date and end > best_annual_end:
                best_annual = float(r["val"])
                best_annual_end = end
                break
    return best_annual


def get_snapshot_at_date(ticker: str, query_date: str) -> dict | None:
    """Return a snapshot reflecting what was publicly known about `ticker` on
    `query_date` (format YYYY-MM-DD). Quarterly precision via 10-Q + exact-day
    market cap from yfinance close × most-recent shares outstanding.
    """
    import math
    facts = _load_facts(ticker)
    if not facts or facts.get("_unavailable"):
        return {"ticker": ticker, "query_date": query_date, "available": False,
                "reason": "No XBRL companyfacts on EDGAR (delisted, pre-IPO, or non-US filer)."}
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return {"ticker": ticker, "query_date": query_date, "available": False,
                "reason": "No us-gaap facts (likely IFRS filer)."}

    cash, cash_src = _value_at_date(us_gaap, CONCEPT_CHAINS["cash"], query_date)
    shares, shares_src = _value_at_date(us_gaap, CONCEPT_CHAINS["shares"], query_date)
    debt, _ = _value_at_date(us_gaap, CONCEPT_CHAINS["long_term_debt"], query_date)
    assets, _ = _value_at_date(us_gaap, CONCEPT_CHAINS["assets"], query_date)
    liabilities, _ = _value_at_date(us_gaap, CONCEPT_CHAINS["liabilities"], query_date)

    revenue = _ttm_value_at_date(us_gaap, CONCEPT_CHAINS["revenue"], query_date) or 0
    rd = _ttm_value_at_date(us_gaap, CONCEPT_CHAINS["rd"], query_date)
    ocf = _ttm_value_at_date(us_gaap, CONCEPT_CHAINS["ocf"], query_date)

    if cash is None and shares is None and revenue == 0:
        return {"ticker": ticker, "query_date": query_date, "available": False,
                "reason": f"No financial data filed on or before {query_date}."}

    # Exact-day market cap: yfinance close on query_date × shares from most recent 10-Q/K
    mkt_cap_m: float | None = None
    if shares:
        try:
            import yfinance as yf
            import numpy as np
            qd = pd.Timestamp(query_date)
            hist = yf.Ticker(ticker).history(
                start=(qd - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                end=(qd + pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                auto_adjust=False,
            )
            if not hist.empty:
                target = pd.Timestamp(query_date, tz=hist.index.tz)
                idx = int(np.abs((hist.index - target).total_seconds().to_numpy()).argmin())
                price = float(hist.iloc[idx]["Close"])
                mkt_cap_m = (price * shares) / 1e6
        except Exception:
            pass

    # Sanity clip
    if mkt_cap_m is not None:
        max_anchor = max(assets or 0, cash or 0, 1) / 1e6
        if mkt_cap_m > 100 * max_anchor or mkt_cap_m > 1_000_000:
            mkt_cap_m = None

    cash_m = cash / 1e6 if cash else None
    revenue_m = revenue / 1e6
    rd_m = rd / 1e6 if rd else None
    debt_m = debt / 1e6 if debt else None
    ev_m = None
    if mkt_cap_m is not None and cash_m is not None:
        ev_m = mkt_cap_m - cash_m + (debt_m or 0)
    log_mkt_cap = math.log10(mkt_cap_m) if mkt_cap_m and mkt_cap_m > 0 else None
    log_revenue = math.log10(revenue_m + 1) if revenue_m >= 0 else 0

    from data_layer import size_band as _size_band
    return {
        "ticker": ticker.upper(),
        "query_date": query_date,
        "available": True,
        "balance_sheet_as_of": (cash_src or shares_src or {}).get("end"),
        "balance_sheet_filed": (cash_src or shares_src or {}).get("filed"),
        "balance_sheet_form": (cash_src or shares_src or {}).get("form"),
        "mkt_cap_m": mkt_cap_m,
        "cash_m": cash_m,
        "debt_m": debt_m,
        "ev_m": ev_m,
        "revenue_m": revenue_m,
        "rd_m": rd_m,
        "assets_m": assets / 1e6 if assets else None,
        "liabilities_m": liabilities / 1e6 if liabilities else None,
        "shares_outstanding": shares,
        "operating_cash_flow": ocf,
        "has_revenue": int(revenue_m > 0),
        "log_mkt_cap": log_mkt_cap,
        "log_revenue": log_revenue,
        "size_band": _size_band(mkt_cap_m),
    }


def available_years(ticker: str) -> list[int]:
    """Years for which we have at least cash + shares from XBRL."""
    facts = _load_facts(ticker)
    if not facts:
        return []
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return []
    years_with_cash: set[int] = set()
    years_with_shares: set[int] = set()
    for concept in CONCEPT_CHAINS["cash"]:
        if concept in us_gaap:
            for unit_rows in us_gaap[concept]["units"].values():
                for r in unit_rows:
                    if r.get("fy") and r.get("fp") == "FY":
                        years_with_cash.add(r["fy"])
    for concept in CONCEPT_CHAINS["shares"]:
        if concept in us_gaap:
            for unit_rows in us_gaap[concept]["units"].values():
                for r in unit_rows:
                    if r.get("fy") and r.get("fp") == "FY":
                        years_with_shares.add(r["fy"])
    return sorted(years_with_cash & years_with_shares)


if __name__ == "__main__":
    for tk in ["VRTX", "ALNY", "ARGX", "KURA"]:
        print(f"\n=== {tk} ===")
        years = available_years(tk)
        print(f"Years available: {years[:5]}...{years[-3:]} (n={len(years)})")
        for yr in [2008, 2010, 2012, 2015, 2018, 2022]:
            snap = get_snapshot(tk, yr)
            if snap and snap.get("available"):
                mc = f"${snap['mkt_cap_m']:>9,.0f}M" if snap.get("mkt_cap_m") else "       n/a"
                cs = f"${(snap.get('cash_m') or 0):>7,.0f}M" if snap.get("cash_m") is not None else "      n/a"
                print(f"  {yr}: mkt_cap={mc}  cash={cs}  rev=${snap['revenue_m']:>7,.0f}M  size={snap['size_band']}")
            elif snap:
                print(f"  {yr}: unavailable ({snap.get('reason', '?')})")

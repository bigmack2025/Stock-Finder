"""
M9: Delisted-biotech ingestion.

Why this matters: the universe is today's living biotechs. Names that delisted
between then and now (acquired into oblivion, bankrupt, reverse-merged out of
biotech) are missing — and those tend to be value traps that the cheapness
signal would have flagged. Without delisted names, backtest results are
biased favorably toward the cheapness signal.

What this module does:
  1. Pulls SEC EDGAR's full quarterly index for one or more past quarters.
  2. Extracts the unique CIKs that filed 10-K that quarter.
  3. Cross-references against current `company_tickers.json` — CIKs not in the
     current list are candidates for delisting.
  4. Fetches each candidate's `submissions/CIK{cik}.json` to get SIC code +
     last-filing date.
  5. Filters to biotech SIC codes (2836 pharma preps, 8731 commercial research,
     2834 pharmaceutical preparations) AND last-filing > 18 months ago.
  6. For each delisted biotech, pulls last-known XBRL companyfacts to recover
     the company's last-reported financial state.
  7. Writes `data/delisted/<TICKER>.json` per-company + a consolidated
     `data/delisted_universe.parquet`.

Public API:
  discover_delistings(quarter="2017Q4") -> int          # how many added
  load_delisted_universe() -> pd.DataFrame
  augment_universe(live_df, with_delisted=True) -> pd.DataFrame

Note: this is M9 v1. The seed set covers 2017 Q4. To expand coverage, run
discover_delistings on additional quarters — caches accumulate.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DELISTED_DIR = DATA_DIR / "delisted"
DELISTED_DIR.mkdir(parents=True, exist_ok=True)

import os as _os
USER_AGENT = (
    f"Biotech-Mispricing-Engine/0.1 "
    f"({_os.environ.get('EDGAR_USER_AGENT_EMAIL', 'biotech-engine@example.com')})"
)

# Biotech-relevant SIC codes
BIOTECH_SICS = {"2836", "8731", "2834"}

# How long must a CIK be silent before we call it delisted? Real delistings
# stop filing within ~12 months. Use 18 months for a margin.
DELISTED_QUIET_MONTHS = 18


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

_last_request_at = 0.0


def _http_get(url: str, accept: str = "*/*") -> bytes | None:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < 0.15:
        time.sleep(0.15 - elapsed)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        _last_request_at = time.time()
        return data
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Form-index parsing
# ---------------------------------------------------------------------------

def _parse_form_idx(text: str) -> list[dict]:
    """Parse EDGAR's form.idx (fixed-width but messy). Return list of
    {form, company, cik, filed, filename} dicts for 10-K* rows (including
    amendments like 10-K/A)."""
    out: list[dict] = []
    for line in text.split("\n"):
        # 10-K, 10-K/A, 10-K405, etc. — any form whose first 4 chars are "10-K"
        if not (line.startswith("10-K") and len(line) > 4 and line[4] in (" ", "/")):
            continue
        # form.idx is fixed-width with variable padding; use simple regex
        m = re.match(r"^(10-K\S*)\s+(.+?)\s{2,}(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\S+)", line)
        if not m:
            continue
        out.append({
            "form": m.group(1),
            "company": m.group(2).strip(),
            "cik": m.group(3).zfill(10),
            "filed": m.group(4),
            "filename": m.group(5),
        })
    return out


def fetch_quarter_form_idx(quarter: str) -> list[dict]:
    """quarter format: '2017Q4' or '2017QTR4'"""
    yr = quarter[:4]
    q_raw = quarter[4:].upper().replace("QTR", "Q")  # accept both forms
    q = "QTR" + q_raw[1]  # canonical form is QTR1..QTR4
    url = f"https://www.sec.gov/Archives/edgar/full-index/{yr}/{q}/form.idx"
    raw = _http_get(url)
    if not raw:
        return []
    text = raw.decode("latin-1", errors="ignore")
    return _parse_form_idx(text)


# ---------------------------------------------------------------------------
# Current ticker map
# ---------------------------------------------------------------------------

def _current_cik_set() -> set[str]:
    """All CIKs of companies currently holding a ticker (from EDGAR's
    company_tickers.json — the live-listed universe)."""
    raw = _http_get("https://www.sec.gov/files/company_tickers.json", accept="application/json")
    if not raw:
        return set()
    data = json.loads(raw)
    return {str(v["cik_str"]).zfill(10) for v in data.values()}


# ---------------------------------------------------------------------------
# Per-CIK enrichment
# ---------------------------------------------------------------------------

def _submissions(cik: str) -> dict | None:
    raw = _http_get(f"https://data.sec.gov/submissions/CIK{cik}.json", accept="application/json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _is_biotech(sub: dict) -> bool:
    sic = sub.get("sic") or ""
    return str(sic) in BIOTECH_SICS


def _last_filing_date(sub: dict) -> str | None:
    recent = sub.get("filings", {}).get("recent", {})
    dates = recent.get("filingDate", [])
    if not dates:
        return None
    return max(dates)


def _months_since(date_str: str) -> int:
    try:
        d = datetime.fromisoformat(date_str)
    except Exception:
        return 0
    delta = datetime.now() - d
    return int(delta.days / 30)


# ---------------------------------------------------------------------------
# Last-known state — pull XBRL companyfacts (reuse historical.py)
# ---------------------------------------------------------------------------

def _last_known_state(ticker_or_cik: str, last_fy: int) -> dict | None:
    """Fetch the last reported FY snapshot via the historical module."""
    try:
        import historical
        # historical.get_snapshot expects ticker, not CIK. We'll need the
        # company's ticker if it had one. For ticker-less entities, can't
        # use historical's price lookup → cash/revenue still work but no mkt cap.
        snap = historical.get_snapshot(ticker_or_cik, last_fy)
        return snap
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _delisted_path(cik: str) -> Path:
    return DELISTED_DIR / f"CIK{cik}.json"


def discover_delistings(quarter: str = "2017Q4", limit: int | None = None, progress: bool = True) -> int:
    """Process one quarter's form.idx → find delisted biotechs → cache last-known state.
    Returns number of new delisted records added."""
    if progress:
        print(f"Step 1/4: pulling form.idx for {quarter}...")
    filings = fetch_quarter_form_idx(quarter)
    if progress:
        print(f"  → {len(filings)} 10-K filings")

    # Unique CIKs (a CIK might file multiple amendments; dedupe)
    cik_to_filing = {}
    for f in filings:
        cik_to_filing.setdefault(f["cik"], f)
    if progress:
        print(f"  → {len(cik_to_filing)} unique CIKs")

    if progress:
        print("Step 2/4: cross-reference against current tickers...")
    live = _current_cik_set()
    candidates = [c for c in cik_to_filing if c not in live]
    if progress:
        print(f"  → {len(candidates)} CIKs not in current tickers (delisted candidates)")

    if limit:
        candidates = candidates[:limit]

    if progress:
        print(f"Step 3/4: enriching {len(candidates)} candidates with SIC + last filing date...")
    biotech_delistings: list[dict] = []
    for i, cik in enumerate(candidates, 1):
        if progress and (i == 1 or i % 25 == 0 or i == len(candidates)):
            print(f"  [{i}/{len(candidates)}]")

        # Skip if we've cached this CIK already (old delisting that's persistent)
        cache_path = _delisted_path(cik)
        if cache_path.exists():
            biotech_delistings.append(json.loads(cache_path.read_text()))
            continue

        sub = _submissions(cik)
        if not sub:
            continue
        if not _is_biotech(sub):
            continue
        last_filed = _last_filing_date(sub) or ""
        if not last_filed:
            continue
        if _months_since(last_filed) < DELISTED_QUIET_MONTHS:
            # Still active, just not in current tickers (could be a delayed update)
            continue

        record = {
            "cik": cik,
            "name": sub.get("name", ""),
            "ticker_was": (sub.get("tickers") or [None])[0],
            "exchange_was": (sub.get("exchanges") or [None])[0],
            "sic": sub.get("sic"),
            "sic_description": sub.get("sicDescription"),
            "last_filing_date": last_filed,
            "delisted_after": last_filed,
            "discovered_quarter": quarter,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        # Pull last-known financials if we have a ticker symbol
        if record["ticker_was"]:
            try:
                last_year = int(last_filed[:4])
                snap = _last_known_state(record["ticker_was"], last_year)
                if snap and snap.get("available"):
                    record["last_known_state"] = snap
            except Exception:
                pass

        cache_path.write_text(json.dumps(record, indent=2))
        biotech_delistings.append(record)

    if progress:
        print(f"  → {len(biotech_delistings)} biotech delistings found this run")

    # Step 4: rebuild parquet from ALL cached records (this run + prior runs)
    if progress:
        print("Step 4/4: writing delisted_universe.parquet from all cached records...")
    all_records = []
    for p in DELISTED_DIR.glob("CIK*.json"):
        try:
            all_records.append(json.loads(p.read_text()))
        except Exception:
            continue
    df = _build_dataframe(all_records)
    df.to_parquet(DATA_DIR / "delisted_universe.parquet", index=False)
    if progress:
        print(f"  ✓ wrote data/delisted_universe.parquet ({len(df)} total rows)")
    return len(all_records)


def _build_dataframe(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        last = r.get("last_known_state") or {}
        rows.append({
            "ticker": r.get("ticker_was") or f"CIK{r['cik']}",
            "name": r.get("name"),
            "region": "US",
            "industry": r.get("sic_description"),
            "sic": r.get("sic"),
            "delisted_after": r.get("delisted_after"),
            "last_filing_date": r.get("last_filing_date"),
            "mkt_cap_m": last.get("mkt_cap_m"),
            "revenue_m": last.get("revenue_m") or 0,
            "cash_m": last.get("cash_m"),
            "debt_m": last.get("debt_m"),
            "ev_m": last.get("ev_m"),
            "size_band": last.get("size_band", "unknown"),
            "discovered_quarter": r.get("discovered_quarter"),
            "is_delisted": True,
        })
    return pd.DataFrame(rows)


def load_delisted_universe() -> pd.DataFrame:
    p = DATA_DIR / "delisted_universe.parquet"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_parquet(p)


def augment_universe(live_df: pd.DataFrame, with_delisted: bool = True) -> pd.DataFrame:
    """Concatenate live + delisted universes into one. The `is_delisted` column
    distinguishes them so callers can filter as needed.
    """
    out = live_df.copy()
    out["is_delisted"] = False
    if not with_delisted:
        return out
    delisted = load_delisted_universe()
    if delisted.empty:
        return out
    # Coerce shape: delisted may be missing some columns the live universe has.
    # Fill with NaN/empty defaults.
    for col in out.columns:
        if col not in delisted.columns:
            delisted[col] = None
    for col in delisted.columns:
        if col not in out.columns:
            out[col] = None
    return pd.concat([out, delisted[out.columns]], ignore_index=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", default="2017Q4")
    ap.add_argument("--limit", type=int, default=None, help="Cap candidate enrichment for testing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    n = discover_delistings(quarter=args.quarter, limit=args.limit, progress=not args.quiet)
    print(f"\nTotal delisted records: {n}")


if __name__ == "__main__":
    _cli()

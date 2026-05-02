"""
Backtest the cheapness signal.

Hypothesis: at time T, names ranked top-quintile by cheapness should outperform
names ranked bottom-quintile over the next 1Y / 3Y.

Design (kept simple deliberately):
  1. Pick a backtest year T (e.g. 2018).
  2. For every ticker in the current US universe, pull its T-end XBRL snapshot
     (cash, debt, EV, mkt cap from price × shares).
  3. Filter to "investable at T" — must have a 12/31/T closing price + the
     required signals.
  4. Run mispricing.compute_score on the synthetic-T universe (peer-relative
     signals are computed against the T-universe median, which is clean).
  5. Bucket by cheapness quintile.
  6. For each ticker, compute forward total return at T+1Y and T+3Y from
     yfinance (auto_adjust=True so we get total return, not price return).
  7. Aggregate returns by bucket — mean, median, hit rate.

KNOWN LIMITATIONS (documented loud in the report):
  - SURVIVOR BIAS — universe is companies that exist today. Names that
    delisted between T and now are missing. The dead names probably skew
    the result *favorable* to expensive (they were the value traps that
    didn't survive). True signal could be weaker than this measures.
  - LOOK-AHEAD MODALITY — modality tags are today's. Doesn't matter for
    this backtest since we only use financial signals, but flagging.
  - PRE-IPO — companies that didn't trade at T are excluded automatically
    (no 12/31/T price → drop).
  - SIGNAL ADJUSTMENT — uses the same cheapness signals as live. Different
    weights would give different results.

Usage:
    from backtest import run_backtest
    result = run_backtest(year=2018, forward_years=[1, 3])

    # CLI
    python backtest.py --year 2018
    python backtest.py --year 2020 --forward-years 1,3
"""

from __future__ import annotations

import argparse
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import historical
import mispricing

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
BACKTEST_DIR = DATA_DIR / "backtest"
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Building the synthetic-T universe
# ---------------------------------------------------------------------------

def _build_synthetic_universe(year: int, tickers: list[str], progress: bool = True) -> pd.DataFrame:
    """For each ticker, get T-end financial state. Returns a DataFrame with the
    columns that mispricing.compute_score expects."""
    rows: list[dict] = []
    n = len(tickers)
    for i, tk in enumerate(tickers, 1):
        if progress and (i == 1 or i % 50 == 0 or i == n):
            print(f"  [{i}/{n}] fetching XBRL for {tk}")
        snap = historical.get_snapshot(tk, year)
        if not snap or not snap.get("available"):
            continue
        # compute_score expects these columns:
        #   ticker, mkt_cap_m_yf, cash_m, debt_m, ev_m, operatingCashflow
        rows.append({
            "ticker": tk,
            "mkt_cap_m_yf": snap.get("mkt_cap_m"),
            "cash_m": snap.get("cash_m"),
            "debt_m": snap.get("debt_m") or 0,
            "ev_m": snap.get("ev_m"),
            "operatingCashflow": snap.get("operating_cash_flow"),
            # carry-along context
            "name": tk,
            "region": "US",
            "primary_modality": "?",   # not used by compute_score
            "size_band": snap.get("size_band", "unknown"),
            "revenue_m_at_t": snap.get("revenue_m") or 0,
            "has_revenue_at_t": snap.get("has_revenue", 0),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------

def _forward_total_return(ticker: str, start_year: int, end_year: int) -> float | None:
    """Total return from 12/31/start_year close to 12/31/end_year close.
    Uses auto_adjust=True so dividends are reinvested.
    Returns None if either price is unavailable.
    """
    try:
        import yfinance as yf
        # Wide windows — pick the trading day closest to year-end
        start = f"{start_year}-12-15"
        end = f"{end_year + 1}-01-15"
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist.empty:
            return None

        target_start = pd.Timestamp(f"{start_year}-12-31", tz=hist.index.tz)
        target_end = pd.Timestamp(f"{end_year}-12-31", tz=hist.index.tz)

        # Filter to find the closest trading day to each year-end
        s_idx = int(np.abs((hist.index - target_start).total_seconds().to_numpy()).argmin())
        e_idx = int(np.abs((hist.index - target_end).total_seconds().to_numpy()).argmin())

        # Sanity: need to actually be reasonably close (within 30 days)
        s_close_date = hist.index[s_idx]
        e_close_date = hist.index[e_idx]
        if abs((s_close_date - target_start).days) > 30:
            return None
        if abs((e_close_date - target_end).days) > 30:
            return None

        p_start = float(hist.iloc[s_idx]["Close"])
        p_end = float(hist.iloc[e_idx]["Close"])
        if p_start <= 0 or p_end <= 0:
            return None
        return (p_end / p_start) - 1.0
    except Exception:
        return None


def _attach_forward_returns(df: pd.DataFrame, year: int, forward_years: list[int], progress: bool = True) -> pd.DataFrame:
    out = df.copy()
    n = len(out)
    for fy in forward_years:
        col = f"ret_{fy}y"
        out[col] = np.nan
        for i, (idx, row) in enumerate(out.iterrows(), 1):
            if progress and (i == 1 or i % 50 == 0 or i == n):
                print(f"  [{i}/{n}] forward {fy}y for {row['ticker']}")
            r = _forward_total_return(row["ticker"], year, year + fy)
            out.at[idx, col] = r
            time.sleep(0.05)  # be nice to yfinance
    return out


# ---------------------------------------------------------------------------
# Bucketing + aggregation
# ---------------------------------------------------------------------------

def _bucket_quintile(scored: pd.DataFrame, n_buckets: int = 5) -> pd.DataFrame:
    out = scored.copy()
    valid = out["cheapness_score"].notna()
    out["bucket"] = np.nan
    if valid.sum() >= n_buckets:
        # Higher cheapness → lower bucket number (1 = cheapest)
        ranks = out.loc[valid, "cheapness_score"].rank(ascending=False, method="first")
        out.loc[valid, "bucket"] = pd.qcut(ranks, n_buckets, labels=False) + 1
    return out


def _aggregate(df: pd.DataFrame, forward_years: list[int]) -> pd.DataFrame:
    rows = []
    for b in sorted(df["bucket"].dropna().unique()):
        sub = df.loc[df["bucket"] == b]
        row = {
            "bucket": int(b),
            "label": "1=cheapest" if b == 1 else (f"{int(b)}=expensive" if b == df["bucket"].max() else f"q{int(b)}"),
            "n": len(sub),
        }
        for fy in forward_years:
            col = f"ret_{fy}y"
            valid = sub[col].dropna()
            row[f"n_with_{fy}y"] = len(valid)
            row[f"mean_{fy}y"] = float(valid.mean()) if len(valid) else np.nan
            row[f"median_{fy}y"] = float(valid.median()) if len(valid) else np.nan
            row[f"hit_rate_{fy}y"] = float((valid > 0).mean()) if len(valid) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_backtest(
    year: int,
    forward_years: list[int] | None = None,
    universe_path: Path | None = None,
    progress: bool = True,
    include_delisted: bool = True,
) -> dict:
    """Run the backtest. Returns a dict with detail DF and summary DF.

    `include_delisted=True` (default for backtest mode): augments the live
    universe with M9-discovered biotech delistings, so survivor bias is
    reduced. Those names get last-known financial state for the backtest year
    and are dropped if they hadn't yet delisted as of the backtest date.
    """
    forward_years = forward_years or [1, 3]
    import engine
    universe = engine.load_universe()

    # M9: optionally fold in delisted biotechs to reduce survivor bias
    if include_delisted:
        try:
            import delisted as _delisted
            aug = _delisted.augment_universe(universe, with_delisted=True)
            n_delisted = int(aug["is_delisted"].sum()) if "is_delisted" in aug.columns else 0
            if progress and n_delisted > 0:
                print(f"  + {n_delisted} delisted biotechs folded in (M9)")
            universe = aug
        except Exception:
            pass

    # US tickers only (XBRL is us-gaap; no IFRS support yet)
    tickers = universe.loc[universe["region"] == "US", "ticker"].tolist()
    if progress:
        print(f"Step 1/4: Building synthetic-{year} universe over {len(tickers)} US tickers...")
    syn = _build_synthetic_universe(year, tickers, progress=progress)
    if progress:
        print(f"  → {len(syn)} tickers had usable {year} XBRL data")

    if progress:
        print(f"Step 2/4: Computing cheapness signals on synthetic-{year} universe...")
    scored = mispricing.compute_score(syn)
    if progress:
        n_scored = int(scored["cheapness_score"].notna().sum())
        print(f"  → {n_scored} tickers scored")

    if progress:
        print(f"Step 3/4: Pulling forward {forward_years}-year returns from yfinance...")
    detail = _attach_forward_returns(scored, year, forward_years, progress=progress)

    if progress:
        print("Step 4/4: Aggregating by quintile...")
    detail = _bucket_quintile(detail, n_buckets=5)
    summary = _aggregate(detail, forward_years)

    # Save artifacts
    out_detail = BACKTEST_DIR / f"detail_{year}.parquet"
    out_summary = BACKTEST_DIR / f"summary_{year}.csv"
    detail.to_parquet(out_detail, index=False)
    summary.to_csv(out_summary, index=False)
    if progress:
        print(f"\n✓ Saved {out_detail.name} and {out_summary.name}")

    return {
        "year": year,
        "forward_years": forward_years,
        "n_universe_attempted": len(tickers),
        "n_with_xbrl": len(syn),
        "n_scored": int(scored["cheapness_score"].notna().sum()),
        "n_with_forward_return": int(detail[f"ret_{forward_years[0]}y"].notna().sum()),
        "detail": detail,
        "summary": summary,
        "saved_at": datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--forward-years", type=str, default="1,3", help="Comma-separated, e.g. 1,3")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    fy = [int(s) for s in args.forward_years.split(",")]
    result = run_backtest(year=args.year, forward_years=fy, progress=not args.quiet)

    print("\n" + "=" * 78)
    print(f"BACKTEST RESULTS — base year {args.year}, forward years {fy}")
    print("=" * 78)
    print(f"Universe attempted:           {result['n_universe_attempted']}")
    print(f"Got {args.year} XBRL snapshot:        {result['n_with_xbrl']}")
    print(f"Scored (cheapness != NaN):    {result['n_scored']}")
    print(f"Has {fy[0]}y forward return:        {result['n_with_forward_return']}")
    print()
    pd.set_option("display.width", 220)
    print(result["summary"].to_string(index=False))


if __name__ == "__main__":
    _cli()

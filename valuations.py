"""
Valuations puller — fetches cash, debt, EV, shares, price from yfinance and
caches per-ticker. Works on-demand (anchor mode hits ~30 tickers) or in bulk
(screener mode pre-warms the full universe).

Cache: data/valuations.parquet  +  data/valuations_<date>.parquet archive.
Stale entries (>24h) are refetched. Failed fetches are recorded with a
fetched_at timestamp so we don't hammer Yahoo retrying dead tickers.

Usage:
    from valuations import get_valuation, get_valuations
    v = get_valuation("KURA")          # single ticker
    df = get_valuations(["KURA","IDYA"])  # batch
    # CLI bulk:
    python valuations.py --all
    python valuations.py --tickers KURA,ALXO,IDYA
"""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR = DATA_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)
CACHE_PATH = DATA_DIR / "valuations.parquet"

# How long is a cached row good for?
STALE_AFTER_HOURS = 24

VALUATION_FIELDS = [
    "ticker",
    "marketCap",
    "totalCash",
    "totalDebt",
    "enterpriseValue",
    "sharesOutstanding",
    "currentPrice",
    "operatingCashflow",
    "freeCashflow",
    "fetched_at",
    "fetch_ok",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_cache() -> pd.DataFrame:
    if not CACHE_PATH.exists():
        return pd.DataFrame(columns=VALUATION_FIELDS)
    return pd.read_parquet(CACHE_PATH)


def _save_cache(df: pd.DataFrame) -> None:
    df.to_parquet(CACHE_PATH, index=False)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive = ARCHIVE_DIR / f"valuations_{today}.parquet"
    shutil.copy(CACHE_PATH, archive)
    # Trim to last 3 archive copies
    archives = sorted(ARCHIVE_DIR.glob("valuations_*.parquet"))
    for old in archives[:-3]:
        old.unlink()


def _is_stale(row: pd.Series) -> bool:
    if pd.isna(row.get("fetched_at")):
        return True
    fetched = pd.to_datetime(row["fetched_at"], utc=True)
    age_hours = (pd.Timestamp.now(tz="UTC") - fetched).total_seconds() / 3600.0
    return age_hours > STALE_AFTER_HOURS


def _fetch_one(ticker: str) -> dict:
    """Hit yfinance for a single ticker. Never raises; returns dict with fetch_ok flag."""
    out = {k: None for k in VALUATION_FIELDS}
    out["ticker"] = ticker
    out["fetched_at"] = _now_iso()
    out["fetch_ok"] = False
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        if not info.get("marketCap"):
            return out
        for k in [
            "marketCap",
            "totalCash",
            "totalDebt",
            "enterpriseValue",
            "sharesOutstanding",
            "currentPrice",
            "operatingCashflow",
            "freeCashflow",
        ]:
            v = info.get(k)
            out[k] = float(v) if v is not None else None
        out["fetch_ok"] = True
    except Exception:
        # Swallow — caller checks fetch_ok
        pass
    return out


def get_valuation(ticker: str, force: bool = False) -> dict:
    """Get one ticker's valuation, using cache if fresh."""
    cache = _load_cache()
    hit = cache.loc[cache["ticker"] == ticker]
    if not force and not hit.empty and not _is_stale(hit.iloc[0]):
        return hit.iloc[0].to_dict()
    row = _fetch_one(ticker)
    # Upsert into cache
    cache = cache.loc[cache["ticker"] != ticker]
    cache = pd.concat([cache, pd.DataFrame([row])], ignore_index=True)
    _save_cache(cache)
    return row


def get_valuations(
    tickers: list[str],
    force: bool = False,
    sleep_s: float = 0.4,
    progress: bool = False,
) -> pd.DataFrame:
    """Batch-fetch valuations. Uses cache for fresh entries; only network-hits stale ones."""
    cache = _load_cache()
    out_rows: list[dict] = []
    to_fetch: list[str] = []
    for tk in tickers:
        hit = cache.loc[cache["ticker"] == tk]
        if not force and not hit.empty and not _is_stale(hit.iloc[0]):
            out_rows.append(hit.iloc[0].to_dict())
        else:
            to_fetch.append(tk)

    n_fetch = len(to_fetch)
    for i, tk in enumerate(to_fetch, 1):
        if progress and (i == 1 or i % 20 == 0 or i == n_fetch):
            print(f"  [{i}/{n_fetch}] fetching {tk}")
        row = _fetch_one(tk)
        out_rows.append(row)
        # Upsert
        cache = cache.loc[cache["ticker"] != tk]
        cache = pd.concat([cache, pd.DataFrame([row])], ignore_index=True)
        if i % 25 == 0 or i == n_fetch:
            _save_cache(cache)
        time.sleep(sleep_s)

    df = pd.DataFrame(out_rows)
    # Re-order so result matches input order
    if not df.empty:
        df = df.set_index("ticker").reindex(tickers).reset_index()
    return df


def annotate_universe(universe: pd.DataFrame) -> pd.DataFrame:
    """Left-join cached valuations onto the universe DataFrame.
    Adds: cash_m, debt_m, ev_m, shares_m, price, fetched_at, fetch_ok, mkt_cap_m_yf.
    Does NOT trigger fetches — call get_valuations() first to populate cache.
    """
    cache = _load_cache()
    if cache.empty:
        # Add empty columns so downstream code can run
        for col in ["cash_m", "debt_m", "ev_m", "shares_m", "price", "mkt_cap_m_yf", "fetched_at", "fetch_ok"]:
            universe[col] = None
        return universe

    cache = cache.copy()
    cache["cash_m"] = cache["totalCash"] / 1e6
    cache["debt_m"] = cache["totalDebt"] / 1e6
    cache["ev_m"] = cache["enterpriseValue"] / 1e6
    cache["shares_m"] = cache["sharesOutstanding"] / 1e6
    cache["price"] = cache["currentPrice"]
    cache["mkt_cap_m_yf"] = cache["marketCap"] / 1e6
    keep = ["ticker", "cash_m", "debt_m", "ev_m", "shares_m", "price", "mkt_cap_m_yf", "fetched_at", "fetch_ok"]
    return universe.merge(cache[keep], on="ticker", how="left")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Fetch valuations for the entire universe.")
    ap.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers to fetch.")
    ap.add_argument("--force", action="store_true", help="Ignore cache freshness.")
    args = ap.parse_args()

    if args.all:
        u = pd.read_parquet(DATA_DIR / "current_universe.parquet")
        tickers = u["ticker"].tolist()
    elif args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        ap.error("Pass --all or --tickers")

    print(f"Fetching {len(tickers)} tickers (will skip fresh cache rows)...")
    df = get_valuations(tickers, force=args.force, progress=True)
    n_ok = int(df["fetch_ok"].fillna(False).sum())
    print(f"Got {n_ok}/{len(df)} successful fetches.")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    _cli()

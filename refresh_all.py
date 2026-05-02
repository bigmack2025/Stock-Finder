"""
Daily refresh: rebuild universe, refresh valuations, prune stale EDGAR cache.
Designed to be invoked by a scheduler (cron, GitHub Actions, Modal, whatever).

Usage:
    python refresh_all.py                    # full refresh
    python refresh_all.py --skip-edgar       # skip the EDGAR cache prune
    python refresh_all.py --tickers KURA,IDYA --quiet
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HEALTH_FILE = DATA / "_refresh_health.json"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _step_universe() -> dict:
    _log("step 1/3: rebuilding universe from xlsx")
    import data_layer
    df = data_layer.load_universe()
    out = data_layer.save(df)
    return {"ok": True, "n_companies": len(df), "saved_to": str(out)}


def _step_valuations(tickers: list[str] | None = None) -> dict:
    _log("step 2/3: refreshing yfinance valuations cache")
    import valuations
    if tickers is None:
        import pandas as pd
        u = pd.read_parquet(DATA / "current_universe.parquet")
        tickers = u["ticker"].tolist()
    df = valuations.get_valuations(tickers, force=True, progress=False, sleep_s=0.3)
    n_ok = int(df["fetch_ok"].fillna(False).sum()) if "fetch_ok" in df.columns else 0
    return {"ok": True, "n_attempted": len(tickers), "n_ok": n_ok}


def _step_edgar_prune(max_age_days: int = 7) -> dict:
    _log(f"step 3/3: pruning EDGAR facts cache older than {max_age_days}d")
    hist_dir = DATA / "historical"
    if not hist_dir.exists():
        return {"ok": True, "n_pruned": 0}
    cutoff = time.time() - (max_age_days * 86400)
    pruned = 0
    for p in hist_dir.glob("facts_*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            pruned += 1
    return {"ok": True, "n_pruned": pruned}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-universe", action="store_true")
    ap.add_argument("--skip-valuations", action="store_true")
    ap.add_argument("--skip-edgar", action="store_true")
    ap.add_argument("--tickers", type=str, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.quiet:
        global _log
        _log = lambda m: None  # noqa: E731

    started = datetime.now(timezone.utc).isoformat()
    health = {"started_at": started, "steps": {}, "errors": []}
    try:
        if not args.skip_universe:
            health["steps"]["universe"] = _step_universe()
        if not args.skip_valuations:
            tk = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
            health["steps"]["valuations"] = _step_valuations(tk)
        if not args.skip_edgar:
            health["steps"]["edgar_prune"] = _step_edgar_prune()
    except Exception as e:
        health["errors"].append(f"{type(e).__name__}: {e}")
        _log(f"ERROR: {e}")
    health["finished_at"] = datetime.now(timezone.utc).isoformat()
    health["ok"] = not health["errors"]

    import json
    HEALTH_FILE.write_text(json.dumps(health, indent=2))
    _log(f"done; health written to {HEALTH_FILE}")
    sys.exit(0 if health["ok"] else 1)


if __name__ == "__main__":
    main()

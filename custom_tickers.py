"""
Custom ticker additions — "Request a ticker" feature.

When a user adds a ticker via the sidebar UI:
  1. We validate via yfinance (must return a market cap > 0)
  2. We pull basic metadata (name, sector, industry)
  3. We persist to data/custom_tickers.json (a global file, all users see additions)
  4. The next universe rebuild folds the addition in via data_layer

Streamlit Cloud's filesystem is ephemeral — additions persist within a container's
lifetime but are lost on container restart. To make them durable we'd commit
the json to git (separate concern, not yet wired). For "me + friends" v1, the
ephemeral behavior is acceptable; users can always re-add a ticker.

Public API:
    validate_ticker(symbol) -> dict | None     # yfinance probe + sanity checks
    add_ticker(symbol, requested_by) -> dict   # validates + persists + returns record
    list_custom_tickers() -> list[dict]
    remove_custom_ticker(symbol) -> None
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CUSTOM_PATH = ROOT / "data" / "custom_tickers.json"


def _load() -> list[dict]:
    if not CUSTOM_PATH.exists():
        return []
    try:
        return json.loads(CUSTOM_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_PATH.write_text(json.dumps(items, indent=2))


def list_custom_tickers() -> list[dict]:
    return _load()


def remove_custom_ticker(symbol: str) -> None:
    items = _load()
    sym = symbol.upper().strip()
    items = [it for it in items if it.get("ticker", "").upper() != sym]
    _save(items)


def validate_ticker(symbol: str) -> dict | None:
    """Probe yfinance — confirm the ticker is real and has financial data.
    Returns a dict with name, mkt_cap_m, revenue_m, region, industry on success.
    Returns None if invalid (no data, delisted, etc.)."""
    if not symbol or len(symbol.strip()) > 10:
        return None
    sym = symbol.strip().upper()
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
    except Exception:
        return None
    mkt_cap = info.get("marketCap")
    if not mkt_cap or mkt_cap <= 0:
        return None

    # Map yfinance country to our region buckets
    country = (info.get("country") or "").strip()
    if country in {"United States"}:
        region = "US"
    elif country in {
        "United Kingdom", "Switzerland", "Germany", "France", "Denmark",
        "Spain", "Ireland", "Netherlands", "Belgium", "Sweden", "Norway",
        "Italy", "Finland", "Austria", "Portugal", "Iceland",
    }:
        region = "Europe"
    elif country:
        region = country  # Keep the literal country name for non-US/EU
    else:
        region = "US"  # default

    industry = info.get("industry") or info.get("sector") or "Drug Manufacturer"
    is_pharma_like = any(
        kw in (industry or "").lower()
        for kw in ["biotech", "pharm", "drug", "therap"]
    )

    return {
        "ticker": sym,
        "name": info.get("longName") or info.get("shortName") or sym,
        "mkt_cap_m": mkt_cap / 1e6,
        "revenue_m": (info.get("totalRevenue") or 0) / 1e6,
        "region": region,
        "country": country,
        "industry": industry,
        "stage_raw": "Has revenue" if (info.get("totalRevenue") or 0) > 0 else "Pre-revenue",
        "subsector_raw": "General Biotech",
        "is_pharma_like": is_pharma_like,
    }


def add_ticker(symbol: str, requested_by: str = "anon") -> dict:
    """Validate + persist. Returns a result dict with status."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "Empty ticker"}

    items = _load()
    if any(it.get("ticker") == sym for it in items):
        return {"ok": False, "error": f"{sym} already in custom list"}

    info = validate_ticker(sym)
    if info is None:
        return {"ok": False, "error": f"{sym} not found on yfinance (or no market cap data). Check spelling."}

    if not info.get("is_pharma_like"):
        # Don't reject — just warn. User may know better than the industry tag.
        info["non_pharma_warning"] = (
            f"yfinance lists this as '{info['industry']}', which doesn't look like "
            f"a drug-maker. Adding anyway, but the engine works best on biotech/pharma."
        )

    record = {
        **info,
        "requested_by": requested_by,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    items.append(record)
    _save(items)
    return {"ok": True, "record": record}

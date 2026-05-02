"""
Similarity engine — VECTORIZED edition.

Role under the new architecture:
    Similarity is *not* the product. It defines the **peer set** that the
    mispricing engine then ranks for cheapness. So the contract is:

      peers(north_star_ticker, n=30) -> list of similar tickers

    The legacy `rank()` interface remains for diagnostics and the screener UI.

Council reco fixes applied:
  - Vectorized with numpy (P2)
  - Universe loaded once, cached (P2)
  - Tests live in tests/ (P1)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from data_layer import MODALITIES

ROOT = Path(__file__).parent
UNIVERSE_PATH = ROOT / "data" / "current_universe.parquet"


# ---------------------------------------------------------------------------
# Frozen-state mutation — apply a historical snapshot to a north-star row
# ---------------------------------------------------------------------------

def _freeze_row_to_year(ns_row: pd.Series, year: int) -> tuple[pd.Series, dict | None]:
    """Replace the north-star's financial fields with values from year T (FY-end)."""
    import math
    from historical import get_snapshot
    snap = get_snapshot(ns_row["ticker"], year)
    return _apply_snapshot(ns_row, snap)


def _freeze_row_to_date(ns_row: pd.Series, query_date: str) -> tuple[pd.Series, dict | None]:
    """Replace the north-star's financial fields with values as of an exact date.
    Uses the most recent 10-Q/10-K publicly known on `query_date` for fundamentals,
    and yfinance close on that date for market cap.
    """
    from historical import get_snapshot_at_date
    snap = get_snapshot_at_date(ns_row["ticker"], query_date)
    return _apply_snapshot(ns_row, snap)


def _apply_snapshot(ns_row: pd.Series, snap: dict | None) -> tuple[pd.Series, dict | None]:
    """Shared logic — fold a snapshot dict into a north-star row, leaving
    modality fields untouched (look-ahead caveat applies)."""
    import math
    if not snap or not snap.get("available"):
        return ns_row, snap
    out = ns_row.copy()
    out["mkt_cap_m"] = snap.get("mkt_cap_m")
    out["revenue_m"] = snap.get("revenue_m") or 0
    out["log_mkt_cap"] = (
        math.log10(out["mkt_cap_m"]) if out["mkt_cap_m"] and out["mkt_cap_m"] > 0 else None
    )
    out["log_revenue"] = math.log10(max(out["revenue_m"], 0) + 1)
    out["has_revenue"] = int((out["revenue_m"] or 0) > 0)
    out["size_band"] = snap.get("size_band", "unknown")
    return out, snap


@dataclass
class Weights:
    """Per-feature weights. Need not sum to 1 — normalized internally."""
    log_mkt_cap: float = 0.30
    has_revenue: float = 0.10
    log_revenue: float = 0.10
    primary_modality: float = 0.40
    compound_modality: float = 0.10

    def as_array(self) -> np.ndarray:
        return np.array([
            self.log_mkt_cap,
            self.has_revenue,
            self.log_revenue,
            self.primary_modality,
            self.compound_modality,
        ], dtype=float)


@lru_cache(maxsize=1)
def load_universe() -> pd.DataFrame:
    return pd.read_parquet(UNIVERSE_PATH)


# ---------------------------------------------------------------------------
# Vectorized similarity
# ---------------------------------------------------------------------------

def _similarity_matrix(north_star_idx: int, df: pd.DataFrame) -> np.ndarray:
    """Return [N x 5] matrix of per-feature similarities to north star.
    Columns: log_mkt_cap, has_revenue, log_revenue, modality_jaccard, compound_match.
    """
    n = len(df)
    sims = np.zeros((n, 5), dtype=float)

    ns = df.iloc[north_star_idx]

    # 1. Log mkt cap — Gaussian kernel, sigma=0.5
    a = df["log_mkt_cap"].to_numpy(dtype=float)
    b = float(ns["log_mkt_cap"]) if pd.notna(ns["log_mkt_cap"]) else np.nan
    if np.isnan(b):
        sims[:, 0] = 0.0
    else:
        diff = np.where(np.isnan(a), np.inf, a - b)
        sims[:, 0] = np.exp(-(diff ** 2) / (2 * 0.5 ** 2))

    # 2. Has-revenue match — binary
    sims[:, 1] = (df["has_revenue"].to_numpy() == int(ns["has_revenue"])).astype(float)

    # 3. Log revenue — Gaussian, sigma=0.7
    ar = df["log_revenue"].to_numpy(dtype=float)
    br = float(ns["log_revenue"])
    sims[:, 2] = np.exp(-((ar - br) ** 2) / (2 * 0.7 ** 2))

    # 4. Modality Jaccard — uses `combined_modalities` (M6 wire-up).
    # That column contains rich tags from 10-K extraction merged with xlsx
    # fallback for the ~33% of names without rich extraction yet, so every
    # ticker has non-empty Jaccard input.
    def _to_set(x) -> set:
        if x is None:
            return set()
        try:
            return set(x) if len(x) > 0 else set()
        except TypeError:
            return set()
    mod_col = "combined_modalities" if "combined_modalities" in df.columns else "modalities"
    ns_mods_set = _to_set(df.iloc[north_star_idx][mod_col])
    def _jaccard_to_ns(other):
        s = _to_set(other)
        if not ns_mods_set or not s:
            return 0.0
        return len(ns_mods_set & s) / len(ns_mods_set | s)
    sims[:, 3] = df[mod_col].apply(_jaccard_to_ns).to_numpy(dtype=float)

    # 5. Compound-modality flag match (1 if same, 0.5 otherwise)
    cm = df["is_compound_modality"].to_numpy()
    ns_cm = int(ns["is_compound_modality"])
    sims[:, 4] = np.where(cm == ns_cm, 1.0, 0.5)

    return sims


def rank(
    north_star_ticker: str,
    weights: Weights | None = None,
    top_n: int = 20,
    universe: pd.DataFrame | None = None,
    exclude_megas: bool = True,
    min_mkt_cap_m: float | None = None,
    max_mkt_cap_m: float | None = None,
    same_region_only: bool = False,
    year: int | None = None,
    date: str | None = None,
) -> pd.DataFrame:
    """Return top_n similarity look-alikes for the north-star ticker.

    Time-machine modes (mutually exclusive — `date` takes priority over `year`):
      year=2018           → snap to FY2018 year-end
      date='2019-06-14'   → exact-day precision, uses most recent 10-Q + that
                            day's market cap

    Modality / sub-sector tags always come from today's xlsx (look-ahead caveat).
    Candidates remain in CURRENT state.
    """
    weights = weights or Weights()
    df = universe if universe is not None else load_universe()

    matches = df.index[df["ticker"].str.upper() == north_star_ticker.upper()].tolist()
    if not matches:
        raise ValueError(f"Ticker {north_star_ticker} not in universe")
    ns_idx = matches[0]
    ns = df.iloc[ns_idx]

    snapshot_used = None
    if date is not None:
        ns_frozen, snapshot_used = _freeze_row_to_date(ns, date)
    elif year is not None:
        ns_frozen, snapshot_used = _freeze_row_to_year(ns, year)
    else:
        ns_frozen = None

    if ns_frozen is not None and snapshot_used and snapshot_used.get("available"):
        df = df.copy()
        for col in ("mkt_cap_m", "revenue_m", "log_mkt_cap", "log_revenue", "has_revenue", "size_band"):
            df.at[ns_idx, col] = ns_frozen[col]
        ns = df.iloc[ns_idx]

    sims = _similarity_matrix(ns_idx, df)
    w = weights.as_array()
    w = w / (np.abs(w).sum() or 1.0)
    scores = sims @ w

    # Build ranking frame
    out = df.copy()
    out["score"] = scores
    out["sim_mkt_cap"] = sims[:, 0]
    out["sim_has_revenue"] = sims[:, 1]
    out["sim_revenue"] = sims[:, 2]
    out["sim_modality"] = sims[:, 3]
    out["sim_compound"] = sims[:, 4]

    # Filter the candidate pool
    out = out.loc[out["ticker"] != ns["ticker"]]
    if exclude_megas and ns["size_band"] != "mega":
        out = out.loc[out["size_band"] != "mega"]
    if min_mkt_cap_m is not None:
        out = out.loc[out["mkt_cap_m"] >= min_mkt_cap_m]
    if max_mkt_cap_m is not None:
        out = out.loc[out["mkt_cap_m"] <= max_mkt_cap_m]
    if same_region_only:
        out = out.loc[out["region"] == ns["region"]]

    out = out.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    keep = [
        "rank", "ticker", "name", "region", "mkt_cap_m", "revenue_m",
        "primary_modality", "size_band", "score",
        "sim_mkt_cap", "sim_revenue", "sim_has_revenue", "sim_modality", "sim_compound",
    ]
    return out[keep]


def peers(
    north_star_ticker: str,
    n: int = 30,
    weights: Weights | None = None,
    same_region_only: bool = False,
    year: int | None = None,
    date: str | None = None,
) -> list[str]:
    """Return the top-N peer tickers (used by mispricing.anchor_screen)."""
    df = rank(
        north_star_ticker,
        weights=weights,
        top_n=n,
        exclude_megas=True,
        same_region_only=same_region_only,
        year=year,
        date=date,
    )
    return df["ticker"].tolist()


def get_north_star_state(north_star_ticker: str, year: int | None = None) -> dict:
    """Return the north-star's effective state — used by the UI to render the
    'frozen state' card. If year is None, returns today's state from the xlsx.
    If year is set, returns the historical snapshot if available (else falls back).
    """
    u = load_universe()
    row = u.loc[u["ticker"].str.upper() == north_star_ticker.upper()].iloc[0]
    out = {
        "ticker": row["ticker"],
        "name": row["name"],
        "year": "today",
        "mkt_cap_m": row["mkt_cap_m"],
        "revenue_m": row["revenue_m"],
        "has_revenue": int(row["has_revenue"]),
        "size_band": row["size_band"],
        "primary_modality": row["primary_modality"],
        "modalities": row["modalities"],
        "available": True,
        "source": "xlsx (current)",
    }
    if year is not None:
        from historical import get_snapshot
        snap = get_snapshot(north_star_ticker, year)
        out["year"] = year
        if snap and snap.get("available"):
            out["mkt_cap_m"] = snap.get("mkt_cap_m")
            out["revenue_m"] = snap.get("revenue_m") or 0
            out["has_revenue"] = int(snap.get("has_revenue", 0))
            out["size_band"] = snap.get("size_band", "unknown")
            out["cash_m"] = snap.get("cash_m")
            out["debt_m"] = snap.get("debt_m")
            out["rd_m"] = snap.get("rd_m")
            out["source"] = f"SEC EDGAR XBRL ({year} 10-K)"
        else:
            out["available"] = False
            out["unavailable_reason"] = (snap or {}).get("reason", "Snapshot unavailable")
    return out


def get_north_star_state_at_date(north_star_ticker: str, query_date: str) -> dict:
    """Like get_north_star_state but for an exact date (YYYY-MM-DD)."""
    from historical import get_snapshot_at_date
    u = load_universe()
    row = u.loc[u["ticker"].str.upper() == north_star_ticker.upper()].iloc[0]
    snap = get_snapshot_at_date(north_star_ticker, query_date)
    out = {
        "ticker": row["ticker"],
        "name": row["name"],
        "query_date": query_date,
        "primary_modality": row["primary_modality"],
        "modalities": row["modalities"],
    }
    if snap and snap.get("available"):
        out.update({
            "available": True,
            "mkt_cap_m": snap.get("mkt_cap_m"),
            "revenue_m": snap.get("revenue_m") or 0,
            "cash_m": snap.get("cash_m"),
            "debt_m": snap.get("debt_m"),
            "rd_m": snap.get("rd_m"),
            "shares_outstanding": snap.get("shares_outstanding"),
            "operating_cash_flow": snap.get("operating_cash_flow"),
            "has_revenue": int(snap.get("has_revenue", 0)),
            "size_band": snap.get("size_band", "unknown"),
            "source": f"SEC EDGAR {snap.get('balance_sheet_form')} (balance sheet as of {snap.get('balance_sheet_as_of')}, filed {snap.get('balance_sheet_filed')}) + yfinance close",
        })
    else:
        out["available"] = False
        out["unavailable_reason"] = (snap or {}).get("reason", "Snapshot unavailable")
    return out


def explain(north_star_ticker: str, candidate_ticker: str, weights: Weights | None = None) -> str:
    weights = weights or Weights()
    u = load_universe()
    ns = u.loc[u["ticker"].str.upper() == north_star_ticker.upper()].iloc[0]
    c = u.loc[u["ticker"].str.upper() == candidate_ticker.upper()].iloc[0]
    # Compute pair similarity using the matrix (cheap)
    df = pd.DataFrame([ns, c]).reset_index(drop=True)
    sims = _similarity_matrix(0, df)[1]
    return "\n".join([
        f"NORTH STAR: {ns['ticker']}  {ns['name']}",
        f"           mkt cap ${(ns['mkt_cap_m'] or 0):,.0f}M | rev ${(ns['revenue_m'] or 0):,.0f}M | modality {ns['primary_modality']}",
        "",
        f"CANDIDATE: {c['ticker']}  {c['name']}",
        f"           mkt cap ${(c['mkt_cap_m'] or 0):,.0f}M | rev ${(c['revenue_m'] or 0):,.0f}M | modality {c['primary_modality']}",
        "",
        f"Mkt-cap similarity:   {sims[0]:.2f}",
        f"Modality (Jaccard):   {sims[3]:.2f}  ({ns['modalities']} vs {c['modalities']})",
        f"Same revenue stage:   {'yes' if sims[1] else 'no'}",
    ])


if __name__ == "__main__":
    print("=== VRTX peers ===")
    print(rank("VRTX", top_n=10).to_string(index=False))
    print("\n=== ALNY peers ===")
    print(rank("ALNY", top_n=10).to_string(index=False))

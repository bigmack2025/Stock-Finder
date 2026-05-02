"""
Mispricing engine — finds biotechs whose financial shape looks "off" relative
to a peer set. Two entry points:

    anchor_screen(north_star_ticker, n_peers=30, top_n=15)
        Use the similarity engine to define the peer set around a north star,
        then rank that peer set by cheapness signals (peer-relative).

    free_screen(modality=None, region='US', size_band=None, top_n=25)
        Pure cheapness ranking across a filter slice — no anchor required.

Cheapness signals (higher = cheaper):
  - net_cash_to_mc:       (cash - debt) / mkt_cap  ; >1 means below net-cash floor
  - inv_ev_cash:          1 / (EV / cash)          ; high when EV is small vs cash
  - peer_log_ev_resid:    -log10(EV / peer_median_EV)   ; negative residual = cheap
  - peer_log_ev_cash_resid: same idea on EV/cash multiple
  - runway_months:        cash / abs(monthly burn)  ; long runway de-risks the position

Each signal is z-scored within the *candidate pool*, then weighted-summed.
Final score is rescaled to 0-100 (100 = cheapest in pool).

THIS IS NOT INVESTMENT ADVICE. The signals are crude and ignore pipeline value,
catalyst risk, and a hundred other things real analysts care about. Use as a
funnel, not a buy list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import engine
import valuations

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

@dataclass
class CheapnessWeights:
    net_cash_to_mc: float = 0.30      # mkt cap below net cash → strong signal
    inv_ev_cash: float = 0.20         # EV small relative to cash
    peer_log_ev_resid: float = 0.20   # cheap relative to peer-set EV
    peer_log_ev_cash_resid: float = 0.20  # cheap on EV/Cash relative to peers
    runway_months: float = 0.10       # de-risking factor (capped)

    def as_dict(self) -> dict[str, float]:
        return {
            "net_cash_to_mc": self.net_cash_to_mc,
            "inv_ev_cash": self.inv_ev_cash,
            "peer_log_ev_resid": self.peer_log_ev_resid,
            "peer_log_ev_cash_resid": self.peer_log_ev_cash_resid,
            "runway_months": self.runway_months,
        }


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def _safe_div(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    out = np.full_like(a, np.nan, dtype=float)
    ok = (b > 0) & np.isfinite(a) & np.isfinite(b)
    out[ok] = a[ok] / b[ok]
    return out


def _zscore(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    finite = np.isfinite(x)
    if finite.sum() < 2:
        return np.zeros_like(x)
    mu = np.nanmean(x[finite])
    sd = np.nanstd(x[finite])
    if sd == 0:
        return np.zeros_like(x)
    z = np.where(finite, (x - mu) / sd, 0.0)
    # Clip extreme outliers so one weird name doesn't dominate
    return np.clip(z, -3.0, 3.0)


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    df must have columns: ticker, mkt_cap_m_yf, cash_m, debt_m, ev_m,
                          operatingCashflow (annual, $).
    Returns df with added signal columns + raw inputs.
    """
    out = df.copy()
    mc = out["mkt_cap_m_yf"].to_numpy(dtype=float)
    cash = out["cash_m"].to_numpy(dtype=float)
    debt = out["debt_m"].fillna(0).to_numpy(dtype=float)
    ev = out["ev_m"].to_numpy(dtype=float)
    ocf_annual = out.get("operatingCashflow", pd.Series([np.nan] * len(out))).to_numpy(dtype=float)

    out["net_cash_m"] = cash - debt
    out["net_cash_to_mc"] = _safe_div(out["net_cash_m"].to_numpy(), mc)
    out["ev_cash_ratio"] = _safe_div(ev, cash)
    out["inv_ev_cash"] = _safe_div(np.ones_like(ev), out["ev_cash_ratio"].to_numpy())

    # Peer-relative residuals — uses MEDIAN of the present pool
    log_ev = np.where((ev > 0) & np.isfinite(ev), np.log10(ev), np.nan)
    log_evcash = np.where(
        (out["ev_cash_ratio"].to_numpy() > 0) & np.isfinite(out["ev_cash_ratio"].to_numpy()),
        np.log10(out["ev_cash_ratio"].to_numpy()),
        np.nan,
    )
    med_log_ev = np.nanmedian(log_ev) if np.isfinite(log_ev).any() else np.nan
    med_log_evcash = np.nanmedian(log_evcash) if np.isfinite(log_evcash).any() else np.nan
    # Negative residual = cheap. Flip sign so larger = cheaper.
    out["peer_log_ev_resid"] = -(log_ev - med_log_ev)
    out["peer_log_ev_cash_resid"] = -(log_evcash - med_log_evcash)

    # Runway months — only meaningful when burning cash
    monthly_burn_m = np.where(ocf_annual < 0, -ocf_annual / 12.0 / 1e6, np.nan)
    runway = _safe_div(cash, monthly_burn_m)
    # Cap at 60 months — beyond that the marginal de-risking is diminishing
    out["runway_months"] = np.minimum(runway, 60.0)

    return out


def compute_score(df: pd.DataFrame, weights: CheapnessWeights | None = None) -> pd.DataFrame:
    """Z-score each signal across the pool, weighted-sum, rescale to 0-100."""
    weights = weights or CheapnessWeights()
    out = compute_signals(df)

    sig_cols = [
        "net_cash_to_mc",
        "inv_ev_cash",
        "peer_log_ev_resid",
        "peer_log_ev_cash_resid",
        "runway_months",
    ]
    Z = np.column_stack([_zscore(out[c].to_numpy()) for c in sig_cols])
    w = np.array([weights.as_dict()[c] for c in sig_cols], dtype=float)
    w = w / (np.abs(w).sum() or 1.0)
    raw = Z @ w

    # Track which rows had ANY non-trivial input. If a name has no valuations
    # data at all, score should be NaN, not 0.
    any_input = np.isfinite(out[["mkt_cap_m_yf", "cash_m", "ev_m"]].to_numpy()).any(axis=1)
    raw = np.where(any_input, raw, np.nan)

    # 0-100 percentile (pool-relative). NaNs stay NaN.
    finite = np.isfinite(raw)
    pct = np.full_like(raw, np.nan, dtype=float)
    if finite.sum() > 1:
        ranks = pd.Series(raw[finite]).rank(pct=True).to_numpy() * 100
        pct[finite] = ranks
    out["cheapness_score"] = pct
    out["cheapness_raw"] = raw
    for c, z in zip(sig_cols, Z.T):
        out[f"z_{c}"] = z

    return out


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def anchor_screen(
    north_star_ticker: str,
    n_peers: int = 30,
    top_n: int = 15,
    weights: CheapnessWeights | None = None,
    same_region_only: bool = False,
    fetch: bool = True,
    year: int | None = None,
    date: str | None = None,
) -> pd.DataFrame:
    """Define peer set via similarity to the north star, then rank by cheapness.

    Time-machine modes (mutually exclusive — `date` takes priority over `year`):
      year=2018           → snap to FY2018 year-end
      date='2019-06-14'   → exact-day precision (10-Q + that day's market cap)

    Candidates remain current.
    """
    universe = engine.load_universe()

    peer_tickers = engine.peers(
        north_star_ticker,
        n=n_peers,
        same_region_only=same_region_only,
        year=year,
        date=date,
    )
    peer_tickers = [north_star_ticker.upper()] + peer_tickers  # include the anchor for context

    if fetch:
        valuations.get_valuations(peer_tickers, progress=False)

    enriched = valuations.annotate_universe(
        universe.loc[universe["ticker"].isin(peer_tickers)].copy()
    )
    if enriched.empty:
        raise RuntimeError("Peer set has no valuation data — run with fetch=True.")

    # Need operatingCashflow which annotate_universe doesn't merge — pull from cache directly
    cache = pd.read_parquet(valuations.CACHE_PATH)
    enriched = enriched.merge(
        cache[["ticker", "operatingCashflow"]],
        on="ticker",
        how="left",
    )

    scored = compute_score(enriched, weights=weights)
    scored = scored.sort_values("cheapness_score", ascending=False, na_position="last")

    # Mark the anchor explicitly
    scored.insert(0, "is_anchor", scored["ticker"].str.upper() == north_star_ticker.upper())

    keep = [
        "is_anchor", "ticker", "name", "region", "primary_modality", "size_band",
        "mkt_cap_m_yf", "cash_m", "debt_m", "ev_m",
        "net_cash_to_mc", "ev_cash_ratio", "runway_months",
        "peer_log_ev_resid", "peer_log_ev_cash_resid",
        "cheapness_score",
    ]
    return scored[keep].reset_index(drop=True).head(top_n + 1)  # +1 for the anchor


def free_screen(
    modality: str | None = None,
    region: str | None = None,
    size_band: str | None = None,
    top_n: int = 25,
    weights: CheapnessWeights | None = None,
    fetch: bool = False,
) -> pd.DataFrame:
    """Cheapness ranking across a filtered slice. Doesn't auto-fetch by default —
    relies on cache populated by `python valuations.py --all`."""
    universe = engine.load_universe()
    pool = universe.copy()
    if modality:
        pool = pool.loc[pool[f"mod_{modality}"] == 1]
    if region:
        pool = pool.loc[pool["region"] == region]
    if size_band:
        pool = pool.loc[pool["size_band"] == size_band]

    if fetch:
        valuations.get_valuations(pool["ticker"].tolist(), progress=True)

    enriched = valuations.annotate_universe(pool)
    cache = pd.read_parquet(valuations.CACHE_PATH) if valuations.CACHE_PATH.exists() else pd.DataFrame(columns=["ticker", "operatingCashflow"])
    if "operatingCashflow" not in enriched.columns:
        enriched = enriched.merge(
            cache[["ticker", "operatingCashflow"]] if "operatingCashflow" in cache.columns else pd.DataFrame(columns=["ticker", "operatingCashflow"]),
            on="ticker",
            how="left",
        )

    scored = compute_score(enriched, weights=weights)
    scored = scored.sort_values("cheapness_score", ascending=False, na_position="last")
    keep = [
        "ticker", "name", "region", "primary_modality", "size_band",
        "mkt_cap_m_yf", "cash_m", "ev_m",
        "net_cash_to_mc", "ev_cash_ratio", "runway_months",
        "cheapness_score",
    ]
    return scored[keep].reset_index(drop=True).head(top_n)


if __name__ == "__main__":
    print("=== Anchor screen: KURA (small/mid oncology) ===")
    df = anchor_screen("KURA", n_peers=20, top_n=10)
    print(df.to_string(index=False))

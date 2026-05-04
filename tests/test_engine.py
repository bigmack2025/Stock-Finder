"""
Golden tests pinning the three sanity-check archetypes from the council pass.

Run from the project root:
    python -m pytest tests/ -v

These tests are intentionally LOOSE — they pin shape and presence-in-top-N,
not exact ordering. Tighten once the engine is calibrated against hand-labeled
peer pairs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

# Make project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import engine
import mispricing


# ---------------------------------------------------------------------------
# Universe shape
# ---------------------------------------------------------------------------

def test_universe_loads():
    u = engine.load_universe()
    assert len(u) > 700, f"expected ~722 companies, got {len(u)}"
    assert "ticker" in u.columns
    assert "primary_modality" in u.columns
    assert "region" in u.columns


def test_universe_has_us_and_intl():
    u = engine.load_universe()
    n_us = (u["region"] == "US").sum()
    n_intl = (u["region"] != "US").sum()
    assert n_us > 500
    assert n_intl > 100, "international sheet must be folded in (council P1)"


def test_modalities_present():
    u = engine.load_universe()
    mods = set(u["primary_modality"].unique())
    # Must include the major buckets
    for required in ["Oncology", "RNA/Antisense", "Gene/Cell Therapy", "Vaccines"]:
        assert required in mods, f"missing modality {required}"


# ---------------------------------------------------------------------------
# Similarity engine — golden archetypes
# ---------------------------------------------------------------------------

def test_vrtx_peer_set_is_mega_cap_drug_makers():
    """Vertex (mega-cap, has revenue) → top peers should be mega-cap drug-makers
    (other large biotechs OR Big Pharma). With pharma loaded, expect names like
    BMY, GILD, REGN, AMGN, GSK, SNY, NVO, PFE."""
    df = engine.rank("VRTX", top_n=10)
    expected_peers = {"BMY", "GILD", "REGN", "AMGN", "GSK", "SNY", "NVO", "PFE", "ARGX", "MRK", "AZN", "LLY", "JNJ", "ABBV"}
    found = set(df["ticker"].values) & expected_peers
    assert len(found) >= 4, f"expected ≥4 mega-cap drug-maker peers in VRTX top-10, found {found}"


def test_alny_peer_set_is_rna_dominated():
    """Alnylam (RNAi platform) → top 5 should include genuine RNA-platform peers
    (IONS, ARWR, MRNA) — checked via rich tags from 10-K extraction since the
    xlsx tag was a coarse 'RNA/Antisense' bucket that lumps RNAi+ASO+mRNA together."""
    df = engine.rank("ALNY", top_n=5)
    u = engine.load_universe()
    rna_keywords = {"RNAi/siRNA", "Antisense oligonucleotide", "mRNA therapeutic", "RNA/Antisense"}
    rna_in_top = 0
    for tk in df["ticker"].tolist():
        row = u.loc[u["ticker"] == tk]
        if row.empty:
            continue
        rich_mods = list(row.iloc[0]["rich_modalities"]) if row.iloc[0]["rich_modalities"] is not None else []
        primary = row.iloc[0]["primary_modality"]
        if any(k in rna_keywords for k in rich_mods) or primary == "RNA/Antisense":
            rna_in_top += 1
    assert rna_in_top >= 2, f"expected ≥2 RNA-platform peers in ALNY top-5, got {rna_in_top}"


def test_kura_peer_set_is_oncology_dominated():
    """Kura (small-mol oncology) → top 5 should share Oncology TA when checked
    against the M6 rich tags from 10-K extraction (not the xlsx primary_modality
    which is coarse for many of KURA's actual peers)."""
    df = engine.rank("KURA", top_n=5)
    u = engine.load_universe()
    n_onc = 0
    for tk in df["ticker"].tolist():
        row = u.loc[u["ticker"] == tk]
        if row.empty:
            continue
        # parquet returns numpy arrays; convert to plain lists
        rich_mods = list(row.iloc[0]["rich_modalities"]) if row.iloc[0]["rich_modalities"] is not None else []
        rich_tas = list(row.iloc[0]["rich_therapeutic_areas"]) if row.iloc[0]["rich_therapeutic_areas"] is not None else []
        primary = row.iloc[0]["primary_modality"]
        if "Oncology" in rich_tas or "Oncology" in rich_mods or primary == "Oncology":
            n_onc += 1
    assert n_onc >= 3, f"expected ≥3 oncology-tagged peers in KURA top-5, got {n_onc}"
    # Mega-cap exclusion should kick in
    assert (df["size_band"] == "mega").sum() == 0


def test_peers_returns_list():
    p = engine.peers("KURA", n=10)
    assert isinstance(p, list)
    assert len(p) == 10
    assert all(isinstance(t, str) for t in p)


# ---------------------------------------------------------------------------
# Mispricing — signal sanity
# ---------------------------------------------------------------------------

def _fake_pool() -> pd.DataFrame:
    """Hand-built tiny pool to test signal arithmetic without yfinance."""
    return pd.DataFrame([
        # Sub-cash deep-value: $100M mkt cap, $200M cash, $0 debt, $-50M EV
        {"ticker": "DEEP", "name": "Deep Value Bio", "region": "US",
         "primary_modality": "Oncology", "size_band": "micro",
         "mkt_cap_m_yf": 100.0, "cash_m": 200.0, "debt_m": 0.0, "ev_m": -50.0,
         "operatingCashflow": -36_000_000.0},
        # Average: $1B mkt cap, $200M cash, EV $800M
        {"ticker": "MID1", "name": "Average Bio", "region": "US",
         "primary_modality": "Oncology", "size_band": "small",
         "mkt_cap_m_yf": 1000.0, "cash_m": 200.0, "debt_m": 50.0, "ev_m": 800.0,
         "operatingCashflow": -120_000_000.0},
        # Expensive: $5B mkt cap, $100M cash, EV $4.9B
        {"ticker": "RICH", "name": "Rich Bio", "region": "US",
         "primary_modality": "Oncology", "size_band": "mid",
         "mkt_cap_m_yf": 5000.0, "cash_m": 100.0, "debt_m": 50.0, "ev_m": 4900.0,
         "operatingCashflow": -300_000_000.0},
    ])


def test_signals_sub_cash_signal_is_positive_for_deep_value():
    df = _fake_pool()
    out = mispricing.compute_signals(df)
    # DEEP has cash > mkt_cap → net_cash_to_mc > 1
    deep_row = out.loc[out["ticker"] == "DEEP"].iloc[0]
    assert deep_row["net_cash_to_mc"] > 1.0, "DEEP should have net cash > mkt cap"
    # And EV/cash should be negative since EV is negative
    assert deep_row["ev_cash_ratio"] < 0


def test_score_ranks_deep_value_first():
    df = _fake_pool()
    scored = mispricing.compute_score(df)
    sorted_ = scored.sort_values("cheapness_score", ascending=False)
    assert sorted_.iloc[0]["ticker"] == "DEEP", \
        f"DEEP should rank cheapest, got {sorted_.iloc[0]['ticker']}"
    assert sorted_.iloc[-1]["ticker"] == "RICH", \
        f"RICH should rank most expensive, got {sorted_.iloc[-1]['ticker']}"


def test_score_handles_missing_data():
    """Companies with no valuation data should get NaN score, not 0."""
    df = pd.DataFrame([
        {"ticker": "A", "name": "Has data", "region": "US",
         "primary_modality": "Oncology", "size_band": "small",
         "mkt_cap_m_yf": 500.0, "cash_m": 100.0, "debt_m": 0.0, "ev_m": 400.0,
         "operatingCashflow": -50_000_000.0},
        {"ticker": "B", "name": "No data", "region": "US",
         "primary_modality": "Oncology", "size_band": "small",
         "mkt_cap_m_yf": np.nan, "cash_m": np.nan, "debt_m": np.nan, "ev_m": np.nan,
         "operatingCashflow": np.nan},
    ])
    scored = mispricing.compute_score(df)
    b_score = scored.loc[scored["ticker"] == "B", "cheapness_score"].iloc[0]
    assert pd.isna(b_score), "no-data companies should have NaN score"


def test_zscore_clips_outliers():
    """A 100-sigma outlier shouldn't dominate the score."""
    x = np.array([0.0, 0.0, 0.0, 0.0, 1000.0])
    z = mispricing._zscore(x)
    assert z.max() <= 3.0
    assert z.min() >= -3.0


# ---------------------------------------------------------------------------
# Anchor screen end-to-end (uses live yfinance cache populated earlier)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "data" / "valuations.parquet").exists(),
    reason="needs valuations cache from `python valuations.py --tickers VRTX,KURA,...`",
)
def test_anchor_screen_kura_returns_oncology_cluster():
    """KURA's anchor screen should produce small-cap names; anchor itself should
    rank cheaply within its peer set."""
    df = mispricing.anchor_screen("KURA", n_peers=20, top_n=10)
    # All peers (including anchor) should be small-cap or below
    n_small = (df["size_band"].isin(["micro", "small", "mid"])).sum()
    assert n_small >= 7, f"expected mostly small-cap peers, got size dist: {df['size_band'].value_counts().to_dict()}"
    # Anchor should rank cheaply in its own peer set
    anchor_rank = df.index[df["is_anchor"]].tolist()[0]
    assert anchor_rank <= 4, \
        f"KURA should rank in its own top-5 by cheapness; ranked {anchor_rank}"


# ---------------------------------------------------------------------------
# Year picker — historical state
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "data" / "historical" / "facts_VRTX.json").exists(),
    reason="needs EDGAR facts cache; run `python historical.py` once",
)
def test_vrtx_year_state_changes():
    """VRTX-2010 state must materially differ from VRTX-today state."""
    today = engine.get_north_star_state("VRTX", year=None)
    y2010 = engine.get_north_star_state("VRTX", year=2010)
    assert y2010["available"], y2010
    # 2010 mkt cap should be far smaller than today's
    assert y2010["mkt_cap_m"] < today["mkt_cap_m"] / 5, \
        f"VRTX-2010 mkt cap should be << today's; got {y2010['mkt_cap_m']} vs {today['mkt_cap_m']}"
    # 2010 was pre-Kalydeco — size should not be mega
    assert y2010["size_band"] != "mega"


@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "data" / "historical" / "facts_VRTX.json").exists(),
    reason="needs EDGAR facts cache",
)
def test_vrtx_year_picker_changes_peer_set():
    """VRTX-today peers should NOT be the same as VRTX-2010 peers."""
    peers_today = set(engine.peers("VRTX", n=10))
    peers_2010 = set(engine.peers("VRTX", n=10, year=2010))
    overlap = peers_today & peers_2010
    # Some overlap is fine (modality is sticky), but the sets must differ materially
    assert len(peers_today - peers_2010) >= 5, \
        f"year picker barely changed VRTX's peer set (overlap={overlap})"


@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "data" / "historical" / "facts_VRTX.json").exists(),
    reason="needs EDGAR facts cache",
)
def test_vrtx_2010_peers_are_smaller():
    """VRTX-2010 was a $7B mid-cap; its peers today should NOT be megacaps."""
    df = engine.rank("VRTX", year=2010, top_n=10)
    n_mega = (df["size_band"] == "mega").sum()
    assert n_mega == 0, f"VRTX-2010 (mid-cap) should not match megacaps; got {n_mega}"
    n_mid = (df["size_band"] == "mid").sum()
    assert n_mid >= 5, f"expected ≥5 mid-cap peers, got {n_mid}"


def test_year_picker_handles_unavailable_gracefully():
    """A year before XBRL coverage should return a snapshot with available=False."""
    from historical import get_snapshot
    snap = get_snapshot("VRTX", 2005)
    assert snap is not None
    assert snap.get("available") is False
    assert "reason" in snap

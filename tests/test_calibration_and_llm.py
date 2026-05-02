"""
Tests for the M6/M7/M8 additions:
  - calibration: pair sampling, label storage, grid search, weights apply
  - pipeline_extractor LLM mode: prompt construction, JSON parsing, fallback
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# calibration: pair sampling
# ---------------------------------------------------------------------------

def test_calibration_sample_pairs_returns_three_buckets():
    import calibration
    pairs = calibration.sample_pairs(n=30, seed=42)
    assert len(pairs) == 30
    buckets = {p["bucket"] for p in pairs}
    # All three sampling strategies should produce results
    assert "obvious_yes" in buckets
    assert "obvious_no" in buckets
    assert "borderline" in buckets


def test_calibration_pairs_have_valid_tickers():
    import calibration, engine
    pairs = calibration.sample_pairs(n=10, seed=42)
    u = engine.load_universe()
    valid_tickers = set(u["ticker"])
    for p in pairs:
        assert p["anchor"] in valid_tickers
        assert p["candidate"] in valid_tickers
        assert p["anchor"] != p["candidate"]


# ---------------------------------------------------------------------------
# calibration: label storage
# ---------------------------------------------------------------------------

def test_calibration_save_and_load_labels(monkeypatch, tmp_path):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    calibration.save_label("alice", "KURA", "AKTS", "peer", bucket="obvious_yes")
    calibration.save_label("alice", "KURA", "MRNA", "not_peer", bucket="obvious_no")
    calibration.save_label("alice", "VRTX", "REGN", "skip")
    labels = calibration.load_labels("alice")
    assert len(labels) == 3
    assert sum(1 for l in labels if l["label"] == "peer") == 1
    assert sum(1 for l in labels if l["label"] == "not_peer") == 1
    assert sum(1 for l in labels if l["label"] == "skip") == 1


def test_calibration_label_overwrite(monkeypatch, tmp_path):
    """Re-grading the same pair should overwrite, not append."""
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    calibration.save_label("alice", "KURA", "AKTS", "peer")
    calibration.save_label("alice", "KURA", "AKTS", "not_peer")
    labels = calibration.load_labels("alice")
    assert len(labels) == 1
    assert labels[0]["label"] == "not_peer"


def test_calibration_username_isolation(monkeypatch, tmp_path):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    calibration.save_label("alice", "KURA", "AKTS", "peer")
    calibration.save_label("bob", "VRTX", "REGN", "peer")
    assert len(calibration.load_labels("alice")) == 1
    assert len(calibration.load_labels("bob")) == 1
    assert calibration.load_labels("alice")[0]["anchor"] == "KURA"
    assert calibration.load_labels("bob")[0]["anchor"] == "VRTX"


# ---------------------------------------------------------------------------
# calibration: grid search
# ---------------------------------------------------------------------------

def test_calibration_grid_search_too_few_labels(monkeypatch, tmp_path):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    calibration.save_label("alice", "KURA", "AKTS", "peer")
    res = calibration.run_grid_search("alice", granularity=4, persist=False)
    assert res["ok"] is False
    assert "Need" in res["reason"]


def test_calibration_apply_returns_none_when_no_calibration(monkeypatch, tmp_path):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    assert calibration.apply_calibrated_weights() is None


def test_calibration_apply_returns_weights_when_calibrated(monkeypatch, tmp_path):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    # Stub a calibrated_weights.json
    payload = {
        "ok": True,
        "best_weights": [0.5, 0.1, 0.1, 0.2, 0.1],
        "best_auc": 0.85,
    }
    (tmp_path / "calibrated_weights.json").write_text(json.dumps(payload))
    w = calibration.apply_calibrated_weights()
    assert w is not None
    assert w.log_mkt_cap == 0.5
    assert w.primary_modality == 0.2


# ---------------------------------------------------------------------------
# pipeline_extractor: LLM mode
# ---------------------------------------------------------------------------

def test_llm_available_false_without_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import pipeline_extractor as pe
    assert pe._llm_available() is False


def test_llm_available_true_with_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    import pipeline_extractor as pe
    assert pe._llm_available() is True


def test_llm_parse_json_clean():
    import pipeline_extractor as pe
    raw = '{"modalities":["mAb"],"therapeutic_areas":["Oncology"],"lead_assets":[],"lead_stage":"Phase 2"}'
    out = pe._parse_llm_json(raw)
    assert out is not None
    assert out["modalities"] == ["mAb"]
    assert out["lead_stage"] == "Phase 2"


def test_llm_parse_json_with_fences():
    import pipeline_extractor as pe
    raw = '```json\n{"modalities":["ADC"],"therapeutic_areas":["Onc"]}\n```'
    out = pe._parse_llm_json(raw)
    assert out is not None
    assert out["modalities"] == ["ADC"]


def test_llm_parse_json_with_preamble():
    import pipeline_extractor as pe
    raw = 'Here is the extraction:\n{"modalities":["RNAi/siRNA"],"therapeutic_areas":[]}'
    out = pe._parse_llm_json(raw)
    assert out is not None
    assert out["modalities"] == ["RNAi/siRNA"]


def test_llm_parse_json_garbage_returns_none():
    import pipeline_extractor as pe
    assert pe._parse_llm_json("not json at all") is None
    assert pe._parse_llm_json("") is None


def test_llm_extract_returns_none_without_api(monkeypatch):
    """LLM call should silently return None if no key is set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import pipeline_extractor as pe
    assert pe._llm_extract("some 10-K text", "VRTX") is None


# ---------------------------------------------------------------------------
# Universe-wide M6 verification
# ---------------------------------------------------------------------------

def test_universe_has_combined_modalities_column():
    import engine
    u = engine.load_universe()
    assert "combined_modalities" in u.columns
    # Most US tickers should have non-empty combined modalities now (post-M6 bulk extract)
    us = u.loc[u["region"] == "US"]
    n_with = sum(1 for x in us["combined_modalities"] if x is not None and len(list(x)) > 0)
    assert n_with > 400, f"expected most US tickers to have combined modalities, got {n_with}/{len(us)}"


def test_modality_source_distribution():
    import engine
    u = engine.load_universe()
    sources = u["modality_source"].value_counts().to_dict()
    # M6 bulk extract should produce a meaningful number of "lexicon" entries
    assert sources.get("lexicon", 0) > 200, f"expected bulk-extract coverage, got {sources}"

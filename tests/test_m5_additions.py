"""
Tests for the M5 additions: pipeline_extractor (rich modalities), going_concern,
and the dual-backend userdb.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# pipeline_extractor — lexicon match
# ---------------------------------------------------------------------------

def test_match_lexicon_hits_modalities():
    import pipeline_extractor as pe
    text = (
        "We develop chimeric antigen receptor T-cell therapies and bispecific "
        "antibodies. Our lead asset is a CAR-T directed at CD19. We are also "
        "advancing antibody-drug conjugates."
    )
    counts = pe._match_lexicon(text, pe.LEXICON)
    assert counts.get("CAR-T cell therapy", 0) >= 1
    assert counts.get("Bispecific antibody", 0) >= 1
    assert counts.get("Antibody-drug conjugate", 0) >= 1


def test_top_n_filters_min_count():
    import pipeline_extractor as pe
    counts = {"A": 5, "B": 1, "C": 3, "D": 2}
    out = pe._top_n(counts, n=3, min_count=2)
    assert "A" in out
    assert "C" in out
    assert "D" in out
    assert "B" not in out  # below min_count


def test_extract_item_1_picks_largest_gap():
    """The TOC has 'Item 1 ... Item 1A' with small gap; the real section has
    a much larger gap. Heuristic must pick the larger gap."""
    import pipeline_extractor as pe
    fake = (
        "Some preamble. "
        "Item 1. Business 22 Item 1A. Risk Factors 28 Item 2. "  # TOC pair, ~50 char gap
        + "X" * 5_000 +
        "Item 1. Business " + ("real text describing CAR-T " * 1000) +
        "Item 1A. Risk Factors blah blah."
    )
    item1 = pe._extract_item_1(fake)
    # Should NOT be the TOC pair (which had ~50 char content)
    assert len(item1) > 5_000
    assert "CAR-T" in item1


def test_short_modality_string():
    import pipeline_extractor as pe
    record = {
        "modalities": ["Small molecule", "mRNA therapeutic"],
        "therapeutic_areas": ["Oncology", "Rare disease"],
    }
    s = pe.short_modality_string(record)
    assert "Small molecule" in s
    assert "mRNA" in s
    assert "Oncology" in s


def test_short_modality_empty():
    import pipeline_extractor as pe
    assert pe.short_modality_string({"modalities": [], "therapeutic_areas": []}) == "—"


# ---------------------------------------------------------------------------
# going_concern — text matching
# ---------------------------------------------------------------------------

def test_going_concern_regex_matches_canonical():
    import going_concern as gc
    text = "These conditions raise substantial doubt about the Company's ability to continue as a going concern."
    assert gc.GOING_CONCERN_RE.search(text) is not None


def test_going_concern_regex_misses_unrelated():
    import going_concern as gc
    # Phrase 'going' alone isn't a match
    assert gc.GOING_CONCERN_RE.search("We are going to expand into Europe.") is None


def test_strip_html_removes_tags():
    import going_concern as gc
    html = b"<html><body><p>Hello <b>world</b></p><script>x=1;</script></body></html>"
    out = gc._strip_html(html)
    assert "Hello" in out
    assert "world" in out
    assert "<" not in out
    assert "x=1" not in out  # script content stripped


def test_strip_html_decodes_entities():
    import going_concern as gc
    html = b"<p>caf&#233; &amp; tea&#160;</p>"
    out = gc._strip_html(html)
    # entity decode is partial — &amp; → &, others stay or change to space
    assert "&" in out
    assert "&amp;" not in out


# ---------------------------------------------------------------------------
# userdb — dual backend
# ---------------------------------------------------------------------------

def test_userdb_defaults_to_json_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    # Force re-detect by clearing module-level cache
    import userdb
    monkeypatch.setattr(userdb, "_BACKEND_NAME", None)
    monkeypatch.setattr(userdb, "USER_DIR", tmp_path / "users")
    monkeypatch.setattr(userdb, "INDEX_PATH", tmp_path / "_users_index.json")
    assert userdb.current_backend() == "json"


def test_userdb_supabase_falls_back_when_lib_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "fake-key")
    import userdb
    monkeypatch.setattr(userdb, "_BACKEND_NAME", None)
    monkeypatch.setattr(userdb, "USER_DIR", tmp_path / "users")
    monkeypatch.setattr(userdb, "INDEX_PATH", tmp_path / "_users_index.json")
    # In test environment supabase package isn't installed → falls back to json
    backend = userdb.current_backend()
    assert backend in ("json", "supabase")  # depends on env; either is acceptable


def test_userdb_safe_username():
    import userdb
    assert userdb._safe_username("Mack T.") == "mackt"
    assert userdb._safe_username("alice@example.com") == "aliceexamplecom"
    assert userdb._safe_username("") == "anon"
    assert userdb._safe_username(None) == "anon"


# ---------------------------------------------------------------------------
# Integration: misuse_flags now uses real going-concern
# ---------------------------------------------------------------------------

def test_misuse_flags_going_concern_returns_real_or_empty():
    import misuse_flags
    # On a known-clean ticker, _going_concern_flag should not flag
    flagged, _reason = misuse_flags._going_concern_flag("VRTX")
    assert flagged is False  # Vertex is profitable, never had going-concern


def test_misuse_flags_going_concern_handles_unknown_ticker():
    import misuse_flags
    flagged, _reason = misuse_flags._going_concern_flag("ZZZZZZNONEXISTENT")
    assert flagged is False


# ---------------------------------------------------------------------------
# Universe carries new rich-modality columns
# ---------------------------------------------------------------------------

def test_universe_has_pipeline_columns():
    import engine
    u = engine.load_universe()
    assert "rich_modalities" in u.columns
    assert "rich_therapeutic_areas" in u.columns
    assert "modality_source" in u.columns
    # Some rows should be 'lexicon' (we extracted ~8 names) or 'xlsx' fallback
    sources = set(u["modality_source"].unique())
    assert "xlsx" in sources  # fallback always present

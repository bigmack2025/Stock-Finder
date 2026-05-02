"""
Tests for the council-additions modules: userdb, misuse_flags, backtest harness,
auth gate, daily refresh script. Targets ~50% line coverage on these new modules.
"""

from __future__ import annotations

import json
import sys
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# userdb
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_userdb(monkeypatch, tmp_path):
    """Redirect userdb storage to a tmp dir for isolated tests."""
    import userdb
    # Patch the paths
    monkeypatch.setattr(userdb, "USER_DIR", tmp_path / "users")
    monkeypatch.setattr(userdb, "INDEX_PATH", tmp_path / "_users_index.json")
    userdb.ensure_schema()
    return userdb


def test_userdb_add_and_list(fresh_userdb):
    u = fresh_userdb
    u.add_user("alice")
    u.add_watchlist("alice", "KURA", name="Kura Oncology", source="anchor:VRTX@2010", note="Ph2 readout")
    u.add_watchlist("alice", "IDYA", name="Ideaya", source="anchor:VRTX@2010")
    wl = u.list_watchlist("alice")
    assert len(wl) == 2
    assert {r["ticker"] for r in wl} == {"KURA", "IDYA"}
    # Most recent first
    assert wl[0]["ticker"] == "IDYA"


def test_userdb_remove(fresh_userdb):
    u = fresh_userdb
    u.add_watchlist("bob", "KURA", name="Kura Oncology")
    u.remove_watchlist("bob", "KURA")
    assert u.list_watchlist("bob") == []


def test_userdb_notes_roundtrip(fresh_userdb):
    u = fresh_userdb
    u.set_note("alice", "KURA", "Ph2 readout June 2026")
    assert u.get_note("alice", "KURA") == "Ph2 readout June 2026"
    # Per-user isolation
    assert u.get_note("bob", "KURA") is None


def test_userdb_username_isolation(fresh_userdb):
    u = fresh_userdb
    u.add_watchlist("alice", "KURA", name="Kura")
    u.add_watchlist("bob", "IDYA", name="Ideaya")
    a = {r["ticker"] for r in u.list_watchlist("alice")}
    b = {r["ticker"] for r in u.list_watchlist("bob")}
    assert a == {"KURA"}
    assert b == {"IDYA"}


def test_userdb_username_normalization(fresh_userdb):
    u = fresh_userdb
    u.add_user("Mack T.")
    u.add_user("mack-t")
    # Both normalize to alphanumeric+_- — different by hyphenation, but special chars stripped
    u.add_watchlist("Mack T.", "KURA")
    wl = u.list_watchlist("Mack T.")
    assert len(wl) == 1


def test_userdb_upsert_watchlist(fresh_userdb):
    u = fresh_userdb
    u.add_watchlist("alice", "KURA", note="first note")
    u.add_watchlist("alice", "KURA", note="updated note")
    wl = u.list_watchlist("alice")
    assert len(wl) == 1
    assert wl[0]["note"] == "updated note"


def test_check_group_password(monkeypatch, fresh_userdb):
    u = fresh_userdb
    monkeypatch.delenv("GROUP_PASSWORD", raising=False)
    # With explicit expected
    assert u.check_group_password("hello", expected="hello")
    assert not u.check_group_password("nope", expected="hello")
    # Empty submitted always fails
    assert not u.check_group_password("", expected="hello")


def test_check_group_password_via_env(monkeypatch, fresh_userdb):
    u = fresh_userdb
    monkeypatch.setenv("GROUP_PASSWORD", "fromenv")
    assert u.check_group_password("fromenv")
    assert not u.check_group_password("default")


# ---------------------------------------------------------------------------
# misuse_flags
# ---------------------------------------------------------------------------

def test_misuse_flags_subten():
    import misuse_flags
    # mkt_cap < $10M should flag
    f = misuse_flags.compute_flags("VRTX", mkt_cap_m=5.0)
    assert f["sub_ten_mkt_cap"] is True
    assert f["any_warning"] is True


def test_misuse_flags_normal_company():
    import misuse_flags
    # VRTX at any normal mkt cap → no sub-$10M flag
    f = misuse_flags.compute_flags("VRTX", mkt_cap_m=100_000.0)
    assert not f["sub_ten_mkt_cap"]


def test_misuse_short_flag_string():
    import misuse_flags
    row = {
        "fresh_ipo": True, "going_concern": False,
        "reverse_merger_shell": True, "sub_ten_mkt_cap": False,
        "near_term_catalyst": True,
    }
    s = misuse_flags.short_flag_string(row)
    assert "🆕" in s
    assert "🐚" in s
    assert "📅" in s
    assert "🛑" not in s
    assert "⚠️" not in s


def test_misuse_short_flag_string_empty():
    import misuse_flags
    s = misuse_flags.short_flag_string({})
    assert s == ""


# ---------------------------------------------------------------------------
# backtest harness
# ---------------------------------------------------------------------------

def test_backtest_aggregate():
    import backtest
    df = pd.DataFrame([
        {"ticker": "A", "bucket": 1.0, "ret_1y": 1.0, "ret_3y": 2.0},
        {"ticker": "B", "bucket": 1.0, "ret_1y": -0.2, "ret_3y": 0.5},
        {"ticker": "C", "bucket": 5.0, "ret_1y": 0.05, "ret_3y": 0.1},
    ])
    summary = backtest._aggregate(df, [1, 3])
    assert "mean_1y" in summary.columns
    assert "median_1y" in summary.columns
    assert "hit_rate_1y" in summary.columns
    assert len(summary) == 2  # buckets 1 and 5


def test_backtest_bucket_quintile():
    import backtest
    # Build a df with 25 distinct cheapness scores
    df = pd.DataFrame({
        "ticker": [f"T{i}" for i in range(25)],
        "cheapness_score": [float(i) for i in range(25)],
    })
    out = backtest._bucket_quintile(df, n_buckets=5)
    assert out["bucket"].notna().all()
    assert set(out["bucket"].unique()) == {1, 2, 3, 4, 5}
    # Highest cheapness should be in bucket 1 (cheapest)
    top = out.loc[out["cheapness_score"] == 24.0].iloc[0]
    assert top["bucket"] == 1


# ---------------------------------------------------------------------------
# Smoke — refresh_all imports
# ---------------------------------------------------------------------------

def test_refresh_all_imports():
    import refresh_all
    assert hasattr(refresh_all, "main")
    assert hasattr(refresh_all, "_step_universe")
    assert hasattr(refresh_all, "_step_valuations")
    assert hasattr(refresh_all, "_step_edgar_prune")

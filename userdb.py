"""
User storage — dual backend.

Auto-detects Supabase env vars (SUPABASE_URL + SUPABASE_ANON_KEY). When set
AND the `supabase` package is importable, persistence goes to Postgres via
Supabase. Otherwise falls back to JSON files on local disk.

Why both:
  - JSON works locally and in any environment without setup. Good for the
    initial Streamlit Cloud deploy where you don't want to fuss with secrets
    on day one.
  - Supabase gives durability across container restarts, which Streamlit Cloud
    cycles every ~10 min of idle. Once anyone in the friend group complains
    about a lost watchlist, set the two env vars in Streamlit secrets and the
    backend swaps with zero code changes.

Activation (15 min, see DEPLOY.md):
  1. Create a free Supabase project
  2. Run the schema SQL from DEPLOY.md
  3. Add SUPABASE_URL + SUPABASE_ANON_KEY to Streamlit secrets

Public API (unchanged across backends):
  ensure_schema()
  add_user(username) -> str
  get_user_id(username) -> str | None
  list_watchlist(username) -> list[dict]
  add_watchlist(username, ticker, name, source, note)
  remove_watchlist(username, ticker)
  set_note(username, ticker, note)
  get_note(username, ticker) -> str | None
  check_group_password(submitted, expected=None) -> bool
  current_backend() -> str   # "supabase" | "json"
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
USER_DIR = ROOT / "data" / "users"
INDEX_PATH = ROOT / "data" / "_users_index.json"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_username(u: str) -> str:
    return "".join(c for c in (u or "").strip().lower() if c.isalnum() or c in "_-")[:40] or "anon"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_BACKEND_NAME: str | None = None
_SUPABASE_CLIENT = None


def _detect_backend() -> str:
    global _BACKEND_NAME, _SUPABASE_CLIENT
    if _BACKEND_NAME is not None:
        return _BACKEND_NAME
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        try:
            from supabase import create_client  # type: ignore
            _SUPABASE_CLIENT = create_client(url, key)
            _BACKEND_NAME = "supabase"
            return _BACKEND_NAME
        except Exception:
            pass
    _BACKEND_NAME = "json"
    return _BACKEND_NAME


def current_backend() -> str:
    return _detect_backend()


# ---------------------------------------------------------------------------
# JSON backend
# ---------------------------------------------------------------------------

def _user_path(username: str) -> Path:
    return USER_DIR / f"{_safe_username(username)}.json"


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write(path: Path, payload) -> None:
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)


def _json_load(username: str) -> dict:
    return _read(_user_path(username), {"username": _safe_username(username), "created_at": _now(), "watchlist": [], "notes": {}})


def _json_save(username: str, doc: dict) -> None:
    _write(_user_path(username), doc)


def _json_ensure_schema() -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)


def _json_add_user(username: str) -> str:
    _json_ensure_schema()
    canon = _safe_username(username)
    if not _user_path(canon).exists():
        _json_save(canon, {"username": canon, "created_at": _now(), "watchlist": [], "notes": {}})
    idx = _read(INDEX_PATH, [])
    if canon not in idx:
        idx.append(canon)
        _write(INDEX_PATH, idx)
    return canon


def _json_get_user_id(username: str) -> str | None:
    if not username:
        return None
    return _safe_username(username) if _user_path(username).exists() else None


def _json_list_watchlist(username: str) -> list[dict]:
    if not username:
        return []
    doc = _json_load(username)
    items = doc.get("watchlist", []) or []
    return sorted(items, key=lambda r: r.get("added_at", ""), reverse=True)


def _json_add_watchlist(username: str, ticker: str, name: str = "", source: str = "", note: str = "") -> None:
    _json_add_user(username)
    doc = _json_load(username)
    wl = doc.get("watchlist", []) or []
    tk = (ticker or "").upper()
    wl = [r for r in wl if r.get("ticker") != tk]
    wl.append({"ticker": tk, "name": name, "source": source, "note": note, "added_at": _now()})
    doc["watchlist"] = wl
    _json_save(username, doc)


def _json_remove_watchlist(username: str, ticker: str) -> None:
    if not username:
        return
    doc = _json_load(username)
    wl = doc.get("watchlist", []) or []
    tk = (ticker or "").upper()
    doc["watchlist"] = [r for r in wl if r.get("ticker") != tk]
    _json_save(username, doc)


def _json_set_note(username: str, ticker: str, note: str) -> None:
    _json_add_user(username)
    doc = _json_load(username)
    notes = doc.get("notes", {}) or {}
    notes[ticker.upper()] = {"note": note, "updated_at": _now()}
    doc["notes"] = notes
    _json_save(username, doc)


def _json_get_note(username: str, ticker: str) -> str | None:
    if not username:
        return None
    doc = _json_load(username)
    entry = (doc.get("notes") or {}).get(ticker.upper())
    return entry.get("note") if entry else None


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------

def _sb():
    return _SUPABASE_CLIENT  # set in _detect_backend


def _sb_ensure_schema() -> None:
    """Schema must be created out-of-band via the Supabase SQL editor —
    `create_client` doesn't execute DDL through the anon key. The DEPLOY.md
    appendix has the SQL. This function is a no-op for parity with JSON.
    """
    return


def _sb_add_user(username: str) -> str:
    canon = _safe_username(username)
    sb = _sb()
    if sb is None:
        return canon
    try:
        sb.table("users").upsert({"username": canon}, on_conflict="username").execute()
    except Exception:
        pass
    return canon


def _sb_get_user_id(username: str) -> str | None:
    if not username:
        return None
    sb = _sb()
    if sb is None:
        return None
    canon = _safe_username(username)
    try:
        res = sb.table("users").select("username").eq("username", canon).limit(1).execute()
        if res.data:
            return canon
    except Exception:
        pass
    return None


def _sb_list_watchlist(username: str) -> list[dict]:
    if not username:
        return []
    sb = _sb()
    if sb is None:
        return []
    canon = _safe_username(username)
    try:
        res = sb.table("watchlist").select("*").eq("username", canon).order("added_at", desc=True).execute()
        return res.data or []
    except Exception:
        return []


def _sb_add_watchlist(username: str, ticker: str, name: str = "", source: str = "", note: str = "") -> None:
    sb = _sb()
    if sb is None:
        return
    _sb_add_user(username)
    canon = _safe_username(username)
    payload = {"username": canon, "ticker": ticker.upper(), "name": name, "source": source, "note": note, "added_at": _now()}
    try:
        sb.table("watchlist").upsert(payload, on_conflict="username,ticker").execute()
    except Exception:
        pass


def _sb_remove_watchlist(username: str, ticker: str) -> None:
    sb = _sb()
    if sb is None:
        return
    canon = _safe_username(username)
    try:
        sb.table("watchlist").delete().eq("username", canon).eq("ticker", ticker.upper()).execute()
    except Exception:
        pass


def _sb_set_note(username: str, ticker: str, note: str) -> None:
    sb = _sb()
    if sb is None:
        return
    _sb_add_user(username)
    canon = _safe_username(username)
    payload = {"username": canon, "ticker": ticker.upper(), "note": note, "updated_at": _now()}
    try:
        sb.table("notes").upsert(payload, on_conflict="username,ticker").execute()
    except Exception:
        pass


def _sb_get_note(username: str, ticker: str) -> str | None:
    if not username:
        return None
    sb = _sb()
    if sb is None:
        return None
    canon = _safe_username(username)
    try:
        res = sb.table("notes").select("note").eq("username", canon).eq("ticker", ticker.upper()).limit(1).execute()
        if res.data:
            return res.data[0].get("note")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API — dispatches based on detected backend
# ---------------------------------------------------------------------------

def ensure_schema() -> None:
    if _detect_backend() == "supabase":
        _sb_ensure_schema()
    else:
        _json_ensure_schema()


def add_user(username: str) -> str:
    return _sb_add_user(username) if _detect_backend() == "supabase" else _json_add_user(username)


def get_user_id(username: str) -> str | None:
    return _sb_get_user_id(username) if _detect_backend() == "supabase" else _json_get_user_id(username)


def list_watchlist(username: str) -> list[dict]:
    return _sb_list_watchlist(username) if _detect_backend() == "supabase" else _json_list_watchlist(username)


def add_watchlist(username: str, ticker: str, name: str = "", source: str = "", note: str = "") -> None:
    if _detect_backend() == "supabase":
        _sb_add_watchlist(username, ticker, name, source, note)
    else:
        _json_add_watchlist(username, ticker, name, source, note)


def remove_watchlist(username: str, ticker: str) -> None:
    if _detect_backend() == "supabase":
        _sb_remove_watchlist(username, ticker)
    else:
        _json_remove_watchlist(username, ticker)


def set_note(username: str, ticker: str, note: str) -> None:
    if _detect_backend() == "supabase":
        _sb_set_note(username, ticker, note)
    else:
        _json_set_note(username, ticker, note)


def get_note(username: str, ticker: str) -> str | None:
    return _sb_get_note(username, ticker) if _detect_backend() == "supabase" else _json_get_note(username, ticker)


# ---------------------------------------------------------------------------
# Friend-group auth — shared password gate
# ---------------------------------------------------------------------------

DEFAULT_GROUP_PASSWORD = "change-me-before-sharing"


def check_group_password(submitted: str, expected: str | None = None) -> bool:
    """Shared password for the whole friend group. Set via GROUP_PASSWORD env
    var (or Streamlit secrets) — the DEFAULT is a placeholder."""
    expected = expected or os.environ.get("GROUP_PASSWORD") or DEFAULT_GROUP_PASSWORD
    return bool(submitted) and submitted == expected


def is_using_default_password() -> bool:
    """True if the deploy is still using the placeholder password.
    The app surfaces this as a banner so the user can't ship-and-forget."""
    expected = os.environ.get("GROUP_PASSWORD") or DEFAULT_GROUP_PASSWORD
    return expected == DEFAULT_GROUP_PASSWORD

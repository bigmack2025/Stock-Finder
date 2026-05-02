"""
Going-concern flag — fetches the latest 10-K from SEC EDGAR for a ticker and
searches for the auditor's "going concern" / "substantial doubt" language.

This is the M5 deliverable that lights up the placeholder in misuse_flags.

The phrase "substantial doubt" or "going concern" in a 10-K's auditor's report
indicates the auditor doesn't believe the company can survive 12+ months
without raising capital. It's a hard solvency flag — orthogonal to the
cheapness signal, since a company with low EV/cash might still be a going
concern (the cash is being burned faster than the auditor's comfort window).

Public API:
  check(ticker) -> {flagged: bool, evidence: str | None, filing_url: str | None,
                    filed: str | None, fetched_at: str}
  short_evidence(ticker) -> str  (one-line for UI)

Cache: data/going_concern/<TICKER>.json (TTL = 90 days)
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "data" / "going_concern"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

import os as _os
USER_AGENT = (
    f"Biotech-Mispricing-Engine/0.1 "
    f"({_os.environ.get('EDGAR_USER_AGENT_EMAIL', 'biotech-engine@example.com')})"
)
TTL_DAYS = 90  # 10-Ks are filed annually, so cache aggressively

# Regex catches the canonical PCAOB phrasing. We also catch sentences that
# pair "substantial doubt" with "ability to continue" — a common alternate.
GOING_CONCERN_RE = re.compile(
    r"(?:going\s+concern|substantial\s+doubt(?:[^.]{0,80})continue\s+as\s+a\s+going\s+concern|"
    r"substantial\s+doubt(?:[^.]{0,80})ability\s+to\s+continue)",
    re.IGNORECASE | re.DOTALL,
)

# Strip HTML tags. Not perfect but good enough for keyword search.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# HTTP plumbing — light rate limiting, polite UA
# ---------------------------------------------------------------------------

_last_request_at = 0.0


def _http_get(url: str, accept: str = "*/*") -> bytes | None:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < 0.15:
        time.sleep(0.15 - elapsed)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        _last_request_at = time.time()
        return data
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# EDGAR submissions → latest 10-K filing URL
# ---------------------------------------------------------------------------

def _latest_10k(cik: str) -> Optional[dict]:
    """Returns {accession, primary_doc, filed, doc_url} for the most recent 10-K, or None."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    raw = _http_get(url, accept="application/json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])
    for i, form in enumerate(forms):
        if form == "10-K":
            accession = accs[i]
            doc = docs[i]
            filed = dates[i]
            acc_clean = accession.replace("-", "")
            cik_int = int(cik)  # canonical form (no leading zeros) for path
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{doc}"
            return {"accession": accession, "primary_doc": doc, "filed": filed, "doc_url": doc_url}
    return None


# ---------------------------------------------------------------------------
# HTML → plain text (cheap)
# ---------------------------------------------------------------------------

def _strip_html(html_bytes: bytes) -> str:
    try:
        s = html_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Drop scripts/styles entirely — they pollute keyword searches
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.IGNORECASE | re.DOTALL)
    # Replace tags with spaces
    s = _TAG_RE.sub(" ", s)
    # Decode common HTML entities used in 10-Ks
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    s = s.replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"')
    # Normalize whitespace
    s = _WS_RE.sub(" ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.json"


def _read_cache(ticker: str) -> Optional[dict]:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age_days = (time.time() - p.stat().st_mtime) / 86400
    if age_days > TTL_DAYS:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_cache(ticker: str, payload: dict) -> None:
    _cache_path(ticker).write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(ticker: str, force: bool = False) -> dict:
    """Fetch the latest 10-K and check for going-concern language.

    Returns a dict; never raises. If we can't find the filing, returns
    {flagged: False, evidence: None, ...} with a status field.
    """
    if not force:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached

    out: dict = {
        "ticker": ticker.upper(),
        "flagged": False,
        "evidence": None,
        "filing_url": None,
        "filed": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }

    # Need the CIK — reuse historical's mapping
    import historical
    cik = historical.get_cik(ticker)
    if not cik:
        out["status"] = "no_cik"
        _write_cache(ticker, out)
        return out

    filing = _latest_10k(cik)
    if not filing:
        out["status"] = "no_10k"
        _write_cache(ticker, out)
        return out

    out["filed"] = filing["filed"]
    out["filing_url"] = filing["doc_url"]

    raw = _http_get(filing["doc_url"])
    if not raw:
        out["status"] = "fetch_failed"
        _write_cache(ticker, out)
        return out

    text = _strip_html(raw)
    if not text:
        out["status"] = "empty_text"
        _write_cache(ticker, out)
        return out

    m = GOING_CONCERN_RE.search(text)
    if m:
        # Capture surrounding context as evidence
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 300)
        evidence = text[start:end].strip()
        # Trim leading/trailing partial words
        evidence = "..." + evidence + "..." if len(evidence) > 100 else evidence
        out["flagged"] = True
        out["evidence"] = evidence

    _write_cache(ticker, out)
    return out


def short_evidence(ticker: str) -> str | None:
    """One-liner for UI: if flagged, return a 1-sentence-ish summary."""
    res = check(ticker)
    if not res.get("flagged"):
        return None
    ev = res.get("evidence") or ""
    # Truncate
    if len(ev) > 240:
        ev = ev[:240].rsplit(" ", 1)[0] + "..."
    return ev


if __name__ == "__main__":
    # Smoke test on a few names. Pick a known small-cap that's likely to
    # have had going-concern language at some point and a known healthy one.
    for tk in ["VRTX", "ALXO", "TPST", "SMMT", "CRDF"]:
        r = check(tk)
        flag = "🛑 FLAGGED" if r["flagged"] else "✓ clean"
        print(f"{tk:6}  {flag}  filed={r.get('filed')}  status={r['status']}")
        if r.get("evidence"):
            print(f"        evidence: {r['evidence'][:200]}...")

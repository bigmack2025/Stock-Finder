"""
Insider buying flag — pulls Form 4 filings from SEC EDGAR for a ticker, parses
the XML for non-derivative *open-market purchases*, and surfaces a flag when
insiders are buying at material size.

Why this matters: in micro/small-cap biotech, *cluster* insider buying (multiple
execs/directors buying near-simultaneously on the open market) is one of the
few signals with documented edge. It's not a guarantee — but a $0 EV/cash name
where the CEO and two directors just bought $500K of stock is qualitatively
different from one where insiders are quiet.

Form 4 XML structure (the only thing we care about):
  - <reportingOwner>: who reported (name + officer/director flags)
  - <nonDerivativeTable>/<nonDerivativeTransaction>: actual stock transactions
    - <transactionCode>: P = open-market purchase, S = open-market sale,
      A = grant, M = option exercise, F = tax withholding, etc.
    - <transactionAcquiredDisposedCode>: A = acquired (in), D = disposed (out)
    - <transactionShares> + <transactionPricePerShare> = $ value

We only care about transactionCode = P. That's the genuine "I'm putting my own
money in" signal — option exercises and grants are noise.

Thresholds for flagging (any one of):
  - 2+ unique insiders bought in last 90 days
  - Single insider bought >$250K in last 90 days
  - Aggregate insider purchases >$500K in last 90 days

Public API:
  check(ticker) -> dict with flagged, total_dollars, n_insiders, recent[],
                   filing_url, fetched_at, status
  short_evidence(ticker) -> str | None  (one-line for UI)

Cache: data/insider_buying/<TICKER>.json (TTL = 3 days — Form 4s land daily)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "data" / "insider_buying"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    f"Biotech-Mispricing-Engine/0.1 "
    f"({os.environ.get('EDGAR_USER_AGENT_EMAIL', 'biotech-engine@example.com')})"
)
TTL_DAYS = 3
LOOKBACK_DAYS = 90

# Thresholds — tunable. These are deliberately permissive for v1; we'd rather
# over-flag and let the user see the evidence than miss real buying.
THRESH_N_INSIDERS = 2
THRESH_SINGLE_DOLLARS = 250_000
THRESH_TOTAL_DOLLARS = 500_000

_last_request_at = 0.0


def _http_get(url: str, accept: str = "*/*") -> bytes | None:
    """Polite HTTP — SEC asks for ≤10 req/s with a UA that includes contact info."""
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
# Submissions API — list recent Form 4s
# ---------------------------------------------------------------------------

def _recent_form4s(cik: str, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Return [{accession, primary_doc, filed, doc_url}, ...] for Form 4s filed
    within the lookback window. The submissions API gives the most recent ~1000
    filings; we filter to form=='4' and date>=cutoff.
    """
    raw = _http_get(f"https://data.sec.gov/submissions/CIK{cik}.json", accept="application/json")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    cik_int = int(cik)
    out: list[dict] = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        filed = dates[i] if i < len(dates) else ""
        if filed and filed < cutoff:
            continue
        accession = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        if not accession or not doc:
            continue
        acc_clean = accession.replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{doc}"
        # The Form 4 XML has a predictable name pattern; if `primaryDocument` is
        # the .htm wrapper, the .xml is alongside it. We try the doc_url first;
        # if it's a .htm we'll resolve to .xml in _fetch_xml.
        out.append({
            "accession": accession,
            "primary_doc": doc,
            "filed": filed,
            "doc_url": doc_url,
            "acc_clean": acc_clean,
            "cik_int": cik_int,
        })
    return out


def _fetch_form4_xml(filing: dict) -> bytes | None:
    """Form 4s have an XML version next to the .htm. Try a few common names."""
    # If primary_doc is already XML, fetch it directly.
    if filing["primary_doc"].lower().endswith(".xml"):
        return _http_get(filing["doc_url"])

    # Otherwise — try the canonical filename pattern, then fall back to listing
    # the filing index.
    base = f"https://www.sec.gov/Archives/edgar/data/{filing['cik_int']}/{filing['acc_clean']}"
    # The most common XML filename is the same accession-number with .xml suffix
    candidates = [
        f"{base}/{filing['primary_doc'].rsplit('.', 1)[0]}.xml",
        f"{base}/wf-form4_{filing['acc_clean']}.xml",  # legacy filer agent pattern
        f"{base}/primary_doc.xml",                       # newer SEC filer agent
    ]
    for url in candidates:
        raw = _http_get(url)
        if raw and raw.lstrip().startswith(b"<?xml"):
            return raw

    # Last resort — fetch the filing index json and find any .xml entry
    idx = _http_get(f"{base}/index.json", accept="application/json")
    if not idx:
        return None
    try:
        items = json.loads(idx).get("directory", {}).get("item", [])
    except Exception:
        return None
    for it in items:
        name = it.get("name", "")
        if name.lower().endswith(".xml") and "form" not in name.lower().replace("form4", ""):
            # Skip the schema xml; pick any other
            raw = _http_get(f"{base}/{name}")
            if raw and raw.lstrip().startswith(b"<?xml"):
                return raw
    return None


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------

def _findtext(elem: ET.Element | None, path: str) -> str | None:
    if elem is None:
        return None
    found = elem.find(path)
    if found is None:
        return None
    return (found.text or "").strip() or None


def _parse_form4(xml_bytes: bytes) -> dict | None:
    """Extract reporter info + non-derivative purchase transactions.
    Returns None on parse failure. Returns {} (no purchases) for valid filings
    where the insider didn't buy anything (sells, grants, etc.).
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    # Reporting owner — name, officer/director flags
    owner_name: str | None = None
    owner_title: str | None = None
    owner_is_officer = False
    owner_is_director = False
    for ro in root.findall("reportingOwner"):
        owner_id = ro.find("reportingOwnerId")
        owner_name = _findtext(owner_id, "rptOwnerName") or owner_name
        rel = ro.find("reportingOwnerRelationship")
        if rel is not None:
            if (_findtext(rel, "isOfficer") or "0").strip() == "1":
                owner_is_officer = True
                owner_title = _findtext(rel, "officerTitle") or owner_title
            if (_findtext(rel, "isDirector") or "0").strip() == "1":
                owner_is_director = True

    purchases: list[dict] = []
    nd_table = root.find("nonDerivativeTable")
    if nd_table is not None:
        for tx in nd_table.findall("nonDerivativeTransaction"):
            coding = tx.find("transactionCoding")
            code = _findtext(coding, "transactionCode")
            # P = open-market purchase. Skip everything else (S sale, M option-exercise,
            # A grant, F tax-withhold, G gift, etc.)
            if code != "P":
                continue
            amounts = tx.find("transactionAmounts")
            if amounts is None:
                continue
            shares_t = amounts.find("transactionShares/value")
            price_t = amounts.find("transactionPricePerShare/value")
            ad_t = amounts.find("transactionAcquiredDisposedCode/value")
            if shares_t is None or price_t is None:
                continue
            try:
                shares = float((shares_t.text or "0").strip())
                price = float((price_t.text or "0").strip())
            except (TypeError, ValueError):
                continue
            ad = (ad_t.text or "").strip() if ad_t is not None else "A"
            # Defensive — purchase should always be Acquired
            if ad != "A":
                continue
            tx_date = _findtext(tx, "transactionDate/value") or ""
            dollars = shares * price
            if dollars <= 0:
                continue
            purchases.append({
                "date": tx_date,
                "shares": shares,
                "price": price,
                "dollars": dollars,
            })

    return {
        "owner_name": owner_name,
        "owner_title": owner_title,
        "owner_is_officer": owner_is_officer,
        "owner_is_director": owner_is_director,
        "purchases": purchases,
    }


# ---------------------------------------------------------------------------
# Aggregate + threshold check
# ---------------------------------------------------------------------------

def _aggregate(filings_parsed: list[dict]) -> dict:
    """Roll up parsed Form 4 results into aggregate stats."""
    insiders: dict[str, dict] = {}
    all_txs: list[dict] = []
    for fp in filings_parsed:
        if not fp or not fp.get("purchases"):
            continue
        owner = fp.get("owner_name") or "Unknown"
        title_parts = []
        if fp.get("owner_title"):
            title_parts.append(fp["owner_title"])
        if fp.get("owner_is_director") and "director" not in (fp.get("owner_title") or "").lower():
            title_parts.append("Director")
        title = ", ".join(title_parts) or ("Officer" if fp.get("owner_is_officer") else "Director" if fp.get("owner_is_director") else "Insider")
        d = insiders.setdefault(owner, {"name": owner, "title": title, "dollars": 0.0, "shares": 0.0, "n_tx": 0})
        for tx in fp["purchases"]:
            d["dollars"] += tx["dollars"]
            d["shares"] += tx["shares"]
            d["n_tx"] += 1
            all_txs.append({**tx, "owner": owner, "title": title})

    total_dollars = sum(v["dollars"] for v in insiders.values())
    n_insiders = len(insiders)
    max_single = max((v["dollars"] for v in insiders.values()), default=0.0)

    flagged = (
        n_insiders >= THRESH_N_INSIDERS
        or max_single >= THRESH_SINGLE_DOLLARS
        or total_dollars >= THRESH_TOTAL_DOLLARS
    )

    return {
        "flagged": flagged,
        "n_insiders": n_insiders,
        "total_dollars": total_dollars,
        "max_single_dollars": max_single,
        "insiders": sorted(insiders.values(), key=lambda x: -x["dollars"]),
        "transactions": sorted(all_txs, key=lambda x: x.get("date", ""), reverse=True),
    }


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
    try:
        _cache_path(ticker).write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(ticker: str, force: bool = False, lookback_days: int = LOOKBACK_DAYS, cached_only: bool = False) -> dict:
    """Pull recent Form 4s, parse purchases, return aggregate flag + evidence.

    `cached_only=True` returns the cached result (or an empty 'not yet computed'
    record) without hitting EDGAR — used by batch flag computation so the table
    render isn't blocked by cold-cache fetches. The Company Peek panel calls
    with cached_only=False to populate on demand.
    """
    if not force:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached
    if cached_only:
        return {
            "ticker": ticker.upper(),
            "flagged": False,
            "n_insiders": 0,
            "total_dollars": 0.0,
            "max_single_dollars": 0.0,
            "insiders": [],
            "transactions": [],
            "lookback_days": lookback_days,
            "fetched_at": None,
            "status": "cache_miss",
        }

    out: dict = {
        "ticker": ticker.upper(),
        "flagged": False,
        "n_insiders": 0,
        "total_dollars": 0.0,
        "max_single_dollars": 0.0,
        "insiders": [],
        "transactions": [],
        "lookback_days": lookback_days,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }

    import historical
    cik = historical.get_cik(ticker)
    if not cik:
        out["status"] = "no_cik"
        _write_cache(ticker, out)
        return out

    filings = _recent_form4s(cik, lookback_days=lookback_days)
    if not filings:
        out["status"] = "no_form4s"
        _write_cache(ticker, out)
        return out

    parsed: list[dict] = []
    for filing in filings:
        xml_bytes = _fetch_form4_xml(filing)
        if not xml_bytes:
            continue
        p = _parse_form4(xml_bytes)
        if p is not None:
            p["filed"] = filing["filed"]
            p["filing_url"] = filing["doc_url"]
            parsed.append(p)

    agg = _aggregate(parsed)
    out.update(agg)
    _write_cache(ticker, out)
    return out


def short_evidence(ticker: str, cached_only: bool = False) -> str | None:
    """One-line summary for UI: '3 insiders bought $1.4M in last 90d (CEO bought $850K)'"""
    res = check(ticker, cached_only=cached_only)
    if not res.get("flagged"):
        return None
    n = res["n_insiders"]
    total = res["total_dollars"]
    insiders = res.get("insiders") or []
    top = insiders[0] if insiders else None
    summary = f"{n} insider{'s' if n != 1 else ''} bought ${total/1e6:.2f}M in last {res['lookback_days']}d"
    if top and top.get("title"):
        summary += f" ({top['title']} bought ${top['dollars']/1e3:.0f}K)"
    return summary


if __name__ == "__main__":
    # Smoke test on a mix of names — at least one should have recent insider buys.
    for tk in ["VRTX", "KURA", "ALXO", "SMMT", "VOR", "CRDF"]:
        r = check(tk)
        flag = "💰 FLAGGED" if r["flagged"] else "  clean  "
        n = r["n_insiders"]
        d = r["total_dollars"]
        print(f"{tk:6}  {flag}  status={r['status']:14}  n_insiders={n}  total=${d/1e6:.2f}M")
        if r["flagged"]:
            print(f"        evidence: {short_evidence(tk)}")

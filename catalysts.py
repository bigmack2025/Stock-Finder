"""
Catalyst calendar — upcoming clinical-trial readouts from clinicaltrials.gov.

Why: a cheap stock with a Phase 3 readout in 6 weeks is a very different bet
from a cheap stock with nothing on the calendar. The cheapness signal can't
see catalysts; this module surfaces them so the user can size accordingly.

Data source: ClinicalTrials.gov v2 API (free, no auth needed).
  - We look up trials by sponsor name (mapped from ticker → company name)
  - Filter to Phase 2 / Phase 3 / Phase 2/3 / Phase 4
  - Filter to studies with a primary completion date in the next 18 months
  - Filter to active statuses (RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED-but-recent)

KNOWN LIMITATIONS:
  - Sponsor-name match is fuzzy. Companies like "Vertex" return both Vertex
    Pharmaceuticals and unrelated "Vertex Energy" / "Vertex Veterinary" hits.
    We disambiguate by looking for biopharma keywords in the OfficialTitle.
  - PrimaryCompletionDate is the *primary endpoint* date — not necessarily
    the readout date. In practice, top-line data lands within a few months
    of primary completion, so this is a useful proxy.
  - PDUFA / FDA action dates are NOT covered here. There's no clean public
    API for those; we'd need a paid feed (BioPharmaCatalyst, RTTNews) or a
    structured FDA scrape. Documented as future work.
  - Some big pharmas have 100+ trials; we cap to 5 most-near-term per ticker
    in the summary, but the full list is in the cached payload for inspection.

Public API:
  upcoming(ticker, company_name) -> dict with flagged, next_date, n_trials,
                                    trials[], summary
  short_summary(ticker, company_name) -> str | None  (one-line for UI)

Cache: data/catalysts/<TICKER>.json (TTL = 7 days; trial state changes slowly)
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "data" / "catalysts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Biotech-Mispricing-Engine/0.1 (catalyst-puller)"
TTL_DAYS = 7
LOOKAHEAD_MONTHS = 18

# Phases worth flagging. Phase 1 readouts rarely move stocks; we skip them.
INTERESTING_PHASES = {"PHASE2", "PHASE3", "PHASE2_PHASE3", "PHASE4"}

# Statuses where the trial is real and the completion date is meaningful.
INTERESTING_STATUSES = {
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
}

# Recently completed trials are *also* worth flagging — top-line data often
# lands 0-3 months after primary completion.
RECENTLY_COMPLETED_STATUSES = {"COMPLETED"}
RECENTLY_COMPLETED_WINDOW_DAYS = 90

# Biopharma-y keywords used to filter sponsor-name false-positives. If the
# sponsor name matches but NONE of these appear in any of the matched studies'
# titles + interventions, we treat the match as a non-pharma collision.
PHARMA_KEYWORDS = re.compile(
    r"\b(cancer|tumor|tumour|oncolog|leukemia|lymphoma|myeloma|sclerosis|"
    r"alzheimer|parkinson|diabetes|hepatitis|HIV|virus|inflammat|asthma|"
    r"COPD|psoriasis|rheumatoid|arthritis|disease|disorder|syndrome|carcinoma|"
    r"trial|study|placebo|efficacy|safety|pharmacokin|antibody|inhibitor|"
    r"vaccine|gene therap|cell therap|monoclon|biolog|kinase|receptor)\b",
    re.IGNORECASE,
)

# Tokens we strip from a company name before sponsor lookup. "Vertex
# Pharmaceuticals Inc" → "Vertex Pharmaceuticals".
SPONSOR_NORMALIZE_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|ltd|limited|holdings?|company|co|plc|sa|nv|ag|gmbh|kg|kgaa|sas|asa)\.?\b",
    re.IGNORECASE,
)

# Aliases for tickers whose CT.gov registered name differs from yfinance's
# longName in a way our normalizer can't bridge. Add here as we encounter them.
# Format: TICKER → list of additional sponsor query strings to try.
TICKER_SPONSOR_ALIASES: dict[str, list[str]] = {
    "MRNA": ["ModernaTX"],
    "BNTX": ["BioNTech SE"],
    "RHHBY": ["Hoffmann-La Roche", "Genentech"],
    "BAYRY": ["Bayer"],
    "SNY": ["Sanofi-Aventis"],
    "AZN": ["AstraZeneca"],
    "NVS": ["Novartis Pharmaceuticals"],
    "GSK": ["GlaxoSmithKline"],
    "JNJ": ["Janssen Research", "Janssen Pharmaceuticals"],
    "ABBV": ["AbbVie"],
    "BMY": ["Bristol-Myers Squibb"],
    "MRK": ["Merck Sharp & Dohme"],
    "LLY": ["Eli Lilly"],
}

_last_request_at = 0.0


def _http_get_json(url: str) -> dict | None:
    global _last_request_at
    elapsed = time.time() - _last_request_at
    # ClinicalTrials.gov has no published rate limit but be polite.
    if elapsed < 0.1:
        time.sleep(0.1 - elapsed)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        _last_request_at = time.time()
        return data
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def _normalize_sponsor(name: str) -> str:
    """Strip corporate suffixes that hurt sponsor-name match precision."""
    s = SPONSOR_NORMALIZE_RE.sub("", name or "").strip()
    s = re.sub(r"[.,]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _query_studies(sponsor_query: str, page_size: int = 100) -> list[dict]:
    """Hit the v2 search endpoint with a sponsor-name query. Returns a list of
    `protocolSection` dicts (one per study).

    The v2 API supports `query.spons=<term>` for sponsor lookup. Combined with
    `filter.advanced` we'd get even tighter precision, but the sponsor query
    alone gets us most of the way.
    """
    if not sponsor_query:
        return []
    base = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "query.spons": sponsor_query,
        "format": "json",
        "pageSize": str(page_size),
        # Cherry-pick the fields we need so the response is small.
        "fields": (
            "protocolSection.identificationModule.nctId,"
            "protocolSection.identificationModule.briefTitle,"
            "protocolSection.identificationModule.officialTitle,"
            "protocolSection.statusModule.overallStatus,"
            "protocolSection.statusModule.primaryCompletionDateStruct.date,"
            "protocolSection.statusModule.primaryCompletionDateStruct.type,"
            "protocolSection.designModule.phases,"
            "protocolSection.designModule.studyType,"
            "protocolSection.conditionsModule.conditions,"
            "protocolSection.armsInterventionsModule.interventions,"
            "protocolSection.sponsorCollaboratorsModule.leadSponsor.name"
        ),
    }
    url = base + "?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if not data:
        return []
    return data.get("studies", []) or []


def _study_to_record(study: dict) -> dict | None:
    """Flatten a v2 study response into a small dict, or None if we can't
    parse the essentials."""
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    design = proto.get("designModule", {})
    cond = proto.get("conditionsModule", {})
    spons = proto.get("sponsorCollaboratorsModule", {})
    inter = proto.get("armsInterventionsModule", {})

    nct = ident.get("nctId")
    if not nct:
        return None
    pcd_struct = status.get("primaryCompletionDateStruct") or {}
    pcd = pcd_struct.get("date")
    pcd_type = pcd_struct.get("type")  # "ACTUAL" vs "ESTIMATED"
    phases = design.get("phases") or []
    overall = status.get("overallStatus")
    sponsor = (spons.get("leadSponsor") or {}).get("name")
    interventions = inter.get("interventions") or []
    intervention_names = [iv.get("name", "") for iv in interventions if iv.get("name")]
    conditions = cond.get("conditions") or []

    return {
        "nct": nct,
        "brief_title": ident.get("briefTitle") or "",
        "official_title": ident.get("officialTitle") or "",
        "status": overall or "",
        "phase": phases[0] if phases else "",
        "phases_all": phases,
        "primary_completion_date": pcd,
        "primary_completion_type": pcd_type,
        "sponsor": sponsor,
        "conditions": conditions,
        "interventions": intervention_names,
        "url": f"https://clinicaltrials.gov/study/{nct}",
    }


def _is_pharma_match(records: list[dict]) -> bool:
    """Sanity-check: do any of these matched trials look like real biopharma
    work? If none of the titles or conditions match pharma keywords, the
    sponsor-name match was probably a name collision (e.g. 'Vertex Energy')."""
    if not records:
        return False
    blob = " ".join(
        (r.get("brief_title", "") + " " + r.get("official_title", "") + " " + " ".join(r.get("conditions", [])))
        for r in records
    )
    return PHARMA_KEYWORDS.search(blob) is not None


def _filter_upcoming(records: list[dict], lookahead_months: int = LOOKAHEAD_MONTHS) -> list[dict]:
    """Return records with a primary completion date in [today - 90d, today + lookahead].
    Includes the small backwards window so just-completed trials whose top-line
    is imminent still surface.
    """
    now = datetime.now(timezone.utc).date()
    lookahead_end = now + timedelta(days=lookahead_months * 30)
    backward_start = now - timedelta(days=RECENTLY_COMPLETED_WINDOW_DAYS)

    out: list[dict] = []
    for r in records:
        pcd = r.get("primary_completion_date") or ""
        if not pcd:
            continue
        # The v2 API returns dates as "YYYY-MM-DD" or "YYYY-MM" (month precision)
        try:
            if len(pcd) == 7:
                pcd_date = datetime.strptime(pcd + "-01", "%Y-%m-%d").date()
            else:
                pcd_date = datetime.strptime(pcd[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        # Phase filter — require at least one Phase 2+ in the phase list.
        # If phase data is missing entirely, skip (we can't confirm it's late-stage).
        phases_all = set(r.get("phases_all") or [])
        if r.get("phase"):
            phases_all.add(r["phase"])
        if not (phases_all & INTERESTING_PHASES):
            continue

        # Status filter
        status = r.get("status") or ""
        is_active = status in INTERESTING_STATUSES
        is_recent_complete = (
            status in RECENTLY_COMPLETED_STATUSES
            and pcd_date >= backward_start
            and pcd_date <= now
        )
        is_future = pcd_date >= now and pcd_date <= lookahead_end

        if not (is_active and is_future) and not is_recent_complete and not (is_active and is_recent_complete):
            # Active trial with completion date in window OR recently-completed → keep
            if not (is_active and pcd_date >= backward_start and pcd_date <= lookahead_end):
                continue

        r["_pcd_date"] = pcd_date.isoformat()
        out.append(r)
    out.sort(key=lambda r: r.get("_pcd_date") or "9999")
    return out


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

def upcoming(ticker: str, company_name: str | None = None, force: bool = False, cached_only: bool = False) -> dict:
    """Return upcoming + recently-completed Phase 2/3 trials for `ticker`.

    `company_name` is used as the sponsor lookup. Pass it from the universe
    row so we don't need to round-trip yfinance for every call.

    `cached_only=True` returns the cached result without hitting clinicaltrials.gov.
    Used by the batch flags path so the screener table renders fast; the Company
    Peek panel populates on demand.
    """
    if not force:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached
    if cached_only:
        return {
            "ticker": ticker.upper(),
            "company_name": company_name,
            "flagged": False,
            "n_trials": 0,
            "next_date": None,
            "trials": [],
            "fetched_at": None,
            "status": "cache_miss",
        }

    out: dict = {
        "ticker": ticker.upper(),
        "company_name": company_name,
        "flagged": False,
        "n_trials": 0,
        "next_date": None,
        "trials": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }

    if not company_name:
        out["status"] = "no_company_name"
        _write_cache(ticker, out)
        return out

    sponsor_q = _normalize_sponsor(company_name)
    if not sponsor_q:
        out["status"] = "empty_sponsor_query"
        _write_cache(ticker, out)
        return out

    # Try multiple sponsor-query variants — CT.gov's `query.spons` is fuzzy
    # and doesn't always match what yfinance returns as longName. e.g. yfinance
    # says "Moderna, Inc." but CT.gov registers them as "ModernaTX, Inc."
    queries_to_try = [
        company_name,           # raw — sometimes more specific
        sponsor_q,              # normalized — strips Inc/Ltd/etc
    ]
    # Add hand-curated aliases for tickers whose registered name differs
    queries_to_try.extend(TICKER_SPONSOR_ALIASES.get(ticker.upper(), []))
    # Add first-word as a permissive fallback (e.g. "Vertex")
    first_word = sponsor_q.split()[0] if sponsor_q else ""
    if first_word and first_word not in queries_to_try and len(first_word) >= 4:
        queries_to_try.append(first_word)

    studies: list[dict] = []
    seen_ncts: set[str] = set()
    for q in queries_to_try:
        if not q:
            continue
        for s in _query_studies(q):
            nct = (
                s.get("protocolSection", {})
                .get("identificationModule", {})
                .get("nctId")
            )
            if nct and nct not in seen_ncts:
                seen_ncts.add(nct)
                studies.append(s)
        if len(studies) >= 50:
            break  # plenty for filtering

    if not studies:
        out["status"] = "no_studies_found"
        _write_cache(ticker, out)
        return out

    records = [_study_to_record(s) for s in studies]
    records = [r for r in records if r is not None]

    # Restrict to LEAD sponsor matches only — `query.spons` also matches
    # collaborators, which surfaces e.g. Vertex-led trials when querying for
    # Moderna (CF collaboration). User expects "their company's" trials.
    match_terms = [t.lower() for t in queries_to_try if t]
    # Add the first significant word from each variant for tolerant matching
    for t in queries_to_try:
        fw = (t or "").lower().split()
        if fw and len(fw[0]) >= 4 and fw[0] not in match_terms:
            match_terms.append(fw[0])

    def _is_lead_match(r: dict) -> bool:
        lead = (r.get("sponsor") or "").lower()
        if not lead:
            return False
        return any(term in lead for term in match_terms)

    records = [r for r in records if _is_lead_match(r)]

    # Sponsor-collision sanity check — if none of the matches look like pharma
    # (e.g. "Vertex Energy" instead of Vertex Pharmaceuticals), bail.
    if not _is_pharma_match(records):
        out["status"] = "non_pharma_match"
        _write_cache(ticker, out)
        return out

    upcoming_records = _filter_upcoming(records)

    out["n_trials"] = len(upcoming_records)
    out["trials"] = upcoming_records[:10]  # cap stored payload
    if upcoming_records:
        out["flagged"] = True
        out["next_date"] = upcoming_records[0].get("_pcd_date")

    _write_cache(ticker, out)
    return out


def short_summary(ticker: str, company_name: str | None = None, cached_only: bool = False) -> str | None:
    """One-line for UI: 'Next readout: 2026-08 (Phase 3 NCT0123 in NSCLC) +3 more'"""
    res = upcoming(ticker, company_name, cached_only=cached_only)
    if not res.get("flagged") or not res.get("trials"):
        return None
    n = res["n_trials"]
    next_t = res["trials"][0]
    pcd = next_t.get("_pcd_date") or next_t.get("primary_completion_date") or "?"
    pcd_short = pcd[:7] if pcd and len(pcd) >= 7 else pcd
    phase = (next_t.get("phase") or "").replace("PHASE", "Ph").replace("_PHASE", "/")
    cond = (next_t.get("conditions") or [""])[0]
    nct = next_t.get("nct", "")
    summary = f"Next readout: {pcd_short}"
    if phase:
        summary += f" ({phase}"
        if cond:
            summary += f" in {cond[:40]}"
        if nct:
            summary += f", {nct}"
        summary += ")"
    if n > 1:
        summary += f" · +{n - 1} more"
    return summary


def upcoming_batch(tickers_with_names: dict[str, str]) -> dict[str, dict]:
    """Pull upcoming for many tickers. Cached individually; sequential since
    we don't want to hammer the API. Returns {ticker: result}."""
    out: dict[str, dict] = {}
    for tk, name in tickers_with_names.items():
        try:
            out[tk] = upcoming(tk, name)
        except Exception as e:
            out[tk] = {"ticker": tk, "flagged": False, "status": f"error:{type(e).__name__}", "trials": []}
    return out


if __name__ == "__main__":
    # Smoke test on a mix — should hit at least Vertex (always running trials)
    samples = [
        ("VRTX", "Vertex Pharmaceuticals"),
        ("KURA", "Kura Oncology"),
        ("ALNY", "Alnylam Pharmaceuticals"),
        ("MRNA", "Moderna"),
        ("PFE", "Pfizer"),
    ]
    for tk, name in samples:
        r = upcoming(tk, name, force=True)
        flag = "📅 FLAGGED" if r["flagged"] else "  no trials  "
        print(f"{tk:6}  {flag}  status={r['status']:22}  n_trials={r['n_trials']}  next={r.get('next_date')}")
        if r["flagged"]:
            print(f"        {short_summary(tk, name)}")

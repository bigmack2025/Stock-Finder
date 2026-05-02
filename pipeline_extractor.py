"""
Pipeline-aware modality extractor — replaces the coarse "General Biotech"
xlsx tag with richer modality information pulled from each company's actual
10-K Item 1 (Business).

Two modes:
  - Lexicon-based (free, ships now): keyword match against a curated lexicon
    of biotech modalities. Catches mAb, ADC, CAR-T, gene therapy, RNAi,
    kinase inhibitor, etc. Good enough to break up the "General Biotech"
    catchall for ~80% of names.
  - LLM-based (when ANTHROPIC_API_KEY or OPENAI_API_KEY set): structured
    extraction of pipeline programs (drug name, indication, modality,
    clinical stage). Better quality, costs ~$0.01 per ticker, ~$6 per universe.

Usage:
    from pipeline_extractor import extract_modalities, get_rich_modalities
    mods = extract_modalities("VRTX")          # → ['Small molecule', 'mRNA', 'Cell therapy']
    df = get_rich_modalities(['VRTX', 'ALNY'])  # → DataFrame

CLI:
    python pipeline_extractor.py --tickers VRTX,ALNY,MRNA       # lexicon mode
    python pipeline_extractor.py --tickers VRTX,KURA --llm      # LLM mode (needs API key)
    python pipeline_extractor.py --all                          # bulk warm cache

Cache: data/pipeline/<TICKER>.json (TTL = 90 days)

The engine picks up rich modalities automatically if data/pipeline/<TICKER>.json
exists for a ticker — see data_layer.py merge logic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "data" / "pipeline"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTL_DAYS = 90


# ---------------------------------------------------------------------------
# Modality lexicon
# Each modality maps to a list of regex patterns. Patterns are conservative —
# we'd rather miss a hit than add a false positive that pollutes the engine.
# ---------------------------------------------------------------------------

LEXICON: dict[str, list[str]] = {
    "Monoclonal antibody": [
        r"\bmonoclonal antibod(?:y|ies)\b",
        r"\bmAb(?:s)?\b",
        r"\btherapeutic antibod(?:y|ies)\b",
    ],
    "Bispecific antibody": [
        r"\bbispecific antibod(?:y|ies)?\b",
        r"\bBiTE\b",
        r"\bbispecific T-?cell engager\b",
    ],
    "Antibody-drug conjugate": [
        r"\bantibody[- ]drug conjugates?\b",
        r"\bADCs?\b",
    ],
    "CAR-T cell therapy": [
        r"\bchimeric antigen receptor\b",
        r"\bCAR[- ]T(?:[- ]cell)?\b",
    ],
    "TCR cell therapy": [
        r"\bT-?cell receptor therap(?:y|ies)\b",
        r"\bTCR[- ]T\b",
    ],
    "Gene therapy": [
        r"\bgene therap(?:y|ies)\b",
        r"\bAAV(?:[- ]\d)?\b",
        r"\badeno-?associated virus\b",
        r"\bin vivo gene\b",
    ],
    "Gene editing": [
        r"\bCRISPR\b",
        r"\bgene edit(?:ing|or)\b",
        r"\bbase editing\b",
        r"\bprime editing\b",
        r"\bzinc[- ]finger nuclease\b",
        r"\bTALEN\b",
    ],
    "Cell therapy (other)": [
        r"\bstem cell therap(?:y|ies)\b",
        r"\biPSC\b",
        r"\binduced pluripotent\b",
        r"\bregenerative medicine\b",
    ],
    "RNAi/siRNA": [
        r"\bRNAi\b",
        r"\bsiRNA\b",
        r"\bRNA interference\b",
    ],
    "Antisense oligonucleotide": [
        r"\bantisense oligonucleotides?\b",
        r"\bASOs?\b",
    ],
    "mRNA therapeutic": [
        r"\bmRNA therapeutics?\b",
        r"\bmessenger RNA\b",
        r"\bmRNA[- ]based\b",
    ],
    "Vaccine": [
        r"\bvaccines?\b",
        r"\bimmunization\b",
    ],
    "Small molecule": [
        r"\bsmall[- ]molecule\b",
        r"\bkinase inhibitor\b",
        r"\bGPCR (?:agonist|antagonist|modulator)\b",
        r"\bligand[- ]based\b",
    ],
    "Protein degrader": [
        r"\bPROTAC\b",
        r"\bmolecular glue\b",
        r"\btargeted protein degrad",
    ],
    "Peptide therapeutic": [
        r"\bpeptide therap(?:y|ies|eutic)\b",
        r"\bcyclic peptides?\b",
    ],
    "Microbiome": [
        r"\bmicrobiome\b",
        r"\blive biotherapeutic\b",
    ],
    "Radiotherapeutic": [
        r"\bradio[- ]?ligand\b",
        r"\bradiopharmaceutical\b",
        r"\btargeted radio",
    ],
    "Oncolytic virus": [
        r"\boncolytic vir(?:us|al)\b",
    ],
    "Diagnostics/devices": [
        r"\bin vitro diagnostic\b",
        r"\bmedical device\b",
    ],
}

# Therapeutic-area lexicon — orthogonal axis
THERAPEUTIC_AREAS: dict[str, list[str]] = {
    "Oncology": [r"\bonco(?:logy|logic)\b", r"\btumou?rs?\b", r"\bcancers?\b", r"\bsolid tumou?r\b", r"\bhematolog(?:y|ic)\b"],
    "Neurology/CNS": [r"\bneurolog(?:y|ic)\b", r"\bAlzheimer", r"\bParkinson", r"\bALS\b", r"\bepilepsy\b", r"\bCNS\b"],
    "Immunology/I&I": [r"\bautoimmun(?:e|ity)\b", r"\bpsoriasis\b", r"\blupus\b", r"\bulcerative\b", r"\brheumatoid\b"],
    "Metabolic/Obesity": [r"\bmetabolic\b", r"\bobesity\b", r"\bdiabet(?:es|ic)\b", r"\bGLP-?1\b", r"\bNASH\b", r"\bMASH\b"],
    "Cardiovascular": [r"\bcardio\b", r"\bheart failure\b", r"\bhypertension\b"],
    "Ophthalmology": [r"\bophthalm(?:ology|ic)\b", r"\bretinal?\b", r"\b(?:wet|dry) (?:age-related )?macular\b"],
    "Rare disease": [r"\brare diseases?\b", r"\borphan\b", r"\bgenetic diseases?\b"],
    "Infectious disease": [r"\bantiviral\b", r"\bantibacter\b", r"\binfectious\b", r"\bHIV\b", r"\bHCV\b"],
    "Dermatology": [r"\bdermatolog(?:y|ic)\b", r"\bskin disease\b"],
    "Hematology": [r"\bhematolog(?:y|ic)\b", r"\bsickle cell\b", r"\bbeta-thalassemia\b"],
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.json"


def _read_cache(ticker: str) -> dict | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) / 86400 > TTL_DAYS:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_cache(ticker: str, payload: dict) -> None:
    _cache_path(ticker).write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# 10-K Item 1 fetch + extraction
# ---------------------------------------------------------------------------

def _fetch_10k_text(ticker: str) -> tuple[str, dict | None]:
    """Returns (text, filing_metadata). Reuses going_concern's HTTP plumbing."""
    import going_concern, historical
    cik = historical.get_cik(ticker)
    if not cik:
        return "", None
    filing = going_concern._latest_10k(cik)
    if not filing:
        return "", None
    raw = going_concern._http_get(filing["doc_url"])
    if not raw:
        return "", filing
    return going_concern._strip_html(raw), filing


def _extract_item_1(full_text: str) -> str:
    """Heuristic split — find the actual Item 1 (Business) section, skipping the
    table of contents which mentions the same heading.

    Strategy: find every "Item 1" / "Item 1A" pairing in order; the actual
    section is the one with the LARGEST gap between Item 1 and Item 1A.
    The TOC pairs typically have only a few hundred chars of gap; the real
    section has tens of thousands.
    """
    if not full_text:
        return ""

    item1_re = re.compile(r"\bItem\s+1\b\.?\s*(?:Business)?", re.IGNORECASE)
    item1a_re = re.compile(r"\bItem\s+1A\b\.?\s*(?:Risk\s+Factors)?", re.IGNORECASE)

    starts = [m.start() for m in item1_re.finditer(full_text)]
    ends = [m.start() for m in item1a_re.finditer(full_text)]
    if not starts:
        return full_text[:80_000]

    # Pair each Item 1 with the next Item 1A occurring after it; pick the largest gap
    best_pair = None
    best_gap = 0
    for s in starts:
        # next end after this start
        candidate_ends = [e for e in ends if e > s]
        if not candidate_ends:
            continue
        e = candidate_ends[0]
        gap = e - s
        if gap > best_gap:
            best_gap = gap
            best_pair = (s, e)

    if best_pair and best_gap > 5_000:
        return full_text[best_pair[0]: best_pair[1]]

    # No good pair found — fall back to a wide window from the last Item 1
    return full_text[starts[-1]: starts[-1] + 100_000]


# ---------------------------------------------------------------------------
# Lexicon match
# ---------------------------------------------------------------------------

def _match_lexicon(text: str, lexicon: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, patterns in lexicon.items():
        n = 0
        for pat in patterns:
            n += len(re.findall(pat, text, flags=re.IGNORECASE))
        if n > 0:
            counts[label] = n
    return counts


def _top_n(counts: dict[str, int], n: int = 3, min_count: int = 2) -> list[str]:
    """Return up to n labels, ranked by count, that pass min_count threshold."""
    filtered = [(k, v) for k, v in counts.items() if v >= min_count]
    filtered.sort(key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in filtered[:n]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_modalities(ticker: str, force: bool = False, prefer_llm: bool = True) -> dict:
    """Pipeline extraction. Tries LLM first if API key set + prefer_llm=True,
    otherwise falls back to lexicon. Returns
    {modalities: list, therapeutic_areas: list, lead_assets: list (LLM only),
     primary_modality: str | None, filing_filed: str, filing_url: str, ...}.
    """
    if not force:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached

    out: dict = {
        "ticker": ticker.upper(),
        "modalities": [],
        "therapeutic_areas": [],
        "primary_modality": None,
        "modality_counts": {},
        "ta_counts": {},
        "filing_filed": None,
        "filing_url": None,
        "source_text_len": 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "method": "lexicon",
        "status": "ok",
    }

    text, filing = _fetch_10k_text(ticker)
    if not text:
        out["status"] = "no_10k_text"
        _write_cache(ticker, out)
        return out

    if filing:
        out["filing_filed"] = filing.get("filed")
        out["filing_url"] = filing.get("doc_url")

    item1 = _extract_item_1(text)
    out["source_text_len"] = len(item1)

    # Try LLM first if available; fall back to lexicon
    llm_result = None
    if prefer_llm and _llm_available():
        try:
            llm_result = _llm_extract(item1, ticker)
            out["method"] = "llm"
        except Exception as e:
            out["llm_error"] = f"{type(e).__name__}: {e}"

    if llm_result:
        out["modalities"] = llm_result.get("modalities", [])
        out["therapeutic_areas"] = llm_result.get("therapeutic_areas", [])
        out["lead_assets"] = llm_result.get("lead_assets", [])
        out["lead_stage"] = llm_result.get("lead_stage")
        out["primary_modality"] = out["modalities"][0] if out["modalities"] else None
    else:
        # Lexicon path
        mod_counts = _match_lexicon(item1, LEXICON)
        ta_counts = _match_lexicon(item1, THERAPEUTIC_AREAS)
        out["modality_counts"] = mod_counts
        out["ta_counts"] = ta_counts
        out["modalities"] = _top_n(mod_counts, n=3, min_count=2)
        out["therapeutic_areas"] = _top_n(ta_counts, n=3, min_count=2)
        out["primary_modality"] = out["modalities"][0] if out["modalities"] else None

    _write_cache(ticker, out)
    return out


# ---------------------------------------------------------------------------
# LLM extraction (M8) — Anthropic preferred, OpenAI fallback
# ---------------------------------------------------------------------------

def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))


_LLM_PROMPT = """You are extracting structured information about a biotech \
company's drug pipeline from the "Item 1. Business" section of its 10-K filing.

Return ONLY a JSON object with these exact keys:
- "modalities": array of drug-class strings the company actually develops, drawn ONLY from this controlled vocabulary:
    ["Monoclonal antibody", "Bispecific antibody", "Antibody-drug conjugate",
     "CAR-T cell therapy", "TCR cell therapy", "Gene therapy", "Gene editing",
     "Cell therapy (other)", "RNAi/siRNA", "Antisense oligonucleotide",
     "mRNA therapeutic", "Vaccine", "Small molecule", "Protein degrader",
     "Peptide therapeutic", "Microbiome", "Radiotherapeutic", "Oncolytic virus",
     "Diagnostics/devices"]
- "therapeutic_areas": array drawn ONLY from:
    ["Oncology", "Neurology/CNS", "Immunology/I&I", "Metabolic/Obesity",
     "Cardiovascular", "Ophthalmology", "Rare disease", "Infectious disease",
     "Dermatology", "Hematology"]
- "lead_assets": array of {"name": str, "indication": str, "stage": str} objects for the named lead programs.
    `stage` MUST be one of: "Preclinical", "Phase 1", "Phase 2", "Phase 3", "Approved", "Marketed".
- "lead_stage": single string with the most-advanced stage across the pipeline (same vocabulary as above).

Important rules:
- Do NOT hallucinate. Only include a modality / TA / asset if the text supports it.
- If the company is partnering on something but not developing it themselves, do not include it.
- Limit "modalities" and "therapeutic_areas" to the top 3 most prominent each.
- Limit "lead_assets" to the 3 most-advanced programs.

10-K Item 1 text:
\"\"\"
{text}
\"\"\"

Return ONLY the JSON object, no preamble."""


def _llm_extract(item1_text: str, ticker: str) -> Optional[dict]:
    """Call the configured LLM and return structured pipeline data, or None on failure."""
    # Truncate very long Item 1 sections to fit token budget (most are 30-100KB; we cap at ~30KB → ~7K tokens)
    text = item1_text[:30_000]
    prompt = _LLM_PROMPT.replace("{text}", text)

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # type: ignore
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text if resp.content else ""
            return _parse_llm_json(raw)
        except ImportError:
            pass

    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # type: ignore
            client = openai.OpenAI()
            resp = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            return _parse_llm_json(raw)
        except ImportError:
            pass

    return None


def _parse_llm_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    # Sometimes the LLM wraps in ```json fences
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Find the first { and last } in case of stray text
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
    # Sanity: must have at least modalities or therapeutic_areas
    if not isinstance(obj, dict):
        return None
    obj.setdefault("modalities", [])
    obj.setdefault("therapeutic_areas", [])
    obj.setdefault("lead_assets", [])
    obj.setdefault("lead_stage", None)
    return obj


def get_rich_modalities(tickers: list[str], progress: bool = False) -> pd.DataFrame:
    rows = []
    for i, tk in enumerate(tickers, 1):
        if progress and (i == 1 or i % 10 == 0 or i == len(tickers)):
            print(f"  [{i}/{len(tickers)}] {tk}")
        rows.append(extract_modalities(tk))
    return pd.DataFrame(rows)


def short_modality_string(record: dict) -> str:
    """One-liner for UI: 'Small molecule + mRNA + Cell therapy / Oncology + Rare'."""
    mods = record.get("modalities") or []
    tas = record.get("therapeutic_areas") or []
    parts = []
    if mods:
        parts.append(" + ".join(mods))
    if tas:
        parts.append(" / " + " + ".join(tas[:2]))
    return "".join(parts) or "—"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=str, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.all:
        u = pd.read_parquet(ROOT / "data" / "current_universe.parquet")
        tickers = u.loc[u["region"] == "US", "ticker"].tolist()
    elif args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        ap.error("--all or --tickers required")

    print(f"Extracting pipeline modalities for {len(tickers)} tickers (lexicon mode)...")
    df = get_rich_modalities(tickers, progress=True)
    rich_count = int(df["modalities"].apply(lambda x: len(x) > 0 if isinstance(x, list) else False).sum())
    print(f"\nGot rich tags for {rich_count}/{len(df)} tickers.")
    print(df[["ticker", "modalities", "therapeutic_areas", "filing_filed"]].head(15).to_string(index=False))


if __name__ == "__main__":
    _cli()

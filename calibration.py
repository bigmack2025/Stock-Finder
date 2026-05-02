"""
Peer-pair grading + grid-search weight calibration (M7).

The cheapness-engine ranks peer sets by a weighted similarity over five
features. Default weights are intuition-set; this module lets you replace
intuition with empirical signal:

  1. Surface 30 candidate (anchor, candidate) pairs sampled across the
     universe — mix of obvious-yes, obvious-no, and borderline.
  2. User grades each as "peers" / "not peers" / "skip".
  3. Grid search over weight combinations finds the set that best
     reproduces the user's labels (ranking-AUC objective).
  4. Output: optimal weights + AUC delta vs default + a diff showing
     how peer rankings shift for a few well-known anchors.

Storage: data/calibration/labels_<username>.json  (per-user grades)
         data/calibration/calibrated_weights.json (current best weights)

Public API:
  sample_pairs(n=30, seed=42) -> list[(anchor, candidate)]
  save_label(username, anchor, candidate, label)  # 'peer' | 'not_peer'
  load_labels(username) -> list[dict]
  run_grid_search(username, granularity=11) -> dict
  apply_calibrated_weights() -> Weights | None     # used by engine when present
"""

from __future__ import annotations

import itertools
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import engine

ROOT = Path(__file__).parent
CAL_DIR = ROOT / "data" / "calibration"
CAL_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Pair sampling
# ---------------------------------------------------------------------------

def sample_pairs(n: int = 30, seed: int = 42) -> list[dict]:
    """Mix three buckets:
      - 10 'obvious yes' pairs (same modality, similar size)
      - 10 'obvious no' pairs (different modality, very different size)
      - 10 'borderline' pairs (engine score in middle quintile)
    """
    rng = random.Random(seed)
    u = engine.load_universe()
    us = u.loc[u["region"] == "US"].copy().reset_index(drop=True)

    def _row(t):
        return us.loc[us["ticker"] == t].iloc[0]

    pairs: list[dict] = []
    seen = set()

    # Obvious yes: same primary_modality, both have valid mkt cap, size band match
    obvious_yes_pool = us.loc[us["mkt_cap_m"].notna() & (us["primary_modality"] != "General Biotech")]
    grouped_yes = obvious_yes_pool.groupby(["primary_modality", "size_band"])
    for (_mod, _size), grp in grouped_yes:
        if len(grp) < 2:
            continue
        sample = grp.sample(min(2, len(grp)), random_state=seed)
        if len(sample) < 2:
            continue
        a, b = sample.iloc[0], sample.iloc[1]
        key = tuple(sorted([a["ticker"], b["ticker"]]))
        if key in seen or len(pairs) >= 10:
            continue
        seen.add(key)
        pairs.append({"anchor": a["ticker"], "candidate": b["ticker"], "bucket": "obvious_yes"})

    # Obvious no: very different size + different modality
    candidates_no = us.loc[us["mkt_cap_m"].notna() & (us["primary_modality"] != "General Biotech")]
    while len([p for p in pairs if p["bucket"] == "obvious_no"]) < 10:
        a = candidates_no.sample(1, random_state=rng.randint(0, 99999)).iloc[0]
        b = candidates_no.sample(1, random_state=rng.randint(0, 99999)).iloc[0]
        if a["ticker"] == b["ticker"]:
            continue
        if a["primary_modality"] == b["primary_modality"]:
            continue
        if a["size_band"] == b["size_band"]:
            continue
        key = tuple(sorted([a["ticker"], b["ticker"]]))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"anchor": a["ticker"], "candidate": b["ticker"], "bucket": "obvious_no"})

    # Borderline: engine ranks them in middle quintile (similarity ~0.4-0.6)
    while len([p for p in pairs if p["bucket"] == "borderline"]) < 10:
        a = us.sample(1, random_state=rng.randint(0, 99999)).iloc[0]
        ranked = engine.rank(a["ticker"], top_n=200)
        # Pick a candidate in the middle range
        if len(ranked) < 50:
            continue
        mid_zone = ranked.iloc[60:140]
        if len(mid_zone) == 0:
            continue
        b_row = mid_zone.sample(1, random_state=rng.randint(0, 99999)).iloc[0]
        key = tuple(sorted([a["ticker"], b_row["ticker"]]))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"anchor": a["ticker"], "candidate": b_row["ticker"], "bucket": "borderline"})

    rng.shuffle(pairs)
    return pairs[:n]


# ---------------------------------------------------------------------------
# Label storage
# ---------------------------------------------------------------------------

def _labels_path(username: str) -> Path:
    safe = "".join(c for c in (username or "").lower() if c.isalnum() or c in "_-")[:40] or "anon"
    return CAL_DIR / f"labels_{safe}.json"


def save_label(username: str, anchor: str, candidate: str, label: str, bucket: str | None = None) -> None:
    """label ∈ {peer, not_peer, skip}"""
    p = _labels_path(username)
    items = json.loads(p.read_text()) if p.exists() else []
    pair_key = f"{anchor.upper()}|{candidate.upper()}"
    items = [it for it in items if it["pair_key"] != pair_key]
    items.append({
        "pair_key": pair_key,
        "anchor": anchor.upper(),
        "candidate": candidate.upper(),
        "label": label,
        "bucket": bucket,
        "graded_at": datetime.now(timezone.utc).isoformat(),
    })
    p.write_text(json.dumps(items, indent=2))


def load_labels(username: str) -> list[dict]:
    p = _labels_path(username)
    if not p.exists():
        return []
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def _similarity_for_pair(anchor: str, candidate: str, weights: engine.Weights) -> float:
    """Compute the engine score for a single (anchor, candidate) pair."""
    df = engine.rank(anchor, weights=weights, top_n=2000, exclude_megas=False)
    sub = df.loc[df["ticker"] == candidate.upper()]
    return float(sub.iloc[0]["score"]) if not sub.empty else 0.0


def _evaluate_weights(weights: engine.Weights, labeled: list[dict]) -> dict:
    """Compute simple ranking AUC: for each peer pair, what % of not-peer pairs
    does it score higher than? Also returns mean peer score and mean not-peer score.
    """
    peers = [p for p in labeled if p["label"] == "peer"]
    nots = [p for p in labeled if p["label"] == "not_peer"]
    if not peers or not nots:
        return {"auc": float("nan"), "mean_peer": float("nan"), "mean_not": float("nan")}
    peer_scores = [_similarity_for_pair(p["anchor"], p["candidate"], weights) for p in peers]
    not_scores = [_similarity_for_pair(p["anchor"], p["candidate"], weights) for p in nots]
    # Pairwise AUC: of all (peer, not) score comparisons, fraction where peer > not
    total = len(peer_scores) * len(not_scores)
    wins = sum(1 for ps in peer_scores for ns in not_scores if ps > ns)
    auc = wins / total
    return {
        "auc": auc,
        "mean_peer": float(np.mean(peer_scores)) if peer_scores else float("nan"),
        "mean_not": float(np.mean(not_scores)) if not_scores else float("nan"),
        "n_peer": len(peers),
        "n_not": len(nots),
    }


def run_grid_search(
    username: str,
    granularity: int = 6,
    persist: bool = True,
) -> dict:
    """Grid-search over the 5 weight knobs.

    With granularity=6, each weight ranges over [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    yielding 6^5 = 7776 combinations. Smaller granularity for speed; larger
    for precision. Only combinations where total weight > 0 are evaluated.
    """
    labeled = [it for it in load_labels(username) if it["label"] in ("peer", "not_peer")]
    n_peer = sum(1 for it in labeled if it["label"] == "peer")
    n_not = sum(1 for it in labeled if it["label"] == "not_peer")
    if n_peer < 3 or n_not < 3:
        return {
            "ok": False,
            "reason": f"Need ≥3 peer + ≥3 not_peer labels; got {n_peer} peer / {n_not} not_peer.",
            "n_peer": n_peer,
            "n_not": n_not,
        }

    grid = np.linspace(0.0, 1.0, granularity)
    default = engine.Weights()
    default_metrics = _evaluate_weights(default, labeled)

    best_weights = default
    best_metrics = default_metrics

    for w_mc, w_hr, w_rv, w_md, w_cm in itertools.product(grid, repeat=5):
        if w_mc + w_hr + w_rv + w_md + w_cm == 0:
            continue
        w = engine.Weights(
            log_mkt_cap=w_mc, has_revenue=w_hr, log_revenue=w_rv,
            primary_modality=w_md, compound_modality=w_cm,
        )
        m = _evaluate_weights(w, labeled)
        if not np.isnan(m["auc"]) and m["auc"] > best_metrics["auc"]:
            best_metrics = m
            best_weights = w

    result = {
        "ok": True,
        "username": username,
        "n_labels": {"peer": n_peer, "not_peer": n_not},
        "default_weights": default.as_array().tolist(),
        "default_auc": default_metrics["auc"],
        "best_weights": best_weights.as_array().tolist(),
        "best_auc": best_metrics["auc"],
        "auc_delta": best_metrics["auc"] - default_metrics["auc"],
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "granularity": granularity,
    }
    if persist:
        (CAL_DIR / f"calibrated_weights_{username}.json").write_text(json.dumps(result, indent=2))
        # Also write a "canonical" copy at the global path for engine to pick up
        (CAL_DIR / "calibrated_weights.json").write_text(json.dumps(result, indent=2))
    return result


def apply_calibrated_weights() -> engine.Weights | None:
    """Return the most recently calibrated Weights, or None if no calibration done.
    Used by app.py to optionally swap in calibrated weights for ranking."""
    p = CAL_DIR / "calibrated_weights.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        bw = data["best_weights"]
        return engine.Weights(
            log_mkt_cap=bw[0], has_revenue=bw[1], log_revenue=bw[2],
            primary_modality=bw[3], compound_modality=bw[4],
        )
    except Exception:
        return None

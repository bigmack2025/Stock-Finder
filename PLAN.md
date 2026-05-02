# Biotech Mispricing Engine — Plan (v2)

## Thesis (revised)

Find biotechs whose **financials + pipeline don't justify the valuation**, before the market re-rates. Survivor cloning is *not* the goal — mispricing is. The "north star" is a template of what an undervalued setup looks like, not a winner to imitate.

Two modes, both shipped:

1. **Anchor mode (primary).** Pick a north-star company. Engine finds its 30 closest peers via similarity (financials + modality), then ranks that peer set by cheapness. Use to discover companies whose valuation is anomalous *relative to actual peers* of the company you anchor on.
2. **Free screener (secondary).** Filter the universe by region/modality/size, rank by cheapness. No anchor required.

When the year picker lands (M1), the anchor becomes time-machine-able — pick a historical state of a north star, e.g. Vertex-2008's mismatch profile, and find current setups that resemble it.

## Architecture

```
┌─ data/ ─────────────────────────────────────────────────────┐
│  current_universe.parquet     722 tickers (US + intl)        │
│  valuations.parquet           yfinance cache, 24h TTL        │
│  archive/{universe,valuations}_<date>.parquet  last 3 kept   │
│  (M1) historical/<TICKER>/snapshots.parquet                  │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ engine.py (similarity, vectorized) ────────────────────────┐
│  rank(ticker, weights, top_n) -> ranked similarity DF       │
│  peers(ticker, n) -> list[ticker]   ← the key contract       │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ valuations.py ─────────────────────────────────────────────┐
│  get_valuation(ticker) / get_valuations([...]) — cached     │
│  CLI: python valuations.py --all   (bulk pre-warm)          │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ mispricing.py ─────────────────────────────────────────────┐
│  anchor_screen(ticker, n_peers, top_n)                      │
│  free_screen(modality, region, size_band, top_n)            │
│  Signals: net_cash_to_mc, ev_cash_ratio, peer_log_ev_resid, │
│           runway_months                                      │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ app.py (Streamlit) ────────────────────────────────────────┐
│  Tabs: Anchor mode | Free screener | About                  │
│  Sidebar: watchlist + CSV export, disclaimer                │
└─────────────────────────────────────────────────────────────┘
```

## Cheapness signals (live)

All signed so **higher = cheaper**. Each is z-scored within the candidate pool and clipped to ±3σ before weighting.

| Signal | Formula | Default weight | Intuition |
|---|---|---|---|
| `net_cash_to_mc` | (cash − debt) / mkt_cap | 30% | >1 means market values whole business below net cash |
| `inv_ev_cash` | 1 / (EV / cash) | 20% | High when EV is small relative to cash |
| `peer_log_ev_resid` | −log10(EV / peer_median(EV)) | 20% | Cheap relative to peer-set EV |
| `peer_log_ev_cash_resid` | −log10(EV/Cash / peer_median) | 20% | Cheap on the EV/Cash multiple specifically |
| `runway_months` | cash / abs(monthly burn), capped at 60mo | 10% | Funded long enough to reach catalysts |

Composite score is rescaled to 0–100 (pool-percentile). The pool is the peer set in anchor mode, or the filtered universe in screener mode.

## What's intentionally missing (and why)

These need data we don't have yet. Each blocks on a specific M1 deliverable.

| Gap | Why it matters | Unblocked by |
|---|---|---|
| Pipeline value | A company can be cheap because its pipeline is genuinely worth nothing. Without pipeline awareness we'll flag value traps. | M1 — EDGAR 10-K extraction (pipeline tables, lead-asset stage) |
| Look-ahead leakage in modality tags | Today's "Oncology" label may not have applied in 2008. | M1 — modality from each year's 10-K, not the master xlsx |
| Survivor bias | Every company in the universe still exists. Dead biotechs (Coronado, Sarepta-pre-2014, etc.) are the "the market was right" examples. | M2 — pull historical EDGAR submissions for SIC 2836/8731, label survivor vs. delisted, fold into history |
| Hand-calibrated weights | Default weights chosen by intuition. | M1.5 — user labels 30 peer pairs, grid-search optimizes |
| Insider buying / dilution overhang | Strong contextual signals for cheapness | M2 — Form 4 + S-3 ingestion from EDGAR |

## Milestones

- **M0 (shipped):** v0 similarity engine over 595 US biotechs, Streamlit UI, sanity-tested.
- **M1 (shipped):** Pivot to mispricing thesis. Vectorized engine. International sheet folded. yfinance valuations puller. Mispricing scorer with 5 cheapness signals. Anchor + screener UI tabs. Watchlist + CSV export. Investment-advice disclaimer. Date-stamped data versioning. Golden tests.
- **M2 (shipped):** SEC EDGAR XBRL companyfacts integrated. `historical.py` returns per-(ticker, year) financial snapshots back to ~2010. Year-picker activated through `engine.rank()` and `mispricing.anchor_screen()`. Streamlit UI gets time-machine toggle + year slider + frozen-state card.
- **M3 (shipped):** Backtest harness over the 2018 and 2020 universes; signal validated as a basket screen. Council items: per-user watchlists, shared-password auth, hit-rate framing, basket-not-tip-sheet banner, misuse flags, data-as-of timestamps, error wrapping, onboarding panel, daily refresh + GH Actions. 31 tests.
- **M4 (shipped):** Date-precision time-machine. `historical.get_snapshot_at_date(ticker, "YYYY-MM-DD")` pulls most recent 10-Q at that point + exact-day market cap. Wired through `engine.rank()` and `mispricing.anchor_screen()`. UI gets year-vs-date toggle. Tested end-to-end on CORT @ 2019-06-14.
- **M5 (shipped):** Going-concern flag with real 10-K text indexer (`going_concern.py`). Pipeline-aware modality extractor (`pipeline_extractor.py`) using lexicon match against 10-K Item 1 — produces rich modality tags (Bispecific antibody, RNAi/siRNA, Gene editing) plus therapeutic areas (Oncology, Neuro/CNS, Rare disease). Surfaced in UI alongside xlsx tags. Dual-backend `userdb.py` auto-switches to Supabase when env vars present. 46 tests.
- **M6 (shipped):** Universe-wide lexicon extraction across all 595 US biotechs (67% have rich modality tags, 81% have therapeutic-area tags). Engine's Jaccard now consumes `combined_modalities` (rich + xlsx fallback) instead of xlsx-only. Distribution check: top tags are Small molecule (152), Monoclonal antibody (114), Gene therapy (95), CAR-T (55), ADC (50). KURA peers now correctly cluster on "small-mol oncology rare-disease" rather than xlsx-coarse "General Biotech."
- **M7 (shipped):** Peer-pair grading tool. Streamlit Calibrate tab samples 30 pairs across three buckets (10 obvious-yes, 10 obvious-no, 10 borderline), persists user labels per-username, runs grid-search calibration (5 weights × 6 levels = 7,776 combinations), reports AUC delta, persists best weights for engine to pick up. ~15-25 minute session for the user.
- **M8 (shipped):** LLM mode in `pipeline_extractor.py`. Auto-detects `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Structured-output prompt extracts modalities + therapeutic areas + lead-asset names + clinical stages. Graceful fallback to lexicon when no API key. Cost ~$0.01 per ticker × 595 = ~$6 to LLM-extract the universe.
- **M9 (shipped):** `delisted.py` finds biotech delistings via EDGAR full quarterly form.idx — extracts CIKs, cross-references against the current ticker JSON to identify delistings, filters by SIC (2834/2836/8731), pulls last-known XBRL state. `backtest.py` now optionally augments with delisted names. Initial seed of 4 biotechs from 2017Q4. To extend coverage: `python delisted.py --quarter 2014Q4` (and other quarters).
- **M10 (deferred — only matters if going public):** Paid licensed data source migration (Polygon/Tiingo) replacing yfinance. Required for commercial use of the live URL; not blocking for me-and-friends.

## Pre-push council fixes (all P0 + key P1 applied)

- ☑️ EDGAR_USER_AGENT_EMAIL env var (was: hardcoded email in source)
- ☑️ Default-password warning banner on login screen (fail-loud, not fail-silent)
- ☑️ `.gitignore` excludes all cache directories (`historical/`, `pipeline/`, `delisted/`, `going_concern/`)
- ☑️ Calibration-active badge on Anchor tab so users know which weights are running

Items deferred (P2 in `COUNCIL_REVIEW_PRE_PUSH.md`):
- Saved screens — not yet
- Email alerting — defer until a friend asks
- Pre-warm-this-slice button on free screener — defer
- Mobile responsiveness — Streamlit's default is acceptable

Items where the user has a decision to make:
- The `Public Biotech Stocks Master List.xlsx` is checked in. Decide whether that source data is yours to publish before pushing.
- Whether to commit cached parquets (`current_universe.parquet`, `valuations.parquet`) for fast cold-start, or let GH Actions rebuild them on first run.

## Council recos — disposition

P0:
- ☑️ "Blew up" definition resolved — replaced by mispricing thesis
- ⏳ Survivor bias — deferred to M4 (needs EDGAR full company list)
- ⏳ Look-ahead leakage in modality — deferred to M2 (needs 10-K extraction)

P1:
- ☑️ International sheet folded into universe (722 total, 127 intl)
- ☑️ Golden tests pinning the three archetypes + signal arithmetic (12 passing)
- ⏳ Hand-calibrated weights — harness scaffolded; needs your peer-pair grading session

P2:
- ☑️ Vectorized engine (numpy)
- ☑️ Date-stamped parquet versioning, last 3 kept
- ☑️ `@st.cache_data` on rank() and screen functions
- ☑️ Watchlist with save/dismiss/CSV export
- ☑️ Investment-advice disclaimer in sidebar
- ☑️ EDGAR rate limit / User-Agent (set in historical.py)

## Council pass before shipping site (M3) — disposition

P0:
- ☑️ Persistent watchlists — JSON-file backend in `userdb.py` (SQLite was the original plan; switched to JSON because some sandbox/overlay filesystems don't support SQLite locking. Same API; trivial to swap to Postgres/Supabase later)
- ☑️ Auth gate — shared-password gate via `userdb.check_group_password()` reading `GROUP_PASSWORD` env var
- ☑️ Daily data refresh — `refresh_all.py` + `.github/workflows/daily_refresh.yml`
- ☑️ Backtest before friends trust it — done across 2018 and 2020, see `BACKTEST_RESULTS.md`
- ⏳ Yahoo Finance ToS — fine at me-and-friends scale; migrate to Polygon/Tiingo if going public

P1:
- ☑️ Basket-not-tip-sheet framing on the Anchor tab
- ☑️ Hit-rate disclosure (50% from 2018 backtest, 25% from 2020)
- ☑️ Misuse flags: fresh IPO, reverse-merger shell, sub-$10M, user catalyst note
- ☑️ Onboarding panel ("First time here? Read this first.")
- ☑️ Data-as-of timestamps in sidebar
- ☑️ Error resilience — `safe_call()` wraps the major paths
- ⏳ Going-concern flag — placeholder, requires 10-K text indexer (M5)

P2:
- ☑️ Test coverage expanded — 31 tests, ~50% line coverage on new modules
- ⏳ Modality taxonomy refinement — needs M5 LLM extraction
- ⏳ Sentry / monitoring — not yet wired
- ⏳ Streamlit replacement (Next.js+FastAPI) — not needed at this scale

## Open questions for next session

- **Definition of cheapness weights.** Do you want to hand-grade 30 peer pairs (e.g. "KURA and AKTS = peers", "KURA and MRNA = not peers") so we can tune the similarity weights with a real signal? 1-hour exercise.
- **Peer-set composition.** Anchor mode currently includes any company in the top-30 similarity pool. For your thesis, do you prefer (a) tighter modality matching (only Oncology peers when anchor is Oncology), (b) widen to capture cross-TA cheapness setups, or (c) toggle?
- **EDGAR scope for M2.** Just Vertex's full history, or also a "fast-failed" name (e.g. one that looked cheap in 2015 but stayed cheap or worse) — to make sure the engine shows the failure modes too?

# Biotech Mispricing Engine — M9 (push-ready)

## Data provenance

`Public Biotech Stocks Master List.xlsx` is the universe of 722 tickers used
throughout the engine. The list was assembled by aggregating publicly-disclosed
information from the major biotech ETFs (XBI, IBB, XPH, IHE), the SEC's
public EDGAR filings, and standard financial news sites in April 2026. None
of it is proprietary data from a paid vendor.

The "Sub-sector (heuristic)" column is opinion-based classification done at
list-build time; the M5 lexicon extractor (`pipeline_extractor.py`) and M8 LLM
extractor improve on these tags by reading each company's actual 10-K. As of
M6 the engine uses the rich tags for ~67% of US tickers and falls back to the
xlsx tags for the rest.

If you fork this repo and don't want to inherit these classifications, replace
the xlsx with your own source data (same column names) and re-run
`python data_layer.py`.

Find biotechs whose **financials don't justify their valuation**. Pick a north-star company **and a year**; we load that company's 10-K state from SEC EDGAR for that fiscal year, find its peer set in today's universe, then rank that peer set by cheapness signals (sub-cash, EV/cash, peer-relative EV residual, runway). Or skip the year for current-vs-current. Or skip the anchor and screen the universe directly.

**M3 ships a backtested-and-shareable build.** Cheapness signal validated across two regimes (2018 bull, 2020 bear); shared-password auth gate so it's safe to share with friends; per-user persistent watchlists; misuse flags on every result; daily data refresh wired to GitHub Actions. Ready to deploy to Streamlit Cloud with an allowlist for your friend group.

The product is the **cheapness ranking inside a peer set**, not the peer set itself. Similarity is the substrate, not the deliverable.

**Time-machine mode (M2 — new this session).** Toggle on, pick a fiscal year (2010+), and the north-star's financials are replaced with that year's state from EDGAR XBRL — so "VRTX-2010" returns today's $5–8B mid-cap clinical biotechs (RYTM, CYTK, MIRM, IDYA, RCUS) instead of today's megacaps. "ALNY-2014" returns today's pre-revenue RNA names (WVE, SEPN, KYTX). Modality tags still come from the current xlsx — pipeline-aware modality lands in M4.

## Run locally

```bash
cd outputs
pip install streamlit pandas openpyxl pyarrow numpy yfinance pytest

python data_layer.py                        # build universe (722 tickers)
python valuations.py --tickers VRTX,KURA,ALXO,IDYA,ARWR   # warm valuations cache
streamlit run app.py                        # http://localhost:8501

python -m pytest tests/                     # 31 golden tests
```

The app prompts for a username + group password. Default password is `change-me-before-sharing` — set the `GROUP_PASSWORD` env var to your real password before deploying.

## Deploy to friends (Streamlit Community Cloud)

1. Push this folder to a private GitHub repo.
2. Go to https://share.streamlit.io, "New app," point at `app.py`.
3. In **App settings → Secrets**, add:
   ```
   GROUP_PASSWORD = "your-real-password-here"
   ```
4. In **Sharing → Viewers**, paste in your friends' Google email addresses (allowlist).
5. Done. Anyone you invite needs both Google sign-in AND the group password.

The daily data refresh runs via `.github/workflows/daily_refresh.yml` — fresh valuations every morning at 11 UTC, committed back to the repo so the Streamlit app sees them on next reload.

## What's in the box

| File | Role |
|---|---|
| `data_layer.py` | xlsx → universe parquet (US + International, modality-tagged, date-stamped). |
| `valuations.py` | yfinance puller: cash, debt, EV, shares, ocf. Cached, 24h TTL, archive of last 3. |
| `historical.py` | SEC EDGAR XBRL companyfacts fetcher. `get_snapshot(ticker, year)` returns historical state. |
| `engine.py` | Vectorized similarity. `peers(ticker, n, year=None)` is the hot contract. |
| `mispricing.py` | Cheapness scorer. `anchor_screen(ticker, year=None)` + `free_screen(...)`. |
| `backtest.py` | **NEW (M3)** — backtest harness. `run_backtest(year, forward_years)` validates the cheapness signal across regimes. CLI: `python backtest.py --year 2018`. |
| `userdb.py` | **NEW (M3)** — JSON-file persistence for watchlists + auth. `add_watchlist`, `list_watchlist`, `set_note`, `check_group_password`. |
| `misuse_flags.py` | **NEW (M3)** — fresh-IPO, reverse-merger-shell, sub-$10M, catalyst-note flags surfaced on every result row. |
| `refresh_all.py` | **NEW (M3)** — daily refresh script invoked by GitHub Actions. |
| `.github/workflows/daily_refresh.yml` | **NEW (M3)** — nightly cron that re-runs `refresh_all.py`. |
| `app.py` | Streamlit UI: auth gate, Anchor mode + Free screener + Backtest evidence + How to read this tabs, watchlist (persisted), misuse flags, basket-not-tip-sheet framing. |
| `tests/` | **31 golden tests** — engine archetypes, signal arithmetic, year-picker, userdb roundtrip, misuse flags, backtest aggregation. |
| `BACKTEST_RESULTS.md` | **NEW (M3)** — 2018 + 2020 backtest writeup with honest caveats. |
| `PLAN.md` | Architecture, milestones (M0→M7), council reco disposition. |
| `data/` | parquets, CSV mirrors, `archive/`, `historical/` (cached XBRL JSON), `users/` (per-user JSON). |

## What it does well

**Current-vs-current**

- **KURA → KURA, FULC, PYXS, CRDF, CHRS.** Tight small-oncology near-cash cluster.
- **ALXO → KURA, ALDX, BBOT, CRDF, PYXS, CHRS, CNTX.** Different anchor, similar peer set.
- **IDYA → VRDN, IDYA, DAWN, RCUS, AUPH, IOVA, TARS.** Mid-cap clinical-stage cluster with $400M–$1B cash.
- **ARWR → WVE, SEPN, DYAI, MRNA, AAPG, SLN.** Clean RNA cluster.
- **VRTX → ARGX, BeOne, BNTX, INCY, INSM, GMAB, JAZZ.** Megacap revenue cluster.

**Time-machine mode (M2)**

- **VRTX-2010** ($7B mkt cap, $176M rev, pre-Kalydeco): returns RYTM, CYTK, MIRM, AXSM, PTGX, KYMR, IDYA, RCUS — today's $5–8B mid-cap clinical biotechs.
- **VRTX-2012** ($8.8B, $143M rev, peri-Incivek): returns CYTK, RYTM, KYMR, AXSM, MDGL — tighter cluster around the $7–10B range.
- **ALNY-2014** ($6B, $0 rev, pre-Onpattro): returns ARWR, IONS, WVE, KYTX, MRNA, AAPG, SEPN — today's pre-revenue RNA platform plays.
- **VRTX-today vs VRTX-2010 peer overlap < 50%** — the year picker is doing real work, not just re-shuffling the same names.

## What it doesn't do (yet)

- **Pipeline-aware modality.** A company tagged "Oncology" today may have been a platform play earlier. Fixed in M4 with LLM extraction over 10-K Item 1 — gives us year-aware modality and lead-asset stage. Surfaced inline in the UI as a caveat when the year picker is active.
- **Pipeline value scoring.** A company can be cheap because its pipeline is genuinely worth nothing. Same M4 fix — once we have Ph2/Ph3 stage, we can score "pipeline depth × stage × indication size" against market cap.
- **Pre-2010 history.** SEC XBRL coverage starts ~2010 (mandatory phase-in 2009–2011). Earlier years require parsing 10-K HTML. Most "pre-blowup" windows for active biotech names start around the IPO date and 2010+ is plenty for almost every interesting comparison. Fixed properly in M5 with HTML parser if needed.
- **IFRS filers.** Companies like ARGX (Belgian, files 20-F under IFRS) have no us-gaap XBRL data on EDGAR. The year picker reports "snapshot unavailable" gracefully. We could pull from their annual reports manually but it's a long-tail problem.
- **Survivor bias.** Universe is companies that exist today. Dead biotechs (Coronado, Sarepta-pre-2014, etc.) are missing. Fixed in M5 with EDGAR full company list.
- **Calibrated weights.** Defaults are gut-set. M3 adds a peer-pair grading tool so you can tune them against your actual taste.

See `PLAN.md` for full milestone list and council reco disposition.

## Cheapness signals

All signed so **higher = cheaper**. Z-scored within the candidate pool, clipped to ±3σ, weighted-summed, rescaled to 0–100.

- **net_cash_to_mc** (30%) — `(cash − debt) / mkt cap`. >1 = market values business below net cash.
- **inv_ev_cash** (20%) — `1 / (EV / cash)`. High when EV is small vs cash.
- **peer_log_ev_resid** (20%) — `−log10(EV / peer_median(EV))`. Cheap relative to peer-set EV.
- **peer_log_ev_cash_resid** (20%) — same idea on the EV/Cash multiple.
- **runway_months** (10%, capped at 60) — `cash / monthly burn`. Funded long enough to reach catalysts.

Weights configurable in `mispricing.CheapnessWeights`.

## Council pass — what shipped this session

P1:
- ☑️ International sheet folded into universe (722 total, 127 international including Hong Kong / China)
- ☑️ Golden tests pinning the three archetypes + signal arithmetic (12 passing)
- ⏳ Hand-calibrated weights (harness scaffolded; needs your peer-pair grading)

P2:
- ☑️ Vectorized engine (numpy)
- ☑️ Date-stamped parquet versioning, last 3 archive copies
- ☑️ `@st.cache_data` on rank() and screen functions
- ☑️ Watchlist with save/CSV export
- ☑️ "Not investment advice" disclaimer

Deferred to M2 with explicit rationale in PLAN.md:
- Pipeline-aware mispricing (needs EDGAR + LLM)
- Look-ahead leakage in modality tags (same)
- Survivor-bias fix (needs EDGAR full company list)

## Not investment advice

The signals are crude. They ignore pipeline value, catalyst calendars, deal history, dilution structure, KOL coverage, partnership economics, IP runway, and a hundred other things real biotech analysts weigh. Use as a funnel, not a buy list.

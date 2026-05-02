# Council review — pre-GitHub push, post-M9

State of the project: **74 tests passing**, all M0–M9 milestones shipped, friend-group deploy fully scaffolded, only M10 (paid licensed data) intentionally deferred. Below is one more sweep through every lens before the user pushes.

## P0 — must fix before pushing

**1. Default password literal in code.** `userdb.DEFAULT_GROUP_PASSWORD = "change-me-before-sharing"` is sitting in the repo. If the user forgets to set `GROUP_PASSWORD` in Streamlit secrets, the app deploys with this string as the password. That's not a security disaster (the literal screams "change me"), but it's a footgun. **Fix: refuse to start the app if the password is the default and we're in a "production" context** — emit a fail-closed startup check.

**2. EDGAR User-Agent has my email baked in.** `Biotech-Mispricing-Engine/0.1 (macktcheli@gmail.com)` is in three modules (`historical.py`, `going_concern.py`, `pipeline_extractor.py`, `delisted.py`). When the user pushes to GitHub, that's their email in public infrastructure-data calls. **Fix: load from env var `EDGAR_USER_AGENT_EMAIL` with a sensible fallback, document in DEPLOY.**

**3. The data files in `data/` are about to bloat the git repo.** `data/historical/` (cached XBRL JSON) is several MB, `data/pipeline/` is ~600 small files, `data/delisted/` is small. Plus parquet artifacts, archive copies, etc. The `.gitignore` excludes user-data dirs but does NOT exclude the cached XBRL/pipeline/delisted JSONs. **Fix: explicit `.gitignore` rules for cache directories. The Streamlit Cloud will re-cache on first request anyway.**

## P1 — should fix before sharing with friends

**4. The Calibrate tab has a UX bug.** When the user clicks ✅/❌/⏭, we save the label and `st.rerun()`. But `st.session_state.cal_pairs` survives reruns, so the next pair shows up immediately — good. But if the user closes the browser and comes back, `cal_pairs` is lost (session-only), and they'd have to re-sample. **Fix: persist the sampled batch alongside the labels.**

**5. The "Pipeline (10-K)" column overlaps modality emojis on narrow screens.** The Anchor results table is now wide (15 columns). Mobile users will scroll horizontally. **Fix: make the rich-pipeline column hidable via a sidebar toggle. Document mobile limitations.**

**6. The free screener still doesn't pre-warm the universe.** It says "Run `python valuations.py --all` to pre-warm" but on Streamlit Cloud the user can't run that — they have to wait for the GH Actions nightly. **Fix: add a "Warm cache for this slice" button that triggers a fetch of just the filtered candidates. Saves users the wait.**

**7. The 4 delisted records is small and won't dent the backtest.** M9's discovery only found 4 biotech delistings in 2017Q4. To get statistical significance, the user needs to run discover_delistings on multiple historical quarters. **Fix: add CLI batch script (`python delisted.py --quarters 2014Q4,2015Q4,2016Q4,2017Q4,2018Q4,2019Q4,2020Q4`) that processes them all.**

**8. No "calibrated weights are now active" indicator on the Anchor tab.** If you finish calibration, the Anchor results silently use the new weights — but you don't know unless you scroll down to the breakdown. **Fix: small badge "Using calibrated weights" or "Using default weights" next to the rank table.**

## P2 — nice to have

**9. Saved screens.** Users will run "VRTX-2010 anchor screen" repeatedly. Should be a one-click "Save this query" → "Re-run my saved queries" workflow.

**10. Alerting.** "Email me when XYZ drops below cash" — meaningful for a screen that finds time-bound mispricings. Out of scope until you actually want to stay logged in to track.

**11. Performance — the `engine.rank()` recomputes the full similarity matrix on every call.** For a single user this is fine; for ~5 friends doing simultaneous queries, the per-call work compounds. **Defer:** at friend-group scale, it's fine. Revisit if it ever feels sluggish.

**12. The `mod_*` one-hot columns in the universe parquet are dead weight now.** Jaccard uses `combined_modalities` directly. The 15 one-hot columns add ~5KB per parquet but no longer contribute. **Defer:** they're cheap to keep and could be useful for future ML approaches.

**13. Universe `Public Biotech Stocks Master List.xlsx` checked into repo.** That's your source data; whether it should be in git depends on whether the data is yours to publish. **Flag for user decision.**

**14. No "data lineage" / changelog on the universe.** When you update the xlsx, there's no record of what changed. **Defer:** quarterly cadence makes this not urgent.

**15. `requirements.txt` doesn't pin upper bounds.** `streamlit>=1.40` could break on a future major version. **Defer:** Streamlit's API has been stable; pinning is paranoia. Add a `requirements-lock.txt` if it ever bites.

## What I'm doing about each

I'll fix all P0 items now. P1 items 4-6, 8 are quick wins so I'll handle them too. P1 item 7 (more delisted quarters) is automation that can run later — I'll make it a one-line invocation. P2 items I'll document but not build.

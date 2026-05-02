# Deploy guide — push to friends in 15 minutes

Goal: a private URL your invited friends can reach with their Google account + the group password. Streamlit Community Cloud is free and the fastest path. ~$0/month.

## Step 1 — Create a private GitHub repo (3 min)

```bash
cd /path/to/outputs

git init
git add .
git commit -m "biotech mispricing engine — initial deploy"

# Create a new private repo on GitHub (UI or CLI)
gh repo create biotech-mispricing --private --source=. --remote=origin --push
# OR manually:
#   git remote add origin git@github.com:YOURUSERNAME/biotech-mispricing.git
#   git branch -M main
#   git push -u origin main
```

The `.gitignore` already excludes user data / cache files / pytest scratch. The `requirements.txt` is what Streamlit Cloud reads to install dependencies.

## Step 2 — Sign up for Streamlit Community Cloud (2 min)

1. Go to https://share.streamlit.io
2. "Sign in with GitHub", grant access to your private repo
3. Click "**New app**"
4. Repository: pick your `biotech-mispricing` repo
5. Branch: `main`
6. Main file path: `app.py`
7. Click "**Advanced settings**"
   - Python version: 3.11
   - Click "**Save**"
8. Click "**Deploy**"

The first deploy takes 2–4 minutes (installing yfinance and friends).

## Step 3 — Set the secrets (2 min)

In the Streamlit Cloud dashboard for your app:

1. Click "**⚙️ Settings**" → "**Secrets**"
2. Paste:
   ```toml
   GROUP_PASSWORD = "pick-your-real-password-here"
   EDGAR_USER_AGENT_EMAIL = "your-real-email@example.com"

   # Optional: enable durable watchlists via Supabase
   # SUPABASE_URL = "https://your-project.supabase.co"
   # SUPABASE_ANON_KEY = "eyJhbGc..."

   # Optional: enable LLM-quality pipeline extraction
   # ANTHROPIC_API_KEY = "sk-ant-..."
   ```
3. Click "Save"
4. The app reboots and uses your secrets.

> **Important.** Skip step 3 and the app banners a warning ("GROUP_PASSWORD env var not set") on every login screen. Your friends would see it. Don't ship like that.

**EDGAR_USER_AGENT_EMAIL** is sent to SEC EDGAR with every API call (their compliance requirement). Use a real address you read — if SEC ever has an issue with your traffic, that's how they'll reach you.

## Step 4 — Allowlist your friends' Google emails (2 min)

In the Streamlit Cloud dashboard:

1. Click "**Settings**" → "**Sharing**"
2. Set viewer access to "**Only specific people can view this app**"
3. Paste your friends' Google email addresses, one per line
4. Save

Now the URL is double-gated: Google sign-in (to prove they're on the allowlist) + your group password (to prove they're a friend, not a Google randomly granted access).

## Step 5 — Wire the daily refresh (3 min, optional)

The repo has `.github/workflows/daily_refresh.yml` already. To activate:

1. Go to your GitHub repo → **Actions** tab
2. You may need to click "I understand my workflows, go ahead and enable them"
3. The workflow runs every day at 11 UTC (~7am ET) and commits fresh `valuations.parquet` back to main. Your live Streamlit app picks it up on next reload.

If you skip this step, valuations get stale at the rate yfinance data ages out of cache (24h). You can also manually trigger refresh via the "Run workflow" button in the Actions tab.

## Step 6 — Send the URL to friends

Each friend needs:
1. The Streamlit URL (something like `https://biotech-mispricing-yourname.streamlit.app/`)
2. The group password

They'll be prompted for Google sign-in (which Streamlit checks against your allowlist), then see the username + password gate inside the app, then they're in.

---

## Known limitation: watchlist persistence on Streamlit Cloud

Streamlit Community Cloud uses an ephemeral filesystem — anything the app writes to disk at runtime is wiped when the container restarts (which can happen any time the app is idle for ~10 min). **Your watchlist survives a single session but will reset when the container cycles.**

For me-and-friends scale, this is usually fine — the export-to-CSV button is right there in the sidebar. But if anyone treats their watchlist as the source of truth, they'll lose it.

### Quick fix when you're ready: Supabase free tier (10 min, zero code change)

The dual-backend lives in `userdb.py` already — it auto-detects `SUPABASE_URL` + `SUPABASE_ANON_KEY` env vars and switches backends with no code change. To activate:

1. **Sign up** at https://supabase.com (free tier handles ~500MB / unlimited rows for this use case)
2. **Create a new project**, wait for it to provision (~2 min)
3. **Run this SQL** in the Supabase SQL editor (the schema is already wired in `userdb.py`):
   ```sql
   create table users (
     id          serial primary key,
     username    text unique not null,
     created_at  timestamptz default now()
   );
   create table watchlist (
     id         bigserial primary key,
     username   text not null,
     ticker     text not null,
     name       text,
     source     text,
     note       text,
     added_at   timestamptz default now(),
     unique(username, ticker)
   );
   create table notes (
     id          bigserial primary key,
     username    text not null,
     ticker      text not null,
     note        text,
     updated_at  timestamptz default now(),
     unique(username, ticker)
   );
   ```
4. In Supabase dashboard → **Settings → API**, grab your `Project URL` and `anon public` key.
5. In Streamlit Cloud → **Secrets**, add:
   ```toml
   SUPABASE_URL = "https://your-project.supabase.co"
   SUPABASE_ANON_KEY = "eyJhbGc..."
   ```
6. Add `supabase>=2.0` to `requirements.txt` and push to GitHub.
7. Reboot the Streamlit app (Settings → "Reboot app").

That's it. Watchlists now persist across container restarts. The userdb.current_backend() function will report "supabase" instead of "json" — visible if you add it to the sidebar.

If you set the env vars but the `supabase` package isn't installed, the code falls back to JSON without crashing.

## What ships in this deploy

- 722-ticker biotech universe (US + International), refreshed daily
- Anchor mode with **time-machine** (year-end *and* exact-date precision via SEC EDGAR XBRL + 10-Q filings)
- Mispricing scorer (5 cheapness signals, 0–100 percentile within peer set)
- Free screener with region/modality/size filters
- Backtest evidence tab (2018 + 2020 results inline)
- Per-user watchlist with note-taking + CSV export
- Misuse flags (🆕 fresh IPO, 🐚 reverse-merger shell, ⚠️ sub-$10M, 📅 catalyst note)
- Investment-advice disclaimer + basket-not-tip-sheet framing
- Onboarding panel for first-time users

## What's still open (deferred to future sessions)

- **Going-concern flag from real 10-K text** — currently a placeholder
- **Pipeline-aware modality** — LLM extraction over 10-K Item 1, replaces today's coarse "General Biotech" tag with year-aware modality + lead-asset stage
- **Survivor-bias dead-company list** — pulls delisted biotechs from EDGAR full submissions index
- **Hand-calibrated weights** — the peer-pair grading session that tunes the engine to your actual taste
- **Supabase migration** — see appendix above; do this when watchlist persistence becomes a real complaint
- **Paid licensed data source** (Polygon/Tiingo) — only matters if going public

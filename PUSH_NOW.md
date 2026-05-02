# Push it live — easy version

Goal: a private URL your friends can use, with their Google account + a shared password. ~15 minutes total. Free.

You'll do four things:
1. Push the code to a private GitHub repo
2. Connect that repo to Streamlit Cloud
3. Set your secrets (password, email)
4. Add your friends' emails to the allowlist

That's it. Send them the URL.

---

## Before you start

Make sure you have:

- **A GitHub account.** If not: https://github.com/signup (free, takes 2 min).
- **The project folder open in Finder.** You can find it at:
  ```
  ~/Library/Application Support/Claude/local-agent-mode-sessions/.../outputs
  ```
  Easier: in this Cowork conversation, click any of the file links I've shared — that opens the folder.

You do NOT need to install anything else. We'll use GitHub's website and Streamlit's website. No terminal commands required.

---

## Step 1 — Create a private GitHub repo (3 min)

1. Go to https://github.com/new
2. **Repository name:** `biotech-mispricing` (or whatever you want)
3. **Description** (optional): "Biotech mispricing screener — private"
4. Set it to **Private** ← important
5. **Do NOT** check "Add a README file" (we already have one)
6. **Do NOT** check "Add .gitignore" (we already have one)
7. Click **Create repository**

You'll land on an empty repo page. Keep this tab open — we'll use it in step 2.

---

## Step 2 — Upload the code (5 min)

The easiest way is GitHub's drag-and-drop:

1. On your new empty repo page, click **"uploading an existing file"** (it's in the gray "Quick setup" box).
2. Open Finder and navigate to your project folder (`outputs/`).
3. **Select all the files and folders** in that folder. On Mac: ⌘+A inside the folder, then drag the selection into the GitHub upload area.
   - **What to skip:** the `__pycache__/`, `.pytest_cache/`, and `pytest-cache-files-*/` folders if Finder shows them. (The `.gitignore` will handle these too if you accidentally include them.)
4. Wait for the upload to finish (you'll see a list of files appear).
5. Scroll to the bottom: in the **Commit changes** box, type `initial commit`.
6. Make sure **"Commit directly to the main branch"** is selected.
7. Click **Commit changes**.

Your code is now on GitHub. Refresh the page and you should see all your files.

---

## Step 3 — Connect Streamlit Cloud (3 min)

1. Go to https://share.streamlit.io
2. Click **Sign in with GitHub**, authorize the GitHub permissions when asked.
3. Click **New app**.
4. **Repository:** pick the `biotech-mispricing` repo you just created.
5. **Branch:** `main`.
6. **Main file path:** `app.py`
7. **Advanced settings** → set Python version to **3.11**.
8. Click **Deploy**.

Wait 2–4 minutes. The first deploy installs all the Python dependencies. You'll see a progress log scroll by. When it's done, you'll get a URL like `https://biotech-mispricing-yourname.streamlit.app`.

If you visit the URL right now, you'll see the login screen with a yellow warning saying "GROUP_PASSWORD env var not set." That's fine — we'll fix it next.

---

## Step 4 — Set the secrets (2 min)

Still on the Streamlit Cloud dashboard for your app:

1. Click the **⚙️ (settings)** icon in the bottom-right of your app's tile (or open the app and click the three-dot menu → "Settings").
2. Click **Secrets** in the left sidebar.
3. Paste this into the box, replacing the placeholder values with your real ones:

```toml
GROUP_PASSWORD = "your-real-password-here"
EDGAR_USER_AGENT_EMAIL = "your-email@example.com"
```

Notes on each:
- **GROUP_PASSWORD** — pick anything memorable. You'll share this with friends. Don't reuse a password from another site.
- **EDGAR_USER_AGENT_EMAIL** — your real email. SEC requires it for their public APIs and uses it only to contact you if your traffic causes them concern (which it won't, our rate is well under their limits).

4. Click **Save**.

Streamlit will reboot the app (~30 sec). The yellow warning disappears.

---

## Step 5 — Allowlist your friends (2 min)

In your app's settings on Streamlit Cloud:

1. Click **Sharing** in the left sidebar.
2. Set **"Who can view this app"** to **"Only specific people can view this app"**.
3. Paste in your friends' Google email addresses, one per line.
4. Click **Save**.

Each invited friend will get an email from Streamlit. They click it, sign in with Google, and they're in.

---

## Step 6 — Send the URL

The URL of your app (something like `https://biotech-mispricing-yourname.streamlit.app`) plus the group password — that's all your friends need.

Send them this:

```
Hey — check out this biotech mispricing tool I've been working on:
  https://biotech-mispricing-yourname.streamlit.app

Group password: [whatever you set]

You'll need to sign in with Google first (I've added you to the allowlist).
```

---

## What to expect once people use it

- **First page-load is slow** (~10 seconds) — the engine is loading the universe + checking historical data on demand.
- **Anchor mode is the main interaction.** Pick a north-star company, optionally toggle time-machine mode, run the screen.
- **Watchlists persist within a session** but may reset when Streamlit recycles the container (every ~10 min idle). If this becomes annoying, the DEPLOY.md appendix walks through the 10-minute Supabase upgrade for durable storage.
- **The data refreshes nightly** if you enabled GitHub Actions in your repo (Actions tab → "I understand my workflows, go ahead and enable them"). Otherwise data is whatever was current at deploy time.

---

## If something goes wrong

- **Streamlit deploy fails with "ModuleNotFoundError"** → Check `requirements.txt` is in your repo. It should be.
- **"GROUP_PASSWORD env var not set" banner** → You missed step 4 or typed the secret wrong. Check Secrets in app settings.
- **Friend can't log in** → Make sure their Google email is on the allowlist (step 5). They need to be signed in as that exact email.
- **App is super slow on first query** → Normal. The historical EDGAR cache fills lazily. Subsequent queries on the same ticker are instant.

If you hit anything weird, copy the error message into a new conversation and I'll debug.

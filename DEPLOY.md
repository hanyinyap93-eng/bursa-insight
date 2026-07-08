# Launch Bursa Insight as a website — step by step

The backend now serves the frontend too, so the **whole app is one service**.
You run/deploy *one* thing and get the full website + API.

---

## PART A — Run it locally as one website (simplest)

### A1. Install dependencies into a project venv (one-time)
> Use a dedicated **virtual environment**, not the Anaconda base env. The base
> env has numpy 2 / pandas 3, which are incompatible with the pinned versions and
> spam `_ARRAY_API not found` errors. The venv below installs the exact pinned set.
```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight\backend"
& "$env:USERPROFILE\anaconda3\python.exe" -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

### A2. Start the one server (frontend + API + data sources)
```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000
```
> If you see `Errno 10048 ... address in use`, a server is already on port 8000.
> Free it: `Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }` — or just use `--port 8001`.

### A3. Open the website
Go to **http://127.0.0.1:8000** — that's the full app (no separate frontend
server needed). The API docs are at http://127.0.0.1:8000/docs.

> You no longer need the `http.server 5500` step — that was only for editing the
> frontend separately. Opening `:8000` serves everything.

### A4. (Optional) Let others on your Wi-Fi see it
Start it bound to all interfaces, then share your PC's LAN IP:
```powershell
& "$env:USERPROFILE\anaconda3\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# others on the same network open  http://<your-LAN-IP>:8000
ipconfig    # find your IPv4 address
```

---

## PART B — Deploy it to the public internet (free)

We'll use **Render.com** (free Python hosting, easiest). You get a public URL
like `https://bursa-insight.onrender.com`.

> ⚠️ **Read this first — the scraping caveat.** klsescreener and Yahoo Finance
> sometimes **block datacenter/cloud IPs**. Locally your home IP works great; on a
> cloud host quotes/charts *may* be rate-limited or empty. The app degrades
> gracefully (cached + fallback lists), but for a rock-solid public site you may
> later need a paid data feed or a proxy. For a demo/portfolio site, Render is fine.

### B1. Put the project on GitHub
1. Create a free GitHub account if you don't have one.
2. Make a new **empty** repository, e.g. `bursa-insight`.
3. From the project folder, push it (run in PowerShell):
```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight"
git init
git add .
git commit -m "Bursa Insight"
git branch -M main
git remote add origin https://github.com/<your-username>/bursa-insight.git
git push -u origin main
```
(The included `.gitignore` keeps caches/secrets out of the repo.)

### B2. Create the Render service (+ database)
1. Sign up at **https://render.com** (free, sign in with GitHub).
2. Click **New +** → **Blueprint**.
3. Pick your `bursa-insight` repo. Render reads the included **`render.yaml`** and
   pre-fills everything — **a web service AND a free Postgres database** (`bursa-db`):
   - Root dir: `backend`
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Database: `bursa-db` — its connection string is wired into the app as
     `DATABASE_URL` automatically, so **email/password accounts persist** across
     redeploys (no manual database setup).
4. Click **Apply** / **Create**. First build takes ~3–5 min (the database is
   created first, then the web service).

> **Free Postgres caveats:** Render's free database has a modest size limit and,
> on legacy free plans, may expire after ~30–90 days (you'll get an email). For a
> long-lived site, upgrade the database to a paid tier later — no code change
> needed. If you ever remove the database, the app automatically falls back to a
> local SQLite file (accounts then don't survive redeploys again).

### B3. Open your live website
Render gives you a URL like **`https://bursa-insight.onrender.com`**. Open it —
that's your public Bursa Insight. The frontend talks to the same origin
automatically (no config needed).

### B4. Updating the live site
Just push to GitHub; Render auto-redeploys:
```powershell
git add .
git commit -m "update"
git push
```

---

## Sign-in & the gated pages (GEX / Market Index / R-Appetite)

Those three pages are protected **server-side**: you sign in with Google, the
backend verifies the Google token against Google's public keys and issues its own
signed session token, and the gated API routes reject any request without a valid
one. Tampering with the browser can't unlock the real data.

Two settings make this work: a **signing secret** and the **Google client ID**.

### Go-live checklist (do this before/at deployment)

- [ ] **1. Signing secret (`BURSA_JWT_SECRET`)** — the private key that signs
      login tokens. If left unset the app uses a PUBLIC dev default and anyone can
      forge a login.
    - **Local:** already set in `backend/.env` (git-ignored — stays on your PC).
      Regenerate anytime with
      `& ".\.venv\Scripts\python.exe" -c "import secrets; print(secrets.token_urlsafe(48))"`
      and paste it after `BURSA_JWT_SECRET=` in `backend/.env`.
    - **Render:** **nothing to do** — `render.yaml` has `generateValue: true`, so
      Render creates a strong secret automatically and keeps it private.
      (Changing it later just logs existing users out — harmless.)

- [ ] **2. Register your website address with Google** — tell Google which sites
      may show your sign-in button (only you can do this; it's in your Google account).
    1. Open **https://console.cloud.google.com/apis/credentials** and sign in with
       the account that owns the OAuth client.
    2. Select the right **project** (top dropdown), then under **OAuth 2.0 Client
       IDs** click the client whose ID starts with `1005786557822-…`.
    3. Under **Authorized JavaScript origins**, make sure these exist (add with
       **+ ADD URI** if missing), then **SAVE**:
       - `http://localhost:8000` and `http://127.0.0.1:8000` (local testing — already added)
       - `https://<your-app>.onrender.com` (**add this when you deploy** — your real Render URL, **no trailing slash**)
    4. Google can take a few minutes to apply changes. Match `http`/`https`, port,
       and no trailing slash exactly, or you'll get an `origin_mismatch` error.

- [ ] **3. (Optional) Point a deployment at a different Google client** — the app
      uses a built-in default client ID, served to the page via `GET /api/config`.
      To override it without editing code, add an env var **`BURSA_GOOGLE_CLIENT_ID`**
      in the Render dashboard (Environment tab). Skip this if the default is fine.

> Two sign-in methods are available: **Google**, and **email + password** (with an
> email-confirmation step, below). If no Google client ID is configured, the modal
> just hides the Google button — email/password still works.

## Email confirmation for email+password sign-up (Gmail)

When someone signs up with an email + password, the app emails them a link to
confirm the address; they can't sign in until they click it. To send those emails
through Gmail:

> Until you do this, sign-up still works in **dev mode** — the confirmation link is
> printed to the **server logs** (Render → your service → Logs) instead of emailed.
> Good for testing; do the steps below before real users sign up.

- [ ] **1. Turn on 2-Step Verification** on the Gmail account you'll send from:
      **https://myaccount.google.com/security** → *2-Step Verification* → follow the
      prompts. (Google only allows "app passwords" once this is on.)
- [ ] **2. Create an App Password:** go to **https://myaccount.google.com/apppasswords**,
      type a name like `Bursa Insight`, click **Create**, and copy the **16-character
      password** it shows (spaces don't matter). This is NOT your normal Gmail password.
- [ ] **3. Add these Environment Variables** in the Render dashboard (your service →
      **Environment**), then **Save** (Render redeploys):
      - `BURSA_SMTP_USER` = your full Gmail address (e.g. `you@gmail.com`)
      - `BURSA_SMTP_PASSWORD` = the 16-character app password from step 2
      - `BURSA_SMTP_FROM` = your Gmail address (same as USER)
      - `BURSA_PUBLIC_URL` = your live URL, e.g. `https://bursa-insight.onrender.com`
        (so the confirmation link points at the real site)
- [ ] **4. Test:** sign up on the live site with an email you can open, then click the
      link in the email — it should confirm the account and sign you in.

> **Locally**, put the same values in `backend/.env` (see `.env.example`) if you want
> to test real emails from your PC — otherwise the link just prints to your console.
> Gmail free sending is ~500 emails/day and mail can land in spam; for higher volume
> or better deliverability, switch to a transactional provider (SendGrid/Resend) —
> same `BURSA_SMTP_*` variables, different host/user/password.

## Notes & troubleshooting

- **Free tier sleeps** after ~15 min idle; the first visit then takes ~30s to wake.
- **First sector-rotation load** is slow (~1–2 min, 13 sector scrapes) then cached.
- **Empty data on the cloud host** = the scraping caveat above. Try `/api/refresh`,
  or consider a paid data feed for production.
- **Custom domain / always-on** = Render's paid plan, or hosts like Railway/Fly.io
  (same `Procfile` works).
- **Secrets** (if you add a keyed provider later) → set them as **Environment
  Variables** in the Render dashboard, never in the repo.

# Launch Bursa Insight as a website — step by step

The backend now serves the frontend too, so the **whole app is one service**.
You run/deploy *one* thing and get the full website + API.

---

## PART A — Run it locally as one website (simplest)

### A1. Install dependencies (one-time)
```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight\backend"
& "$env:USERPROFILE\anaconda3\python.exe" -m pip install -r requirements.txt
```

### A2. Start the one server (frontend + API + data sources)
```powershell
& "$env:USERPROFILE\anaconda3\python.exe" -m uvicorn app.main:app --port 8000
```

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

### B2. Create the Render service
1. Sign up at **https://render.com** (free, sign in with GitHub).
2. Click **New +** → **Blueprint**.
3. Pick your `bursa-insight` repo. Render reads the included **`render.yaml`** and
   pre-fills everything:
   - Root dir: `backend`
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Click **Apply** / **Create**. First build takes ~3–5 min.

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

## Google Sign-In (GEX + Market Index gate)

The GEX and Market Index sections require signing in. The app uses **Google
Identity Services**, with an email sign-in as a fallback. To turn on the
"Sign in with Google" button:

1. Go to **Google Cloud Console → APIs & Services → Credentials → Create
   Credentials → OAuth client ID → Web application**.
2. Under **Authorized JavaScript origins**, add every origin the app is served
   from, e.g. `https://bursa-insight.onrender.com`, `http://localhost:8000`,
   `http://127.0.0.1:8000` (and `http://127.0.0.1:5500` if you use the static
   dev server). No redirect URI is needed for the button flow.
3. Copy the **Client ID** (`…apps.googleusercontent.com`) and paste it into the
   one meta tag near the top of `frontend/index.html`:
   `<meta name="google-client-id" content="PASTE_CLIENT_ID_HERE" />`
4. Redeploy. Leaving it empty keeps the email fallback only.

> This is a client-side gate (it controls the UI). It verifies the Google ID
> token in the browser but does not yet protect the API — for real access
> control, verify the token server-side (JWT audience = your Client ID) and
> guard the `/api/gex/*` and `/api/fbm/*` routes.

## Notes & troubleshooting

- **Free tier sleeps** after ~15 min idle; the first visit then takes ~30s to wake.
- **First sector-rotation load** is slow (~1–2 min, 13 sector scrapes) then cached.
- **Empty data on the cloud host** = the scraping caveat above. Try `/api/refresh`,
  or consider a paid data feed for production.
- **Custom domain / always-on** = Render's paid plan, or hosts like Railway/Fly.io
  (same `Procfile` works).
- **Secrets** (if you add a keyed provider later) → set them as **Environment
  Variables** in the Render dashboard, never in the repo.

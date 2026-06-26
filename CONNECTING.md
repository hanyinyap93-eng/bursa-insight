# Connecting the Data Source — step by step

Bursa Insight pulls **live, keyless** data from three public sources:

| Source | What it gives | Auth |
|---|---|---|
| **klsescreener** (`/v2/screener/quote_results`, `/v2/stocks/view/0200I`) | prices, fundamentals, KLCI constituents | none |
| **Yahoo Finance** (`yfinance`) | OHLC history for charts / Index Health / backtests | none |
| **News RSS** (The Edge, Reuters, CNBC, Yahoo…) | news feed | none |

There is **no API key, database, or connection string**. "Connecting" just means
**starting the backend**, which fetches these over HTTP on demand and caches the
results. The frontend talks only to your backend.

```
Browser (frontend :5500)  ──►  Your backend (FastAPI :8000)  ──►  klsescreener / Yahoo / RSS
```

---

## Prerequisites (one-time)

- **Python** — you already have Anaconda at `C:\Users\lukey\anaconda3\python.exe`.
  (The Windows Store `python.exe` is a non-working stub — use the Anaconda one.)

---

## Step 1 — Install the backend dependencies (one-time)

Open **PowerShell** and run:

```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight\backend"
& "$env:USERPROFILE\anaconda3\python.exe" -m pip install -r requirements.txt
```

This installs FastAPI, uvicorn, yfinance, pandas, feedparser, etc.

## Step 2 — Start the backend  (this connects the data sources)

```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight\backend"
& "$env:USERPROFILE\anaconda3\python.exe" -m uvicorn app.main:app --port 8000
```

Leave this window open — it's your live data server. You'll see request logs here.
(If port 8000 is busy, use `--port 8001` and update `const API` in
`frontend/index.html` to match.)

## Step 3 — Verify the connection

In a **second** PowerShell window (keep the server running in the first):

```powershell
# whole-market quotes from klsescreener (should print 31)
(Invoke-RestMethod "http://127.0.0.1:8000/api/quotes?lookback=1y").Count

# KLCI Index Health from live constituents (should print a % and 30 stocks)
$o = Invoke-RestMethod "http://127.0.0.1:8000/api/breadth/overview"
"health=$($o.health_pct)%  constituents=$($o.constituents)  as_of=$($o.as_of)"
```

Or just open the interactive API docs in a browser: **http://127.0.0.1:8000/docs**
and click "Try it out" on any endpoint.

## Step 4 — Start the frontend

In the second window:

```powershell
cd "C:\Users\lukey\Bursa market Screener\bursa-insight\frontend"
& "$env:USERPROFILE\anaconda3\python.exe" -m http.server 5500
```

## Step 5 — Open the app

Go to **http://127.0.0.1:5500** in your browser. The ticker tape, Index Health,
sector rotation, screener and news will all populate from live data.

> Tip: after editing files, do a **hard refresh** (`Ctrl`+`Shift`+`R`) — the simple
> `http.server` caches pages.

---

## Refreshing / troubleshooting

- **Force-refresh the cached data:** `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/refresh`
- **First sector-rotation load is slow (~1–2 min)** — it scrapes 13 sector member
  lists, then caches for an hour.
- **"parsed only 0 constituents" / empty data** — a transient klsescreener throttle.
  The app falls back to the last-good cached list, so it keeps working; just retry
  in a minute. (The scraper uses a full browser User-Agent, which is required.)
- **Chart shows an error** — make sure the backend (Step 2) is still running, then
  hard-refresh.

---

## (Optional) Adding a keyed provider later

If you ever add a provider that needs a key (e.g. Twelve Data for real-time),
put it in `backend/.env` (gitignored) and read it in `app/config.py`:

```
BURSA_TWELVEDATA_KEY=your_key_here
```

Never hardcode keys in source files.

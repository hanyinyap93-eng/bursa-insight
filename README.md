# Bursa Insight

A TradingView-style stock app for **Bursa Malaysia + S&P 500**, built around the
**Index Health** breadth-and-momentum engine from this project's Bursa Index
Health notebooks/skills. Live(ish) pricing, a sector-aware screener, sector
rotation, news, alerts and a backtester.

> MVP scope: Web app · Bursa Malaysia (KLCI + 13 sector indices) + S&P 500 index ·
> mixed data (delayed/free now, real-time tier later) · guest = limited, login = full.

## Layout

```
bursa-insight/
  backend/     FastAPI service + the Index Health engine   (see backend/README.md)
  frontend/    Single-page dashboard demo (index.html) that calls the API
```

## Run it

**1. Backend**
```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

**2. Frontend** — open `frontend/index.html` in a browser (it points at
`http://127.0.0.1:8000`). Or serve it:
```bash
cd frontend && python -m http.server 5500   # then open http://127.0.0.1:5500
```

The first sector-rotation load takes ~1–2 min (13 sectors scraped + downloaded),
then it's cached. Everything else is fast.

## What's built (MVP)

- **Index Health engine** — extracted from the notebooks; identical methodology
  (SMA breadth, new high/low, momentum, RSI → Trend Quality/Sentiment → Health %).
- **Market breadth at a glance** — headline KLCI gauge, component sub-scores,
  advancers/decliners, expanding/contracting verdict.
- **Screener** — top-N constituents correlated to the index + custom/preset screens.
- **Sector rotation heatmap** — per-sector breadth health from each sector's own
  constituents, ranked and time-sliced.
- **Index Health threshold alerts** — create/list/delete + evaluate-now.
- **Backtest a screen** — Index-Health timing strategy and signal-screen, vs buy & hold.
- **News** — aggregated local + global RSS, tagged to sectors/indices.

## Roadmap (next)

- Auth (JWT) + persistent watchlists/screens/alerts (SQLAlchemy already vendored).
- Real-time price tier for logged-in users.
- S&P 500 constituent breadth (currently index price only).
- Scheduled cache refresh + push/email alert delivery.
- Richer charts (Lightweight Charts) and a React/Next.js frontend.

# Bursa Insight — Backend (FastAPI)

A TradingView-style market app for **Bursa Malaysia + S&P 500**, built around the
**Index Health** breadth-and-momentum engine extracted from the project's Bursa
Index Health notebooks/skills.

The engine scores each constituent on four signals — **SMA breadth, new high/low,
momentum, RSI** — grouped into *Trend Quality* and *Trend Sentiment*, summed into
a normalised **Index Health %**. Methodology is identical to the notebooks, so API
numbers match the reports.

## Quick start

```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/docs
```

Offline self-check (no network): `python smoke_test.py`

## Architecture

```
app/
  main.py            FastAPI app + all routes
  config.py          settings (env: BURSA_*)
  schemas.py         pydantic request models
  core/
    index_health.py  ENGINE — constituents scrape, yfinance download (cached),
                     compute_health (breadth %), per-sector breadth, indicators
    service.py       in-memory TTL cache over the engine (compute once, serve many)
    screener.py      correlated-constituents + custom screen + presets
    breadth.py       'market breadth at a glance' overview, health series,
                     sector rotation heatmap
    backtest.py      backtest a screen (health-threshold timing + signal screen)
    alerts.py        Index Health threshold alerts (in-memory store)
    news.py          local + global RSS aggregator, tagged to sectors/indices
_cache/              parquet price cache (auto-created)
```

Data sources (MVP, "mixed" tier): **yfinance** (delayed prices) + **klsescreener**
(live constituent scrape). Real-time tier is a later upgrade.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/indices` | Index registry (KLCI, SPX) |
| GET | `/api/breadth/overview` | **Market breadth at a glance** (gauge, components, verdict) |
| GET | `/api/breadth/series` | Index Health time series + index overlay (for charts) |
| GET | `/api/sectors/rotation` | **Sector rotation** heatmap + ranked snapshot |
| GET | `/api/screener/correlated` | Top-N constituents correlated to the index |
| GET | `/api/screener/presets` | Built-in preset screens |
| POST | `/api/screener/run` | Run a custom screen |
| GET | `/api/news` | Aggregated local + global news (filter by scope/sector/index) |
| GET/POST/DELETE | `/api/alerts` | Manage Index Health threshold alerts |
| GET | `/api/alerts/evaluate` | Which alerts are firing now |
| POST | `/api/backtest/health` | **Backtest** the Index-Health timing strategy |
| POST | `/api/backtest/screen` | Backtest an equal-weight signal screen |
| GET | `/api/stock/{ticker}` | Per-stock OHLC + SMA/RSI (e.g. `1155.KL`) |
| POST | `/api/refresh` | Force-refresh caches |

### Examples

```bash
# Market breadth overview
curl "http://127.0.0.1:8000/api/breadth/overview"

# Top 10 stocks correlated to the KLCI over the last 60 bars
curl "http://127.0.0.1:8000/api/screener/correlated?top=10&window=60"

# Custom screen: oversold names highly correlated to the index
curl -X POST http://127.0.0.1:8000/api/screener/run \
  -H "Content-Type: application/json" \
  -d '{"rsi_oversold": true, "min_correlation": 0.4}'

# Alert: KLCI Index Health crosses below -10%
curl -X POST http://127.0.0.1:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{"metric":"index_health","op":"cross_below","threshold":-10,"label":"KLCI weak"}'

# Backtest: long KLCI when health>0, flat when health<-10
curl -X POST http://127.0.0.1:8000/api/backtest/health \
  -H "Content-Type: application/json" -d '{"entry":0,"exit":-10}'
```

## Priority custom features (built)

- **Index Health threshold alerts** — `core/alerts.py`, `/api/alerts*`
- **Sector rotation heatmap** — `core/breadth.py:sector_rotation`, `/api/sectors/rotation`
- **Backtest a screen** — `core/backtest.py`, `/api/backtest/*`
- **Market breadth at a glance** — `core/breadth.py:breadth_overview`, `/api/breadth/overview`

## Notes & next steps

- **Sector rotation** computes each sector's health from its *constituents* (scraped
  from each klsescreener sector page) because Yahoo does not carry the Bursa sector
  index symbols. First call is slow (~1–2 min, 13 sectors); results are cached 1h.
- **Auth tiers** (guest = limited charts, login = full) are stubbed in config; wire
  JWT + a users/watchlists table next (SQLAlchemy is in requirements).
- **Scheduled refresh + alert dispatch**: run `/api/refresh` then
  `/api/alerts/evaluate` on a cron and push notifications for the `firing` list.
- **S&P 500**: index price is served; constituent breadth for SPX is a later add.

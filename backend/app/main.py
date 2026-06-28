"""
Bursa Insight API — FastAPI app.

Endpoints (MVP):
  GET  /                         - service info
  GET  /health                   - liveness
  GET  /api/indices              - index registry
  GET  /api/breadth/overview     - 'market breadth at a glance' (KLCI)
  GET  /api/breadth/series       - Index Health time series + index overlay
  GET  /api/sectors/rotation     - sector rotation heatmap + ranked snapshot
  GET  /api/screener/correlated  - top-N constituents correlated to the index
  GET  /api/screener/presets     - preset screen definitions
  POST /api/screener/run         - run a custom screen
  GET  /api/news                 - aggregated local + global market news
  GET  /api/alerts               - list alerts
  POST /api/alerts               - create an alert
  DELETE /api/alerts/{id}        - delete an alert
  GET  /api/alerts/evaluate      - evaluate alerts now (which are firing)
  POST /api/backtest/health      - backtest the Index Health timing strategy
  POST /api/backtest/screen      - backtest an equal-weight signal screen
  GET  /api/stock/{ticker}       - per-stock OHLC + indicators for charts
  POST /api/refresh              - force-refresh caches
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .core import alerts as alerts_mod
from .core import backtest as bt
from .core import breadth as breadth_mod
from .core import index_health as ih
from .core import news as news_mod
from .core import screener as screener_mod
from .core import service
from .schemas import (
    AlertRequest, BacktestHealthRequest, BacktestScreenRequest, ScreenRequest,
)

app = FastAPI(title=settings.app_name, version=settings.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins == "*" else settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/info")
def info():
    return {"app": settings.app_name, "version": settings.version,
            "docs": "/docs", "indices": list(service.INDEXES)}


@app.get("/health")
def liveness():
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Indices & breadth
# --------------------------------------------------------------------------- #
@app.get("/api/indices")
def indices():
    return [{"id": k, **v} for k, v in service.INDEXES.items()]


@app.get("/api/breadth/overview")
def breadth_overview(index: str = "KLCI", lookback: str = "1y"):
    try:
        return breadth_mod.breadth_overview(index, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"breadth compute failed: {exc}")


@app.get("/api/breadth/series")
def breadth_series(index: str = "KLCI", lookback: str = "1y"):
    try:
        return breadth_mod.health_series(index, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"series compute failed: {exc}")


@app.get("/api/search")
def search(q: str, limit: int = 12):
    """Search the whole Bursa market by stock code or ticker name/symbol."""
    from .core import klse_quotes
    query = (q or "").strip().upper()
    if not query:
        return []
    try:
        quotes = klse_quotes.get_quotes()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"search failed: {exc}")
    import re as _re
    clean = lambda s: _re.sub(r"\s*\[[^\]]*\]\s*$", "", s or "").strip()
    out = []
    for code, rec in quotes.items():
        name = (rec.get("name") or "").upper()
        digits = code.upper()
        if query == digits or digits.startswith(query) or query in name:
            out.append({
                "code": code, "name": clean(rec.get("name")), "ticker": f"{code}.KL",
                "last": rec.get("last"), "chg_pct": rec.get("chg_pct"),
                "sector": rec.get("sector"),
            })
    out.sort(key=lambda r: (
        r["code"].upper() != query,                       # exact code first
        not (r["name"] or "").upper().startswith(query),   # name prefix next
        not r["code"].upper().startswith(query),           # code prefix
        r["name"] or "",
    ))
    return out[:limit]


@app.get("/api/quotes")
def quotes(index: str = "KLCI", lookback: str = "1y"):
    try:
        return breadth_mod.quotes(index, lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"quotes failed: {exc}")


@app.get("/api/sector/{key}")
def sector_detail(key: str, lookback: str = "1y"):
    try:
        return breadth_mod.sector_detail(key, lookback)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"sector detail failed: {exc}")


@app.get("/api/sectors/rotation")
def sector_rotation(lookback: str = "1y"):
    try:
        return breadth_mod.sector_rotation(lookback)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"sector rotation failed: {exc}")


# --------------------------------------------------------------------------- #
# Screener
# --------------------------------------------------------------------------- #
@app.get("/api/screener/correlated")
def screener_correlated(index: str = "KLCI", lookback: str = "1y",
                        top: int = Query(10, ge=1, le=100),
                        window: int = Query(None)):
    try:
        result = service.get_health(index, lookback)
        return screener_mod.correlated_constituents(result, top=top, window=window)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"correlation screen failed: {exc}")


@app.get("/api/screener/presets")
def screener_presets():
    return {name: vars(c) for name, c in screener_mod.PRESETS.items()}


@app.post("/api/screener/run")
def screener_run(req: ScreenRequest):
    try:
        result = service.get_health(req.index, req.lookback)
        criteria = screener_mod.ScreenCriteria(
            above_sma=req.above_sma, momentum_up=req.momentum_up,
            rsi_overbought=req.rsi_overbought, rsi_oversold=req.rsi_oversold,
            new_high=req.new_high, new_low=req.new_low,
            min_correlation=req.min_correlation, min_return_pct=req.min_return_pct,
            max_return_pct=req.max_return_pct, sectors=req.sectors,
            healthy_only=req.healthy_only,
        )
        rows = screener_mod.screen(result, criteria, corr_window=req.corr_window)
        return {"count": len(rows), "results": rows}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"screen failed: {exc}")


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
@app.get("/api/news")
def news(scope: str = None, sector: str = None, index: str = None,
         limit: int = Query(50, ge=1, le=200)):
    return news_mod.get_news(scope=scope, sector=sector, index=index, limit=limit)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
@app.get("/api/alerts")
def list_alerts():
    return [a.to_dict() for a in alerts_mod.list_alerts()]


@app.post("/api/alerts")
def create_alert(req: AlertRequest):
    try:
        a = alerts_mod.create_alert(req.metric, req.op, req.threshold, req.label)
        return a.to_dict()
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: int):
    if not alerts_mod.delete_alert(alert_id):
        raise HTTPException(404, "alert not found")
    return {"deleted": alert_id}


@app.get("/api/alerts/evaluate")
def evaluate_alerts():
    try:
        return {"firing": alerts_mod.evaluate()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"evaluate failed: {exc}")


# --------------------------------------------------------------------------- #
# Backtests
# --------------------------------------------------------------------------- #
@app.post("/api/backtest/health")
def backtest_health(req: BacktestHealthRequest):
    try:
        return bt.backtest_health_threshold(
            req.index, req.lookback, req.entry, req.exit_, req.cost_bps)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"backtest failed: {exc}")


@app.post("/api/backtest/screen")
def backtest_screen(req: BacktestScreenRequest):
    try:
        return bt.backtest_signal_screen(
            req.index, req.lookback, req.require_above_sma,
            req.require_momentum_up, req.cost_bps)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"backtest failed: {exc}")


# --------------------------------------------------------------------------- #
# Per-stock chart data
# --------------------------------------------------------------------------- #
@app.get("/api/stock/{ticker}")
def stock(ticker: str, lookback: str = "1y"):
    """OHLC + SMA/RSI for a single stock (charts). ticker e.g. '1155.KL'."""
    import yfinance as yf
    try:
        # Ticker.history() returns clean single-level OHLCV columns, avoiding the
        # MultiIndex/duplicate-column issues that yf.download can produce.
        raw = yf.Ticker(ticker).history(period=lookback, interval="1d", auto_adjust=False)
        if raw is None or raw.empty:
            raise HTTPException(404, f"no data for {ticker}")
        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        close = raw["Close"]
        out = {
            "ticker": ticker,
            "dates": [str(d.date()) for d in raw.index],
            "open": [round(float(x), 4) for x in raw["Open"]],
            "high": [round(float(x), 4) for x in raw["High"]],
            "low": [round(float(x), 4) for x in raw["Low"]],
            "close": [round(float(x), 4) for x in close],
            "volume": [int(x) if not _isnan(x) else 0 for x in raw["Volume"]],
            "sma10": [_r(x) for x in ih.sma(close, 10)],
            "rsi10": [_r(x) for x in ih.rsi(close, 10)],
        }
        return out
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"stock fetch failed: {exc}")


@app.post("/api/refresh")
def refresh():
    service.refresh()
    return {"status": "refreshed"}


def _isnan(x):
    try:
        return x != x
    except Exception:  # noqa: BLE001
        return True


def _r(x):
    return None if _isnan(x) else round(float(x), 4)


# --------------------------------------------------------------------------- #
# Serve the frontend (single deployable unit). Mounted LAST so the /api routes
# above always take precedence. Visiting "/" serves frontend/index.html.
# --------------------------------------------------------------------------- #
from pathlib import Path  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")

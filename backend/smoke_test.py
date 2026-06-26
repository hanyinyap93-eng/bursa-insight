"""Offline smoke test: engine math + app import (no network required)."""
import sys
import numpy as np
import pandas as pd

from app.core import index_health as ih
from app.core import screener, breadth, backtest

# --- synthetic price panel: 200 business days, 6 fake constituents + index ---
rng = np.random.default_rng(42)
dates = pd.bdate_range("2024-01-01", periods=200)
meta = pd.DataFrame([
    {"Ticker": "1155.KL", "Code": "1155", "Name": "MAYBANK", "Sector": "Financial Services", "Sector Abbrev": "FIN"},
    {"Ticker": "1023.KL", "Code": "1023", "Name": "CIMB", "Sector": "Financial Services", "Sector Abbrev": "FIN"},
    {"Ticker": "5347.KL", "Code": "5347", "Name": "TENAGA", "Sector": "Utilities", "Sector Abbrev": "UTL"},
    {"Ticker": "4707.KL", "Code": "4707", "Name": "NESTLE", "Sector": "Consumer Products & Services", "Sector Abbrev": "CON"},
    {"Ticker": "5183.KL", "Code": "5183", "Name": "PCHEM", "Sector": "Industrial Products & Services", "Sector Abbrev": "IND"},
    {"Ticker": "2445.KL", "Code": "2445", "Name": "KLK", "Sector": "Plantation", "Sector Abbrev": "PLT"},
])
cols = {}
for tkr in meta["Ticker"]:
    steps = rng.normal(0.0003, 0.012, len(dates))
    cols[tkr] = 5.0 * np.exp(np.cumsum(steps))
# index = average of constituents (so correlation is high & positive)
panel = pd.DataFrame(cols, index=dates)
panel[ih.INDEX_SYMBOL_KLCI] = panel.mean(axis=1) * 200

cfg = ih.BreadthConfig(index_symbol=ih.INDEX_SYMBOL_KLCI, cache_dir=None)
result = ih.compute_health(cfg, tickers_meta=meta, close=panel.copy())

assert result.health_pct.notna().sum() > 100, "health series too short"
assert set(ih.HEALTH_COMPONENTS) == set(result.component_pct.columns), "components mismatch"
print(f"[OK] compute_health: latest health = {result.health_pct.iloc[-1]:.1f}%  "
      f"constituents={result.close.shape[1]}  sectors={result.sector_health_pct.shape[1]}")

# screener: correlated constituents
corr = screener.correlated_constituents(result, top=3)
assert len(corr) == 3 and all("correlation" in r for r in corr), "corr screen failed"
print(f"[OK] correlated top3: " + ", ".join(f"{r['name']}={r['correlation']}" for r in corr))

# screener: custom screen
rows = screener.screen(result, screener.ScreenCriteria(above_sma=True))
print(f"[OK] screen(above_sma=True): {len(rows)} matches")

# breadth overview (uses service cache -> patch service to use our result)
from app.core import service
service._cache.set("health:KLCI:1y", result)
ov = breadth.breadth_overview("KLCI")
assert "health_pct" in ov and "components" in ov, "overview payload bad"
print(f"[OK] breadth_overview: verdict={ov['verdict']} health={ov['health_pct']}% "
      f"trend={ov['trend_5d']} above_sma={ov['pct_above_sma']}%")

# backtest: health threshold
service._cache.set("health:KLCI:1y", result)
res = backtest.backtest_health_threshold("KLCI", entry=0.0, exit_=-10.0)
assert "equity" in res and "stats" in res, "backtest payload bad"
print(f"[OK] backtest_health: total={res['stats']['total_return_pct']}% "
      f"vs buy&hold={res['buy_hold_stats']['total_return_pct']}% trades={res['n_trades']}")

# backtest: signal screen
res2 = backtest.backtest_signal_screen("KLCI")
print(f"[OK] backtest_screen: total={res2['stats']['total_return_pct']}% "
      f"avg_holdings={res2['avg_holdings']}")

# alerts
from app.core import alerts
a = alerts.create_alert("index_health", "below", -5.0, label="KLCI weak")
fired = alerts.evaluate()
print(f"[OK] alerts: created id={a.id}, firing now={len(fired)}")

# app import
from app.main import app
routes = [r.path for r in app.routes if hasattr(r, "path")]
need = ["/api/breadth/overview", "/api/sectors/rotation", "/api/screener/run",
        "/api/news", "/api/alerts", "/api/backtest/health", "/api/stock/{ticker}"]
missing = [r for r in need if r not in routes]
assert not missing, f"missing routes: {missing}"
print(f"[OK] FastAPI app imports: {len(routes)} routes, all key endpoints present")

print("\nALL SMOKE TESTS PASSED")

"""
'Market breadth at a glance' overview — Bursa Insight.

Condenses the Index Health engine into a single dashboard payload: the headline
KLCI health gauge, the four component sub-scores, advancers/decliners by signal,
and a one-line verdict (expanding / contracting). Plus the sector-rotation
snapshot (latest per-sector index health, ranked).
"""
from __future__ import annotations

import pandas as pd

from . import index_health as ih
from . import service


def _trend(series: pd.Series, n: int = 5) -> str:
    s = series.dropna()
    if len(s) < n + 1:
        return "flat"
    delta = s.iloc[-1] - s.iloc[-1 - n]
    return "expanding" if delta > 0.5 else "contracting" if delta < -0.5 else "flat"


def breadth_overview(index: str = "KLCI", lookback: str = "1y", corr_window: str = None) -> dict:
    """Single-payload market-breadth snapshot for the overview page."""
    result = service.get_health(index, lookback)
    warm = result.cfg.warmup
    health_pct = result.health_pct.iloc[warm:]
    cur = service.latest(health_pct)
    prev = float(health_pct.iloc[-2]) if len(health_pct) > 1 else cur

    # advancers / decliners on the latest bar, by component
    sig = result.signals
    last = {}
    for comp in ih.HEALTH_COMPONENTS:
        up = int(sig[comp]["up"].iloc[-1].sum())
        down = int(sig[comp]["drop"].iloc[-1].sum())
        last[comp] = {"bullish": up, "bearish": down}

    components = {
        comp: round(float(result.component_pct[comp].iloc[-1]), 1)
        for comp in ih.HEALTH_COMPONENTS
    }
    n_const = result.close.shape[1]
    above_sma = int(sig["sma"]["up"].iloc[-1].sum())

    from . import screener
    win = _CORR_WINDOW.get(corr_window) if corr_window else None
    correlated = [
        {"code": r["code"], "name": r["name"],
         "correlation": r["correlation"], "return_pct": r["return_pct"]}
        for r in screener.correlated_constituents(result, top=6, window=win)
    ]

    return {
        "index": index,
        "correlated": correlated,            # top stocks correlated to the index
        "corr_window": corr_window or lookback,
        "as_of": str(result.health_pct.index[-1].date()),
        "constituents": n_const,
        "health_pct": round(cur, 1) if cur is not None else None,
        "health_prev_pct": round(prev, 1) if prev is not None else None,
        "verdict": "expanding" if cur is not None and cur > prev else "contracting",
        "trend_5d": _trend(health_pct),
        "pct_above_sma": round(above_sma / n_const * 100, 1) if n_const else None,
        "components": components,            # -100..100 per component
        "component_breadth": last,           # bullish/bearish counts per component
        "index_level": service.latest(result.index_price) if result.index_price is not None else None,
        "spark": [round(float(x), 1) for x in health_pct.tail(60)],  # health sparkline
        "index_spark": (
            [round(float(x), 2)
             for x in result.index_price.reindex(health_pct.index).ffill().tail(60)]
            if result.index_price is not None else None
        ),  # KLCI index price line (same 60-bar window as the health sparkline)
    }


SECTOR_DISPLAY = {
    "CONSUMER": "Consumer P&S", "IND-PROD": "Industrial P&S", "CONSTRUCTN": "Construction",
    "TECHNOLOGY": "Technology", "FINANCE": "Financial Services", "PROPERTIES": "Property",
    "PLANTATION": "Plantation", "REIT": "REIT", "ENERGY": "Energy", "HEALTH": "Healthcare",
    "TELECOMMUNICATIONS": "Telco & Media", "TRANSPORTATION": "Transport & Logistics",
    "UTILITIES": "Utilities",
}


_CORR_WINDOW = {"1mo": 21, "3mo": 63, "6mo": 126, "1y": 252, "2y": 504}


def stock_index_correlations(ticker: str, lookback: str = "6mo") -> dict:
    """Correlation of a stock's daily returns to every index (KLCI + sectors).

    Computed over the trailing window given by `lookback`. Returns the list
    sorted by correlation (descending)."""
    import pandas as pd
    cfg = service._cfg("KLCI", "2y")
    sclose = ih.download_prices([ticker], cfg)
    if ticker in sclose.columns:
        s = sclose[ticker]
    else:
        s = sclose.iloc[:, 0]
    panel = service.get_index_price_panel()
    if panel is None or panel.empty:
        return {"ticker": ticker, "lookback": lookback, "correlations": []}
    df = panel.copy()
    df["__STOCK__"] = s
    df = df.dropna(subset=["__STOCK__"]).sort_index()
    n = _CORR_WINDOW.get(lookback, 126)
    df = df.tail(n + 1)
    rets = df.pct_change()
    sret = rets["__STOCK__"]
    names = {**SECTOR_DISPLAY, "KLCI": "FBM KLCI"}
    out = []
    for col in panel.columns:
        c = rets[col].corr(sret)
        if c == c:  # not NaN
            out.append({"index": col, "name": names.get(col, col),
                        "correlation": round(float(c), 3)})
    out.sort(key=lambda r: r["correlation"], reverse=True)
    return {"ticker": ticker, "lookback": lookback, "correlations": out}


def index_ohlc(key: str, lookback: str = "5y") -> dict:
    """OHLC for an index so it can be charted like a stock.

    KLCI uses the real ^KLSE OHLC; a sector uses an equal-weight OHLC built from
    its constituents (each normalised to 100 at the window start). Cached.
    """
    import pandas as pd
    from . import klse_prices
    cache_key = f"indexohlc:{key}:{lookback}"
    cached = service._cache.get(cache_key, ttl=service.TTL_SECONDS)
    if cached is not None:
        return cached

    if key == "KLCI":
        raw = klse_prices.history(ih.INDEX_SYMBOL_KLCI, lookback=lookback)
        o, h, l, c, v = raw["Open"], raw["High"], raw["Low"], raw["Close"], raw["Volume"]
        name, nconst = "FBM KLCI", 30
    else:
        code = ih.SECTOR_INDEX_CODES.get(key)
        if not code:
            raise ValueError(f"unknown index '{key}'")
        meta = ih.get_index_tickers(code, 3, 4)
        if meta.empty:
            raise ValueError(f"no constituents for '{key}'")
        tickers = meta["Ticker"].tolist()
        # eq-weight OHLC: pull each constituent's OHLC (klsescreener), normalise
        from concurrent.futures import ThreadPoolExecutor
        fields = {"Open": [], "High": [], "Low": [], "Close": []}
        used = 0

        def _ohlc(t):
            try:
                return klse_prices.history(t, lookback=lookback)
            except Exception:  # noqa: BLE001
                return None
        with ThreadPoolExecutor(max_workers=8) as ex:
            subs = list(ex.map(_ohlc, tickers))
        for sub in subs:
            if sub is None or sub.empty:
                continue
            base = sub["Close"].ffill().bfill().iloc[0]
            if not base or base != base:
                continue
            for f in fields:
                fields[f].append(sub[f] / base * 100.0)
            used += 1
        if not fields["Close"]:
            raise ValueError(f"no price data for '{key}'")
        o = pd.concat(fields["Open"], axis=1).mean(axis=1)
        h = pd.concat(fields["High"], axis=1).mean(axis=1)
        l = pd.concat(fields["Low"], axis=1).mean(axis=1)
        c = pd.concat(fields["Close"], axis=1).mean(axis=1)
        v, name, nconst = None, SECTOR_DISPLAY.get(key, key) + " (eq-wt)", used

    df = pd.DataFrame({"o": o, "h": h, "l": l, "c": c}).dropna().sort_index()
    sma = df["c"].rolling(10, min_periods=10).mean()
    vol = ([int(x) if x == x else 0 for x in v.reindex(df.index)] if v is not None
           else [0] * len(df))
    out = {
        "ticker": key, "name": name, "is_index": True, "constituents": nconst,
        "dates": [str(d.date()) for d in df.index],
        "open": [round(float(x), 4) for x in df["o"]],
        "high": [round(float(x), 4) for x in df["h"]],
        "low": [round(float(x), 4) for x in df["l"]],
        "close": [round(float(x), 4) for x in df["c"]],
        "volume": vol,
        "sma10": [None if x != x else round(float(x), 4) for x in sma],
        "fundamentals": {},
    }
    service._cache.set(cache_key, out)
    return out


def _sector_data(key: str, lookback: str):
    """Heavy per-sector data (constituents + prices), cached so window changes
    don't trigger a re-scrape. Returns (meta, HealthResult)."""
    code = ih.SECTOR_INDEX_CODES.get(key)
    if not code:
        raise ValueError(f"unknown sector '{key}'")
    cache_key = f"sectordata:{key}:{lookback}"
    cached = service._cache.get(cache_key, ttl=service.TTL_SECONDS * 2)
    if cached is not None:
        return cached
    cfg = service._cfg("KLCI", lookback)
    meta = ih.get_index_tickers(code, cfg.max_retries, cfg.retry_wait)
    if meta.empty:
        raise ValueError(f"no constituents for sector '{key}'")
    close = ih.download_prices(meta["Ticker"].tolist(), cfg)
    res = ih.compute_health(cfg, tickers_meta=meta, close=close)
    service._cache.set(cache_key, (meta, res))
    return meta, res


def sector_detail(key: str, lookback: str = "1y", corr_window: str = None) -> dict:
    """Index Health + index-price proxy for one Bursa sector (clicked in the UI).

    Health = breadth over the sector's constituents; the index price is an
    equal-weighted price index of those constituents. The top-correlated list is
    computed over `corr_window` (1mo..2y) so it can share the UI's lookback.
    """
    meta, res = _sector_data(key, lookback)
    warm = min(res.cfg.warmup, max(len(res.health_pct) - 2, 0))
    hp = res.health_pct.iloc[warm:]

    # equal-weight price index, normalised to 100 at the first valid bar
    base = res.close.ffill().bfill().iloc[0]
    eq_full = (res.close.divide(base) * 100.0).mean(axis=1)
    eq = eq_full.iloc[warm:]

    # stocks most correlated to this sector's index, over the chosen window
    win = _CORR_WINDOW.get(corr_window) if corr_window else None
    rets = res.close.pct_change()
    iret = eq_full.pct_change()
    if win:
        rets = rets.tail(win + 1)
        iret = iret.tail(win + 1)
    mi = meta.set_index("Ticker")
    correlated = []
    for tkr in res.close.columns:
        c = rets[tkr].corr(iret)
        if c != c:  # NaN
            continue
        col = res.close[tkr].dropna()
        ret = (col.iloc[-1] / col.iloc[0] - 1) * 100 if len(col) > 1 else None
        nm = mi.loc[tkr]["Name"] if tkr in mi.index else tkr
        correlated.append({"code": tkr.split(".")[0], "name": nm,
                           "correlation": round(float(c), 3),
                           "return_pct": round(float(ret), 2) if ret is not None else None})
    correlated.sort(key=lambda r: r["correlation"], reverse=True)
    correlated = correlated[:6]

    cur = float(hp.iloc[-1])
    prev = float(hp.iloc[-2]) if len(hp) > 1 else cur
    n = res.close.shape[1]
    above_sma = int(res.signals["sma"]["up"].iloc[-1].sum())

    # Use the SAME eq-wt index as the chart (index_ohlc, 5y-anchored) so the
    # panel's index level matches the chart. Fall back to the local 1y series.
    try:
        oh = index_ohlc(key, "5y")
        index_level = round(oh["close"][-1], 2)
        index_spark = [round(x, 2) for x in oh["close"][-60:]]
    except Exception:  # noqa: BLE001
        index_level = round(float(eq.iloc[-1]), 2)
        index_spark = [round(float(x), 2) for x in eq.reindex(hp.index).ffill().tail(60)]

    return {
        "key": key, "name": SECTOR_DISPLAY.get(key, key),
        "correlated": correlated, "corr_window": corr_window or lookback,
        "as_of": str(hp.index[-1].date()), "constituents": n,
        "health_pct": round(cur, 1),
        "verdict": "expanding" if cur > prev else "contracting",
        "trend_5d": _trend(hp),
        "pct_above_sma": round(above_sma / n * 100, 1) if n else None,
        "spark": [round(float(x), 1) for x in hp.tail(60)],
        "index_spark": index_spark,
        "index_level": index_level,
        "is_proxy": True,
    }


def health_series(index: str = "KLCI", lookback: str = "1y") -> dict:
    """Time series for the Index Health chart (health % + index overlay)."""
    result = service.get_health(index, lookback)
    warm = result.cfg.warmup
    h = result.health_pct.iloc[warm:]
    payload = {
        "index": index,
        "dates": [str(d.date()) for d in h.index],
        "health_pct": [round(float(x), 2) for x in h],
        "components": {
            comp: [round(float(x), 2) for x in result.component_pct[comp].iloc[warm:]]
            for comp in ih.HEALTH_COMPONENTS
        },
    }
    if result.index_price is not None:
        idx = result.index_price.reindex(h.index).ffill()
        payload["index_level"] = [None if pd.isna(x) else round(float(x), 2) for x in idx]
    return payload


def quotes(index: str = "KLCI", lookback: str = "1y") -> list[dict]:
    """Watchlist/ticker quotes for the index constituents.

    Live-ish prices + fundamentals come from klsescreener (one whole-market
    scrape, cached ~3 min). Any code the scrape misses falls back to the last
    two closes of the cached Yahoo panel, so the endpoint always returns data.
    Returns the index row first, then constituents sorted by |chg%|.
    """
    from . import klse_quotes

    result = service.get_health(index, lookback)
    meta = result.meta.set_index("Ticker")
    try:
        live = klse_quotes.get_quotes()
    except Exception:  # noqa: BLE001 - degrade to Yahoo-only if scrape fails
        live = {}

    rows = []
    # index row (from the Yahoo index series; quote_results is stocks only)
    if result.index_price is not None and len(result.index_price) > 1:
        ip = result.index_price.dropna()
        last, prev = float(ip.iloc[-1]), float(ip.iloc[-2])
        rows.append({"code": index, "name": service.INDEXES.get(index, {}).get("name", index),
                     "sector": "Index", "last": round(last, 2),
                     "chg": round(last - prev, 2),
                     "chg_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
                     "is_index": True})

    for tkr in result.close.columns:
        code = tkr.split(".")[0]
        m = meta.loc[tkr] if tkr in meta.index else None
        q = live.get(code.zfill(4))
        row = {
            "code": code, "ticker": tkr,
            "name": (m["Name"] if m is not None else tkr),
            "sector": (m["Sector"] if m is not None else "Unknown"),
            "is_index": False, "source": "klse" if q and q.get("last") else "yahoo",
        }
        if q and q.get("last"):
            row.update({
                "last": q["last"], "chg": q.get("change"), "chg_pct": q.get("chg_pct") or 0.0,
                "volume": q.get("volume"), "pe": q.get("pe"), "dy": q.get("dy"),
                "eps": q.get("eps"), "market_cap": q.get("market_cap"),
            })
        else:
            col = result.close[tkr].dropna()
            if len(col) < 2:
                continue
            last, prev = float(col.iloc[-1]), float(col.iloc[-2])
            row.update({"last": round(last, 3), "chg": round(last - prev, 3),
                        "chg_pct": round((last / prev - 1) * 100, 2) if prev else 0.0})
        rows.append(row)

    head = [r for r in rows if r.get("is_index")]
    body = sorted([r for r in rows if not r.get("is_index")],
                  key=lambda r: abs(r.get("chg_pct") or 0), reverse=True)
    return head + body


def sector_rotation(lookback: str = "1y") -> dict:
    """Latest per-sector breadth health, ranked — the sector rotation heatmap.

    Consumes the (Date x sector) health-% DataFrame from the service (breadth
    over each sector's own constituents). Returns a ranked latest snapshot plus
    a downsampled matrix for the heatmap.
    """
    ih_df = service.get_sector_health(lookback)
    if ih_df is None or ih_df.empty:
        return {"as_of": None, "ranked": [], "heatmap": {"dates": [], "sectors": [], "values": {}}}
    warm = min(service._cfg().warmup, max(len(ih_df) - 2, 0))
    ih_df = ih_df.iloc[warm:].dropna(how="all")

    latest_row = ih_df.iloc[-1]
    prev_row = ih_df.iloc[-6] if len(ih_df) > 6 else ih_df.iloc[0]
    ranked = []
    for sec in ih_df.columns:
        cur = latest_row[sec]
        if pd.isna(cur):
            continue
        cur = float(cur)
        chg = cur - float(prev_row[sec]) if not pd.isna(prev_row[sec]) else 0.0
        ranked.append({
            "sector": sec,
            "index_health": round(cur, 1),
            "change_5d": round(chg, 1),
            "state": "leading" if cur > 0 and chg >= 0 else
                     "improving" if chg > 0 else
                     "weakening" if cur > 0 else "lagging",
        })
    ranked.sort(key=lambda r: r["index_health"], reverse=True)

    # downsampled matrix for the heatmap (cap columns to keep payload small)
    dates = ih_df.index
    step = max(1, len(dates) // 120)
    cols = dates[::step]
    # NaN -> None: Starlette's JSON encoder rejects NaN (allow_nan=False)
    matrix = {
        sec: [None if pd.isna(v) else round(float(v), 1) for v in ih_df[sec].iloc[::step]]
        for sec in ih_df.columns
    }
    return {
        "as_of": str(dates[-1].date()),
        "ranked": ranked,
        "heatmap": {
            "dates": [str(d.date()) for d in cols],
            "sectors": list(ih_df.columns),
            "values": matrix,
        },
    }

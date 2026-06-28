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


def breadth_overview(index: str = "KLCI", lookback: str = "1y") -> dict:
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

    return {
        "index": index,
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

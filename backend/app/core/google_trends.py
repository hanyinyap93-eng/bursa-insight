"""
Google Trends correlation for the Risk Appetite page.

FREE / unofficial route via ``pytrends``. Google rate-limits datacenter IPs
(Render), so every ``(keyword, lookback)`` result is cached for 6h and any
failure degrades gracefully to a 503 the frontend shows as "try again later".

For a keyword we pull Google Trends "interest over time" (geo=MY) and correlate
its WEEKLY % change against the weekly % change of each index close over the
chosen lookback (3mo / 6mo / 1y / 2y):

    FBM KLCI   0200I
    FBM Mid 70 0863I
    FBM EMAS   0865I   (verified klsescreener UDF code)
    FBM ACE    0871I
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd

from . import klse_prices

# Verified klsescreener TradingView-UDF index codes (see klse_prices.history).
INDICES = {
    "FBM KLCI":   "0200I",
    "FBM Mid 70": "0863I",
    "FBM EMAS":   "0865I",
    "FBM ACE":    "0871I",
}

# lookback -> (pytrends timeframe, klse_prices lookback, window in days)
# pytrends has no native 6-month preset, so 6mo/1y both pull "today 12-m"
# (weekly granularity) and we trim by the window; 2y uses the 5-y weekly feed.
_LOOKBACKS = {
    "3mo": ("today 3-m",  "6mo", 93),
    "6mo": ("today 12-m", "1y",  186),
    "1y":  ("today 12-m", "1y",  372),
    "2y":  ("today 5-y",  "2y",  744),
}
GEO = "MY"                      # Malaysian search interest (Bursa context)

_CACHE: dict = {}              # (keyword.lower(), lookback) -> (ts, payload)
_CACHE_TTL = 6 * 3600          # 6h — spare the rate limit; Trends updates slowly
_LOCK = threading.Lock()


def _trends_weekly(keyword: str, timeframe: str) -> pd.Series:
    """Weekly Google Trends interest (0-100) for one keyword. Raises on failure."""
    from pytrends.request import TrendReq

    py = TrendReq(hl="en-US", tz=480)          # tz 480 = UTC+8 (Malaysia)
    py.build_payload([keyword], timeframe=timeframe, geo=GEO)
    df = py.interest_over_time()
    if df is None or df.empty or keyword not in df.columns:
        raise RuntimeError("no Google Trends data for this keyword")
    if "isPartial" in df.columns:
        df = df[~df["isPartial"].astype(bool)]  # drop the incomplete last bucket
    s = df[keyword].astype(float)
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s.resample("W").mean().dropna()


def correlate(keyword: str, lookback: str = "1y") -> dict:
    """Correlate a keyword's weekly search interest with the four FBM indices."""
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("keyword is required")
    if lookback not in _LOOKBACKS:
        lookback = "1y"

    ck = (keyword.lower(), lookback)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(ck)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    timeframe, price_lb, win = _LOOKBACKS[lookback]
    trend = _trends_weekly(keyword, timeframe)
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=win)
    trend = trend[trend.index >= cutoff]
    if len(trend) < 6:
        raise RuntimeError("not enough Google Trends history for this window")
    t_ret = trend.pct_change().replace([np.inf, -np.inf], np.nan)

    dates = [str(d.date()) for d in trend.index]
    interest = [round(float(x), 1) for x in trend.values]

    corr_rows, levels = [], {}
    for name, code in INDICES.items():
        r, n, lvl = None, 0, []
        try:
            px = klse_prices.history(code, lookback=price_lb)["Close"].dropna()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
            pw = px.resample("W").last()
            pw = pw[pw.index >= cutoff]
            # index level rebased to 100, aligned to the trend's weekly dates
            aln = pw.reindex(trend.index).ffill()
            nn = aln.dropna()
            if nn.size:
                base = nn.iloc[0] or 1.0
                lvl = [round(float(x) / base * 100, 2) if x == x else None
                       for x in aln.values]
            join = pd.concat([t_ret, pw.pct_change()], axis=1,
                             keys=["t", "p"]).dropna()
            if len(join) >= 5:
                c = float(join["t"].corr(join["p"]))
                r = None if c != c else round(c, 2)
                n = int(len(join))
        except Exception:  # noqa: BLE001
            pass
        corr_rows.append({"index": name, "code": code, "correlation": r, "n": n})
        levels[name] = lvl

    payload = {
        "keyword": keyword, "geo": GEO, "lookback": lookback,
        "as_of": dates[-1] if dates else None,
        "correlations": corr_rows,
        "series": {"dates": dates, "interest": interest, "levels": levels},
        "note": ("Weekly search interest vs weekly index returns. Google Trends "
                 "data is relative (0-100) and unofficial — read it as a sentiment "
                 "gauge, not a trading signal."),
    }
    with _LOCK:
        _CACHE[ck] = (now, payload)
    return payload

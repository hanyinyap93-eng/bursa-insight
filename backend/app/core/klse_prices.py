"""
klsescreener price source (TradingView UDF datafeed) — Bursa Insight.

Yahoo Finance blocks datacenter / cloud IPs, so on a server (e.g. Render)
`yfinance` returns no data and every price-based feature breaks. klsescreener
exposes a TradingView UDF datafeed that works from the cloud and serves fresh
daily OHLC for both stocks and indexes. This module is the PRIMARY price source;
yfinance is kept only as a fallback.

Endpoint: /v2/trading_view/history?symbol=<code>&resolution=D&from=<ts>&to=<ts>
Returns: {"s":"ok","t":[...unix],"o":[...],"h":[...],"l":[...],"c":[...],"v":[...]}
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

UDF_HISTORY = "https://www.klsescreener.com/v2/trading_view/history"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

_LOOKBACK_DAYS = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 400, "2y": 760,
                  "5y": 1850, "10y": 3700, "max": 5000}


def _symbol(ticker: str) -> str:
    """Map a yfinance-style ticker to a klsescreener UDF symbol.

    '1155.KL' -> '1155', '^KLSE'/'KLCI' -> '0200I', '0005I.KL' -> '0005I'.
    """
    t = str(ticker).strip().upper()
    if t in ("^KLSE", "KLCI"):
        return "0200I"
    if t.endswith(".KL"):
        t = t[:-3]
    return t


def _lookback_seconds(lookback: str) -> int:
    return _LOOKBACK_DAYS.get(lookback, 400) * 86400


def history(ticker: str, lookback: str = "5y", resolution: str = "D",
            retries: int = 2, wait: float = 1.5) -> pd.DataFrame:
    """Daily OHLCV for one symbol as a DataFrame indexed by date."""
    sym = _symbol(ticker)
    to = int(time.time())
    frm = to - _lookback_seconds(lookback)
    url = (f"{UDF_HISTORY}?symbol={urllib.parse.quote(sym)}"
           f"&resolution={resolution}&from={frm}&to={to}")
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            raw = urllib.request.urlopen(req, timeout=25).read()
            d = json.loads(raw)
            if d.get("s") != "ok" or not d.get("t"):
                raise RuntimeError(f"udf status={d.get('s')}")
            idx = pd.to_datetime([int(x) for x in d["t"]], unit="s").normalize()
            df = pd.DataFrame({
                "Open": d["o"], "High": d["h"], "Low": d["l"], "Close": d["c"],
                "Volume": d.get("v", [0] * len(d["t"])),
            }, index=idx)
            return df[~df.index.duplicated(keep="last")].sort_index()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(wait)
    raise RuntimeError(f"klse history failed for {ticker}: {last}")


def close_panel(tickers, lookback: str = "1y", max_workers: int = 16) -> pd.DataFrame:
    """Close-price panel (Date x ticker) fetched concurrently from the UDF feed.

    Columns are keyed by the ORIGINAL ticker (e.g. '^KLSE', '1155.KL') so callers
    that look up a specific symbol keep working.
    """
    syms = list(dict.fromkeys(tickers))
    out = {}

    def fetch(t):
        try:
            return t, history(t, lookback)["Close"]
        except Exception:  # noqa: BLE001
            return t, None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for t, s in ex.map(fetch, syms):
            if s is not None and len(s):
                out[t] = s
    if not out:
        raise RuntimeError("klse close_panel: no data for any symbol")
    return pd.DataFrame(out).sort_index()

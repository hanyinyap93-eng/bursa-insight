"""KLCI 30 fund flow — tick-rule order-flow estimate (ported from the
KLCI_Fund_Flow notebook, adapted to the klsescreener intraday feed).

Intraday bars are signed by the tick rule (price uptick = buyer-initiated,
downtick = seller-initiated; flow = price x volume in MYR) and aggregated into a
daily Net = Buy − Sell per constituent over the trailing ~1 month. Uses
klsescreener 5-minute bars (works on cloud, unlike Yahoo intraday). This is an
estimate; exact trade-side data needs a paid Bursa feed.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from . import klse_prices as kp

RESOLUTION = "5"    # 5-minute bars from klsescreener (~71 bars per trading day)
DAYS = 32           # trailing calendar days (~22 trading days ≈ 1 month)
_MYT = pd.Timedelta(hours=8)   # klsescreener timestamps are UTC; Bursa is UTC+8


def _history_intraday(ticker: str, days: int, resolution: str) -> pd.DataFrame:
    """Intraday Close+Volume for one symbol, timestamps in MYT (NOT normalised to
    date, unlike klse_prices.history)."""
    import time
    to = int(time.time())
    frm = to - days * 86400
    url = (f"{kp.UDF_HISTORY}?symbol={urllib.parse.quote(kp._symbol(ticker))}"
           f"&resolution={resolution}&from={frm}&to={to}")
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=kp.HEADERS), timeout=30).read()
    d = json.loads(raw)
    if d.get("s") != "ok" or not d.get("t"):
        raise RuntimeError(f"udf status={d.get('s')}")
    idx = pd.to_datetime([int(x) for x in d["t"]], unit="s") + _MYT
    df = pd.DataFrame({"Close": d["c"], "Volume": d.get("v", [0] * len(d["t"]))}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _fetch_intraday(tickers, days=DAYS, resolution=RESOLUTION):
    def fetch(t):
        try:
            return t, _history_intraday(t, days, resolution)
        except Exception:  # noqa: BLE001
            return t, None

    closes, vols = {}, {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for t, df in ex.map(fetch, list(dict.fromkeys(tickers))):
            if df is not None and len(df):
                closes[t], vols[t] = df["Close"], df["Volume"]
    if not closes:
        raise RuntimeError("no intraday data returned from klsescreener")
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(vols).sort_index()


def compute(constituents: pd.DataFrame) -> dict:
    """constituents: DataFrame with Ticker, Code, Name, Sector columns."""
    close, vol = _fetch_intraday(constituents["Ticker"].tolist())

    # tick rule: sign each bar, carry the sign through unchanged prints
    sgn = np.sign(close.diff()).replace(0, np.nan).ffill()
    flow = (sgn * close * vol).where(vol > 0)          # signed MYR flow per bar
    buy, sell = flow.clip(lower=0), (-flow).clip(lower=0)

    day = pd.Series(close.index.date, index=close.index)
    daily_buy = buy.groupby(day).sum()
    daily_sell = sell.groupby(day).sum()
    daily_net = (daily_buy - daily_sell).sort_index()
    if daily_net.empty:
        raise RuntimeError("no daily flow computed")

    name = dict(zip(constituents["Ticker"], constituents["Name"]))
    code = dict(zip(constituents["Ticker"], constituents["Code"]))
    sect = dict(zip(constituents["Ticker"], constituents["Sector"]))

    def _r(x):
        return 0.0 if pd.isna(x) else round(float(x), 0)

    month_net, month_buy, month_sell = daily_net.sum(), daily_buy.sum(), daily_sell.sum()
    cols = list(daily_net.columns)

    # 1-month net buy/sell per constituent (ranked max net-buy -> max net-sell)
    month = [{
        "code": code.get(c, c.replace(".KL", "")), "name": name.get(c, c),
        "ticker": c, "sector": sect.get(c, "Unknown"),
        "net": _r(month_net.get(c)), "buy": _r(month_buy.get(c)), "sell": _r(month_sell.get(c)),
    } for c in cols]
    month.sort(key=lambda r: r["net"], reverse=True)

    # latest one-day flow (ranked max net-buy -> max net-sell)
    last = daily_net.index.max()
    lrow = daily_net.loc[last]
    latest = [{"code": code.get(c, c.replace(".KL", "")), "name": name.get(c, c),
               "net": _r(lrow.get(c))} for c in cols if not pd.isna(lrow.get(c))]
    latest.sort(key=lambda r: r["net"], reverse=True)

    # per-sector 1-month net
    sec_net, sec_n = {}, {}
    for c in cols:
        s = sect.get(c, "Unknown")
        sec_net[s] = sec_net.get(s, 0.0) + (0.0 if pd.isna(month_net.get(c)) else float(month_net[c]))
        sec_n[s] = sec_n.get(s, 0) + 1
    sectors = [{"sector": s, "net": round(v, 0), "members": sec_n[s]} for s, v in sec_net.items()]
    sectors.sort(key=lambda r: r["net"], reverse=True)

    index_daily = [{"date": str(d), "net": _r(daily_net.loc[d].sum())} for d in daily_net.index]

    # per-constituent daily net series (for the 30-day accumulation histograms),
    # ordered by cumulative net flow (most accumulated first) like the notebook
    dates_list = [str(d) for d in daily_net.index]
    stocks_daily = [{
        "code": code.get(c, c.replace(".KL", "")), "name": name.get(c, c),
        "total": _r(month_net.get(c)),
        "net": [_r(x) for x in daily_net[c].values],
    } for c in cols]
    stocks_daily.sort(key=lambda r: r["total"], reverse=True)

    return {
        "daily": {"dates": dates_list, "stocks": stocks_daily},
        "as_of": str(last), "interval": RESOLUTION + "m", "days": int(daily_net.shape[0]),
        "start": str(daily_net.index.min()), "end": str(daily_net.index.max()),
        "latest_day": str(last),
        "month": month, "latest": latest, "sectors": sectors, "index_daily": index_daily,
        "totals": {"month_net": _r(month_net.sum()), "latest_net": _r(lrow.sum())},
    }

"""
Malaysia analyst sentiment — KLCI constituents.

Primary source is **i3investor** (klse.i3investor.com price-target pages),
which is server-rendered and reachable from cloud hosts — unlike Yahoo
Finance, whose recommendation API blocks datacenter IPs. For each constituent
we scrape the individual analyst calls (target price + BUY/HOLD/SELL + broker +
date), keep each broker's LATEST call (the current consensus), tally them into
buy/hold/sell buckets and a -1..+1 score, and average the target-price upside.

yfinance (the original notebook source, with its 5-bucket strongBuy..strongSell
breakdown) is kept as a fallback for when the backend runs from a residential
connection where Yahoo is reachable.
"""
from __future__ import annotations

import datetime as _dt
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

RATING_COLS = ["strongBuy", "buy", "hold", "sell", "strongSell"]
RATING_LABELS = {
    "strongBuy": "Strong Buy", "buy": "Buy", "hold": "Hold",
    "sell": "Sell", "strongSell": "Strong Sell",
}
RATING_W = [2, 1, 0, -1, -2]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
I3_PT_URL = "https://klse.i3investor.com/web/stock/analysis-price-target/{code}"

# broker call wording -> bucket
_BUY = {"BUY", "OUTPERFORM", "ADD", "ACCUMULATE", "TRADING BUY", "OVERWEIGHT",
        "POSITIVE", "STRONG BUY", "LONG"}
_HOLD = {"HOLD", "NEUTRAL", "MARKET PERFORM", "EQUAL WEIGHT", "EQUAL-WEIGHT",
         "MARKET WEIGHT", "TRADING SELL AND BUY"}
_SELL = {"SELL", "UNDERPERFORM", "REDUCE", "TAKE PROFIT", "SELL ON STRENGTH",
         "UNDERWEIGHT", "NEGATIVE", "FULLY VALUED", "AVOID", "SHORT"}
_ALLCALLS = _BUY | _HOLD | _SELL
_RECENT_MONTHS = 18   # drop brokers whose last call is older than this


def _bucket(call: str):
    c = (call or "").strip().upper()
    return "buy" if c in _BUY else "hold" if c in _HOLD else "sell" if c in _SELL else None


def _score(counts: dict) -> float:
    """5-bucket score (yfinance path): -1..+1 via the ±2/±1/0 weights."""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return sum(counts[c] * w for c, w in zip(RATING_COLS, RATING_W)) / (2 * total)


def _score3(counts: dict) -> float:
    """buy/hold/sell score (i3 path): (buy − sell) / total, range -1..+1."""
    total = counts.get("buy", 0) + counts.get("hold", 0) + counts.get("sell", 0)
    if total == 0:
        return 0.0
    return (counts.get("buy", 0) - counts.get("sell", 0)) / total


def sentiment_label(score: float) -> str:
    return ("Bullish" if score > 0.25 else
            "Leaning bullish" if score > 0.05 else
            "Bearish" if score < -0.25 else
            "Leaning bearish" if score < -0.05 else "Neutral")


# --------------------------------------------------------------------------- #
# Primary: i3investor price-target pages
# --------------------------------------------------------------------------- #
def _i3_one(code: str):
    """Parse one stock's analyst calls from its i3investor PT page.

    Returns (buckets, avg_target, avg_upside_pct, n_brokers) using each
    broker's most recent call, or None if the stock has no coverage.
    """
    try:
        req = urllib.request.Request(I3_PT_URL.format(code=code),
                                     headers={"User-Agent": _UA})
        html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    # rows are JS arrays of quoted strings; a valid row has a call word cell
    latest = {}   # broker -> (date, call, target, upside)
    for arr in re.findall(r'\[((?:\s*"(?:[^"\\]|\\.)*"\s*,?)+)\]', html):
        cells = [c.strip() for c in re.findall(r'"((?:[^"\\]|\\.)*)"', arr)]
        ci = next((i for i, c in enumerate(cells) if c.upper() in _ALLCALLS), None)
        if ci is None or ci + 1 >= len(cells):
            continue
        call = cells[ci]
        broker = cells[ci + 1]
        date = cells[0] if re.match(r"\d{4}-\d{2}-\d{2}", cells[0] or "") else ""
        target = _num(cells[ci - 2]) if ci >= 2 else None
        upside = _pct(cells[ci - 1]) if ci >= 1 else None
        key = broker.upper()
        if key not in latest or date > latest[key][0]:
            latest[key] = (date, call, target, upside)
    if not latest:
        return None
    cutoff = (_dt.date.today() - _dt.timedelta(days=int(_RECENT_MONTHS * 30.4))).isoformat()
    counts, targets, upsides = {"buy": 0, "hold": 0, "sell": 0}, [], []
    for date, call, target, upside in latest.values():
        if date and date < cutoff:
            continue   # broker dropped coverage
        b = _bucket(call)
        if not b:
            continue
        counts[b] += 1
        if target:
            targets.append(target)
        if upside is not None:
            upsides.append(upside)
    if sum(counts.values()) == 0:
        return None
    avg_t = round(sum(targets) / len(targets), 3) if targets else None
    avg_u = round(sum(upsides) / len(upsides), 1) if upsides else None
    return counts, avg_t, avg_u, sum(counts.values())


def _num(s):
    try:
        return float(re.sub(r"[^\d.]", "", s or ""))
    except (TypeError, ValueError):
        return None


def _pct(s):
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", s or "")
    return float(m.group(1)) if m else None


def fetch_i3_ratings(tickers, names=None):
    """Current analyst consensus per ticker from i3investor (cloud-friendly).

    Returns a list of dicts shaped like the yfinance path (strongBuy..strongSell
    counts, with i3's buy/hold/sell mapped in and strong* left 0), plus target
    price + upside. Most bullish first; stocks without coverage are skipped.
    """
    names = names if names is not None else tickers
    codes = [str(t).split(".")[0] for t in tickers]
    rows = []

    def one(i):
        res = _i3_one(codes[i])
        return i, res

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(one, range(len(codes))))
    for i, res in results:
        if not res:
            continue
        counts, avg_t, avg_u, total = res
        rec = {c: 0 for c in RATING_COLS}
        rec["buy"], rec["hold"], rec["sell"] = counts["buy"], counts["hold"], counts["sell"]
        rows.append({
            "ticker": tickers[i], "code": codes[i], "name": names[i],
            **rec, "total": total,
            "score": round(_score3(counts), 4),
            "target": avg_t, "upside_pct": avg_u,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# Fallback: yfinance (residential connections only)
# --------------------------------------------------------------------------- #
def fetch_analyst_ratings(tickers, names=None):
    """Latest analyst recommendation counts per ticker via yfinance."""
    import yfinance as yf
    names = names if names is not None else tickers
    rows = []
    for t, nm in zip(tickers, names):
        counts = None
        try:
            rec = yf.Ticker(t).recommendations
            if rec is not None and len(rec):
                r0 = rec.reset_index()
                if "period" in r0.columns and (r0["period"] == "0m").any():
                    r0 = r0[r0["period"] == "0m"]
                r0 = r0.iloc[0]
                counts = {c: int(r0.get(c, 0) or 0) for c in RATING_COLS}
        except Exception:  # noqa: BLE001
            counts = None
        if counts is None or sum(counts.values()) == 0:
            continue
        rows.append({
            "ticker": t, "code": str(t).split(".")[0], "name": nm, **counts,
            "total": sum(counts.values()), "score": round(_score(counts), 4),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def build_sentiment(constituents: pd.DataFrame, index: str = "KLCI") -> dict:
    """Full analyst-sentiment payload for an index's constituents."""
    tickers = constituents["Ticker"].tolist()
    names = constituents["Name"].tolist()

    stocks, source, scorer = fetch_i3_ratings(tickers, names), "i3investor", _score3
    if not stocks:  # cloud-blocked i3 or no coverage -> try Yahoo (residential)
        stocks, source, scorer = fetch_analyst_ratings(tickers, names), "yfinance", _score
    if not stocks:
        raise RuntimeError(
            "no analyst ratings retrievable from i3investor or Yahoo Finance. "
            "i3investor may be temporarily unreachable; try again shortly.")

    composition = {c: sum(s.get(c, 0) for s in stocks) for c in RATING_COLS}
    total = sum(composition.values())
    overall = round(scorer(composition), 4) if total else 0.0
    ups = [s["upside_pct"] for s in stocks if s.get("upside_pct") is not None]
    return {
        "index": index,
        "source": source,
        "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
        "overall": {
            "score": overall,
            "label": sentiment_label(overall),
            "total_ratings": total,
            "stocks_covered": len(stocks),
            "stocks_universe": int(len(constituents)),
            "composition": composition,
            "avg_upside_pct": round(sum(ups) / len(ups), 1) if ups else None,
        },
        "rating_labels": RATING_LABELS,
        "stocks": stocks,
    }

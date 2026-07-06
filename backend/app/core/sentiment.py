"""
Malaysia analyst sentiment — KLCI constituents.

Port of the notebook's Section 6 ("Malaysia analyst sentiment"): pulls the
latest analyst recommendation counts per constituent from yfinance and
aggregates them into a -1..+1 sentiment score per stock plus an overall
index-level gauge. Returns a JSON-friendly dict (no matplotlib here — the
frontend draws the bars/gauge).
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

RATING_COLS = ["strongBuy", "buy", "hold", "sell", "strongSell"]
RATING_LABELS = {
    "strongBuy": "Strong Buy", "buy": "Buy", "hold": "Hold",
    "sell": "Sell", "strongSell": "Strong Sell",
}
RATING_W = [2, 1, 0, -1, -2]


def _score(counts: dict) -> float:
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return sum(counts[c] * w for c, w in zip(RATING_COLS, RATING_W)) / (2 * total)


def sentiment_label(score: float) -> str:
    return ("Bullish" if score > 0.25 else
            "Leaning bullish" if score > 0.05 else
            "Bearish" if score < -0.25 else
            "Leaning bearish" if score < -0.05 else "Neutral")


def fetch_analyst_ratings(tickers, names=None):
    """Latest analyst recommendation counts per ticker via yfinance.

    Returns a list of dicts (ticker, name, counts, total, score), most
    bullish first. Stocks without coverage are skipped.
    """
    import yfinance as yf
    names = names if names is not None else tickers
    rows = []
    for t, nm in zip(tickers, names):
        counts = None
        try:
            rec = yf.Ticker(t).recommendations  # trend: period, strongBuy..strongSell
            if rec is not None and len(rec):
                r0 = rec.reset_index()
                if "period" in r0.columns and (r0["period"] == "0m").any():
                    r0 = r0[r0["period"] == "0m"]
                r0 = r0.iloc[0]
                counts = {c: int(r0.get(c, 0) or 0) for c in RATING_COLS}
        except Exception:  # noqa: BLE001 - per-stock coverage is best-effort
            counts = None
        if counts is None or sum(counts.values()) == 0:
            continue
        rows.append({
            "ticker": t,
            "code": str(t).split(".")[0],
            "name": nm,
            **counts,
            "total": sum(counts.values()),
            "score": round(_score(counts), 4),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def build_sentiment(constituents: pd.DataFrame, index: str = "KLCI") -> dict:
    """Full analyst-sentiment payload for an index's constituents.

    constituents: DataFrame with 'Ticker' and 'Name' columns (the same frame
    service.get_constituents() returns).
    """
    stocks = fetch_analyst_ratings(
        constituents["Ticker"].tolist(), constituents["Name"].tolist())
    if not stocks:
        # don't cache an empty gauge as if it were a valid reading
        raise RuntimeError(
            "no analyst ratings retrievable — Yahoo Finance appears to block "
            "this server's IP (common on cloud hosts). Sentiment works when "
            "the backend runs from a residential connection.")
    composition = {c: sum(s[c] for s in stocks) for c in RATING_COLS}
    total = sum(composition.values())
    overall = round(_score(composition), 4) if total else 0.0
    return {
        "index": index,
        "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
        "overall": {
            "score": overall,
            "label": sentiment_label(overall),
            "total_ratings": total,
            "stocks_covered": len(stocks),
            "stocks_universe": int(len(constituents)),
            "composition": composition,
        },
        "rating_labels": RATING_LABELS,
        "stocks": stocks,
    }

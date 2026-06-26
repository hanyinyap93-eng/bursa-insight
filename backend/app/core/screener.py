"""
Stock screener — Bursa Insight.

Two layers:
  1. correlated_constituents() — your notebook's "top-N constituents most
     correlated to the index" logic, generalised: rank every constituent by the
     correlation of its daily returns to the index's daily returns.
  2. screen() — filter constituents by their latest Index Health signals
     (SMA breadth, momentum, RSI, new high/low) plus correlation and return,
     with a saved-criteria structure the API/frontend can persist.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from . import index_health as ih


def correlated_constituents(result: ih.HealthResult, top: int = 10,
                            window: Optional[int] = None) -> list[dict]:
    """Rank constituents by return-correlation to the index.

    window: trailing N rows to correlate over (None = full lookback).
    Returns rows with code, name, sector, correlation, period return %.
    """
    if result.index_price is None:
        return []
    close = result.close
    idx = result.index_price.reindex(close.index).ffill()
    if window:
        close = close.tail(window)
        idx = idx.tail(window)
    stock_ret = close.pct_change()
    idx_ret = idx.pct_change()
    meta = result.meta.set_index("Ticker")
    rows = []
    for tkr in close.columns:
        s = stock_ret[tkr]
        corr = s.corr(idx_ret)
        if pd.isna(corr):
            continue
        col = close[tkr].dropna()
        ret = (col.iloc[-1] / col.iloc[0] - 1.0) * 100.0 if len(col) > 1 else None
        m = meta.loc[tkr] if tkr in meta.index else None
        rows.append({
            "ticker": tkr,
            "code": (m["Code"] if m is not None else tkr.split(".")[0]),
            "name": (m["Name"] if m is not None else tkr),
            "sector": (m["Sector"] if m is not None else "Unknown"),
            "correlation": round(float(corr), 3),
            "return_pct": round(float(ret), 2) if ret is not None else None,
        })
    rows.sort(key=lambda r: r["correlation"], reverse=True)
    return rows[:top] if top else rows


def _latest_signal(boolean_df: pd.DataFrame, tkr: str) -> bool:
    if tkr not in boolean_df.columns:
        return False
    col = boolean_df[tkr].dropna()
    return bool(col.iloc[-1]) if len(col) else False


@dataclass
class ScreenCriteria:
    """A saveable screen. All filters are optional; None = ignore."""
    above_sma: Optional[bool] = None        # close > SMA(10)
    momentum_up: Optional[bool] = None       # 10-day momentum > 0
    rsi_overbought: Optional[bool] = None     # RSI(10) > 70
    rsi_oversold: Optional[bool] = None       # RSI(10) < 30
    new_high: Optional[bool] = None           # 25-day new high
    new_low: Optional[bool] = None            # 25-day new low
    min_correlation: Optional[float] = None
    min_return_pct: Optional[float] = None
    max_return_pct: Optional[float] = None
    sectors: Optional[list[str]] = None       # filter to Bursa sector names
    healthy_only: Optional[bool] = None       # net signal score > 0


def screen(result: ih.HealthResult, criteria: ScreenCriteria,
           corr_window: Optional[int] = None) -> list[dict]:
    """Apply a ScreenCriteria over the constituents; return matching rows."""
    sig = result.signals
    corr_rows = {r["ticker"]: r for r in correlated_constituents(result, top=0, window=corr_window)}
    meta = result.meta.set_index("Ticker")
    out = []
    for tkr in result.close.columns:
        cr = corr_rows.get(tkr, {})
        comps = {
            "above_sma": _latest_signal(sig["sma"]["up"], tkr),
            "below_sma": _latest_signal(sig["sma"]["drop"], tkr),
            "momentum_up": _latest_signal(sig["momentum"]["up"], tkr),
            "rsi_overbought": _latest_signal(sig["rsi"]["up"], tkr),
            "rsi_oversold": _latest_signal(sig["rsi"]["drop"], tkr),
            "new_high": _latest_signal(sig["hl"]["up"], tkr),
            "new_low": _latest_signal(sig["hl"]["drop"], tkr),
        }
        # net signal score (-4..+4): how many of the 4 components are bullish
        net = sum(
            (1 if comps[u] else 0) - (1 if comps[d] else 0)
            for u, d in (("above_sma", "below_sma"), ("momentum_up", "momentum_up"),
                         ("rsi_overbought", "rsi_oversold"), ("new_high", "new_low"))
        )
        c = criteria
        if c.above_sma is not None and comps["above_sma"] != c.above_sma:
            continue
        if c.momentum_up is not None and comps["momentum_up"] != c.momentum_up:
            continue
        if c.rsi_overbought is not None and comps["rsi_overbought"] != c.rsi_overbought:
            continue
        if c.rsi_oversold is not None and comps["rsi_oversold"] != c.rsi_oversold:
            continue
        if c.new_high is not None and comps["new_high"] != c.new_high:
            continue
        if c.new_low is not None and comps["new_low"] != c.new_low:
            continue
        if c.min_correlation is not None and (cr.get("correlation") or -1) < c.min_correlation:
            continue
        if c.min_return_pct is not None and (cr.get("return_pct") or -1e9) < c.min_return_pct:
            continue
        if c.max_return_pct is not None and (cr.get("return_pct") or 1e9) > c.max_return_pct:
            continue
        if c.healthy_only and net <= 0:
            continue
        m = meta.loc[tkr] if tkr in meta.index else None
        sector = m["Sector"] if m is not None else "Unknown"
        if c.sectors and sector not in c.sectors:
            continue
        out.append({
            "ticker": tkr,
            "code": cr.get("code", tkr.split(".")[0]),
            "name": cr.get("name", m["Name"] if m is not None else tkr),
            "sector": sector,
            "correlation": cr.get("correlation"),
            "return_pct": cr.get("return_pct"),
            "net_signal": net,
            **comps,
        })
    out.sort(key=lambda r: (r["net_signal"], r.get("correlation") or 0), reverse=True)
    return out


# Preset screens the frontend can offer out of the box.
PRESETS = {
    "oversold_correlated": ScreenCriteria(rsi_oversold=True, min_correlation=0.3),
    "breakout": ScreenCriteria(new_high=True, momentum_up=True, above_sma=True),
    "uptrend_healthy": ScreenCriteria(above_sma=True, momentum_up=True, healthy_only=True),
    "weak_breakdown": ScreenCriteria(new_low=True, above_sma=False),
}

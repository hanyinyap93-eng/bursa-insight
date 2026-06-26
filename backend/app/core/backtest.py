"""
Backtest-a-screen — Bursa Insight.

Two backtest modes:
  1. backtest_health_threshold() — treat the index Index Health % as a timing
     signal: go long the index when health crosses above `entry`, exit when it
     drops below `exit_`. Reports return, equity curve, trades, vs buy & hold.
  2. backtest_signal_screen() — for each day, the "portfolio" is the set of
     constituents passing a simple signal screen (e.g. above SMA + momentum up);
     hold them equally next day. Reports the strategy equity vs the index.

This is an MVP backtester: daily bars, next-bar execution, no costs/slippage by
default (a flat bps cost can be supplied). Intended for idea validation, not
production PnL.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import index_health as ih
from . import service


def _stats(equity: pd.Series, periods_per_year: int = 252) -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0,
                "sharpe": 0.0, "volatility_pct": 0.0}
    rets = eq.pct_change().dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    years = max(len(eq) / periods_per_year, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0
    roll_max = eq.cummax()
    dd = (eq / roll_max - 1.0).min()
    vol = rets.std() * np.sqrt(periods_per_year)
    sharpe = (rets.mean() * periods_per_year) / (rets.std() * np.sqrt(periods_per_year)) \
        if rets.std() > 0 else 0.0
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "volatility_pct": round(float(vol) * 100, 2),
    }


def backtest_health_threshold(index: str = "KLCI", lookback: str = "1y",
                              entry: float = 0.0, exit_: float = -10.0,
                              cost_bps: float = 0.0) -> dict:
    """Long the index when health% > entry, flat when health% < exit_."""
    result = service.get_health(index, lookback)
    if result.index_price is None:
        raise ValueError("No index price available for backtest")
    warm = result.cfg.warmup
    health = result.health_pct.iloc[warm:]
    price = result.index_price.reindex(health.index).ffill()
    rets = price.pct_change().fillna(0.0)

    # position: state machine with hysteresis (entry above, exit below)
    pos = np.zeros(len(health))
    holding = False
    h = health.to_numpy()
    for i in range(len(h)):
        if not holding and h[i] > entry:
            holding = True
        elif holding and h[i] < exit_:
            holding = False
        pos[i] = 1.0 if holding else 0.0
    position = pd.Series(pos, index=health.index)
    # trade next bar (signal known at close, act on next return)
    exposure = position.shift(1).fillna(0.0)
    turns = exposure.diff().abs().fillna(0.0)
    cost = turns * (cost_bps / 10000.0)
    strat_ret = exposure * rets - cost
    equity = (1.0 + strat_ret).cumprod()
    bh = (1.0 + rets).cumprod()

    # trade list
    trades = []
    entry_i = None
    ex = exposure.to_numpy()
    for i in range(len(ex)):
        if ex[i] == 1.0 and (i == 0 or ex[i - 1] == 0.0):
            entry_i = i
        if entry_i is not None and (i == len(ex) - 1 or ex[i + 1] == 0.0) and ex[i] == 1.0:
            ed, xd = price.index[entry_i], price.index[i]
            pnl = price.iloc[i] / price.iloc[entry_i] - 1.0
            trades.append({"entry": str(ed.date()), "exit": str(xd.date()),
                           "return_pct": round(float(pnl) * 100, 2)})
            entry_i = None

    return {
        "strategy": "health_threshold",
        "index": index,
        "params": {"entry": entry, "exit": exit_, "cost_bps": cost_bps},
        "dates": [str(d.date()) for d in equity.index],
        "equity": [round(float(x), 4) for x in equity],
        "buy_hold": [round(float(x), 4) for x in bh],
        "stats": _stats(equity),
        "buy_hold_stats": _stats(bh),
        "trades": trades,
        "n_trades": len(trades),
        "time_in_market_pct": round(float(exposure.mean()) * 100, 1),
    }


def backtest_signal_screen(index: str = "KLCI", lookback: str = "1y",
                           require_above_sma: bool = True,
                           require_momentum_up: bool = True,
                           cost_bps: float = 0.0) -> dict:
    """Equal-weight the constituents passing the screen each day; hold next bar."""
    result = service.get_health(index, lookback)
    warm = result.cfg.warmup
    close = result.close
    rets = close.pct_change().fillna(0.0)
    sig = result.signals

    mask = pd.DataFrame(True, index=close.index, columns=close.columns)
    if require_above_sma:
        mask &= sig["sma"]["up"].reindex_like(mask).fillna(False)
    if require_momentum_up:
        mask &= sig["momentum"]["up"].reindex_like(mask).fillna(False)
    mask = mask.iloc[warm:]
    rets = rets.iloc[warm:]

    # equal weight across the names selected at prior close
    weights = mask.div(mask.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    exposure = weights.shift(1).fillna(0.0)
    port_ret = (exposure * rets).sum(axis=1)
    turnover = exposure.diff().abs().sum(axis=1).fillna(0.0)
    port_ret = port_ret - turnover * (cost_bps / 10000.0)
    equity = (1.0 + port_ret).cumprod()

    bh = None
    if result.index_price is not None:
        idx = result.index_price.reindex(equity.index).ffill().pct_change().fillna(0.0)
        bh = (1.0 + idx).cumprod()

    return {
        "strategy": "signal_screen",
        "index": index,
        "params": {"require_above_sma": require_above_sma,
                   "require_momentum_up": require_momentum_up, "cost_bps": cost_bps},
        "dates": [str(d.date()) for d in equity.index],
        "equity": [round(float(x), 4) for x in equity],
        "buy_hold": [round(float(x), 4) for x in bh] if bh is not None else None,
        "stats": _stats(equity),
        "buy_hold_stats": _stats(bh) if bh is not None else None,
        "avg_holdings": round(float(mask.sum(axis=1).mean()), 1),
    }

"""CAPM / Markowitz mean-variance analytics for a portfolio of Bursa stocks.

Ported from CAPM_MPT_Portfolio.ipynb (portfolio_common.py methodology):
annualised mean/cov from daily LOG returns; long-only, fully-invested weights;
CAPM beta/alpha by regressing the portfolio's excess return on the MARKET's
(market first — the notebook's documented bug fix).

scipy-free: the long-only max-Sharpe / min-variance weights and the efficient
frontier come from a Monte-Carlo sweep of the weight simplex (the same approach
the notebook draws its frontier cloud from), refined by keeping the best sample.
Pure numpy keeps the Render build light (no scipy dependency).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252
RISK_FREE = 0.035          # ~3.5% p.a. Malaysia MGS proxy (notebook default is 0)
_MC_SAMPLES = 20000
_SEED = 101                # fixed -> deterministic recommendations


# --------------------------------------------------------------------------- #
# Core mean-variance maths
# --------------------------------------------------------------------------- #
def _ann_stats(log_ret: pd.DataFrame):
    """Annualised mean vector and covariance matrix from daily log returns."""
    mu = log_ret.mean().values * TRADING_DAYS
    cov = log_ret.cov().values * TRADING_DAYS
    return mu, cov


def _ret_vol_sr(w, mu, cov, rf):
    w = np.asarray(w, float)
    ret = float(mu @ w)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    sr = (ret - rf) / vol if vol > 0 else float("nan")
    return ret, vol, sr


def _monte_carlo(mu, cov, rf, n=_MC_SAMPLES, seed=_SEED):
    """n random long-only, fully-invested portfolios over the weight simplex."""
    d = len(mu)
    rng = np.random.default_rng(seed)
    # Dirichlet(1,..) == uniform on the simplex; mix in a few near-corner draws
    # so single-name/edge optima are well sampled.
    W = rng.random((n, d))
    W /= W.sum(axis=1, keepdims=True)
    ret = W @ mu
    vol = np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", W, cov, W), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(vol > 0, (ret - rf) / vol, np.nan)
    return W, ret, vol, sr


def _frontier(vol, ret, nbins=42):
    """Upper-left envelope of the MC cloud: max return in each volatility bin."""
    lo, hi = float(np.min(vol)), float(np.max(vol))
    if hi <= lo:
        return []
    edges = np.linspace(lo, hi, nbins + 1)
    pts = []
    for k in range(nbins):
        m = (vol >= edges[k]) & (vol < edges[k + 1] if k < nbins - 1 else vol <= edges[k + 1])
        if m.any():
            j = int(np.argmax(ret[m]))
            pts.append((float(vol[m][j]), float(ret[m][j])))
    # keep only the non-decreasing upper hull (efficient part)
    pts.sort()
    hull, best = [], -1e18
    for v, r in pts:
        if r > best:
            hull.append({"vol": round(v, 4), "ret": round(r, 4)})
            best = r
    return hull


# --------------------------------------------------------------------------- #
# CAPM
# --------------------------------------------------------------------------- #
def capm(port_rets: pd.Series, bench_rets: pd.Series, rf: float = RISK_FREE) -> dict:
    """CAPM beta/alpha: regress PORTFOLIO excess return on MARKET excess return.

    beta = Cov(port, mkt) / Var(mkt), alpha = intercept (annualised).
    """
    df = pd.concat([port_rets, bench_rets], axis=1).dropna()
    df.columns = ["p", "b"]
    if len(df) < 10:
        return {"beta": None, "alpha_annual": None, "alpha_daily": None,
                "r_squared": None, "n": len(df)}
    rf_d = rf / TRADING_DAYS
    x = (df["b"] - rf_d).values          # market excess (independent)
    y = (df["p"] - rf_d).values          # portfolio excess (dependent)
    vx = x.var(ddof=1)
    if vx <= 0:
        return {"beta": None, "alpha_annual": None, "alpha_daily": None,
                "r_squared": None, "n": len(df)}
    beta = float(np.cov(x, y, ddof=1)[0, 1] / vx)
    alpha_d = float(y.mean() - beta * x.mean())
    corr = float(np.corrcoef(x, y)[0, 1])
    return {"beta": round(beta, 4), "alpha_daily": alpha_d,
            "alpha_annual": round(alpha_d * TRADING_DAYS, 4),
            "r_squared": round(corr * corr, 4), "n": int(len(df))}


def corr_matrix(close: pd.DataFrame) -> dict:
    """Pearson correlation of daily log returns between the columns of `close`,
    plus the average off-diagonal correlation. Used by the standalone
    correlation panel (with its own lookback) and by analyze()."""
    close = close.dropna(how="all").ffill().dropna()
    names = list(close.columns)
    d = len(names)
    if d == 0 or len(close) < 5:
        return {"names": names, "matrix": [], "avg": None, "n": int(len(close))}
    log_ret = np.log(close / close.shift(1)).dropna()
    cmat = np.corrcoef(log_ret.values, rowvar=False) if d > 1 else np.array([[1.0]])
    cmat = np.nan_to_num(np.atleast_2d(cmat), nan=0.0)
    matrix = [[round(float(cmat[i][j]), 2) for j in range(d)] for i in range(d)]
    avg = None
    if d > 1:
        off = [cmat[i][j] for i in range(d) for j in range(i + 1, d)]
        avg = round(float(np.mean(off)), 2) if off else None
    return {"names": names, "matrix": matrix, "avg": avg, "n": int(len(log_ret))}


# --------------------------------------------------------------------------- #
# Public: full analysis for one portfolio
# --------------------------------------------------------------------------- #
def analyze(close: pd.DataFrame, weights_current, klci_rets: pd.Series | None,
            rf: float = RISK_FREE) -> dict:
    """Full CAPM/MPT analysis.

    close            : price panel (columns = display names, index = dates)
    weights_current  : current value weights aligned to close.columns (may be None
                       -> equal weight)
    klci_rets        : daily benchmark returns for CAPM (may be None)
    """
    close = close.dropna(how="all").ffill().dropna()
    names = list(close.columns)
    d = len(names)
    if d == 0 or len(close) < 20:
        return {"ok": False, "reason": "not enough price history"}

    log_ret = np.log(close / close.shift(1)).dropna()
    stock_rets = close.pct_change().dropna()
    mu, cov = _ann_stats(log_ret)
    sd = np.sqrt(np.diag(cov))

    # current weights (normalised); default equal-weight
    if weights_current is None or float(np.nansum(weights_current)) <= 0:
        wc = np.full(d, 1.0 / d)
    else:
        wc = np.nan_to_num(np.asarray(weights_current, float), nan=0.0)
        wc = wc / wc.sum() if wc.sum() > 0 else np.full(d, 1.0 / d)

    # per-stock risk/return points (for the CAPM risk-return chart)
    stocks = [{"name": names[i], "ret": round(float(mu[i]), 4),
               "vol": round(float(sd[i]), 4), "weight": round(float(wc[i]), 4)}
              for i in range(d)]

    # Monte-Carlo frontier + optimal long-only weights
    if d == 1:
        w_ms = w_mv = np.array([1.0])
        cloud, frontier = [], []
    else:
        W, ret, vol, sr = _monte_carlo(mu, cov, rf)
        i_ms = int(np.nanargmax(sr))
        i_mv = int(np.argmin(vol))
        w_ms, w_mv = W[i_ms], W[i_mv]
        frontier = _frontier(vol, ret)
        # downsample the cloud for the chart (deterministic stride)
        step = max(1, len(vol) // 1400)
        cloud = [{"vol": round(float(vol[k]), 4), "ret": round(float(ret[k]), 4),
                  "sr": round(float(sr[k]), 3) if np.isfinite(sr[k]) else 0.0}
                 for k in range(0, len(vol), step)]

    def pack(w):
        r, v, s = _ret_vol_sr(w, mu, cov, rf)
        return {"weights": {names[i]: round(float(w[i]), 4) for i in range(d)},
                "ret": round(r, 4), "vol": round(v, 4), "sharpe": round(s, 4) if s == s else None}

    cur = pack(wc)
    max_sharpe = pack(w_ms)
    min_var = pack(w_mv)

    # portfolio daily returns with current weights (buy&hold-ish, daily rebal)
    port_ret = (stock_rets * wc).sum(axis=1)
    capm_klci = capm(port_ret, klci_rets, rf) if klci_rets is not None else None

    # concentration (Herfindahl) — 1/d is perfectly diversified
    hhi = float((wc ** 2).sum())

    # correlation of daily returns between holdings (drives diversification: the
    # optimiser rewards low/negative pairs). Average off-diagonal too.
    cmat = np.corrcoef(log_ret.values, rowvar=False) if d > 1 else np.array([[1.0]])
    cmat = np.nan_to_num(np.atleast_2d(cmat), nan=0.0)
    correlation = {
        "names": names,
        "matrix": [[round(float(cmat[i][j]), 2) for j in range(d)] for i in range(d)],
    }
    if d > 1:
        off = [cmat[i][j] for i in range(d) for j in range(i + 1, d)]
        correlation["avg"] = round(float(np.mean(off)), 2) if off else None

    return {
        "ok": True,
        "names": names,
        "rf": rf,
        "stocks": stocks,
        "current": cur,
        "max_sharpe": max_sharpe,
        "min_var": min_var,
        "capm": capm_klci,
        "correlation": correlation,
        "frontier": frontier,
        "cloud": cloud,
        "hhi": round(hhi, 3),
        "diversified_hhi": round(1.0 / d, 3),
    }

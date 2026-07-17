"""
KLCI VIX — synthetic 30-day volatility index for the FBM KLCI.

Bursa's KLCI options (OKLI) are too illiquid for the true CBOE VIX formula, so
this is a *synthetic* VIX: a blend of

  * Yang-Zhang realized volatility (uses O/H/L/C — ~8x more efficient than a
    close-only estimator), and
  * an EWMA / RiskMetrics (lambda 0.94) conditional-vol proxy,

expressed VIX-style (annualized %, ~1-month horizon). Regime bands are the
10th / 90th percentile of the available history: at/above the 90th = FEAR (often
capitulation, contrarian-bullish when Index Health is deeply negative); at/below
the 10th = COMPLACENCY (risk builds quietly when paired with a stretched market).

The notebook (Section 8) blends Yang-Zhang with a GARCH(1,1) conditional vol and
overlays warrant-implied vol. GARCH needs `arch`/scipy, which this backend
deliberately excludes to stay lightweight on Render, so we take the notebook's
own EWMA fallback path. The near-ATM warrant-IV marker is derived on the frontend
from the GEX warrant chain the GEX page already loads (no second scrape).
"""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

VIX_WINDOW = 21       # rolling window for the realized-vol estimators (trading days)
ANN = 252             # trading days per year (annualization factor)
EWMA_LAMBDA = 0.94    # RiskMetrics decay


def _yang_zhang(o, h, l, c, n=VIX_WINDOW):
    """Yang-Zhang annualized volatility (%), drift-independent, uses O/H/L/C."""
    co = np.log(o / c.shift(1))          # overnight (close-to-open) log return
    oc = np.log(c / o)                   # intraday (open-to-close) log return
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)  # Rogers-Satchell
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = (co.rolling(n).var() + k * oc.rolling(n).var()
           + (1 - k) * rs.rolling(n).mean())
    return np.sqrt(var * ANN) * 100


def build_vix_payload(months: int = 12):
    """Compute the synthetic KLCI VIX and shape the frontend JSON payload."""
    from . import klse_prices
    from . import index_health as ih

    df = klse_prices.history(ih.INDEX_SYMBOL_KLCI, lookback="5y").dropna(how="all")
    if df.empty or len(df) < VIX_WINDOW + 5:
        raise RuntimeError("KLCI VIX: insufficient KLCI price history")
    o, h, l, c = (df[k].astype(float) for k in ("Open", "High", "Low", "Close"))

    ret = np.log(c / c.shift(1)).dropna()
    vol_yz = _yang_zhang(o, h, l, c)
    vol_ew = np.sqrt((ret ** 2).ewm(alpha=1 - EWMA_LAMBDA).mean() * ANN) * 100

    # KLCI VIX = mean of the Yang-Zhang realized and EWMA conditional estimators
    vix = pd.concat([vol_yz.rename("yz"), vol_ew.rename("ew")], axis=1) \
            .mean(axis=1).dropna()
    if vix.empty:
        raise RuntimeError("KLCI VIX: no volatility series computed")

    p10, p90 = float(vix.quantile(0.10)), float(vix.quantile(0.90))
    cur = float(vix.iloc[-1])
    pctl = float((vix < cur).mean() * 100)
    regime = "FEAR" if cur >= p90 else ("COMPLACENCY" if cur <= p10 else "NORMAL")

    start = vix.index.max() - pd.DateOffset(months=months)
    show = vix[vix.index >= start]
    yz_show = vol_yz.reindex(show.index)
    close_show = c.reindex(show.index)

    def _ser(s):
        return [None if (v != v) else round(float(v), 2) for v in s]

    read = _vix_read(regime, cur, pctl)
    return {
        "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
        "current": round(cur, 2),
        "percentile": round(pctl, 1),
        "regime": regime,
        "p10": round(p10, 2),
        "p90": round(p90, 2),
        "window": VIX_WINDOW,
        "history_years": round(len(vix) / ANN, 1),
        "dates": [d.strftime("%Y-%m-%d") for d in show.index],
        "vix": _ser(show),
        "yz": _ser(yz_show),
        "klci_close": _ser(close_show),
        "read": read["en"], "read_zh": read["zh"],
    }


def _vix_read(regime, cur, pctl):
    """Short EN + ZH regime interpretation for the readout line."""
    if regime == "FEAR":
        return {
            "en": (f"KLCI VIX {cur:.1f}% sits in the top decile of its history "
                   f"({pctl:.0f}th pct) — elevated fear, often near capitulation; "
                   "historically a contrarian-bullish tell when Index Health is "
                   "deeply negative."),
            "zh": (f"KLCI VIX {cur:.1f}% 处于历史高位十分位（{pctl:.0f} 百分位）"
                   "— 恐慌升温，常接近抛售末段；当指数健康度深度为负时，历史上偏逆势看多。"),
        }
    if regime == "COMPLACENCY":
        return {
            "en": (f"KLCI VIX {cur:.1f}% is in the bottom decile ({pctl:.0f}th pct) "
                   "— unusual calm; risk tends to build quietly, watch for a "
                   "stretched (over-extended) Index Health."),
            "zh": (f"KLCI VIX {cur:.1f}% 处于历史低位十分位（{pctl:.0f} 百分位）"
                   "— 市场异常平静；风险易在暗中积累，留意指数健康度是否过度拉伸。"),
        }
    return {
        "en": (f"KLCI VIX {cur:.1f}% is mid-range ({pctl:.0f}th pct) — no "
               "volatility extreme; lean on breadth (Index Health) and the GEX "
               "map for the trade signal."),
        "zh": (f"KLCI VIX {cur:.1f}% 处于中间区间（{pctl:.0f} 百分位）"
               "— 无波动极值；交易信号以广度（指数健康度）与 GEX 图为主。"),
    }

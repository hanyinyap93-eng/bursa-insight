"""
Risk appetite — FBM ACE / MID 70 / KLCI index spreads
(Ziemba turn-of-year & size effect).

Backend port of Section 6 of KLSE_KLCI_Breadth_EPS_2026_refined.ipynb:

  Indexes (klsescreener UDF charting feed, yfinance ^KLSE as KLCI fallback):
    FBM KLCI   0200I   large-cap benchmark
    FBM ACE    0871I   speculative small/growth — sharpest risk-appetite read
    FBM MID 70 0863I   mid-cap — intermediate risk tier

  Per spread (ACE−KLCI, MID70−KLCI, ACE−MID70), on daily returns:
    * raw spread r_hi − r_lo and the book's beta-adjusted form r_hi − β·r_lo,
      β = rolling(252d) Cov/Var
    * score H = 50 + 50·tanh(z/2), z = 21-day cumulative spread standardized
      over a rolling year (>60 risk-on, <40 defensive)
    * by-month mean daily spread (bps) with t-stats — Ziemba turn-of-year
      (Dec/Jan) diagnostic
  H_RiskAppetite = average of the two vs-KLCI scores.
"""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

from . import klse_prices

ROLL_WINDOW = 252     # default 1 trading year (z-score standardisation window)
SPREAD_SMOOTH = 21    # trading days of cumulative spread fed into the z-score
HISTORY = "5y"        # rolling stats need well over a year
# selectable health-score lookback (the z-score standardisation window)
LB_DAYS = {"3mo": 63, "6mo": 126, "1y": 252, "2y": 504}

BURSA_INDICES = {     # name: klsescreener code
    "FBM KLCI":   "0200I",
    "FBM ACE":    "0871I",
    "FBM MID 70": "0863I",
}
SPREADS = [("FBM ACE", "FBM KLCI"), ("FBM MID 70", "FBM KLCI"),
           ("FBM ACE", "FBM MID 70")]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _load_index_closes() -> dict:
    """Daily closes per index; KLCI falls back to yfinance ^KLSE."""
    out = {}
    for name, code in BURSA_INDICES.items():
        try:
            s = klse_prices.history(code, lookback=HISTORY)["Close"].dropna()
            if len(s) > ROLL_WINDOW:
                out[name] = s
        except Exception:  # noqa: BLE001
            pass
    if "FBM KLCI" not in out:
        import yfinance as yf
        s = yf.Ticker("^KLSE").history(period=HISTORY)["Close"].dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        out["FBM KLCI"] = s
    return out


def _regime(h: float) -> str:
    return ("risk appetite improving" if h > 60 else
            "defensive market" if h < 40 else "neutral")


def build_risk_appetite(display_years: int = 1, roll: int = ROLL_WINDOW) -> dict:
    """Full JSON payload: spread scores, seasonality tables, rebased levels.

    `roll` is the z-score standardisation window (the health score's lookback);
    beta stays on the fixed 252-day window as a separate diagnostic."""
    closes = _load_index_closes()
    if "FBM KLCI" not in closes or len(closes) < 2:
        raise RuntimeError("could not load enough Bursa indexes "
                           f"(got: {', '.join(closes) or 'none'})")
    rets = pd.DataFrame({k: v.pct_change() for k, v in closes.items()}) \
        .dropna(how="all")

    spreads_out, h_series = [], {}
    for hi, lo in SPREADS:
        if hi not in rets.columns or lo not in rets.columns:
            continue
        tag = f"{hi.split()[-1]}-{lo.split()[-1]}"      # ACE-KLCI, 70-KLCI, ACE-70
        pair = rets[[hi, lo]].dropna()
        spread = pair[hi] - pair[lo]

        beta = pair[hi].rolling(ROLL_WINDOW).cov(pair[lo]) \
            / pair[lo].rolling(ROLL_WINDOW).var()

        cum = spread.rolling(SPREAD_SMOOTH).sum()
        z = (cum - cum.rolling(roll).mean()) / cum.rolling(roll).std()
        h = (50 + 50 * np.tanh(z / 2)).dropna()
        h_series[tag] = h

        # by-month turn-of-year diagnostics (full history)
        g = spread.groupby(spread.index.month)
        monthly = [{"month": MONTH_ABBR[m - 1],
                    "mean_bps": round(float(g.mean()[m]) * 1e4, 2),
                    "n": int(g.count()[m]),
                    "t_stat": round(float(g.mean()[m] /
                                          (g.std()[m] / np.sqrt(g.count()[m]))), 2)}
                   for m in sorted(g.groups)]

        cum21 = spread.rolling(SPREAD_SMOOTH).sum().dropna()
        spreads_out.append({
            "tag": tag, "hi": hi, "lo": lo,
            "h": round(float(h.iloc[-1]), 1) if len(h) else None,
            "beta": round(float(beta.dropna().iloc[-1]), 2)
                    if beta.notna().any() else None,
            "spread_21d_pct": round(float(cum21.iloc[-1]) * 100, 2)
                              if len(cum21) else None,
            "monthly": monthly,
        })

    if not spreads_out:
        raise RuntimeError("no index spreads could be computed")

    # composite = average of the vs-KLCI scores, on their common calendar
    vs_klci = [h_series[t] for t in ("ACE-KLCI", "70-KLCI") if t in h_series]
    composite = pd.concat(vs_klci, axis=1).mean(axis=1).dropna() if vs_klci else \
        list(h_series.values())[0]
    h_now = float(composite.iloc[-1])

    # display window: H score series + rebased index levels
    cutoff = composite.index.max() - pd.DateOffset(years=display_years)
    comp_v = composite[composite.index >= cutoff]
    series = {"dates": [str(d.date()) for d in comp_v.index],
              "composite": [round(float(x), 1) for x in comp_v]}
    for tag, h in h_series.items():
        series[tag] = [round(float(x), 1) if x == x else None
                       for x in h.reindex(comp_v.index).ffill()]

    rebased = {}
    for name, s in closes.items():
        sv = s[s.index >= cutoff].reindex(comp_v.index).ffill().dropna()
        if len(sv):
            rebased[name] = [round(float(x), 2) for x in sv / sv.iloc[0] * 100]

    return {
        "as_of": str(composite.index[-1].date()),
        "history": HISTORY,
        "display_years": display_years,
        "roll_days": roll,
        "h_risk_appetite": round(h_now, 1),
        "regime": _regime(h_now),
        "spreads": spreads_out,
        "series": series,
        "rebased": {"dates": series["dates"], "levels": rebased},
        "notes": {
            "score": f"H = 50 + 50·tanh(z/2); z = 21d cumulative return spread "
                     f"standardized over a rolling {roll}-day window. "
                     f">60 risk-on, <40 defensive.",
            "beta": "rolling 252d Cov/Var of the pair's daily returns "
                    "(book's beta-adjusted spread r_i − β·r_KLCI)",
            "toy": "Ziemba turn-of-year: look for strong Dec/Jan mean spreads "
                   "(small/speculative outperforming) in the monthly table.",
        },
    }


def build_ra_correlation(lookback: str = "1y") -> dict:
    """Correlation of each risk-appetite health-score series (ACE-KLCI, 70-KLCI,
    ACE-70 and the composite H_RiskAppetite) with the FBM KLCI index level over
    the most recent `lookback` window. Scores use the canonical 252-day
    standardisation."""
    n = LB_DAYS.get(lookback, ROLL_WINDOW)
    closes = _load_index_closes()
    if "FBM KLCI" not in closes:
        return {"names": [], "corr": [], "vs": "FBM KLCI", "lookback": lookback, "n": 0}
    rets = pd.DataFrame({k: v.pct_change() for k, v in closes.items()}).dropna(how="all")

    h_series: dict = {}
    for hi, lo in SPREADS:
        if hi not in rets.columns or lo not in rets.columns:
            continue
        tag = f"{hi.split()[-1]}-{lo.split()[-1]}"
        pair = rets[[hi, lo]].dropna()
        spread = pair[hi] - pair[lo]
        cum = spread.rolling(SPREAD_SMOOTH).sum()
        z = (cum - cum.rolling(ROLL_WINDOW).mean()) / cum.rolling(ROLL_WINDOW).std()
        h_series[tag] = (50 + 50 * np.tanh(z / 2)).dropna()

    vs_klci = [h_series[t] for t in ("ACE-KLCI", "70-KLCI") if t in h_series]
    if not vs_klci:
        return {"names": [], "corr": [], "vs": "FBM KLCI", "lookback": lookback, "n": 0}
    composite = pd.concat(vs_klci, axis=1).mean(axis=1).dropna()

    cols = {t: h_series[t] for t in ("ACE-KLCI", "70-KLCI", "ACE-70") if t in h_series}
    cols["H_RiskAppetite"] = composite
    frame = pd.DataFrame(cols)
    frame["__KLCI__"] = closes["FBM KLCI"]     # the market to correlate each score against
    frame = frame.dropna().tail(n)

    names = [c for c in cols]
    if len(frame) < 5:
        return {"names": names, "corr": [None] * len(names),
                "vs": "FBM KLCI", "lookback": lookback, "n": int(len(frame))}
    klci = frame["__KLCI__"].values
    corr = []
    for name in names:
        c = np.corrcoef(frame[name].values, klci)[0, 1]
        corr.append(round(float(c), 2) if c == c else None)   # NaN-safe
    return {"names": names, "corr": corr,
            "vs": "FBM KLCI", "lookback": lookback, "n": int(len(frame))}

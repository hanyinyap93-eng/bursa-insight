"""KLCI warrant-implied volatility ("wVIX") — a forward-looking companion to klci_vix.

klci_vix.py measures what the index HAS done (Yang-Zhang realized + EWMA). This
measures what warrant prices say it WILL do: back out Black-Scholes implied vol
from the structured warrants on each KLCI constituent, interpolate each name to 30
days, and combine them with a realized correlation matrix:

    sigma_KLCI^2 = sum_i sum_j w_i w_j sigma_i sigma_j rho_ij

Ported from the author's klci_warrant_vix.py, with its two blocked dependencies
replaced by this app's own infrastructure:

    scipy.brentq          -> _iv() bisection here (no scipy in requirements)
    yfinance prices/caps  -> klse_prices.close_panel + klse_quotes (yfinance is
                             blocked from datacenter IPs, which is why this app
                             scrapes klsescreener for everything else)
    /v2/screener-warrants -> the working /v2/screener_warrants/quote_results
                             endpoint, queried per underlying via m_code

IMPORTANT, and why this does not replace klci_vix: warrant prices are a LIVE
snapshot with no history to backfill, so a wVIX can only ever be computed for
today. Each run appends one row to a CSV on the persistent disk, so the series
builds forward from the first run.

CAVEATS carried over from the original: issuer-quoted prices carry a vol premium
(the level reads high), last-trade prices can be stale, and the correlation is
historical rather than implied. Treat it as a relative gauge, not a VIX
equivalent. This feed also exposes no volume column, so unlike the original we
cannot drop warrants that did not trade today — stale quotes are filtered only
by moneyness, minimum price, expiry and a sane-IV band.
"""
from __future__ import annotations

import csv
import datetime as _dt
import math
import os
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from bs4 import BeautifulSoup as _BS

from . import klse_prices, klse_quotes
from . import index_health as ih

CACHE_DIR = Path(os.environ.get("BURSA_CACHE_DIR")
                 or Path(__file__).resolve().parents[2] / "_cache")
HISTORY_CSV = CACHE_DIR / "klci_wvix_history.csv"

BASE = "https://www.klsescreener.com"
WARRANTS_URL = BASE + "/v2/screener_warrants/quote_results"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36"),
           "X-Requested-With": "XMLHttpRequest",
           "Referer": BASE + "/v2/screener-warrants"}

RATE = 0.03              # MYR risk-free (continuous), ~OPR / 1y MGS
DEFAULT_DIV = 0.035      # used when the quote feed has no dividend yield
INDEX_DIV = 0.04         # KLCI dividend yield
INDEX_MCODE = "0650"     # FBMKLCI underlying (index warrants)
TARGET_DAYS = 30
MIN_DAYS, MAX_DAYS = 14, 400
# --- stale-quote defence -----------------------------------------------------
# The original drops warrants that did not trade today (require_volume). This
# feed exposes no volume column, and the screener's relative_volume_min filter
# returns an EMPTY chain for every underlying, so that defence is unavailable.
# Verified live, first run: the basket read 18.6% while the FBMKLCI index
# warrants read 8.6% and implied correlation came out ~0 (impossible) — the
# signature of stale quotes on illiquid warrants inflating IV. These three
# substitutes deliberately deviate from the script, and the page says so:
#   * a tighter moneyness band, so only near-ATM (the liquid ones) count
#   * a tighter sane-IV band
#   * the MEDIAN per expiry once there are enough quotes, so one stale outlier
#     cannot drag the bucket, and a per-stock IV/RV sanity cap
MONEYNESS_BAND = 0.08    # |ln(K/S)| <= band (original: 0.15)
MIN_PRICE = 0.03         # RM; below this the half-sen tick dominates the IV
IV_LO, IV_HI = 0.05, 1.20  # sane-IV band for a warrant (original: 0.02-2.0)
MEDIAN_MIN_QUOTES = 3    # per expiry: median once we have this many quotes
MAX_IV_RV_RATIO = 3.0    # reject a stock's IV above this multiple of its realized vol
CORR_LOOKBACK = 60
RV_LOOKBACK = 21
ANN = 252
REQUEST_PAUSE = 0.35     # be gentle: ~31 sequential POSTs per build


# --------------------------------------------------------------------------- #
# Black-Scholes + implied vol (no scipy; per-stock dividend yield)
# --------------------------------------------------------------------------- #
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S, K, T, r, q, vol, call: bool) -> float:
    if vol <= 0 or T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    sq = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * vol * vol) * T) / sq
    d2 = d1 - sq
    if call:
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def _iv(px_per_share, S, K, T, r, q, call: bool):
    """Implied vol by bisection on [1e-4, 5]; None when unsolvable/below intrinsic."""
    if not px_per_share or px_per_share <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = _bs_price(S, K, T, r, q, lo, call) - px_per_share
    f_hi = _bs_price(S, K, T, r, q, hi, call) - px_per_share
    if f_lo > 0 or f_hi < 0:        # below intrinsic, or absurdly expensive
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(S, K, T, r, q, mid, call) - px_per_share
        if abs(f_mid) < 1e-10 or (hi - lo) < 1e-9:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Scrape one underlying's warrant chain
# --------------------------------------------------------------------------- #
_RATIO_RE = re.compile(r"^\s*([\d.]+)\s*:\s*([\d.]+)\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _num(txt):
    try:
        return float(str(txt).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _ratio(txt):
    """'500 : 1' -> 500.0 (warrants per ONE underlying share)."""
    m = _RATIO_RE.match(str(txt or ""))
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
            return a / b if b else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return _num(txt)


def fetch_chain(sess, m_code: str) -> list[dict]:
    """Warrant rows for one underlying.

    Row layout: Name | Maturity | Price | Strike | Mother Share | Ratio | Gearing | ...
    """
    r = sess.post(WARRANTS_URL, data={"getquote": "1", "m_code": str(m_code)},
                  headers=HEADERS, timeout=45)
    if not r.ok:
        return []
    out = []
    for tr in _BS(r.text, "html.parser").find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 6 or not _DATE_RE.match(tds[1]):
            continue
        name = tds[0].upper()
        suffix = name.split("-")[-1] if "-" in name else ""
        if suffix.startswith("C"):
            call = True
        elif suffix.startswith("H"):
            call = False
        else:
            continue                       # not a structured call/put warrant
        out.append({"name": name, "maturity": tds[1], "call": call,
                    "price": _num(tds[2]), "strike": _num(tds[3]),
                    "spot": _num(tds[4]), "ratio": _ratio(tds[5])})
    return out


# --------------------------------------------------------------------------- #
# Per-underlying 30-day IV
# --------------------------------------------------------------------------- #
def per_underlying_iv(rows: list[dict], q: float, today: _dt.date,
                      target_T: float) -> tuple[float | None, int]:
    """ATM-weighted IV per expiry, interpolated linear-in-total-variance to 30d."""
    buckets: dict[float, list[tuple[float, float]]] = {}
    used = 0
    for w in rows:
        px, K, S, cr = w["price"], w["strike"], w["spot"], w["ratio"]
        if not all(isinstance(v, (int, float)) and v for v in (px, K, S, cr)):
            continue
        if px < MIN_PRICE or K <= 0 or S <= 0 or cr <= 0:
            continue
        try:
            mat = _dt.date.fromisoformat(w["maturity"])
        except ValueError:
            continue
        days = (mat - today).days
        if days < MIN_DAYS or days > MAX_DAYS:
            continue                                   # expired / too far out
        if abs(math.log(K / S)) > MONEYNESS_BAND:
            continue
        T = days / 365.0
        iv = _iv(px * cr, S, K, T, RATE, q, w["call"])  # price per ONE share
        if iv is None or not (IV_LO <= iv <= IV_HI):
            continue
        weight = math.exp(-((math.log(K / S)) / 0.05) ** 2)   # ATM-weighted
        buckets.setdefault(T, []).append((iv, weight))
        used += 1
    if not buckets:
        return None, 0

    Ts = sorted(buckets)
    vs = []
    for T in Ts:
        pairs = buckets[T]
        if len(pairs) >= MEDIAN_MIN_QUOTES:
            # median: one stale quote cannot drag the expiry (stale-quote defence)
            s = sorted(iv for iv, _w in pairs)
            mid = len(s) // 2
            vs.append(s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid]))
        else:
            wsum = sum(w for _, w in pairs) + 1e-9
            vs.append(sum(iv * w for iv, w in pairs) / wsum)
    if len(Ts) == 1 or target_T <= Ts[0]:
        return float(vs[0]), used          # flat extrapolation
    if target_T >= Ts[-1]:
        return float(vs[-1]), used
    tv = np.interp(target_T, Ts, [v * v * T for v, T in zip(vs, Ts)])
    return float(math.sqrt(tv / target_T)), used


# --------------------------------------------------------------------------- #
# History on the persistent disk (one row per date)
# --------------------------------------------------------------------------- #
_FIELDS = ["date", "wvix_pct", "index_warrant_iv_pct", "klci_rv21_pct",
           "avg_corr", "implied_corr", "names_with_iv", "weight_coverage_pct",
           "warrants_used"]


def _read_history() -> list[dict]:
    if not HISTORY_CSV.exists():
        return []
    try:
        with HISTORY_CSV.open("r", encoding="utf-8", newline="") as fh:
            return [r for r in csv.DictReader(fh) if r.get("date")]
    except Exception:  # noqa: BLE001
        return []


def _append_history(row: dict) -> list[dict]:
    rows = {r["date"]: r for r in _read_history()}
    rows[row["date"]] = {k: ("" if row.get(k) is None else row.get(k)) for k in _FIELDS}
    out = [rows[d] for d in sorted(rows)]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY_CSV.open("w", encoding="utf-8", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=_FIELDS)
            wr.writeheader()
            wr.writerows(out)
    except Exception:  # noqa: BLE001
        pass          # a read-only disk must not fail the whole computation
    return out


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_wvix_payload() -> dict:
    import requests

    today = _dt.date.today()
    target_T = TARGET_DAYS / 365.0

    cons = ih.fetch_klci_constituents_klse()          # [(code, name, sector), ...]
    if not cons:
        raise RuntimeError("KLCI wVIX: no constituents")
    codes = [c for c, _n, _s in cons]

    quotes = {}
    try:
        quotes = klse_quotes.get_quotes() or {}
    except Exception:  # noqa: BLE001
        pass

    # prices for realized vol + the correlation matrix (klsescreener, not yfinance)
    tickers = [f"{c}.KL" for c in codes]
    px = klse_prices.close_panel(tickers + [ih.INDEX_SYMBOL_KLCI], lookback="1y")
    rets = np.log(px.astype("float64")).diff().dropna(how="all")

    sess = requests.Session()
    sess.headers.update(HEADERS)

    names, ivs, rvs, weights, nwar = [], [], [], [], []
    rejected = 0
    for code, name, _sec in cons:
        tkr = f"{code}.KL"
        qrow = quotes.get(code) or {}
        dy = qrow.get("dy")
        q = (float(dy) / 100.0) if isinstance(dy, (int, float)) and dy and dy > 0 else DEFAULT_DIV
        try:
            rows = fetch_chain(sess, code)
        except Exception:  # noqa: BLE001
            rows = []
        iv, n = per_underlying_iv(rows, q, today, target_T)
        mcap = qrow.get("market_cap")
        rv = (float(rets[tkr].tail(RV_LOOKBACK).std() * math.sqrt(ANN))
              if tkr in rets.columns and rets[tkr].tail(RV_LOOKBACK).notna().sum() > 5 else np.nan)
        # sanity cap: an IV far above the stock's own realized vol is a stale
        # quote, not a forecast — drop it and let the RV x premium fallback stand
        if iv is not None and rv == rv and rv > 0 and iv > MAX_IV_RV_RATIO * rv:
            iv, rejected = None, rejected + 1
        names.append(name)
        ivs.append(iv if iv is not None else np.nan)
        rvs.append(rv)
        weights.append(float(mcap) if isinstance(mcap, (int, float)) and mcap and mcap > 0 else np.nan)
        nwar.append(n)
        time.sleep(REQUEST_PAUSE)

    st = pd.DataFrame({"code": codes, "name": names, "ticker": tickers,
                       "iv30": ivs, "rv21": rvs, "mcap": weights, "n_warrants": nwar})
    if st["mcap"].notna().sum() == 0:
        st["mcap"] = 1.0                               # equal weight if caps missing
    st["mcap"] = st["mcap"].fillna(st["mcap"].median())
    st["weight"] = st["mcap"] / st["mcap"].sum()

    # names with no usable warrant: realized vol x the median IV/RV premium
    prem = (st["iv30"] / st["rv21"]).replace([np.inf, -np.inf], np.nan).median()
    prem = float(prem) if prem == prem and prem > 0 else 1.0
    st["vol_used"] = st["iv30"].fillna(st["rv21"] * prem)
    st["vol_used"] = st["vol_used"].fillna(st["vol_used"].median())
    st["source"] = np.where(st["iv30"].notna(), "warrant IV", "RV x premium")
    if st["vol_used"].notna().sum() == 0:
        raise RuntimeError("KLCI wVIX: no usable volatilities")

    # index vol from the weighted covariance
    cols = [t for t in st["ticker"] if t in rets.columns]
    R = rets[cols].tail(CORR_LOOKBACK).corr().reindex(index=st["ticker"], columns=st["ticker"]).values
    R = np.nan_to_num(np.asarray(R, dtype="float64"), nan=0.0)
    np.fill_diagonal(R, 1.0)
    wv = (st["weight"].fillna(0.0) * st["vol_used"].fillna(0.0)).to_numpy(dtype="float64")
    var_i = float(wv @ R @ wv)
    wvix = math.sqrt(max(var_i, 0.0))

    sq = float((wv ** 2).sum())
    denom = float(wv.sum() ** 2 - sq)
    avg_rho = (var_i - sq) / denom if denom > 1e-18 else float("nan")

    # the FBMKLCI index warrants themselves -> direct index IV + implied correlation
    try:
        idx_rows = fetch_chain(sess, INDEX_MCODE)
    except Exception:  # noqa: BLE001
        idx_rows = []
    idx_iv, idx_n = per_underlying_iv(idx_rows, INDEX_DIV, today, target_T)
    impl_rho = (((idx_iv ** 2) - sq) / denom
                if idx_iv is not None and denom > 1e-18 else None)

    klci_rv = np.nan
    if ih.INDEX_SYMBOL_KLCI in rets.columns:
        klci_rv = float(rets[ih.INDEX_SYMBOL_KLCI].tail(RV_LOOKBACK).std() * math.sqrt(ANN))

    cover = float(st.loc[st["iv30"].notna(), "weight"].sum())

    def _r(x, nd=2):
        return None if x is None or x != x else round(float(x), nd)

    row = {"date": today.isoformat(),
           "wvix_pct": _r(100 * wvix),
           "index_warrant_iv_pct": _r(100 * idx_iv) if idx_iv is not None else None,
           "klci_rv21_pct": _r(100 * klci_rv),
           "avg_corr": _r(avg_rho, 3),
           "implied_corr": _r(impl_rho, 3),
           "names_with_iv": int(st["iv30"].notna().sum()),
           "weight_coverage_pct": _r(100 * cover, 1),
           "warrants_used": int(sum(nwar)),
           "iv_rejected_names": int(rejected)}
    hist = _append_history(row)

    def _f(v):
        try:
            return None if v in ("", None) else float(v)
        except (TypeError, ValueError):
            return None

    constituents = [
        {"code": r.code, "name": r.name,
         "iv30_pct": _r(100 * r.iv30) if r.iv30 == r.iv30 else None,
         "rv21_pct": _r(100 * r.rv21) if r.rv21 == r.rv21 else None,
         "weight_pct": _r(100 * r.weight, 2),
         "n_warrants": int(r.n_warrants), "source": r.source}
        for r in st.sort_values("weight", ascending=False).itertuples()
    ]

    return {**row,
            "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
            "target_days": TARGET_DAYS,
            "iv_rv_premium_median": _r(prem, 3),
            "index_warrants_used": idx_n,
            "history": {"dates": [h["date"] for h in hist],
                        "wvix": [_f(h.get("wvix_pct")) for h in hist],
                        "index_iv": [_f(h.get("index_warrant_iv_pct")) for h in hist],
                        "klci_rv": [_f(h.get("klci_rv21_pct")) for h in hist]},
            "constituents": constituents,
            "note": ("Warrant-implied 30-day vol. Issuer quotes carry a premium so the "
                     "level reads above realized vol; correlation is historical, not "
                     "implied. This feed has no volume column, so instead of dropping "
                     "warrants that did not trade we use the median IV per expiry, a "
                     "tighter near-ATM band and an IV/RV sanity cap — a deviation from "
                     "the reference script. History starts from the first run: warrant "
                     "prices cannot be backfilled.")}

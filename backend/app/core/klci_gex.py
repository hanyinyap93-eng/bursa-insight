"""
KLCI index-warrant Gamma Exposure (GEX) — issuer hedging map.

Backend port of the notebook's Sections 7-8 (klci_gex.py). Pipeline:

  1. DISCOVER the live FBMKLCI structured-warrant chain. Layers are merged:
       [T]  TradingView symbol search (live-only by construction, primary)
       [M]  Macquarie Malaysia warrant-search API (best-effort)
       [i3] i3investor related-warrants pages seeded from known counters
       [L]  legacy klsescreener endpoints (only if still <3 names)
     Falls back to the embedded GEX_CODES snapshot if every layer fails.
     The discovered live set is snapshotted to CSV (12h TTL) for fast re-runs.
  2. SCRAPE each warrant's terms (type/strike/ratio/maturity/issue size/price)
     from its klsescreener page, with i3investor + the Bursa term-sheet
     announcement as fallbacks.
  3. PRICE issuer gamma per warrant (Black-Scholes; IV implied from the traded
     warrant price when solvable, else GEX_DEFAULT_IV) and aggregate.

Sign convention: Bursa SW issuers are ALWAYS short the warrants they list and
delta-hedge via FKLI => issuer gamma is NEGATIVE for calls AND puts. The map
shows destabilising hedge-flow zones, not SPX-style pinning.
"""
from __future__ import annotations

import datetime as _dt
import math
import re as _re
import time as _time
from pathlib import Path

import pandas as pd
import requests as _rq

try:  # Cloudflare blocks python-requests TLS fingerprints on some networks;
    # curl_cffi impersonates Chrome's handshake. Optional: pip install curl_cffi
    from curl_cffi import requests as _cffi
except ImportError:
    _cffi = None

from bs4 import BeautifulSoup as _BS

from .index_health import HEADERS

CACHE_DIR = Path(__file__).resolve().parents[2] / "_cache"

GEX_BASE = "https://www.klsescreener.com"
GEX_HEADERS = {"User-Agent": HEADERS["User-Agent"],
               "Referer": GEX_BASE + "/v2/screener-warrants",
               "X-Requested-With": "XMLHttpRequest"}
GEX_R, GEX_Q, GEX_DEFAULT_IV = 0.030, 0.038, 0.12  # MYR r, KLCI div yield, IV fallback
GEX_HAIRCUT = 0.5    # fraction of issue size assumed investor-held
GEX_LIVE_CACHE = CACHE_DIR / "klci_live_warrants.csv"
GEX_LIVE_TTL_H = 12  # hours before re-discovery

# Embedded fallback chain (screener-warrants UI snapshot 2026-07-05; expired
# names are dropped automatically at fetch). Used only if discovery fails.
GEX_CODES = [
    "FBMKLCI-HEK", "FBMKLCI-CRG", "FBMKLCI-HEP", "FBMKLCI-CRF",
    "FBMKLCI-HEG", "FBMKLCI-CRR", "FBMKLCI-CRJ", "FBMKLCI-HER",
    "FBMKLCI-HEO", "FBMKLCI-HEM", "FBMKLCI-CRI", "FBMKLCI-HEQ",
    "FBMKLCI-CRH", "FBMKLCI-HEN", "FBMKLCI-CRQ", "FBMKLCI-HEL",
    "FBMKLCI-CRB", "FBMKLCI-CRK", "FBMKLCI-HEJ", "FBMKLCI-CRE",
]

I3_BASE = "https://klse.i3investor.com"
I3_SEEDS = ["0200I", "0650G7", "0650QT", "0650HB"]

MQ_BASE = "https://www.malaysiawarrants.com.my"
MQ_CANDIDATES = [
    "/apimqmy/warrantsearch?underlying=FBMKLCI&type=all&issuer=all"
    "&maturity=all&expiry=all&moneyness=all&effectiveGearing=all&indicator=all",
    "/apimqmy/searchwarrantsdata?underlying=FBMKLCI",
]

GEX_TV_QUERIES = ["FBMKLCI-", "FBMKLCI"]
GEX_TV_ENDPOINTS = [  # TradingView migrated search to v3; try both shapes
    "https://symbol-search.tradingview.com/symbol_search/v3/"
    "?text={q}&hl=0&exchange=MYX&lang=en&search_type=undefined"
    "&domain=production&sort_by_country=MY",
    "https://symbol-search.tradingview.com/symbol_search/"
    "?text={q}&hl=0&exchange=MYX&lang=en&type=&domain=production",
]


def gex_session():
    """Chrome-impersonating session if curl_cffi is installed, else requests."""
    if _cffi is not None:
        return _cffi.Session(impersonate="chrome")
    return _rq.Session()


def _name_to_code(name):
    """FBMKLCI-CFS -> 0650FS ; FBMKLCI-HG7 -> 0650G7 (drop the C/H letter)."""
    try:
        suf = name.split("-")[1]
        return "0650" + suf[1:]
    except (IndexError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Discovery layers
# --------------------------------------------------------------------------- #
def _harvest(text, found):
    """Pull FBMKLCI names (+codes when linked) out of any HTML/JSON blob."""
    for m in _re.finditer(
            r'href="[^"]*/web/stock/[a-z-]+/(0650[A-Za-z0-9]{2})"[^>]*>\s*'
            r'(FBMKLCI-[CH][A-Z0-9]{1,3})', text):
        found[m.group(2).upper()] = m.group(1).upper()
    soup = _BS(text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = _re.search(r"/v2/stocks/view/([0-9A-Za-z]+)", a["href"])
        nm = a.get_text(strip=True).upper()
        if m and nm.startswith("FBMKLCI-"):
            found[nm] = m.group(1)
    for m in _re.finditer(r"(FBMKLCI-[CH][A-Z0-9]{1,3})\b", text):
        nm = m.group(1).upper()
        found.setdefault(nm, _name_to_code(nm))
    return found


def _tv_items(sess, query):
    import json as _j
    for url in GEX_TV_ENDPOINTS:
        try:
            r = sess.get(url.format(q=query),
                         headers={"User-Agent": GEX_HEADERS["User-Agent"],
                                  "Origin": "https://www.tradingview.com",
                                  "Referer": "https://www.tradingview.com/"},
                         timeout=30)
            if r.ok:
                data = _j.loads(r.text.replace("<em>", "").replace("</em>", ""))
                if isinstance(data, dict):  # v3: {"symbols": [...]}
                    data = data.get("symbols", [])
                if data:
                    return data
        except Exception:  # noqa: BLE001
            continue
    return []


def _tv_search(sess):
    """{name: code} of live FBMKLCI warrants from TradingView (live-only)."""
    found = {}
    for q in GEX_TV_QUERIES:
        for it in _tv_items(sess, q):
            blob = " ".join(str(it.get(k, "")) for k in
                            ("symbol", "description", "prefix"))
            blob = blob.replace("<em>", "").replace("</em>", "").upper()
            m = _re.search(r"(FBMKLCI-[CH][A-Z0-9]{1,3})\b", blob)
            if not m:
                continue
            nm = m.group(1)
            mc = _re.search(r"\b(0650[A-Z0-9]{2})\b", blob)
            found[nm] = mc.group(1) if mc else _name_to_code(nm)
        if found:
            break
    return found


def _mq_chain(sess):
    """Macquarie warrant-search API: live warrants, all issuers (best-effort)."""
    found = {}
    for path in MQ_CANDIDATES:
        try:
            r = sess.get(MQ_BASE + path,
                         headers={"User-Agent": GEX_HEADERS["User-Agent"],
                                  "Referer": MQ_BASE + "/tools/warrantsearch/",
                                  "Accept": "application/json, */*"},
                         timeout=30)
            if r.ok and "FBMKLCI-" in r.text:
                for m in _re.finditer(
                        r'(0650[A-Z0-9]{2})\W{1,80}?(FBMKLCI-[CH][A-Z0-9]{1,3})'
                        r'|(FBMKLCI-[CH][A-Z0-9]{1,3})\W{1,80}?(0650[A-Z0-9]{2})',
                        r.text.upper()):
                    code = m.group(1) or m.group(4) or ""
                    name = m.group(2) or m.group(3) or ""
                    if name and code:
                        found[name] = code
                for m in _re.finditer(r"(FBMKLCI-[CH][A-Z0-9]{1,3})\b",
                                      r.text.upper()):
                    found.setdefault(m.group(1), _name_to_code(m.group(1)))
        except Exception:  # noqa: BLE001
            pass
        if found:
            break
    return found


def gex_discover(sess):
    """Find FBMKLCI warrants; layers are merged. Returns ({name: code}, {name: source})."""
    found, src = {}, {}

    def _merge(new, tag):
        for k, v in (new or {}).items():
            if k not in found:
                found[k] = v
                src[k] = tag
            elif found[k] is None and v:
                found[k] = v

    hdr = dict(GEX_HEADERS)
    hdr.update({"Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"})

    try:  # [T] TradingView — live-only by construction (primary)
        _merge(_tv_search(sess), "tradingview")
    except Exception:  # noqa: BLE001
        pass
    try:  # [M] Macquarie — live-only, all issuers
        _merge(_mq_chain(sess), "macquarie")
    except Exception:  # noqa: BLE001
        pass

    # [i3] chain seeds — cheap and proven live
    seeds = list(dict.fromkeys(I3_SEEDS + [c for c in list(found.values())[:2]]))[:6]
    for seed in seeds:
        try:
            r = sess.get(f"{I3_BASE}/web/stock/related-warrants/{seed}",
                         headers=hdr, timeout=60)
            if r.ok and "FBMKLCI-" in r.text:
                _merge(_harvest(r.text, {}), "i3")
        except Exception:  # noqa: BLE001
            pass
        _time.sleep(0.4)

    # [L] legacy klsescreener endpoints
    if len(found) < 3:
        battery = [
            ("POST", GEX_BASE + "/v2/screener-warrants/quote_results",
             {"getquote": "1"}),
            ("POST", GEX_BASE + "/v2/screener/quote_results",
             {"getquote": "1", "board": "3", "sector": "24"}),
            ("GET", GEX_BASE + "/v2/announcements/index/1", None),
        ]
        for method, url, data in battery:
            try:
                if method == "POST":
                    h = dict(hdr)
                    h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                    r = sess.post(url, data=data, headers=h, timeout=60)
                else:
                    r = sess.get(url, headers=hdr, timeout=60)
                if r.ok and "FBMKLCI-" in r.text:
                    _merge(_harvest(r.text, {}), "legacy")
            except Exception:  # noqa: BLE001
                pass
            _time.sleep(0.3)
    return found, src


# --------------------------------------------------------------------------- #
# Per-warrant term scraping
# --------------------------------------------------------------------------- #
def _i3_terms(sess, name, code, w):
    """Fill missing term fields from the i3investor overview page."""
    try:
        r = sess.get(f"{I3_BASE}/web/stock/overview/{code}",
                     headers=GEX_HEADERS, timeout=30)
        if not r.ok:
            return w
    except Exception:  # noqa: BLE001
        return w
    t = _BS(r.text, "html.parser").get_text("|", strip=True)

    def rx(pat, cast=str):
        m = _re.search(pat, t, _re.I)
        if not m:
            return None
        try:
            return cast(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            return None

    if not w.get("wtype"):
        m = _re.search(r"\b([CP])W FTSE BURSA MALAYSIA KLCI", t)
        w["wtype"] = {"C": "CALL", "P": "PUT"}.get(m.group(1)) if m else None
    if not w.get("maturity"):
        mm = _re.search(r"Maturity Date\|(\d{1,2})-([A-Za-z]{3})-(\d{4})", t)
        if mm:
            try:
                w["maturity"] = _dt.datetime.strptime(
                    "-".join(mm.groups()), "%d-%b-%Y").date().isoformat()
            except ValueError:
                pass
    if not w.get("strike"):
        w["strike"] = rx(r"Exercise Price\|(?:MYR\s*)?([\d,]+\.?\d*)", float)
    if not w.get("ratio"):
        w["ratio"] = rx(r"Ratio\|([\d.]+)\s*:\s*1", float)
    if not w.get("issue_size"):
        w["issue_size"] = rx(r"Issue Size\|([\d,]+)", float)
    if not w.get("issuer"):
        w["issuer"] = rx(r"Issuer\|([A-Z][A-Z ().&\-]+)")
    if not w.get("underlying"):
        w["underlying"] = rx(r"Underlying Stock Price\|([\d,]+\.?\d*)", float)
    return w


def _cell_after(soup, label):
    for td in soup.find_all(["td", "th"]):
        if td.get_text(strip=True).lower() == label.lower():
            nxt = td.find_next_sibling(["td", "th"])
            if nxt:
                return nxt.get_text(strip=True)
    return None


def gex_fetch_warrant(sess, name, code):
    """Scrape one warrant's terms from its klsescreener page."""
    r = None
    for slug in filter(None, dict.fromkeys([code, name])):
        for _attempt in range(2):  # retry once with backoff
            try:
                r = sess.get(f"{GEX_BASE}/v2/stocks/view/{slug}",
                             headers=GEX_HEADERS, timeout=30)
                if r.ok and name.split("-")[0] in r.text:
                    break
            except Exception:  # noqa: BLE001
                r = None
            _time.sleep(5)
        if r is not None and r.ok and name.split("-")[0] in r.text:
            break
    if r is None or not r.ok:
        return None
    soup = _BS(r.text, "html.parser")

    def grab(label, pat, cast=str):
        v = _cell_after(soup, label)
        if v is None:
            m = _re.search(pat, soup.get_text("|", strip=True))
            v = m.group(1) if m else None
        if v is None:
            return None
        v = v.replace(",", "")
        if label == "Ratio":
            m = _re.match(r"([\d.]+)\s*:\s*1", v)
            v = m.group(1) if m else v
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    price = None
    m = _re.search(r'name="twitter:data1"\s+content="([\d.]+)"', r.text)
    if m:
        price = float(m.group(1))
    if price is None:
        for tag in soup.find_all(["h2", "h5"]):
            t = tag.get_text(strip=True)
            if _re.fullmatch(r"\d+\.\d{3}", t):
                price = float(t)
                break
    m = _re.search(r"UP TO\s+([\d,]+)\s+EUROPEAN", r.text, _re.I)
    issue = float(m.group(1).replace(",", "")) if m else None

    w = dict(name=name, code=code or name,
             wtype=grab("Type", r"Type\s*\|?\s*(CALL|PUT)"),
             maturity=grab("Maturity", r"Maturity\s*\|?\s*(\d{4}-\d{2}-\d{2})"),
             strike=grab("Strike value", r"Strike value\s*\|?\s*([\d,]+\.?\d*)", float),
             ratio=grab("Ratio", r"Ratio\s*\|?\s*([\d.]+)\s*:\s*1", float),
             underlying=grab("Share Price", r"Share Price\s*\|?\s*([\d,]+\.?\d*)", float),
             issuer=grab("Issuer", r"Issuer\s*\|?\s*([A-Z][A-Z ().&\-]+)"),
             price=price, issue_size=issue)
    if not all([w["wtype"], w["maturity"], w["strike"], w["ratio"],
                w["issue_size"]]):
        w = _i3_terms(sess, name, w["code"], w)
    if not w.get("issue_size"):
        try:  # term-sheet title on the warrant's announcements page
            ra = sess.get(f"{GEX_BASE}/v2/announcements/stock/{w['code']}",
                          headers=GEX_HEADERS, timeout=30)
            m2 = _re.search(r"UP TO\s+([\d,]+)\s+EUROPEAN", ra.text, _re.I)
            if m2:
                w["issue_size"] = float(m2.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            pass
    if not all([w["wtype"], w["maturity"], w["strike"], w["ratio"]]):
        return None
    return w


# --------------------------------------------------------------------------- #
# Black-Scholes gamma / implied vol (math.erf — no scipy dependency)
# --------------------------------------------------------------------------- #
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _npdf(x):
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _bs_price(S, K, T, r, q, s, call):
    if T <= 0 or s <= 0:
        return max((S - K) if call else (K - S), 0.0) * math.exp(-r * max(T, 0))
    d1 = (math.log(S / K) + (r - q + 0.5 * s * s) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    if call:
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def _bs_gamma(S, K, T, r, q, s):
    if T <= 0 or s <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * s * s) * T) / (s * math.sqrt(T))
    return math.exp(-q * T) * _npdf(d1) / (S * s * math.sqrt(T))


def _iv(px_index, S, K, T, call):
    """Implied vol by bisection on [1e-3, 3.0]; None if unsolvable."""
    if not px_index or px_index <= 0 or T <= 0:
        return None
    if px_index <= _bs_price(S, K, T, GEX_R, GEX_Q, 1e-9, call) + 1e-9:
        return None
    lo, hi = 1e-3, 3.0
    f_lo = _bs_price(S, K, T, GEX_R, GEX_Q, lo, call) - px_index
    f_hi = _bs_price(S, K, T, GEX_R, GEX_Q, hi, call) - px_index
    if f_lo * f_hi > 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(S, K, T, GEX_R, GEX_Q, mid, call) - px_index
        if abs(f_mid) < 1e-10 or (hi - lo) < 1e-9:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# GEX computation
# --------------------------------------------------------------------------- #
def gex_compute(dfw, spot, held=None, haircut=GEX_HAIRCUT):
    """Per-warrant issuer GEX (RM per 1% KLCI move; negative = short gamma)."""
    held, today, rows = held or {}, _dt.date.today(), []
    for _, w in dfw.iterrows():
        T = (_dt.date.fromisoformat(w.maturity) - today).days / 365.0
        if T <= 0:
            continue  # expired
        call = w.wtype == "CALL"
        units = held.get(w["name"], (w.issue_size or 0) * haircut)
        basis = "investor-held" if w["name"] in held else f"issue x {haircut:g}"
        if units <= 0:
            continue  # issue size unknown
        iv = _iv((w.price or 0) * w.ratio, spot, w.strike, T, call) or GEX_DEFAULT_IV
        gex = -_bs_gamma(spot, w.strike, T, GEX_R, GEX_Q, iv) \
            * (units / w.ratio) * spot ** 2 * 0.01
        rows.append({**w, "T_years": T, "iv": iv, "units": units,
                     "units_basis": basis, "gex_rm_per_1pct": gex})
    return pd.DataFrame(rows)


def gex_profile(res, spot, span=0.06, n=101):
    """Net issuer GEX re-priced over a hypothetical +/-span spot range."""
    import numpy as np
    grid = np.linspace(spot * (1 - span), spot * (1 + span), n)
    prof = np.zeros_like(grid)
    for _, w in res.iterrows():
        prof += [-_bs_gamma(s, w.strike, w.T_years, GEX_R, GEX_Q, w.iv)
                 * (w.units / w.ratio) * s ** 2 * 0.01 for s in grid]
    return grid, prof


def run_klci_gex(spot=None, haircut=GEX_HAIRCUT, use_live_cache=True):
    """Discover + scrape the FBMKLCI warrant chain and compute the GEX table.

    Returns (res DataFrame, spot float). Raises RuntimeError when no live
    warrant survives filtering.
    """
    sess = gex_session()
    warr, wsrc = None, {}
    if use_live_cache and GEX_LIVE_CACHE.exists():
        age_h = (_dt.datetime.now() - _dt.datetime.fromtimestamp(
            GEX_LIVE_CACHE.stat().st_mtime)).total_seconds() / 3600
        if age_h <= GEX_LIVE_TTL_H:
            lc = pd.read_csv(GEX_LIVE_CACHE)
            if len(lc):
                warr = dict(zip(lc["name"], lc["code"]))
                wsrc = dict(zip(lc["name"], lc.get("source", "cache")))
    if warr is None:
        warr, wsrc = gex_discover(sess)
    if not warr:  # every discovery layer failed -> embedded snapshot
        warr = {c.upper(): _name_to_code(c.upper()) for c in GEX_CODES}
        wsrc = {k: "fallback" for k in warr}
    det = []
    for nm, cd in sorted(warr.items()):
        w = gex_fetch_warrant(sess, nm, cd)
        if w:
            det.append(w)
        _time.sleep(0.8)
    dfw = pd.DataFrame(det)
    if dfw.empty:
        raise RuntimeError("no warrant details scraped")
    if spot is None:
        med = dfw.underlying.dropna().median() if dfw.underlying.notna().any() else None
        if med and med > 500:
            spot = float(med)
        else:
            import yfinance as yf
            spot = float(yf.Ticker("^KLSE").history(period="5d")["Close"]
                         .dropna().iloc[-1])
    res = gex_compute(dfw, spot, None, haircut)
    if res.empty:
        raise RuntimeError(
            "No LIVE FBMKLCI warrants survived filtering. Bursa may have few "
            "or no active FBMKLCI index warrants listed right now.")
    res["discovery_source"] = res["name"].map(wsrc).fillna("?")
    try:  # snapshot the LIVE set for fast re-runs
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"name": res["name"], "code": res["code"],
                      "source": res["discovery_source"]}) \
            .drop_duplicates("name").to_csv(GEX_LIVE_CACHE, index=False)
    except Exception:  # noqa: BLE001
        pass
    return res, spot


# --------------------------------------------------------------------------- #
# JSON payload + Index Health x GEX readout
# --------------------------------------------------------------------------- #
def _readout(health_pct, net_mn, spot, trough):
    """Regime read combining breadth health with GEX geometry (EN + ZH)."""
    dist = (spot - trough) / spot * 100
    in_zone = abs(dist) < 1.0
    strong = health_pct is not None and health_pct > 20
    weak = health_pct is not None and health_pct < -15

    pos_en = ("spot is INSIDE the max hedging-pressure zone" if in_zone else
              f"gamma trough sits {abs(dist):.1f}% "
              f"{'below' if dist > 0 else 'above'} spot")
    pos_zh = ("现价处于伽马高压区内" if in_zone else
              f"伽马谷位于现价{'下方' if dist > 0 else '上方'}约 {abs(dist):.1f}%")

    if health_pct is None:
        read_en = ("Index Health unavailable - use the gamma zone as a "
                   "volatility filter: wider stops / smaller size inside it.")
        read_zh = "指数健康不可用 - 以伽马区作为波动过滤器：区内宽止损/减仓。"
    elif strong and not in_zone:
        read_en = ("Strong breadth with hedge pressure elsewhere - cleaner trend "
                   "regime; expect acceleration only on a pullback into the gamma zone.")
        read_zh = "健康度强而对冲压力区在别处 - 趋势环境较干净，回踩伽马区时预期加速波动。"
    elif strong and in_zone:
        read_en = ("Strong breadth inside the pressure zone - momentum breakouts get "
                   "amplified by issuer hedging; favour continuation over fading.")
        read_zh = "健康度强但正处于对冲高压区 - 动量突破易被放大，顺势优于逆势。"
    elif weak and in_zone:
        read_en = ("Weak breadth INSIDE the pressure zone - highest amplified-"
                   "volatility risk; reduce FKLI size and widen ATR stops.")
        read_zh = "健康度弱且正处于高压区 - 波动放大风险最高，仓位宜减。"
    elif weak and dist > 0:
        read_en = ("Weak breadth with the gamma trough below - a breakdown into that "
                   "zone can be accelerated by issuer hedge-selling; downside is asymmetric.")
        read_zh = "健康度弱且伽马谷在下方 - 跌入该区可能被发行商对冲加速，下行风险不对称。"
    elif weak:
        read_en = ("Weak breadth with the trough above - rallies into the zone risk "
                   "amplified whipsaw from issuer hedging.")
        read_zh = "健康度弱，伽马谷在上方 - 反弹进入该区易受对冲流放大后回落。"
    else:
        read_en = ("Neutral breadth - use the gamma zone purely as a volatility "
                   "filter: wider stops / smaller size inside it, normal parameters outside.")
        read_zh = "健康度中性 - 以伽马区作为波动过滤器：区内宽止损/减仓，区外正常。"
    return {
        "health_pct": None if health_pct is None else round(float(health_pct), 2),
        "net_gex_rm_mn": round(net_mn, 2),
        "trough": round(float(trough), 1),
        "distance_pct": round(float(dist), 2),
        "in_zone": bool(in_zone),
        "position": pos_en, "position_zh": pos_zh,
        "read": read_en, "read_zh": read_zh,
    }


def build_gex_payload(health_pct=None, spot=None):
    """Run the GEX pipeline and shape everything the frontend needs as JSON."""
    res, spot = run_klci_gex(spot=spot)

    def _rf(x, nd=4):
        try:
            f = float(x)
            return None if f != f else round(f, nd)
        except (TypeError, ValueError):
            return None

    warrants = []
    for _, w in res.iterrows():
        warrants.append({
            "name": w["name"], "code": w["code"], "type": w["wtype"],
            "maturity": w["maturity"], "strike": _rf(w["strike"]),
            "ratio": _rf(w["ratio"]), "price": _rf(w["price"]),
            "issuer": w.get("issuer"), "issue_size": _rf(w["issue_size"], 0),
            "units": _rf(w["units"], 0), "units_basis": w["units_basis"],
            "iv": _rf(w["iv"]), "t_years": _rf(w["T_years"]),
            "gex_rm_per_1pct": _rf(w["gex_rm_per_1pct"], 0),
            "discovery_source": w["discovery_source"],
        })

    by_strike = []
    for k, grp in res.groupby("strike"):
        call_g = float(grp.loc[grp.wtype == "CALL", "gex_rm_per_1pct"].sum())
        put_g = float(grp.loc[grp.wtype == "PUT", "gex_rm_per_1pct"].sum())
        by_strike.append({"strike": _rf(k), "call_gex": round(call_g, 0),
                          "put_gex": round(put_g, 0),
                          "net_gex": round(call_g + put_g, 0)})
    by_strike.sort(key=lambda r: r["strike"])

    grid, prof = gex_profile(res, spot)
    trough = float(grid[prof.argmin()])
    net = float(res.gex_rm_per_1pct.sum())

    top = res.reindex(res.gex_rm_per_1pct.abs()
                      .sort_values(ascending=False).index).head(5)
    top_conc = [{"name": w["name"], "strike": _rf(w["strike"]),
                 "type": w["wtype"], "maturity": w["maturity"],
                 "iv": _rf(w["iv"]), "gex_rm_per_1pct": _rf(w["gex_rm_per_1pct"], 0),
                 "units_basis": w["units_basis"]} for _, w in top.iterrows()]

    return {
        "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
        "spot": round(spot, 2),
        "net_gex_rm_per_1pct": round(net, 0),
        "net_gex_rm_mn": round(net / 1e6, 2),
        "warrants_live": len(res),
        "calls": int((res.wtype == "CALL").sum()),
        "puts": int((res.wtype == "PUT").sum()),
        "assumptions": {"r": GEX_R, "q": GEX_Q, "default_iv": GEX_DEFAULT_IV,
                        "haircut": GEX_HAIRCUT,
                        "note": "issuer short gamma; units = issue size x "
                                "haircut unless investor-held supplied"},
        "warrants": warrants,
        "by_strike": by_strike,
        "profile": {"levels": [round(float(x), 1) for x in grid],
                    "net_gex_rm_mn": [round(float(x) / 1e6, 3) for x in prof],
                    "trough": round(trough, 1)},
        "top_concentrations": top_conc,
        "readout": _readout(health_pct, net / 1e6, spot, trough),
    }

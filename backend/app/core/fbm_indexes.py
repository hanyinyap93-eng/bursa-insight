"""
FBM market-index health — Mid 70, ACE, EMAS, Fledgling.

Backend port of the four FBM_*_Index_Health notebooks. Each index:

  1. Constituents scraped from the investingmalaysia.com category listing
     (paginated; /stock/<symbol>-<code>/ product URLs), with a disk-backed
     last-good fallback like the KLCI constituents.
  2. Sector per stock from the investingmalaysia 'Bursa Malaysia Sector'
     subcategories (scraped once into im_sector_map.csv, then cached), with
     the KLCI curated map + keyword classifier as fallbacks.
  3. Prices via the shared download_prices (klsescreener UDF -> yfinance).
     None of these indexes has a Yahoo symbol, so the index overlay is an
     equal-weight proxy (rebased mean, base = 100) — same as the notebooks.
  4. Health via the same compute_health engine as the KLCI; the per-sector
     Health % is recomputed here over ALL sectors present (the engine's own
     sector loop only covers the 11 KLCI sectors and would drop TEC/REI/TRD).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pandas as pd

from . import index_health as ih

CACHE_DIR = Path(os.environ.get("BURSA_CACHE_DIR")
                 or Path(__file__).resolve().parents[2] / "_cache")
SECTOR_MAP_CSV = CACHE_DIR / "im_sector_map.csv"   # shared code->sector cache
# Packaged seed data (checked into the repo): investingmalaysia blocks some
# cloud IPs, so a fresh server with an empty cache still needs constituents.
DATA_DIR = Path(__file__).resolve().parents[1] / "data"

IM_BASE = "https://investingmalaysia.com/category"
FBM_INDEXES = {
    "MID70":     {"name": "FBM Mid 70",     "slug": "fbm-mid-70"},
    "ACE":       {"name": "FBM ACE",        "slug": "fbm-ace"},
    "EMAS":      {"name": "FBM EMAS",       "slug": "fbm-emas"},
    "FLEDGLING": {"name": "FBM Fledgling",  "slug": "fbm-fledgling"},
}

_HDRS = {"User-Agent": ih.HEADERS["User-Agent"],
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}

# investingmalaysia 'Bursa Malaysia Sector' subcategories -> sector buckets
BM_SECTOR_CATS = {
    "bm-construction": "CST", "bm-consumer-products-services": "CON",
    "bm-energy": "ENE", "bm-financial-services": "FIN", "bm-health-care": "HLT",
    "bm-industrial": "IND", "bm-industrial-products-services": "IND",
    "bm-plantation": "PLT", "bm-property": "PRP", "bm-reit": "REI",
    "bm-technology": "TEC", "bm-telecommunications-media": "TEL",
    "bm-trading-services": "TRD", "bm-transportation-logistics": "TRN",
    "bm-utilities": "UTL",
}
# display names beyond the KLCI's 11 curated sectors
SECTOR_NAME_EXT = {**ih.SECTOR_NAME, "TEC": "Technology", "REI": "REIT",
                   "TRD": "Trading Services", "UNK": "Unclassified"}


def _fetch(url):
    req = urllib.request.Request(url, headers=_HDRS)
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def fetch_category(base_url, max_pages=40, wait=1.0):
    """Walk a paginated investingmalaysia category listing -> {code: symbol}.

    Symbol and Bursa code come from the /stock/<symbol>-<code>/ product URLs.
    Sidebar/footer product widgets are stripped so promoted stocks don't leak
    in. Stops at the first page yielding nothing new (or a 404 past the end).
    """
    out = {}
    for p in range(1, max_pages + 1):
        url = base_url if p == 1 else f"{base_url.rstrip('/')}/page/{p}/"
        try:
            page = _fetch(url)
        except Exception:  # noqa: BLE001
            break
        page = re.sub(r'<ul[^>]*class="[^"]*product_list_widget[^"]*".*?</ul>',
                      " ", page, flags=re.S)
        got = re.findall(r'<h3[^>]*>\s*<a[^>]*/stock/([a-z0-9]+)-([0-9]{4,5})[a-z]*/?"',
                         page, flags=re.S | re.I)
        if not got:
            got = re.findall(r'href="[^"]*/stock/([a-z0-9]+)-([0-9]{4,5})[a-z]*/"',
                             page, flags=re.I)
        before = len(out)
        for slug, code in got:
            out.setdefault(code, slug.upper())
        if len(out) == before:
            break
        time.sleep(wait)
    return out


def load_sector_map():
    """code -> sector abbrev for ~1,100 Bursa stocks. Scraped once, then cached."""
    for path in (SECTOR_MAP_CSV, DATA_DIR / "im_sector_map.csv"):
        if path.exists():
            df = pd.read_csv(path, dtype=str)
            return dict(zip(df["code"], df["sector"]))
    smap = {}
    for cat, abbrev in BM_SECTOR_CATS.items():
        got = fetch_category(f"{IM_BASE}/bursa-malaysia-sector/{cat}/")
        for code in got:
            smap.setdefault(code, abbrev)
    if smap:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"code": list(smap), "sector": list(smap.values())}) \
            .to_csv(SECTOR_MAP_CSV, index=False)
    return smap


def get_fbm_constituents(key: str) -> pd.DataFrame:
    """Constituents DataFrame (Ticker/Code/Name/Sector/Sector Abbrev) with a
    disk-backed last-good fallback, mirroring service.get_constituents()."""
    info = FBM_INDEXES[key]
    const_file = CACHE_DIR / f"fbm_{key.lower()}_constituents.json"
    rows, used = None, None
    try:
        got = fetch_category(f"{IM_BASE}/ftse-bursa-malaysia-index/{info['slug']}/")
        if len(got) >= 5:
            rows, used = got, "investingmalaysia"
    except Exception:  # noqa: BLE001
        rows = None
    if rows is not None:
        smap = load_sector_map()
        recs = []
        for code, nm in rows.items():
            sec = smap.get(code) or ih.CODE_SECTOR.get(code) \
                or ih.classify_sector(nm) or "UNK"
            recs.append({"Ticker": f"{code}.KL", "Code": code, "Name": nm,
                         "Sector": SECTOR_NAME_EXT.get(sec, sec),
                         "Sector Abbrev": sec})
        df = pd.DataFrame(recs).drop_duplicates("Ticker").reset_index(drop=True)
        df.attrs["source"] = used
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            const_file.write_text(df.to_json(orient="records"), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return df
    # scrape failed -> last-good disk copy, then the packaged repo seed
    for path, src in ((const_file, "disk-cache"),
                      (DATA_DIR / f"fbm_{key.lower()}_constituents.json", "seed")):
        if path.exists():
            try:
                cached = pd.read_json(path)
                if len(cached) >= 5:
                    cached.attrs["source"] = src
                    return cached
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError(f"{info['name']} constituent scrape failed and no disk cache")


def _sector_table(result, meta):
    """Per-sector Health % over ALL sectors present (latest bar + 5d change)."""
    close, sig = result.close, result.signals
    mapping = meta.set_index("Ticker")["Sector Abbrev"]
    rows = []
    for sec in sorted(meta["Sector Abbrev"].unique()):
        cols = [c for c in close.columns if mapping.get(c) == sec]
        if not cols:
            continue
        total = None
        for comp in ih.HEALTH_COMPONENTS:
            net = sig[comp]["up"][cols].sum(axis=1) - sig[comp]["drop"][cols].sum(axis=1)
            total = net if total is None else total + net
        pct = (total / (4 * len(cols)) * 100.0).dropna()
        if pct.empty:
            continue
        last = float(pct.iloc[-1])
        prev5 = float(pct.iloc[-6]) if len(pct) > 6 else last
        rows.append({"abbrev": sec,
                     "sector": SECTOR_NAME_EXT.get(sec, sec),
                     "members": int(len(cols)),
                     "health_pct": round(last, 1),
                     "chg_5d": round(last - prev5, 1)})
    rows.sort(key=lambda r: r["health_pct"], reverse=True)
    return rows


def build_fbm_health(key: str, lookback: str = "1y", term: str = "short") -> dict:
    """Full JSON payload for one FBM index: overall health, sparks, sectors.

    term: short (10/25) | mid (20/50) | long (50/100) health lookbacks.
    """
    key = key.upper()
    if key not in FBM_INDEXES:
        raise ValueError(f"unknown FBM index '{key}' "
                         f"(one of {', '.join(FBM_INDEXES)})")
    info = FBM_INDEXES[key]
    meta = get_fbm_constituents(key)
    # distinct index_symbol per index so the price caches don't collide
    cfg = ih.BreadthConfig(lookback=lookback, cache_dir=CACHE_DIR,
                           index_symbol=f"EW_{key}")
    ih.apply_term(cfg, term)
    close = ih.download_prices(meta["Ticker"].tolist(), cfg)
    # No Yahoo symbol for these indexes -> equal-weight proxy (rebased mean)
    reb = close.apply(lambda s: s / s.dropna().iloc[0])
    close[cfg.index_symbol] = reb.mean(axis=1) * 100.0
    result = ih.compute_health(cfg, tickers_meta=meta, close=close.copy())

    hp = result.health_pct.dropna().iloc[cfg.warmup:]   # drop the warm-up bars
    latest = float(hp.iloc[-1])
    prev5 = float(hp.iloc[-6]) if len(hp) > 6 else latest
    comp = result.component_pct.dropna().iloc[-1]
    spark = hp.tail(120)
    proxy = result.index_price.dropna()
    proxy = proxy[proxy.index >= spark.index.min()] if len(spark) else proxy

    return {
        "key": key,
        "name": info["name"],
        "as_of": str(hp.index[-1].date()),
        "lookback": lookback,
        "term": term,
        "term_periods": {"period": cfg.h_sma_period, "hl_period": cfg.hl_period},
        "constituents": int(len(meta)),
        "priced": int(result.close.shape[1]),
        "source": meta.attrs.get("source", "?"),
        "health_pct": round(latest, 2),
        "verdict": "expanding" if latest >= 0 else "contracting",
        "trend_5d": round(latest - prev5, 2),
        "components": {k: round(float(comp[k]), 1) for k in comp.index},
        "dates": [str(d.date()) for d in spark.index],
        "spark": [round(float(x), 2) for x in spark],
        "index_spark": [round(float(x), 2) for x in proxy.tail(len(spark))],
        "index_level": round(float(proxy.iloc[-1]), 2) if len(proxy) else None,
        "index_note": "equal-weight proxy (base=100)",
        "sectors": _sector_table(result, meta),
    }

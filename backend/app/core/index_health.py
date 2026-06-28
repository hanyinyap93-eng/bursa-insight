"""
Index Health engine — Bursa Insight.

Extracted from the Bursa "Index Health" notebooks (KLCI standalone + per-sector
skills). This is the data + compute layer only; all matplotlib / PDF / report
code from the notebooks has been removed. The breadth + 4-component Index Health
methodology (SMA breadth, new-high/low, momentum, RSI) is unchanged so API
results match the notebooks.

Public surface used by the API:
    BreadthConfig                     - tuning knobs (lookbacks, RSI thresholds, ...)
    get_klci_tickers(...)             - constituents (ticker + Bursa sector)
    download_prices(...)              - yfinance close panel, parquet-cached
    compute_health(...)               - HealthResult (breadth % of constituents)
    compute_sector_index_health(...)  - SectorIndexHealth (per sector-index series)
    sector_index_composite(...)       - aggregate 13-sector composite scores
    SECTOR_INDEX_YF                   - Bursa sector index code -> Yahoo symbol
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except Exception:  # pragma: no cover - yfinance optional at import time
    yf = None

# --------------------------------------------------------------------------- #
# Constituent sources & sector classification (from the KLCI standalone skill)
# --------------------------------------------------------------------------- #
KLSE_KLCI_URL = "https://www.klsescreener.com/v2/stocks/view/0200I"
# Sector index constituents come from the markets/bursa page (per the per-sector
# Index Health skills), NOT the stocks/view page used for the KLCI.
KLSE_SECTOR_URL = "https://www.klsescreener.com/v2/markets/bursa/{code}"

# klsescreener index codes for the 13 Bursa sector indices (constituents are
# scraped from each page, mirroring the per-sector Index Health skills).
SECTOR_INDEX_CODES = {
    "CONSUMER": "0001I", "IND-PROD": "0002I", "CONSTRUCTN": "0003I",
    "TECHNOLOGY": "0005I", "FINANCE": "0010I", "PROPERTIES": "0020I",
    "PLANTATION": "0025I", "REIT": "0050I", "ENERGY": "0061I",
    "HEALTH": "0062I", "TELECOMMUNICATIONS": "0063I",
    "TRANSPORTATION": "0064I", "UTILITIES": "0065I",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Offline fallback list (used if the live scrape fails). Also the source of the
# curated code -> Bursa sector map applied to known constituents.
FALLBACK_CONSTITUENTS = [
    ("5326", "99SMART", "CON"), ("1015", "AMBANK", "FIN"),
    ("6888", "AXIATA", "TEL"), ("6947", "CDB", "TEL"),
    ("1023", "CIMB", "FIN"), ("5398", "GAMUDA", "CST"),
    ("5819", "HLBANK", "FIN"), ("5225", "IHH", "HLT"),
    ("1961", "IOICORP", "PLT"), ("2445", "KLK", "PLT"),
    ("6012", "MAXIS", "TEL"), ("1155", "MAYBANK", "FIN"),
    ("3816", "MISC", "TRN"), ("5296", "MRDIY", "CON"),
    ("4707", "NESTLE", "CON"), ("1295", "PBBANK", "FIN"),
    ("5183", "PCHEM", "IND"), ("5681", "PETDAG", "ENE"),
    ("6033", "PETGAS", "UTL"), ("8869", "PMETAL", "IND"),
    ("4065", "PPB", "CON"), ("1066", "RHBBANK", "FIN"),
    ("5285", "SDG", "PLT"), ("4197", "SIME", "IND"),
    ("5555", "SUNMED", "HLT"), ("5211", "SUNWAY", "PRP"),
    ("5347", "TENAGA", "UTL"), ("4863", "TM", "TEL"),
    ("4677", "YTL", "UTL"), ("6742", "YTLPOWR", "UTL"),
]

SECTOR_ABBREV = {
    "Financial Services": "FIN", "Telecommunications & Media": "TEL",
    "Consumer Products & Services": "CON", "Plantation": "PLT",
    "Utilities": "UTL", "Industrial Products & Services": "IND",
    "Health Care": "HLT", "Construction": "CST",
    "Transportation & Logistics": "TRN", "Energy": "ENE", "Property": "PRP",
}
SECTOR_NAME = {v: k for k, v in SECTOR_ABBREV.items()}
CODE_SECTOR = {code: sec for code, _name, sec in FALLBACK_CONSTITUENTS}

_SECTOR_KEYWORDS = [
    ("bank", "FIN"), ("financ", "FIN"), ("insur", "FIN"),
    ("telecom", "TEL"), ("mobile", "TEL"), ("internet", "TEL"),
    ("network", "TEL"), ("digital", "TEL"), ("media", "TEL"),
    ("construct", "CST"),
    ("health", "HLT"), ("hospital", "HLT"), ("medical", "HLT"), ("pharma", "HLT"),
    ("plantation", "PLT"),
    ("shipping", "TRN"), ("maritime", "TRN"), ("transport", "TRN"),
    ("logistic", "TRN"), ("port", "TRN"),
    ("propert", "PRP"), ("real estate", "PRP"),
    ("power", "UTL"), ("electric", "UTL"), ("utilit", "UTL"), ("gas", "UTL"),
    ("oil", "ENE"), ("petroleum", "ENE"), ("energy", "ENE"),
    ("chemical", "IND"), ("metal", "IND"), ("alumini", "IND"),
    ("industrial", "IND"), ("conglomerate", "IND"), ("manufactur", "IND"),
    ("retail", "CON"), ("food", "CON"), ("beverage", "CON"),
    ("consumer", "CON"), ("agricultur", "CON"),
]


def classify_sector(text: str) -> Optional[str]:
    """Best-effort map of a free-text sector/name to a Bursa bucket abbrev."""
    t = str(text).lower()
    for kw, code in _SECTOR_KEYWORDS:
        if kw in t:
            return code
    return None


# --------------------------------------------------------------------------- #
# Bursa sector indices on Yahoo Finance (KLSE sector index codes -> .KL symbol).
# Used for the per-sector index-health (sector rotation) view and breadth pages.
# Codes mirror the klsescreener / Bursa sector index set.
# --------------------------------------------------------------------------- #
SECTOR_INDEX_YF = {
    "CONSUMER": "0001I.KL",          # Consumer Products & Services
    "IND-PROD": "0002I.KL",          # Industrial Products & Services
    "CONSTRUCTN": "0003I.KL",        # Construction
    "TECHNOLOGY": "0005I.KL",        # Technology
    "FINANCE": "0010I.KL",           # Financial Services
    "PROPERTIES": "0020I.KL",        # Property
    "PLANTATION": "0025I.KL",        # Plantation
    "REIT": "0050I.KL",              # REIT
    "ENERGY": "0061I.KL",            # Energy
    "HEALTH": "0062I.KL",            # Health Care
    "TELECOMMUNICATIONS": "0063I.KL",  # Telecommunications & Media
    "TRANSPORTATION": "0064I.KL",    # Transportation & Logistics
    "UTILITIES": "0065I.KL",         # Utilities
}
SECTOR_INDEX_ORDER = list(SECTOR_INDEX_YF.keys())
INDEX_SYMBOL_KLCI = "^KLSE"
INDEX_SYMBOL_SPX = "^GSPC"

HEALTH_COMPONENTS = ("momentum", "rsi", "sma", "hl")
HEALTH_LABELS = {
    "momentum": "Trend sentiment - Momentum",
    "rsi": "Trend sentiment - RSI",
    "sma": "Trend quality - SMA",
    "hl": "Trend quality - H/L",
}


@dataclass
class BreadthConfig:
    lookback: str = "1y"
    interval: str = "1d"
    auto_adjust: bool = False
    index_symbol: str = INDEX_SYMBOL_KLCI
    constituents_source: str = "klse"   # "klse" live scrape | "local" fallback
    constituents_url: Optional[str] = None
    cache_dir: Optional[Path] = None
    refresh_cache: bool = False
    max_retries: int = 3
    retry_wait: float = 4.0
    mom_period: int = 10
    h_rsi_period: int = 10
    h_rsi_up: float = 70.0
    h_rsi_down: float = 30.0
    h_sma_period: int = 10
    hl_period: int = 25
    health_smooth: int = 10
    warmup: int = 25


# --------------------------------------------------------------------------- #
# Indicators (verbatim methodology from the notebooks)
# --------------------------------------------------------------------------- #
def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI, NaN-resilient (computed on valid obs then reindexed)."""
    s = close.dropna()
    n = len(s)
    if n <= period:
        return pd.Series(np.full(len(close), np.nan), index=close.index, name="RSI")
    delta = s.diff()
    gain = delta.clip(lower=0.0).to_numpy()
    loss = (-delta).clip(lower=0.0).to_numpy()
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    avg_gain[period] = np.nanmean(gain[1:period + 1])
    avg_loss[period] = np.nanmean(loss[1:period + 1])
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = np.where(avg_loss == 0, 100.0, out)
    out[:period] = np.nan
    return pd.Series(out, index=s.index, name="RSI").reindex(close.index)


def clean_close(close: pd.DataFrame) -> pd.DataFrame:
    """Drop all-NaN rows and forward-fill internal gaps."""
    return close.dropna(how="all").sort_index().ffill()


# --------------------------------------------------------------------------- #
# Constituents
# --------------------------------------------------------------------------- #
def fetch_sector_constituents_klse(code, retries=3, wait=4.0):
    """Scrape a Bursa sector index's constituents from the markets/bursa page.

    Used for sector indices (0005I Technology, 0010I Financials, ...) — mirrors
    the per-sector Index Health skills. Returns [(stock_code, name, sector_abbrev)].
    """
    import urllib.request
    url = KLSE_SECTOR_URL.format(code=code)
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            pairs = re.findall(r'/stocks/view/([0-9A-Z]+)"[^>]*>\s*([^<]+?)\s*<', html)
            out, seen = [], set()
            for c, name in pairs:
                c, name = c.strip(), name.strip()
                digits = re.sub(r"\D", "", c)
                if not digits or c.endswith("I"):     # skip index self-links
                    continue
                c = digits.zfill(4)
                if c in seen:
                    continue
                seen.add(c)
                sec = CODE_SECTOR.get(c) or classify_sector(name) or "UNK"
                out.append((c, name, sec))
            if len(out) < 3:
                raise RuntimeError(f"parsed only {len(out)} constituents (layout changed?)")
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(wait)
    raise RuntimeError(f"klse sector fetch failed for {code}: {last_err}")


def fetch_klci_constituents_klse(url=None, retries=3, wait=4.0):
    """Scrape the live FBM KLCI 'Components' table from klsescreener (0200I)."""
    url = url if (url and "klsescreener" in url) else KLSE_KLCI_URL
    return _scrape_klse_components(url, retries, wait, min_rows=10)


def _scrape_klse_components(url, retries=3, wait=4.0, min_rows=10):
    import urllib.request
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            m = re.search(r"Components(.*?)Comments", html, re.S)
            region = m.group(1) if m else html
            pairs = re.findall(
                r'/stocks/view/(\d{4}[A-Z]*)"[^>]*>\s*([A-Za-z0-9&.\- ]+?)\s*<', region
            )
            out, seen = [], set()
            for code, name in pairs:
                code, name = code.strip(), name.strip()
                if not code or code.endswith("I"):
                    continue
                code = code.zfill(4)
                if code in seen:
                    continue
                seen.add(code)
                sec = CODE_SECTOR.get(code) or classify_sector(name) or "UNK"
                out.append((code, name, sec))
            if len(out) < min_rows:
                raise RuntimeError(f"parsed only {len(out)} constituents (layout changed?)")
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(wait)
    raise RuntimeError(f"klsescreener constituents fetch failed: {last_err}")


def get_klci_tickers(source="klse", url=None, retries=3, wait=4.0, verbose=False):
    """Return constituents (ticker + sector) as a DataFrame.

    source: "klse" (live scrape, default) | "local" (embedded fallback only).
    Falls back to the embedded 30-name list if the scrape fails.
    """
    rows, used = None, None
    if source == "klse":
        try:
            rows, used = fetch_klci_constituents_klse(url, retries, wait), "klse"
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[constituents] klsescreener fetch failed ({exc}); using fallback.")
    if rows is None:
        rows, used = FALLBACK_CONSTITUENTS, "fallback"
    df = pd.DataFrame(
        [
            {
                "Ticker": f"{code}.KL",
                "Code": code,
                "Name": name,
                "Sector": SECTOR_NAME.get(sec, sec),
                "Sector Abbrev": sec,
            }
            for code, name, sec in rows
        ]
    ).drop_duplicates("Ticker").reset_index(drop=True)
    df.attrs["source"] = used
    return df


# --------------------------------------------------------------------------- #
# Price download (yfinance + parquet cache)
# --------------------------------------------------------------------------- #
def _cache_is_fresh(cached, symbols):
    if cached.empty or not set(symbols).issubset(cached.columns):
        return False
    last_cached = pd.Timestamp(cached.index.max()).normalize()
    today = pd.Timestamp.today().normalize()
    last_weekday = today if today.weekday() < 5 else today - pd.offsets.BDay(1)
    return last_cached >= last_weekday - pd.offsets.BDay(1)


def download_prices(tickers, cfg, extra=None):
    if yf is None:
        raise ImportError("yfinance is required. pip install yfinance.")
    symbols = list(dict.fromkeys(list(tickers) + (extra or [])))
    cache_file = None
    if cfg.cache_dir is not None:
        cache_dir = Path(cfg.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_idx = re.sub(r"\W+", "", cfg.index_symbol)
        # pickle cache: no parquet engine (pyarrow/fastparquet) dependency
        cache_file = cache_dir / f"close_{safe_idx}_{cfg.lookback}_{cfg.interval}.pkl"
        if cache_file.exists() and not cfg.refresh_cache:
            try:
                cached = pd.read_pickle(cache_file)
                if _cache_is_fresh(cached, symbols):
                    return cached[symbols]
            except Exception:  # noqa: BLE001 - ignore a corrupt/incompatible cache
                pass
    last_err = None
    for _ in range(cfg.max_retries):
        try:
            raw = yf.download(
                symbols, period=cfg.lookback, interval=cfg.interval,
                auto_adjust=cfg.auto_adjust, group_by="column",
                threads=True, progress=False,
            )
            if raw is None or raw.empty or "Close" not in raw:
                raise RuntimeError("yfinance returned no data (network/rate-limit?)")
            close = raw["Close"].copy()
            if isinstance(close, pd.Series):
                close = close.to_frame()
            close = close.dropna(axis=1, how="all")
            if close.empty:
                raise RuntimeError("All tickers failed to download.")
            close = close.dropna(how="all").sort_index().ffill()
            if cache_file is not None:
                try:
                    close.to_pickle(cache_file)
                except Exception:  # noqa: BLE001 - caching is best-effort
                    pass
            return close
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(cfg.retry_wait)
    raise RuntimeError(f"Failed to download prices: {last_err}")


# --------------------------------------------------------------------------- #
# Breadth Index Health (% of constituents) — compute_health
# --------------------------------------------------------------------------- #
def health_component_signals(close, cfg):
    mom = close - close.shift(cfg.mom_period)
    rsi_df = close.apply(lambda s: rsi(s, cfg.h_rsi_period))
    sma_df = close.apply(lambda s: sma(s, cfg.h_sma_period))
    prior_high = close.shift(1).rolling(cfg.hl_period, min_periods=cfg.hl_period).max()
    prior_low = close.shift(1).rolling(cfg.hl_period, min_periods=cfg.hl_period).min()
    return {
        "momentum": {"up": mom > 0, "drop": mom < 0},
        "rsi": {"up": rsi_df > cfg.h_rsi_up, "drop": rsi_df < cfg.h_rsi_down},
        "sma": {"up": close > sma_df, "drop": close < sma_df},
        "hl": {"up": close > prior_high, "drop": close < prior_low},
    }


@dataclass
class HealthResult:
    close: pd.DataFrame
    component_net: pd.DataFrame
    component_pct: pd.DataFrame
    health: pd.Series
    health_pct: pd.Series
    sector_health: pd.DataFrame
    sector_health_pct: pd.DataFrame
    index_price: Optional[pd.Series]
    cfg: BreadthConfig
    meta: pd.DataFrame
    signals: dict = field(default_factory=dict)


def compute_health(cfg=None, tickers_meta=None, close=None) -> HealthResult:
    cfg = cfg or BreadthConfig()
    meta = tickers_meta if tickers_meta is not None else get_klci_tickers(
        cfg.constituents_source, cfg.constituents_url, cfg.max_retries, cfg.retry_wait
    )
    if close is None:
        close = download_prices(meta["Ticker"].tolist(), cfg, extra=[cfg.index_symbol])
    close = clean_close(close)
    index_price = None
    if cfg.index_symbol in close.columns:
        index_price = close[cfg.index_symbol].dropna()
        close = close.drop(columns=[cfg.index_symbol])
    sig = health_component_signals(close, cfg)
    n_const = len(meta) if meta is not None else close.shape[1]
    component_net = pd.DataFrame(
        {k: v["up"].sum(axis=1) - v["drop"].sum(axis=1) for k, v in sig.items()},
        index=close.index,
    )[list(HEALTH_COMPONENTS)]
    component_pct = component_net / n_const * 100.0
    health = component_net.sum(axis=1).rename("Index Health")
    health_pct = component_pct.mean(axis=1).rename("Index Health %")
    mapping = meta.set_index("Ticker")["Sector Abbrev"]
    sector_health = pd.DataFrame(index=close.index)
    sector_health_pct = pd.DataFrame(index=close.index)
    for code in SECTOR_ABBREV.values():
        cols = [c for c in close.columns if mapping.get(c) == code]
        if not cols:
            continue
        total = None
        for comp in HEALTH_COMPONENTS:
            net_c = sig[comp]["up"][cols].sum(axis=1) - sig[comp]["drop"][cols].sum(axis=1)
            total = net_c if total is None else total + net_c
        sector_health[code] = total
        sector_health_pct[code] = total / (4 * len(cols)) * 100.0
    sector_health = sector_health.dropna(how="all")
    sector_health_pct = sector_health_pct.dropna(how="all")
    return HealthResult(
        close, component_net, component_pct, health, health_pct,
        sector_health, sector_health_pct, index_price, cfg, meta, sig,
    )


# --------------------------------------------------------------------------- #
# Per-sector-index health (sector rotation) — from sector index price series
# --------------------------------------------------------------------------- #
@dataclass
class SectorIndexHealth:
    prices: pd.DataFrame
    trend_quality: pd.DataFrame
    trend_sentiment: pd.DataFrame
    index_health: pd.DataFrame
    components: dict
    cfg: BreadthConfig


def compute_sector_index_health(prices, cfg=None) -> SectorIndexHealth:
    """Per-sector Trend Quality / Trend Sentiment / Index Health (each -100..100)."""
    cfg = cfg or BreadthConfig()
    prices = prices.sort_index()
    sma_d, mom_d, rsi_d, hl_d = {}, {}, {}, {}
    for col in prices.columns:
        c = prices[col]
        sma_d[col] = np.sign(c - sma(c, cfg.h_sma_period))
        mom_d[col] = np.sign(c - c.shift(cfg.mom_period))
        r = rsi(c, cfg.h_rsi_period)
        rs = pd.Series(
            np.where(r > cfg.h_rsi_up, 1.0, np.where(r < cfg.h_rsi_down, -1.0, 0.0)),
            index=c.index,
        )
        rs[r.isna()] = np.nan
        rsi_d[col] = rs
        ph = c.rolling(cfg.hl_period, min_periods=cfg.hl_period).max().shift(1)
        pl = c.rolling(cfg.hl_period, min_periods=cfg.hl_period).min().shift(1)
        hh = pd.Series(np.where(c > ph, 1.0, np.where(c < pl, -1.0, 0.0)), index=c.index)
        hh[ph.isna()] = np.nan
        hl_d[col] = hh
    comp = {
        "sma": pd.DataFrame(sma_d)[prices.columns],
        "momentum": pd.DataFrame(mom_d)[prices.columns],
        "rsi": pd.DataFrame(rsi_d)[prices.columns],
        "hl": pd.DataFrame(hl_d)[prices.columns],
    }
    trend_quality = (comp["sma"] + comp["hl"]) / 2 * 100
    trend_sentiment = (comp["momentum"] + comp["rsi"]) / 2 * 100
    index_health = (comp["sma"] + comp["momentum"] + comp["rsi"] + comp["hl"]) / 4 * 100
    return SectorIndexHealth(prices, trend_quality, trend_sentiment, index_health, comp, cfg)


def sector_index_composite(result: SectorIndexHealth) -> pd.DataFrame:
    """Aggregate the sector signals into composite scores (matches Pine /N*100)."""
    comp = result.components
    n = result.prices.shape[1]
    sc = {k: comp[k].fillna(0).sum(axis=1) / n * 100 for k in HEALTH_COMPONENTS}
    df = pd.DataFrame(sc)[list(HEALTH_COMPONENTS)]
    df["Index Health"] = df[list(HEALTH_COMPONENTS)].mean(axis=1)
    df["Trend Quality"] = (df["sma"] + df["hl"]) / 2
    df["Trend Sentiment"] = (df["momentum"] + df["rsi"]) / 2
    return df


def fetch_sector_indices_yf(cfg: BreadthConfig) -> pd.DataFrame:
    """Download the 13 Bursa sector indices from Yahoo Finance -> Date x sector.

    NOTE: Yahoo does not currently carry the Bursa sector index symbols, so this
    is kept only as an optional path. The reliable route is breadth over each
    sector's constituents — see sector_rotation_health().
    """
    symbols = list(SECTOR_INDEX_YF.values())
    close = download_prices(symbols, cfg)
    rename = {v: k for k, v in SECTOR_INDEX_YF.items()}
    close = close.rename(columns=rename)
    cols = [c for c in SECTOR_INDEX_ORDER if c in close.columns]
    return close[cols].dropna(how="all")


def get_index_tickers(code="0200I", retries=3, wait=4.0) -> pd.DataFrame:
    """Constituents (ticker + sector) for any klsescreener index code.

    KLCI (0200I) uses the stocks/view Components table; sector indices use the
    markets/bursa page.
    """
    try:
        if code == "0200I":
            rows = fetch_klci_constituents_klse(None, retries, wait)
        else:
            rows = fetch_sector_constituents_klse(code, retries, wait)
    except Exception:  # noqa: BLE001
        rows = FALLBACK_CONSTITUENTS if code == "0200I" else []
    return pd.DataFrame(
        [
            {"Ticker": f"{c}.KL", "Code": c, "Name": n,
             "Sector": SECTOR_NAME.get(s, s), "Sector Abbrev": s}
            for c, n, s in rows
        ]
    ).drop_duplicates("Ticker").reset_index(drop=True)


def sector_breadth_series(code: str, cfg: BreadthConfig):
    """Breadth Index Health % time series for one sector index (from its members).

    Returns (health_pct: pd.Series, n_constituents: int). Empty Series if the
    sector has no fetchable constituents.
    """
    meta = get_index_tickers(code, cfg.max_retries, cfg.retry_wait)
    if meta.empty:
        return pd.Series(dtype=float), 0
    close = download_prices(meta["Ticker"].tolist(), cfg)
    # no index overlay needed here; reuse the breadth math via compute_health
    res = compute_health(cfg, tickers_meta=meta, close=close)
    return res.health_pct, res.close.shape[1]


def sector_rotation_health(cfg: BreadthConfig, sectors=None) -> pd.DataFrame:
    """Assemble a (Date x sector) DataFrame of breadth Index Health % per sector.

    For each sector index, health = % breadth over that sector's own
    constituents (the method used by the per-sector Index Health skills).
    Sectors that fail to fetch are skipped so the heatmap still renders.
    """
    sectors = sectors or list(SECTOR_INDEX_CODES.keys())
    series = {}
    for sec in sectors:
        code = SECTOR_INDEX_CODES.get(sec)
        if not code:
            continue
        try:
            hp, n = sector_breadth_series(code, cfg)
            if len(hp) and n:
                series[sec] = hp
        except Exception:  # noqa: BLE001
            continue
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series).sort_index()
    return df[[s for s in SECTOR_INDEX_ORDER if s in df.columns]]

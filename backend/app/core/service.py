"""
Caching service layer over the Index Health engine.

Computing health pulls ~30 tickers from yfinance, so results are cached in
memory with a TTL. Endpoints call get_health()/get_sector_health() instead of
hitting the engine directly. A background refresh can be triggered with
refresh().
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from . import index_health as ih

CACHE_DIR = Path(__file__).resolve().parents[2] / "_cache"
TTL_SECONDS = 60 * 30  # 30 minutes

# Index registry: what the app exposes. KLCI = breadth over constituents.
INDEXES = {
    "KLCI": {
        "name": "FTSE Bursa Malaysia KLCI",
        "symbol": ih.INDEX_SYMBOL_KLCI,
        "kind": "breadth",  # health from constituent breadth
    },
    "SPX": {
        "name": "S&P 500",
        "symbol": ih.INDEX_SYMBOL_SPX,
        "kind": "index_only",  # index price only (no constituent breadth in MVP)
    },
}


@dataclass
class _Entry:
    value: object
    ts: float


class _Cache:
    def __init__(self):
        self._d: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key, ttl=TTL_SECONDS):
        with self._lock:
            e = self._d.get(key)
            if e is None or (time.monotonic() - e.ts) > ttl:
                return None
            return e.value

    def set(self, key, value):
        with self._lock:
            self._d[key] = _Entry(value, time.monotonic())

    def clear(self):
        with self._lock:
            self._d.clear()


_cache = _Cache()
_compute_lock = threading.Lock()


def _cfg(index: str = "KLCI", lookback: str = "1y") -> ih.BreadthConfig:
    meta = INDEXES.get(index, INDEXES["KLCI"])
    return ih.BreadthConfig(
        lookback=lookback,
        index_symbol=meta["symbol"],
        cache_dir=CACHE_DIR,
        constituents_source="klse",
    )


_CONST_FILE = CACHE_DIR / "klci_constituents.json"


def get_constituents(force: bool = False):
    """KLCI constituents with disk-backed resilience.

    Live-scrape klsescreener; on success persist the list to disk. If the scrape
    fails or degrades to the embedded fallback (e.g. a transient throttle), reuse
    the last-good list from disk so a momentary outage doesn't drop us to the
    static 30-name list. Returns a constituents DataFrame.
    """
    import json
    df = ih.get_klci_tickers("klse")
    used = df.attrs.get("source")
    if used == "klse" and len(df) >= 10:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CONST_FILE.write_text(df.to_json(orient="records"), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return df
    # scrape degraded -> prefer the last-good disk cache over the embedded fallback
    if _CONST_FILE.exists():
        try:
            import pandas as pd
            cached = pd.read_json(_CONST_FILE)
            if len(cached) >= 10:
                cached.attrs["source"] = "disk-cache"
                return cached
        except Exception:  # noqa: BLE001
            pass
    return df


def get_health(index: str = "KLCI", lookback: str = "1y", force: bool = False) -> ih.HealthResult:
    """Breadth Index Health for an index (cached). KLCI only in the MVP."""
    key = f"health:{index}:{lookback}"
    if not force:
        cached = _cache.get(key)
        if cached is not None:
            return cached
    with _compute_lock:
        if not force:
            cached = _cache.get(key)
            if cached is not None:
                return cached
        cfg = _cfg(index, lookback)
        cfg.refresh_cache = force
        meta = get_constituents() if index == "KLCI" else None
        result = ih.compute_health(cfg, tickers_meta=meta)
        _cache.set(key, result)
        return result


def get_sector_health(lookback: str = "1y", force: bool = False):
    """Per-sector breadth Index Health % (sector rotation).

    Returns a (Date x sector) DataFrame where each column is that sector index's
    breadth health % computed over its own constituents. Cached (heavier: it
    scrapes + downloads 13 sector member sets).
    """
    key = f"sector:{lookback}"
    if not force:
        cached = _cache.get(key, ttl=TTL_SECONDS * 2)
        if cached is not None:
            return cached
    with _compute_lock:
        if not force:
            cached = _cache.get(key, ttl=TTL_SECONDS * 2)
            if cached is not None:
                return cached
        cfg = _cfg("KLCI", lookback)
        cfg.refresh_cache = force
        df = ih.sector_rotation_health(cfg)
        _cache.set(key, df)
        return df


def refresh():
    """Force-refresh the primary caches (used by /refresh and scheduled jobs)."""
    _cache.clear()
    get_health("KLCI", force=True)
    try:
        get_sector_health(force=True)
    except Exception:  # noqa: BLE001 - sector indices may be unavailable
        pass


def latest(series: pd.Series, default=None):
    return float(series.iloc[-1]) if len(series) else default

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


# Heavy results worth persisting to disk so a server restart doesn't re-scrape
# the whole market (these are the slow, throttle-prone keys).
_DISK_PREFIXES = ("sector:", "indexpanel:", "sectordata:", "health:", "indexohlc:")
_DISK_TTL = 60 * 60 * 12  # disk cache valid for 12h


class _Cache:
    def __init__(self):
        self._d: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _disk_path(key):
        import re
        safe = re.sub(r"\W+", "_", key)
        return CACHE_DIR / f"svc_{safe}.pkl"

    def get(self, key, ttl=TTL_SECONDS):
        with self._lock:
            e = self._d.get(key)
            if e is not None and (time.monotonic() - e.ts) <= ttl:
                return e.value
        # in-memory miss/expired -> try the on-disk copy for heavy keys
        if key.startswith(_DISK_PREFIXES):
            try:
                import time as _t
                import pickle
                p = self._disk_path(key)
                if p.exists() and (_t.time() - p.stat().st_mtime) <= _DISK_TTL:
                    value = pickle.loads(p.read_bytes())
                    with self._lock:
                        self._d[key] = _Entry(value, time.monotonic())
                    return value
            except Exception:  # noqa: BLE001 - ignore a corrupt/incompatible file
                pass
        return None

    def get_stale(self, key):
        """Return (value, age_seconds) ignoring TTL, or None. Loads from disk if
        needed. Used for stale-while-revalidate so requests never block once a
        value exists."""
        with self._lock:
            e = self._d.get(key)
            if e is not None:
                return e.value, (time.monotonic() - e.ts)
        if key.startswith(_DISK_PREFIXES):
            try:
                import time as _t
                import pickle
                p = self._disk_path(key)
                if p.exists():
                    age = _t.time() - p.stat().st_mtime
                    value = pickle.loads(p.read_bytes())
                    with self._lock:
                        self._d[key] = _Entry(value, time.monotonic())
                    return value, age
            except Exception:  # noqa: BLE001
                pass
        return None

    def set(self, key, value):
        with self._lock:
            self._d[key] = _Entry(value, time.monotonic())
        if key.startswith(_DISK_PREFIXES):
            try:
                import pickle
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                self._disk_path(key).write_bytes(pickle.dumps(value))
            except Exception:  # noqa: BLE001 - best-effort persistence
                pass

    def clear(self):
        with self._lock:
            self._d.clear()


_cache = _Cache()
_compute_lock = threading.Lock()

# background-refresh de-dup (one in-flight rebuild per key)
_refreshing: set = set()
_refresh_lock = threading.Lock()


def _spawn_refresh(key, builder):
    """Rebuild `key` in a background thread (at most one in flight per key)."""
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _run():
        try:
            _cache.set(key, builder())
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def _swr(key, ttl, builder, force=False):
    """Stale-while-revalidate: serve the cached value instantly (even if stale)
    and refresh in the background; only block when there is no value yet."""
    if not force:
        got = _cache.get_stale(key)
        if got is not None:
            value, age = got
            if age > ttl:
                _spawn_refresh(key, builder)
            return value
    with _compute_lock:
        if not force:
            got = _cache.get_stale(key)
            if got is not None:
                return got[0]
        value = builder()
        _cache.set(key, value)
        return value


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


def _build_health(index, lookback, force):
    cfg = _cfg(index, lookback)
    cfg.refresh_cache = force
    meta = get_constituents() if index == "KLCI" else None
    return ih.compute_health(cfg, tickers_meta=meta)


def get_health(index: str = "KLCI", lookback: str = "1y", force: bool = False) -> ih.HealthResult:
    """Breadth Index Health (stale-while-revalidate cached). KLCI only in MVP."""
    key = f"health:{index}:{lookback}"
    return _swr(key, TTL_SECONDS, lambda: _build_health(index, lookback, force), force=force)


def get_sector_health(lookback: str = "1y", force: bool = False):
    """Per-sector breadth Index Health % (sector rotation), SWR-cached.

    (Date x sector) DataFrame; heavy (scrapes + downloads 13 sector member sets).
    """
    key = f"sector:{lookback}"
    cfg = _cfg("KLCI", lookback)
    cfg.refresh_cache = force
    return _swr(key, TTL_SECONDS * 2, lambda: ih.sector_rotation_health(cfg), force=force)


def _build_index_panel(lookback):
    import pandas as pd
    cfg = _cfg("KLCI", lookback)
    cols = {}
    try:
        kl = ih.download_prices([ih.INDEX_SYMBOL_KLCI], cfg)
        cols["KLCI"] = kl[ih.INDEX_SYMBOL_KLCI]
    except Exception:  # noqa: BLE001
        pass
    for sec, code in ih.SECTOR_INDEX_CODES.items():
        try:
            meta = ih.get_index_tickers(code, cfg.max_retries, cfg.retry_wait)
            if meta.empty:
                continue
            close = ih.download_prices(meta["Ticker"].tolist(), cfg)
            base = close.ffill().bfill().iloc[0]
            cols[sec] = (close.divide(base) * 100.0).mean(axis=1)
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame(cols).sort_index()


def get_index_price_panel(lookback: str = "2y", force: bool = False):
    """(Date x index) price panel: KLCI + 13 sector eq-wt indexes. SWR-cached."""
    key = f"indexpanel:{lookback}"
    return _swr(key, TTL_SECONDS * 2, lambda: _build_index_panel(lookback), force=force)


def warm_all():
    """Rebuild the heavy caches (force). Used on startup and by the scheduler."""
    for fn in (lambda: get_health("KLCI", force=True),
               lambda: get_sector_health(force=True),
               lambda: get_index_price_panel(force=True)):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def refresh():
    """Force-refresh the primary caches (used by /refresh and scheduled jobs)."""
    _cache.clear()
    warm_all()


# Background scheduler: proactively re-warm caches so users always hit fresh
# data (no on-request rebuild). Interval configurable via BURSA_REFRESH_MINUTES.
_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    import os

    minutes = float(os.environ.get("BURSA_REFRESH_MINUTES", "20"))
    interval = max(60.0, minutes * 60.0)

    def _loop():
        # initial warm on boot (uses on-disk cache if fresh)
        warm_all()
        while True:
            time.sleep(interval)
            try:
                warm_all()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_loop, daemon=True).start()


def latest(series: pd.Series, default=None):
    return float(series.iloc[-1]) if len(series) else default

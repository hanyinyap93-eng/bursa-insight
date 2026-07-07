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
_DISK_PREFIXES = ("sector:", "indexpanel:", "sectordata:", "health:", "indexohlc:",
                  "sentiment:", "gex:", "fbm:", "riskapp:")
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

# Per-key compute locks: a slow first build (e.g. the GEX scrape or a blocked
# yfinance call on a cloud host) must not serialize unrelated keys behind it.
_key_locks: dict = {}
_key_locks_guard = threading.Lock()


def _compute_lock_for(key):
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock

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
    with _compute_lock_for(key):
        if not force:
            got = _cache.get_stale(key)
            if got is not None:
                return got[0]
        value = builder()
        _cache.set(key, value)
        return value


def _cfg(index: str = "KLCI", lookback: str = "1y", term: str = "short") -> ih.BreadthConfig:
    meta = INDEXES.get(index, INDEXES["KLCI"])
    cfg = ih.BreadthConfig(
        lookback=lookback,
        index_symbol=meta["symbol"],
        cache_dir=CACHE_DIR,
        constituents_source="klse",
    )
    return ih.apply_term(cfg, term)   # short/mid/long -> SMA & H/L lookbacks


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


def _build_health(index, lookback, term, force):
    cfg = _cfg(index, lookback, term)
    cfg.refresh_cache = force
    meta = get_constituents() if index == "KLCI" else None
    return ih.compute_health(cfg, tickers_meta=meta)


def get_health(index: str = "KLCI", lookback: str = "1y", term: str = "short",
               force: bool = False) -> ih.HealthResult:
    """Breadth Index Health (stale-while-revalidate cached). KLCI only in MVP.

    term: short (10/25) | mid (20/50) | long (50/100) — SMA/momentum/RSI and
    new-high/low lookbacks.
    """
    key = f"health:{index}:{lookback}:{term}"
    return _swr(key, TTL_SECONDS, lambda: _build_health(index, lookback, term, force), force=force)


def get_sector_health(lookback: str = "1y", term: str = "short", force: bool = False):
    """Per-sector breadth Index Health % (sector rotation), SWR-cached.

    (Date x sector) DataFrame; heavy (scrapes + downloads 13 sector member sets).
    """
    key = f"sector:{lookback}:{term}"
    cfg = _cfg("KLCI", lookback, term)
    cfg.refresh_cache = force
    return _swr(key, TTL_SECONDS * 2, lambda: ih.sector_rotation_health(cfg), force=force)


def _build_index_panel(lookback):
    """(Date x index) price panel: KLCI raw + each sector as an equal-weight
    proxy (rebased to 100). All constituent prices are fetched in ONE batched
    download so they cache together (previously 14 separate downloads shared —
    and kept overwriting — one cache file, so nothing cached and every build
    re-downloaded ~300 stocks; that took 7+ min and stalled on a cloud host)."""
    import pandas as pd
    cfg = _cfg("KLCI", lookback)
    # 1) resolve each sector's constituents (sector-page scrapes)
    members = {}
    for sec, code in ih.SECTOR_INDEX_CODES.items():
        try:
            meta = ih.get_index_tickers(code, cfg.max_retries, cfg.retry_wait)
            if not meta.empty:
                members[sec] = meta["Ticker"].tolist()
        except Exception:  # noqa: BLE001
            continue
    # 2) one batched price download for every symbol (KLCI + all members)
    syms = list(dict.fromkeys(
        [ih.INDEX_SYMBOL_KLCI] + [t for tks in members.values() for t in tks]))
    try:
        px = ih.download_prices(syms, cfg)
    except Exception:  # noqa: BLE001
        px = pd.DataFrame()
    if px.empty:
        return pd.DataFrame()
    # 3) assemble: KLCI level + per-sector equal-weight (rebased to 100)
    cols = {}
    if ih.INDEX_SYMBOL_KLCI in px.columns:
        cols["KLCI"] = px[ih.INDEX_SYMBOL_KLCI]
    for sec, tks in members.items():
        have = [t for t in tks if t in px.columns]
        if not have:
            continue
        sub = px[have]
        base = sub.ffill().bfill().iloc[0]
        cols[sec] = (sub.divide(base) * 100.0).mean(axis=1)
    return pd.DataFrame(cols).sort_index()


def get_index_price_panel(lookback: str = "2y", force: bool = False):
    """(Date x index) price panel: KLCI + 13 sector eq-wt indexes. SWR-cached."""
    key = f"indexpanel:{lookback}"
    return _swr(key, TTL_SECONDS * 2, lambda: _build_index_panel(lookback), force=force)


def get_analyst_sentiment(index: str = "KLCI", force: bool = False) -> dict:
    """Malaysia analyst sentiment over the KLCI constituents (SWR-cached).

    Slow first build (~30 yfinance recommendation calls), so it is cached with
    a long TTL and persisted to disk.
    """
    from . import sentiment as sent_mod

    key = f"sentiment:{index}"

    def _build():
        meta = get_constituents()
        return sent_mod.build_sentiment(meta, index=index)

    return _swr(key, TTL_SECONDS * 12, _build, force=force)  # 6h TTL


def get_klci_gex(force: bool = False) -> dict:
    """KLCI warrant Gamma Exposure payload (SWR-cached).

    Very slow first build (discovers + scrapes the whole FBMKLCI warrant
    chain with polite delays), so it is cached with a long TTL, persisted to
    disk, and served stale-while-revalidate.
    """
    from . import klci_gex as gex_mod

    key = "gex:KLCI"

    def _build():
        health_pct = None
        try:  # combine with today's Index Health for the regime readout
            result = get_health("KLCI")
            hp = result.health_pct.dropna()
            if len(hp):
                health_pct = float(hp.iloc[-1])
        except Exception:  # noqa: BLE001 - readout degrades gracefully
            pass
        return gex_mod.build_gex_payload(health_pct=health_pct)

    return _swr(key, TTL_SECONDS * 24, _build, force=force)  # 12h TTL


def get_fbm_health(key: str, lookback: str = "1y", term: str = "short",
                   force: bool = False) -> dict:
    """FBM market-index health (Mid 70 / ACE / EMAS / Fledgling), SWR-cached.

    Heavy first build: scrapes the constituent list from investingmalaysia and
    downloads every member's prices (EMAS is ~200+ stocks), so it is cached
    with a long TTL and persisted to disk. term: short | mid | long.
    """
    from . import fbm_indexes as fbm_mod

    k = key.upper()
    cache_key = f"fbm:{k}:{lookback}:{term}"
    return _swr(cache_key, TTL_SECONDS * 4,  # 2h TTL
                lambda: fbm_mod.build_fbm_health(k, lookback, term), force=force)


def get_risk_appetite(force: bool = False) -> dict:
    """Risk-appetite index spreads (ACE / MID 70 vs KLCI), SWR-cached.

    Light build (3 index histories from the klsescreener UDF feed), but cached
    like everything else so the page loads instantly.
    """
    from . import risk_appetite as ra_mod

    return _swr("riskapp:1", TTL_SECONDS,  # 30 min TTL
                lambda: ra_mod.build_risk_appetite(), force=force)


def warm_all():
    """Pre-warm every heavy view so users always hit a populated cache.

    Runs on startup and on the scheduler (always-on host). The core KLCI /
    sector short-term views + index panel are force-refreshed so intraday
    prices and breadth stay current; the short build downloads the prices once
    and the mid/long recomputes + the FBM indexes reuse those cached prices.
    The slow scrapes (sentiment, GEX) and everything else are warmed without
    force — SWR only rebuilds them in the background when their long TTLs lapse.
    """
    from . import fbm_indexes as fbm_mod

    jobs = [
        # core — forced so prices/breadth stay fresh (short shares its prices)
        lambda: get_health("KLCI", term="short", force=True),
        lambda: get_sector_health(term="short", force=True),
        # panel: NOT forced — a forced rebuild re-downloads all 13 sector
        # constituent sets (~7 min on a rate-limited host). Non-force reuses
        # the disk cache; SWR refreshes it in the background when it goes stale.
        lambda: get_index_price_panel(),
        # KLCI + sector mid/long — cheap recompute over the just-cached prices
        lambda: get_health("KLCI", term="mid"),
        lambda: get_health("KLCI", term="long"),
        lambda: get_sector_health(term="mid"),
        lambda: get_sector_health(term="long"),
        # slow scrapes — long TTL, SWR keeps them fresh
        lambda: get_analyst_sentiment("KLCI"),
        lambda: get_klci_gex(),
        lambda: get_risk_appetite(),
    ]
    # FBM market indexes × terms — prices shared per index; per-term recompute
    # is cheap, so the Market Index page is never a cold on-request build.
    for k in fbm_mod.FBM_INDEXES:
        for term in ("short", "mid", "long"):
            jobs.append(lambda k=k, term=term: get_fbm_health(k, term=term))

    for fn in jobs:
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

"""
klsescreener quotes provider — Bursa Insight.

Scrapes the whole-market quote table from klsescreener's
`/v2/screener/quote_results` page in ONE request: last price, change, change %,
volume and key fundamentals (EPS, PE, DY, NTA, P/B, market cap) for every Bursa
stock. Server-rendered HTML, so it parses without a browser; no API key, no
per-symbol rate limit. Cached with a short TTL.

This is delayed data (~15 min, klsescreener's feed from Bursa) — same tier as
Yahoo. Use it for quotes/fundamentals (ticker tape, snapshots); OHLC history for
charts/breadth/backtests still comes from yfinance.

Returned shape: { "<4-digit code>": {code,last,change,chg_pct,volume,eps,pe,dy,
nta,pb,market_cap}, ... }
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request

QUOTE_URL = "https://www.klsescreener.com/v2/screener/quote_results"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
TTL = 180  # 3 minutes

# column index -> field (confirmed against the live 19-cell MAYBANK row:
# 0 name, 1 code, 2 sector, 3 last, 4 change, 5 chg%, 6 52w-range, 7 volume,
# 8 EPS, 10 NTA, 11 PE, 12 DY, 14 P/B, 15 market-cap)
_NUM_COLS = {3: "last", 4: "change", 5: "chg_pct", 7: "volume", 8: "eps",
             10: "nta", 11: "pe", 12: "dy", 14: "pb", 15: "market_cap"}
_TXT_COLS = {0: "name", 2: "sector"}

_lock = threading.Lock()
_cache = {"ts": 0.0, "data": {}}


def _num(s):
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch(retries=3, wait=3.0) -> dict:
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(QUOTE_URL, headers=HEADERS)
            html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            out = {}
            for row in re.findall(r'<tr class="list">(.*?)</tr>', html, re.S):
                mcode = re.search(r'/v2/stocks/view/(\d{4}[A-Z]*)/', row)
                if not mcode:
                    continue
                code = mcode.group(1)
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if len(cells) < 8:
                    continue
                rec = {"code": code}
                for idx, field in _NUM_COLS.items():
                    rec[field] = _num(cells[idx]) if idx < len(cells) else None
                for idx, field in _TXT_COLS.items():
                    rec[field] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cells[idx])).strip() \
                        if idx < len(cells) else None
                key = code.zfill(4) if code.isdigit() else code
                out[key] = rec
            if len(out) < 50:
                raise RuntimeError(f"parsed only {len(out)} quotes (layout changed?)")
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(wait)
    raise RuntimeError(f"klsescreener quote scrape failed: {last_err}")


def get_quotes(force: bool = False) -> dict:
    """Whole-market quote dict keyed by 4-digit code (cached ~3 min)."""
    now = time.monotonic()
    with _lock:
        if force or (now - _cache["ts"]) > TTL or not _cache["data"]:
            _cache["data"] = _fetch()
            _cache["ts"] = now
        return _cache["data"]


def get_one(code: str):
    """Quote for a single 4-digit Bursa code (e.g. '1155')."""
    return get_quotes().get(str(code).zfill(4))

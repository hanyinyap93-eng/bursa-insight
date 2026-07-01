"""
News aggregator — Bursa Insight.

Pulls market news from public RSS feeds (local Malaysia + global), tags each
item to a Bursa sector / index by keyword, and serves a filterable feed. Cached
with a short TTL. feedparser does the parsing; no API keys needed for RSS.

Feeds are configurable; the defaults below are public business/market RSS feeds.
If a feed is unreachable it is skipped, so the endpoint degrades gracefully.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

try:
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None

from . import index_health as ih

# (label, scope, url). scope: "local" (Malaysia) | "global".
# Malaysia-market focused. Google News searches aggregate Bursa/KLCI coverage
# from many outlets (local AND global) filtered to Malaysia relevance; the rest
# are Malaysian business desks. Per-item publisher is extracted for Google News.
_GN = ("https://news.google.com/rss/search?q={q}+when:7d&hl=en-MY&gl=MY&ceid=MY:en")
FEEDS = [
    ("Google News", "local", _GN.format(q="(Bursa+Malaysia+OR+KLCI+OR+%22FBM+KLCI%22)")),
    ("Google News", "local", _GN.format(q="(Malaysia+economy+OR+ringgit+OR+%22Bank+Negara%22)")),
    ("Free Malaysia Today", "local", "https://www.freemalaysiatoday.com/category/business/feed/"),
    ("Malay Mail", "local", "https://www.malaymail.com/feed/rss/money"),
    ("BusinessToday", "local", "https://www.businesstoday.com.my/feed/"),
]

# keyword -> sector tag (re-uses the engine's sector keyword logic + index names)
_SECTOR_TERMS = {
    "TECHNOLOGY": ["semiconductor", "tech", "chip", "software", "data centre", "data center"],
    "FINANCE": ["bank", "banking", "insurer", "insurance", "financial"],
    "PLANTATION": ["palm oil", "plantation", "cpo", "planter"],
    "ENERGY": ["oil", "petronas", "petroleum", "energy", "gas"],
    "PROPERTIES": ["property", "real estate", "developer"],
    "HEALTH": ["healthcare", "hospital", "glove", "pharma"],
    "UTILITIES": ["tenaga", "utility", "power", "electricity"],
    "CONSTRUCTN": ["construction", "infrastructure", "contractor"],
    "TELECOMMUNICATIONS": ["telco", "telecom", "5g", "broadband"],
    "TRANSPORTATION": ["airline", "port", "logistics", "shipping"],
    "CONSUMER": ["retail", "consumer", "f&b", "food"],
    "REIT": ["reit", "real estate investment"],
}
_INDEX_TERMS = {
    "KLCI": ["klci", "bursa", "kuala lumpur", "malaysia", "ringgit", "fbm"],
    "SPX": ["s&p 500", "s&p500", "wall street", "nasdaq", "dow jones", "fed", "federal reserve"],
}

_cache = {"ts": 0.0, "items": []}
TTL = 60 * 10  # 10 min


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    scope: str
    published: str
    summary: str
    sectors: list
    indices: list


def _tag(text: str):
    t = text.lower()
    sectors = [sec for sec, kws in _SECTOR_TERMS.items() if any(k in t for k in kws)]
    indices = [idx for idx, kws in _INDEX_TERMS.items() if any(k in t for k in kws)]
    return sectors, indices


def _get(url, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _strip(html):
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def _fetch_all() -> list[dict]:
    if feedparser is None:
        return []
    items = []
    for label, scope, url in FEEDS:
        try:
            parsed = feedparser.parse(_get(url))       # urllib timeout: never hangs
        except Exception:  # noqa: BLE001
            continue
        for e in parsed.entries[:20]:
            title = getattr(e, "title", "").strip()
            src = label
            # Google News: real publisher is in e.source.title, and the title
            # carries a trailing " - Publisher" that we strip for a clean headline.
            try:
                s = getattr(e, "source", None)
                if s is not None and getattr(s, "title", None):
                    src = s.title
                if " - " in title:
                    head, pub = title.rsplit(" - ", 1)
                    if 0 < len(pub) <= 40:
                        title = head.strip()
                        if src == label:
                            src = pub.strip()
            except Exception:  # noqa: BLE001
                pass
            summary = _strip(getattr(e, "summary", ""))[:280]
            link = getattr(e, "link", "")
            published = getattr(e, "published", "") or getattr(e, "updated", "")
            sectors, indices = _tag(f"{title} {summary}")
            items.append({
                "title": title, "link": link, "source": src, "scope": scope,
                "published": published, "summary": summary,
                "sectors": sectors, "indices": indices,
            })
    # de-duplicate by title (Google News + local desks overlap)
    seen, uniq = set(), []
    for it in items:
        k = it["title"].lower()[:80]
        if k and k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def get_news(scope: str = None, sector: str = None, index: str = None,
             limit: int = 50, force: bool = False) -> list[dict]:
    now = time.monotonic()
    if force or (now - _cache["ts"]) > TTL or not _cache["items"]:
        _cache["items"] = _fetch_all()
        _cache["ts"] = now
    items = _cache["items"]
    if scope:
        items = [i for i in items if i["scope"] == scope]
    if sector:
        items = [i for i in items if sector in i["sectors"]]
    if index:
        items = [i for i in items if index in i["indices"]]
    return items[:limit]

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
FEEDS = [
    ("The Edge Markets", "local", "https://theedgemalaysia.com/rss"),
    ("The Star Business", "local", "https://www.thestar.com.my/rss/business"),
    ("Bursa Announcements", "local", "https://www.bursamalaysia.com/misc/rss/announcements"),
    ("Reuters Markets", "global", "https://www.reutersagency.com/feed/?best-topics=markets&post_type=best"),
    ("CNBC Markets", "global", "https://search.cnbc.com/rss/2.0/CNBC.xml"),
    ("Yahoo Finance", "global", "https://finance.yahoo.com/news/rssindex"),
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


def _fetch_all() -> list[dict]:
    if feedparser is None:
        return []
    items = []
    for label, scope, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception:  # noqa: BLE001
            continue
        for e in parsed.entries[:25]:
            title = getattr(e, "title", "").strip()
            summary = getattr(e, "summary", "")[:400]
            link = getattr(e, "link", "")
            published = getattr(e, "published", "") or getattr(e, "updated", "")
            sectors, indices = _tag(f"{title} {summary}")
            items.append({
                "title": title, "link": link, "source": label, "scope": scope,
                "published": published, "summary": summary,
                "sectors": sectors, "indices": indices,
            })
    return items


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

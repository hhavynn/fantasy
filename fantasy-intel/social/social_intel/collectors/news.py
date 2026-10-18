"""News RSS: Google News search feeds (per query, last day) and direct outlet feeds.

News is what reporters confirm; the social sources are what people argue about. News items carry no
engagement, so they count toward a cluster's coverage, confirm or deny rumours, and give ideas a
verifiable link.
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import SocialPost
from social_intel.core.http import FetchError


def parse_rss(text: str, now: datetime, method: str, community: str) -> list[SocialPost]:
    root = ET.fromstring(text)
    out = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        source = item.find("source")
        outlet = source.text if source is not None and source.text else community
        if method == "google-news-rss" and " - " in title:
            title = title.rsplit(" - ", 1)[0]  # Google appends " - Outlet"
        desc = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        pub = item.findtext("pubDate")
        try:
            created = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            created = None
        guid = item.findtext("guid") or link
        out.append(SocialPost(
            platform="news", external_id=hashlib.sha1(guid.encode()).hexdigest()[:16], url=link,
            created_at=created, collected_at=now, title=title,
            text=title if method == "google-news-rss" or desc[:80] in title else f"{title}\n{desc[:600]}",
            author=outlet, community=community, collection_method=method, confidence=0.9,
        ))
    return out


@register
class GoogleNewsCollector(Collector):
    name = "google_news"
    platform = "news"
    method = "google-news-rss"
    interval_minutes = 20
    description = "Google News RSS search (when:1d) per query"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts, fails = [], 0
        queries = ctx.config.extra.get("google_news_queries", []) + ctx.config.extra.get("hot_player_queries", [])
        for q in queries:
            try:
                r = ctx.fetcher.get("https://news.google.com/rss/search",
                                    params={"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
            except FetchError:
                fails += 1
                continue
            posts.extend(parse_rss(r.text, ctx.now, self.method, f"gnews:{q}"))
        if not posts and fails:
            raise FetchError(f"google news: {fails} queries failed")
        return posts, {}


@register
class NewsFeedsCollector(Collector):
    name = "news_feeds"
    platform = "news"
    method = "outlet-rss"
    interval_minutes = 20
    description = "Outlet RSS feeds (ESPN, PFT, Yahoo, CBS, RotoWire)"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts, errors = [], []
        for name, url in ctx.config.news_feeds.items():
            try:
                r = ctx.fetcher.get(url)
                posts.extend(parse_rss(r.text, ctx.now, self.method, name))
            except (FetchError, ET.ParseError) as e:
                errors.append(f"{name}: {e}")
        if not posts and errors:
            raise FetchError("; ".join(errors)[:300])
        return posts, {"errors": errors}

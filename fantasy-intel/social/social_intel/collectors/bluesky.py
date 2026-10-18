"""Bluesky public search.

``api.bsky.app/xrpc/app.bsky.feed.searchPosts`` answers without auth (the documented
``public.api.bsky.app`` host returns 403 for search; verified 2026-10-17). Cursor pagination is
refused without auth, so each run asks for one page per query and sort, narrowed with ``since``
to the last collection, which is the incremental cursor.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import Engagement, SocialPost, iso, parse_iso
from social_intel.core.http import FetchError

SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
AUTHOR_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"


def bsky_post(item: dict[str, Any], now: datetime, method: str, community: str | None) -> SocialPost | None:
    rec = item.get("record") or {}
    uri = item.get("uri") or ""
    m = re.match(r"at://([^/]+)/app\.bsky\.feed\.post/(\w+)", uri)
    if not m:
        return None
    author = (item.get("author") or {}).get("handle")
    text = rec.get("text") or ""
    tags = [f.get("tag") for fac in rec.get("facets") or [] for f in fac.get("features") or [] if f.get("tag")]
    return SocialPost(
        platform="bluesky", external_id=m.group(2), url=f"https://bsky.app/profile/{author or m.group(1)}/post/{m.group(2)}",
        created_at=parse_iso(rec.get("createdAt") or item.get("indexedAt")), collected_at=now, text=text,
        author=author, community=community, hashtags=tags,
        engagement=Engagement(likes=item.get("likeCount"), comments=item.get("replyCount"),
                              shares=(item.get("repostCount") or 0) + (item.get("quoteCount") or 0)),
        collection_method=method, confidence=0.95, lang=(rec.get("langs") or [None])[0],
        parent_id=((rec.get("reply") or {}).get("root") or {}).get("uri"),
    )


@register
class BlueskyCollector(Collector):
    name = "bluesky"
    platform = "bluesky"
    method = "bsky-appview-search"
    interval_minutes = 15
    description = "Bluesky searchPosts (top + latest) per query, no auth"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        cursor = ctx.new_cursor()
        since_map: dict[str, str] = cursor.get("since", {})
        posts: list[SocialPost] = []
        failures = 0
        queries = ctx.config.search_terms
        for q in queries:
            since = parse_iso(since_map.get(q)) or ctx.now - timedelta(hours=36)
            since = max(since - timedelta(hours=6), ctx.now - timedelta(hours=48))  # overlap: engagement keeps moving
            for sort in ("top", "latest"):
                try:
                    r = ctx.fetcher.get(SEARCH, params={"q": q, "limit": 100, "sort": sort, "lang": "en",
                                                        "since": iso(since)})
                except FetchError:
                    failures += 1
                    continue
                for item in r.json().get("posts", []):
                    p = bsky_post(item, ctx.now, self.method, f"search:{q}")
                    if p:
                        posts.append(p)
            since_map[q] = iso(ctx.now)
        for actor in ctx.config.bluesky_actors:
            try:
                r = ctx.fetcher.get(AUTHOR_FEED, params={"actor": actor, "limit": 30, "filter": "posts_no_replies"})
            except FetchError:
                failures += 1
                continue
            for it in r.json().get("feed", []):
                p = bsky_post(it.get("post") or {}, ctx.now, "bsky-author-feed", f"actor:{actor}")
                if p:
                    p.is_repost = bool(it.get("reason"))
                    posts.append(p)
        if not posts and failures:
            raise FetchError(f"bluesky: {failures} requests failed", blocked=True)
        cursor["since"] = since_map
        return posts, cursor

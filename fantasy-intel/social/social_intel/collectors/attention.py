"""Attention signals: sources that say *how many people care* rather than what they say.

* ``sleeper_trending`` -- Sleeper's trending adds/drops (24 h counts across millions of leagues).
  The earliest free fantasy signal: a backup RB shows up here hours before the takes do.
* ``google_trends`` -- Google's daily trending-searches RSS (US). Only ~10 items, so it mostly
  catches the very biggest NFL stories; cheap enough to poll.
* ``wikipedia`` -- daily article pageviews for the most-rostered players; a spike against the
  player's own 7-day median is an attention surge (1-2 day lag).

All emit ``SocialPost`` rows on attention platforms; scoring counts them toward momentum, never as
discussion text.
"""

from __future__ import annotations

import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import Engagement, SocialPost
from social_intel.core.http import BOT_UA, FetchError

HT = "{https://trends.google.com/trending/rss}"
POSITION_TITLE = {"QB": "quarterback", "RB": "running_back", "WR": "wide_receiver", "TE": "tight_end", "K": "kicker"}


@register
class SleeperTrendingCollector(Collector):
    name = "sleeper_trending"
    platform = "sleeper"
    method = "sleeper-trending-api"
    interval_minutes = 30
    description = "Sleeper trending adds/drops, 24 h window"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        sleeper_to_gsis: dict[str, str] = ctx.config.extra.get("sleeper_to_gsis", {})
        names: dict[str, str] = ctx.config.extra.get("sleeper_names", {})
        posts = []
        for kind in ("add", "drop"):
            r = ctx.fetcher.get(f"https://api.sleeper.app/v1/players/nfl/trending/{kind}",
                                params={"lookback_hours": 24, "limit": 40}, cache_ttl_s=600)
            for rank, row in enumerate(r.json(), 1):
                sid = str(row.get("player_id"))
                gsis = sleeper_to_gsis.get(sid)
                name = names.get(sid, f"Sleeper player {sid}")
                verb = "added" if kind == "add" else "dropped"
                posts.append(SocialPost(
                    platform="sleeper", external_id=f"{kind}:{sid}",
                    url=f"https://sleeper.com/players/nfl/{sid}",
                    created_at=ctx.now, collected_at=ctx.now,
                    text=f"{name} trending {kind}: {verb} in {row.get('count', 0):,} Sleeper leagues in 24h "
                         f"(#{rank} {kind})",
                    community=f"trending_{kind}", engagement=Engagement(likes=int(row.get("count") or 0)),
                    collection_method=self.method, confidence=1.0,
                    extra={"sleeper_id": sid, "gsis_id": gsis, "rank": rank, "direction": kind, "subject": name,
                           "count_24h": row.get("count")},
                ))
        return posts, {}


@register
class GoogleTrendsCollector(Collector):
    name = "google_trends"
    platform = "google_trends"
    method = "google-trends-daily-rss"
    interval_minutes = 30
    description = "Google daily trending searches RSS (US, ~10 items)"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        r = ctx.fetcher.get("https://trends.google.com/trending/rss", params={"geo": ctx.config.trends_geo})
        root = ET.fromstring(r.text)
        posts = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            traffic = (item.findtext(f"{HT}approx_traffic") or "0").replace("+", "").replace(",", "")
            news = [item.findtext(f"{HT}news_item/{HT}news_item_title") or ""]
            news_url = item.findtext(f"{HT}news_item/{HT}news_item_url")
            pub = item.findtext("pubDate")
            try:
                views = int(traffic.replace("K", "000").replace("M", "000000"))
            except ValueError:
                views = None
            created = parsedate_to_datetime(pub) if pub else ctx.now
            posts.append(SocialPost(
                platform="google_trends", external_id=f"{title.lower()}:{created:%Y-%m-%d}",  # one row per term per day
                url=news_url or f"https://trends.google.com/trends/explore?q={quote(title)}&geo=US",
                created_at=created, collected_at=ctx.now,
                text=f"Trending search: {title}. {' '.join(news)}".strip(), title=title,
                engagement=Engagement(views=views), collection_method=self.method, confidence=0.9,
                community="daily_trends", extra={"approx_traffic": traffic},
            ))
        return posts, {}


@register
class WikipediaPageviewsCollector(Collector):
    name = "wikipedia"
    platform = "wikipedia"
    method = "wikimedia-pageviews"
    interval_minutes = 12 * 60
    description = "Daily Wikipedia pageviews for top players; emits spikes vs 7-day median"
    spike_ratio = 1.6
    min_views = 1500

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        players: list[dict] = ctx.config.extra.get("wiki_players", [])  # [{"name", "gsis_id", "title"?}]
        cursor = ctx.new_cursor()
        titles: dict[str, str] = cursor.get("titles", {})
        end = (ctx.now - timedelta(days=1)).strftime("%Y%m%d")
        start = (ctx.now - timedelta(days=10)).strftime("%Y%m%d")
        posts, misses = [], 0
        headers = {"User-Agent": BOT_UA}
        for p in players:
            items, title = None, titles.get(p["gsis_id"])
            for cand in ([title] if title else self._candidates(p)):
                url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
                       f"{quote(cand, safe='')}/daily/{start}/{end}")
                try:
                    items = ctx.fetcher.get(url, headers=headers, cache_ttl_s=6 * 3600, min_interval_s=0.2).json().get("items", [])
                    title = cand
                    break
                except FetchError as e:
                    if e.status != 404:  # 404 = no such article: try the next title
                        misses += 1
                        break
            if not items or not title:
                misses += 1
                continue
            titles[p["gsis_id"]] = title
            if len(items) < 4:
                continue
            views = [i["views"] for i in items]
            last, base = views[-1], statistics.median(views[:-1])
            ratio = last / max(base, 1)
            if ratio >= self.spike_ratio and last >= self.min_views:
                day = items[-1]["timestamp"][:8]
                posts.append(SocialPost(
                    platform="wikipedia", external_id=f"{title}:{day}",
                    url=f"https://en.wikipedia.org/wiki/{quote(title)}",
                    created_at=datetime.strptime(day, "%Y%m%d").replace(tzinfo=ctx.now.tzinfo), collected_at=ctx.now,
                    text=f"{p['name']} Wikipedia pageviews spiked to {last:,} ({ratio:.1f}x the 7-day median of {base:,.0f})",
                    community="pageviews", engagement=Engagement(views=last), collection_method=self.method,
                    confidence=0.9, extra={"gsis_id": p["gsis_id"], "ratio": round(ratio, 2), "median": base},
                ))
        cursor["titles"] = titles
        if not posts and misses >= len(players) and players:
            raise FetchError("wikipedia: no titles resolved")
        return posts, cursor

    @staticmethod
    def _candidates(p: dict) -> list[str]:
        """Article titles to try, most specific first. The pageviews REST API answers 404 for a missing
        article (cheap, not rate-limited), unlike the MediaWiki query API, which 429s quickly."""
        base = p["name"].replace(" ", "_")
        role = POSITION_TITLE.get(p.get("position") or "")
        return [t for t in (f"{base}_({role})" if role else None, f"{base}_(American_football)", base) if t]

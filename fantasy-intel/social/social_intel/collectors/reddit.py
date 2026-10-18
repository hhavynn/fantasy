"""Reddit without an account.

Since late May 2026 Reddit answers unauthenticated ``.json`` listings with 403 (verified
2026-10-17: www, old and api hosts all refuse or redirect to login), and new OAuth apps need
manual approval. Two free paths still work and complement each other:

* ``reddit`` -- Atom feeds of a multi-subreddit listing (``/r/a+b+c/hot.rss?limit=100``). Titles,
  bodies, authors, timestamps; no scores. Reddit allows roughly one feed request a minute, so a
  run fetches hot then rising with a 65 s gap.
* ``reddit_activity`` -- Arctic Shift (arctic-shift.photon-reddit.com), a public Reddit archive that
  ingests within minutes. Scores there are frozen at capture, so instead of trusting them we count
  *fresh comments per thread*: comments/hour on a thread is the engagement velocity, and the comment
  text tells us whether people are arguing. It also returns brand-new posts (the "new" listing).

Both emit the same post key for a thread, so the store merges them into one row.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import SocialPost
from social_intel.core.http import FetchError

ATOM = "{http://www.w3.org/2005/Atom}"
ARCTIC = "https://arctic-shift.photon-reddit.com/api"


def _strip_html(s: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", html.unescape(s), flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\[link\]|\[comments\]|submitted by\s+/u/\S+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_reddit_atom(text: str, now: datetime, method: str = "reddit-rss") -> list[SocialPost]:
    root = ET.fromstring(text)
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        rid = (entry.findtext(f"{ATOM}id") or "").removeprefix("t3_")
        if not rid:
            continue
        link = entry.find(f"{ATOM}link")
        cat = entry.find(f"{ATOM}category")
        title = html.unescape(entry.findtext(f"{ATOM}title") or "")
        published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
        body = _strip_html(entry.findtext(f"{ATOM}content") or "")[:2000]
        sub = cat.get("term") if cat is not None else None
        out.append(SocialPost(
            platform="reddit", external_id=rid, url=link.get("href") if link is not None else "",
            created_at=datetime.fromisoformat(published) if published else None, collected_at=now,
            title=title, text=(title + "\n" + body).strip(),
            author=(entry.findtext(f"{ATOM}author/{ATOM}name") or "").removeprefix("/u/") or None,
            community=f"r/{sub}" if sub else None, collection_method=method, confidence=0.85,
        ))
    return out


@register
class RedditCollector(Collector):
    name = "reddit"
    platform = "reddit"
    method = "reddit-rss"
    interval_minutes = 20
    description = "Multi-subreddit hot + rising Atom feeds (no account; ~1 request/min)"
    listings = ("hot", "rising")
    gap_s = 65.0

    def subs(self, ctx: CollectContext) -> list[str]:
        return ctx.config.subreddits

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        multi = "+".join(self.subs(ctx))
        posts: list[SocialPost] = []
        errors: list[str] = []
        for i, listing in enumerate(self.listings):
            if i:
                ctx.fetcher.sleep(self.gap_s)
            try:
                r = ctx.fetcher.get(f"https://www.reddit.com/r/{multi}/{listing}.rss", params={"limit": 100})
            except FetchError as e:
                errors.append(f"{listing}: {e.status}")
                if e.status == 429:
                    break  # the feed budget is spent; the next run picks up
                continue
            for p in parse_reddit_atom(r.text, ctx.now, self.method):
                p.extra["listing"] = listing
                posts.append(p)
        if not posts and errors:
            raise FetchError("reddit rss: " + "; ".join(errors), status=429 if "429" in "".join(errors) else 403,
                             blocked=True)
        return posts, {"errors": errors}


@register
class RedditTeamsCollector(RedditCollector):
    name = "reddit_teams"
    interval_minutes = 120
    description = "All 32 team subreddits in one hot feed"
    listings = ("hot",)

    def subs(self, ctx: CollectContext) -> list[str]:
        return ctx.config.extra.get("team_subreddits", [])


@register
class RedditActivityCollector(Collector):
    name = "reddit_activity"
    platform = "reddit"
    method = "arctic-shift"
    interval_minutes = 15
    description = "Arctic Shift archive: fresh comments per thread (velocity + debate text) and new posts"
    activity_subs = ("fantasyfootball", "nfl", "DynastyFF")
    pages = 4

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts: dict[str, SocialPost] = {}
        by_link: dict[str, list[dict]] = defaultdict(list)
        link_meta: dict[str, dict] = {}
        spans: dict[str, tuple[float, float]] = {}
        for sub in self.activity_subs:
            before = None
            times: list[float] = []
            for _ in range(self.pages):
                params = {"subreddit": sub, "limit": 100, "sort": "desc"}
                if before:
                    params["before"] = int(before)
                data = ctx.fetcher.get(f"{ARCTIC}/comments/search", params=params).json().get("data") or []
                if not data:
                    break
                for c in data:
                    lid = (c.get("link_id") or "").removeprefix("t3_")
                    if not lid:
                        continue
                    by_link[lid].append(c)
                    times.append(float(c.get("created_utc") or 0))
                    link_meta.setdefault(lid, {"title": c.get("link_title"), "permalink": c.get("link_permalink"),
                                               "sub": sub})
                before = min(float(c.get("created_utc") or 0) for c in data)
                if ctx.now.timestamp() - before > 3 * 3600:
                    break  # three hours of comments is plenty for a rate
            if times:
                spans[sub] = (min(times), max(times))
            # brand-new submissions (the "new" listing)
            for d in ctx.fetcher.get(f"{ARCTIC}/posts/search",
                                     params={"subreddit": sub, "limit": 100, "sort": "desc"}).json().get("data") or []:
                p = self._post(d, ctx.now)
                if p:
                    posts[p.external_id] = p
        for lid, comments in by_link.items():
            meta = link_meta[lid]
            lo, hi = spans.get(meta["sub"], (0.0, 0.0))
            hours = max((ctx.now.timestamp() - lo) / 3600, 0.25)
            rate = len(comments) / hours
            top = sorted(comments, key=lambda c: len(c.get("body") or ""), reverse=True)[:8]
            extra = {
                "comment_rate_per_h": round(rate, 2),
                "fresh_comments": len(comments),
                "window_h": round(hours, 2),
                "controversial_share": round(sum(1 for c in comments if c.get("controversiality")) / len(comments), 3),
                "sample_comments": [(c.get("body") or "")[:280] for c in top],
            }
            if lid in posts:
                posts[lid].extra.update(extra)
                continue
            title = html.unescape(meta.get("title") or "")
            permalink = meta.get("permalink") or f"https://www.reddit.com/r/{meta['sub']}/comments/{lid}/"
            posts[lid] = SocialPost(
                platform="reddit", external_id=lid,
                url=permalink if permalink.startswith("http") else "https://www.reddit.com" + permalink,
                created_at=None, collected_at=ctx.now, title=title, text=title, community=f"r/{meta['sub']}",
                collection_method=self.method, confidence=0.8, extra=extra,
            )
        return list(posts.values()), {"links": len(by_link)}

    def _post(self, d: dict[str, Any], now: datetime) -> SocialPost | None:
        if d.get("stickied") or d.get("over_18") or not d.get("id"):
            return None
        title = html.unescape(d.get("title") or "")
        body = html.unescape(d.get("selftext") or "")[:2000]
        if body in ("[removed]", "[deleted]"):
            body = ""
        return SocialPost(
            platform="reddit", external_id=d["id"],
            url="https://www.reddit.com" + (d.get("permalink") or f"/comments/{d['id']}/"),
            created_at=datetime.fromtimestamp(d["created_utc"], tz=timezone.utc) if d.get("created_utc") else None,
            collected_at=now, title=title, text=(title + "\n" + body).strip(), author=d.get("author"),
            community="r/" + (d.get("subreddit") or ""), collection_method=self.method, confidence=0.8,
            parent_id=(d.get("crosspost_parent") or "").removeprefix("t3_") or None,
            is_repost=bool(d.get("crosspost_parent")),
            extra={"link_flair_text": d.get("link_flair_text")} if d.get("link_flair_text") else {},
        )

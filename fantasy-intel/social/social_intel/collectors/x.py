"""X / Twitter without an API subscription.

Nitter is gone (nitter.net 410, xcancel suspended, the project archived) and twscrape/twikit need
logged-in X accounts. The working free route (verified 2026-10-17) is FxTwitter's public API:
``api.fxtwitter.com/2/profile/<user>/statuses`` returns an account's latest ~20 posts with likes,
reposts, replies, quotes and views. There is no search, so we follow a curated list of insiders and
fantasy analysts -- which is where breaking injury/trade news starts anyway.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import Engagement, SocialPost, parse_iso
from social_intel.core.http import FetchError


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        from datetime import timezone
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        return parse_iso(v)
    except ValueError:
        try:
            return parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None


def fx_post(s: dict[str, Any], now: datetime, account: str) -> SocialPost | None:
    sid = str(s.get("id") or "")
    if not sid:
        return None
    author = (s.get("author") or {}).get("screen_name") or account
    text = s.get("text") or (s.get("raw_text") or {}).get("text") or ""
    reposted_by = s.get("reposted_by")
    return SocialPost(
        platform="x", external_id=sid, url=s.get("url") or f"https://x.com/{author}/status/{sid}",
        created_at=_ts(s.get("created_timestamp")) or _ts(s.get("created_at")), collected_at=now, text=text,
        author=author, community=f"account:{account}",
        engagement=Engagement(likes=s.get("likes"), comments=s.get("replies"),
                              shares=(s.get("reposts") or 0) + (s.get("quotes") or 0), views=s.get("views")),
        collection_method="fxtwitter-profile-statuses", confidence=0.85,
        is_repost=bool(reposted_by), parent_id=(s.get("quote") or {}).get("id"),
        lang=s.get("lang"),
    )


@register
class XCollector(Collector):
    name = "x"
    platform = "x"
    method = "fxtwitter-profile-statuses"
    interval_minutes = 20
    description = "X timelines of curated NFL insiders/analysts via the public FxTwitter API"

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts, failures = [], []
        for account in ctx.config.x_accounts:
            try:
                r = ctx.fetcher.get(f"https://api.fxtwitter.com/2/profile/{account}/statuses")
            except FetchError as e:
                failures.append(f"{account}: {e.status}")
                continue
            data = r.json()
            items = data.get("results") or data.get("statuses") or data.get("tweets") or []
            for s in items:
                p = fx_post(s, ctx.now, account)
                if p:
                    posts.append(p)
        if not posts and failures:
            raise FetchError("fxtwitter: " + "; ".join(failures[:5]), blocked=True)
        return posts, {"failures": failures}


@register
class InstagramCollector(Collector):
    name = "instagram"
    platform = "instagram"
    method = "instaloader-session"
    interval_minutes = 360
    optional = True
    description = "Instagram public profiles; needs an instaloader session file (anonymous access is 401)"

    def available(self) -> tuple[bool, str]:
        import os
        if not os.getenv("INSTAGRAM_SESSION_USER"):
            return False, "anonymous Instagram returns 401; set INSTAGRAM_SESSION_USER to an instaloader session"
        try:
            import instaloader  # noqa: F401
        except ImportError:
            return False, "instaloader not installed (uv pip install instaloader)"
        return True, ""

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        import itertools
        import os

        import instaloader  # type: ignore

        L = instaloader.Instaloader(download_pictures=False, download_videos=False, save_metadata=False)
        L.load_session_from_file(os.environ["INSTAGRAM_SESSION_USER"])
        posts = []
        for account in ctx.config.instagram_accounts:
            try:
                prof = instaloader.Profile.from_username(L.context, account)
                for ig in itertools.islice(prof.get_posts(), 12):
                    posts.append(SocialPost(
                        platform="instagram", external_id=ig.shortcode, url=f"https://www.instagram.com/p/{ig.shortcode}/",
                        created_at=ig.date_utc.replace(tzinfo=ctx.now.tzinfo), collected_at=ctx.now,
                        text=ig.caption or "", author=account, community=f"account:{account}",
                        hashtags=list(ig.caption_hashtags), engagement=Engagement(likes=ig.likes, comments=ig.comments,
                                                                                   views=ig.video_view_count),
                        collection_method=self.method, confidence=0.9))
            except Exception as e:  # instaloader raises many types on blocks
                raise FetchError(f"instagram {account}: {e}", blocked=True) from e
        return posts, {}

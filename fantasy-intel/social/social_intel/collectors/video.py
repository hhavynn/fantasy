"""YouTube and TikTok.

* ``youtube`` -- channel RSS feeds (15 latest uploads with view counts, no key) plus a yt-dlp
  search sorted by upload date (``sp=CAISAhAB``) for what everyone is making videos about right now.
  The top search results get a full metadata pass (exact upload time, likes, comment count).
* ``tiktok`` (optional) -- yt-dlp on public creator pages. Hashtag pages are broken in yt-dlp and
  TikTok's Creative Center API needs a login, so creators are the only free route.

yt-dlp is a binary we shell out to (installed via Homebrew/uv); without it both collectors report
"skipped" instead of failing the run. Keep it updated (``brew upgrade yt-dlp``): it breaks when
YouTube/TikTok change their pages.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from social_intel.collectors.base import CollectContext, Collector, register
from social_intel.contract import Engagement, SocialPost, parse_iso
from social_intel.core.http import FetchError

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def ytdlp_bin() -> str | None:
    return os.getenv("YTDLP_BIN") or shutil.which("yt-dlp")


def run_ytdlp(args: list[str], timeout: int = 120) -> list[dict[str, Any]]:
    exe = ytdlp_bin()
    if not exe:
        raise FetchError("yt-dlp not installed")
    proc = subprocess.run([exe, "--quiet", "--no-warnings", "--ignore-errors", *args],
                          capture_output=True, text=True, timeout=timeout)
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not out and proc.returncode:
        raise FetchError(f"yt-dlp exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    return out


def parse_channel_feed(text: str, now: datetime, channel: str) -> list[SocialPost]:
    root = ET.fromstring(text)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        vid = e.findtext(f"{YT}videoId")
        if not vid:
            continue
        group = e.find(f"{MEDIA}group")
        stats = group.find(f"{MEDIA}community/{MEDIA}statistics") if group is not None else None
        rating = group.find(f"{MEDIA}community/{MEDIA}starRating") if group is not None else None
        title = e.findtext(f"{ATOM}title") or ""
        desc = group.findtext(f"{MEDIA}description") if group is not None else ""
        out.append(SocialPost(
            platform="youtube", external_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
            created_at=parse_iso(e.findtext(f"{ATOM}published")), collected_at=now, title=title,
            text=f"{title}\n{(desc or '')[:500]}".strip(), author=channel, community=f"channel:{channel}",
            engagement=Engagement(views=int(stats.get("views")) if stats is not None and stats.get("views") else None,
                                  likes=int(rating.get("count")) if rating is not None and rating.get("count") else None),
            collection_method="youtube-channel-rss", confidence=0.95,
            extra={"shorts": "#shorts" in (title + (desc or "")).lower()},
        ))
    return out


def ytdlp_video_post(d: dict[str, Any], now: datetime, method: str, community: str) -> SocialPost | None:
    vid = d.get("id")
    if not vid:
        return None
    ts = d.get("timestamp")
    created = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
    if not created and d.get("upload_date"):
        created = datetime.strptime(d["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
    platform = "tiktok" if "tiktok" in (d.get("extractor_key") or d.get("ie_key") or d.get("webpage_url") or "").lower() else "youtube"
    title = d.get("title") or ""
    desc = d.get("description") or ""
    url = d.get("webpage_url") or d.get("url") or (f"https://www.youtube.com/watch?v={vid}" if platform == "youtube" else "")
    return SocialPost(
        platform=platform, external_id=str(vid), url=url, created_at=created, collected_at=now,
        title=title, text=f"{title}\n{desc[:500]}".strip() if desc and desc[:60] not in title else title,
        author=d.get("channel") or d.get("uploader"), community=community,
        hashtags=[t.lstrip("#") for t in (d.get("tags") or [])][:20],
        engagement=Engagement(views=d.get("view_count"), likes=d.get("like_count"), comments=d.get("comment_count"),
                              shares=d.get("repost_count")),
        collection_method=method, confidence=0.9,
        extra={k: d.get(k) for k in ("duration", "channel_follower_count", "channel_id") if d.get(k) is not None},
    )


@register
class YouTubeCollector(Collector):
    name = "youtube"
    platform = "youtube"
    method = "youtube-rss+yt-dlp"
    interval_minutes = 45
    description = "YouTube channel RSS + yt-dlp upload-date search; full metadata for top results"
    search_results = 25
    detail_top = 8

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts: list[SocialPost] = []
        errors: list[str] = []
        for name, cid in ctx.config.youtube_channels.items():
            try:
                r = ctx.fetcher.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid})
                posts.extend(parse_channel_feed(r.text, ctx.now, name))
            except (FetchError, ET.ParseError) as e:
                errors.append(f"rss {name}: {e}")
        if ytdlp_bin():
            found: dict[str, SocialPost] = {}
            for q in ctx.config.extra.get("youtube_queries", ctx.config.search_terms[:3]):
                url = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp=CAISAhAB"
                try:
                    for d in run_ytdlp(["--flat-playlist", "-j", "--playlist-end", str(self.search_results), url]):
                        p = ytdlp_video_post(d, ctx.now, "yt-dlp-search", f"search:{q}")
                        if p:
                            found.setdefault(p.external_id, p)
                except (FetchError, subprocess.TimeoutExpired) as e:
                    errors.append(f"search {q}: {e}")
            top = sorted(found.values(), key=lambda p: p.engagement.views or 0, reverse=True)[:self.detail_top]
            if top:
                try:
                    for d in run_ytdlp(["-j", "--skip-download", *[p.url for p in top]], timeout=240):
                        p = ytdlp_video_post(d, ctx.now, "yt-dlp-video", found.get(d.get("id"), top[0]).community or "")
                        if p:
                            found[p.external_id] = p
                except (FetchError, subprocess.TimeoutExpired) as e:
                    errors.append(f"detail: {e}")
            posts.extend(found.values())
        if not posts and errors:
            raise FetchError("; ".join(errors)[:300])
        return posts, {"errors": errors[-6:]}


@register
class TikTokCollector(Collector):
    name = "tiktok"
    platform = "tiktok"
    method = "yt-dlp-tiktok-user"
    interval_minutes = 180
    optional = True
    description = "TikTok creator pages via yt-dlp (hashtag pages are broken upstream)"

    def available(self) -> tuple[bool, str]:
        return (True, "") if ytdlp_bin() else (False, "yt-dlp not installed")

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        posts, errors = [], []
        for handle in ctx.config.extra.get("tiktok_creators", []):
            try:
                for d in run_ytdlp(["--flat-playlist", "-j", "--playlist-end", "12",
                                    f"https://www.tiktok.com/@{handle}"]):
                    d.setdefault("extractor_key", "TikTok")
                    d.setdefault("webpage_url", f"https://www.tiktok.com/@{handle}/video/{d.get('id')}")
                    p = ytdlp_video_post(d, ctx.now, self.method, f"creator:{handle}")
                    if p:
                        p.author = p.author or handle
                        posts.append(p)
            except (FetchError, subprocess.TimeoutExpired) as e:
                errors.append(f"{handle}: {e}")
        if not posts and errors:
            raise FetchError("; ".join(errors)[:300], blocked=True)
        return posts, {"errors": errors}

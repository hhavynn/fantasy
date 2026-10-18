"""Collector parsers against real (trimmed) responses, HTTP retry/backoff, and fault isolation."""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import NOW, fixture_json, fixture_text

from social_intel.collectors import REGISTRY
from social_intel.collectors.base import CollectContext, Collector, SourceConfig, register, run_collectors
from social_intel.collectors.bluesky import bsky_post
from social_intel.collectors.news import parse_rss
from social_intel.collectors.reddit import RedditActivityCollector, RedditCollector, parse_reddit_atom
from social_intel.collectors.video import parse_channel_feed, ytdlp_video_post
from social_intel.collectors.x import fx_post
from social_intel.core.http import Fetcher, FetchError


def fetcher_for(routes: dict[str, tuple[int, str]], calls: list | None = None) -> Fetcher:
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(req.url))
        for prefix, (status, body) in routes.items():
            if str(req.url).startswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(404, text="")

    return Fetcher(transport=httpx.MockTransport(handler), sleep=lambda s: None, min_interval_s=0)


# ---------------------------------------------------------------- parsers

def test_reddit_atom_has_text_timestamps_and_no_fake_engagement():
    posts = parse_reddit_atom(fixture_text("reddit_hot.rss"), NOW)
    assert len(posts) == 6
    p = posts[0]
    assert p.platform == "reddit" and p.external_id and p.url.startswith("https://www.reddit.com/")
    assert p.created_at is not None and p.community.startswith("r/")
    assert p.engagement.likes is None and p.engagement.comments is None  # RSS has no scores: unknown, not zero


def test_arctic_activity_counts_fresh_comments_per_thread():
    comments, posts_ = fixture_json("arctic_comments.json"), fixture_json("arctic_posts.json")
    newest = max(c["created_utc"] for c in comments["data"])
    from datetime import datetime, timezone
    now = datetime.fromtimestamp(newest + 600, tz=timezone.utc)
    f = fetcher_for({"https://arctic-shift.photon-reddit.com/api/comments": (200, json.dumps(comments)),
                     "https://arctic-shift.photon-reddit.com/api/posts": (200, json.dumps(posts_))})
    col = RedditActivityCollector()
    col.activity_subs = ("fantasyfootball",)
    col.pages = 1
    posts, cur = col.collect(CollectContext(f, SourceConfig("nfl"), {}, now))
    active = [p for p in posts if p.extra.get("fresh_comments")]
    assert active, "every commented thread becomes a post with a measured comment rate"
    assert sum(p.extra["fresh_comments"] for p in active) == len(comments["data"])
    assert all(p.extra["comment_rate_per_h"] > 0 and p.extra["sample_comments"] for p in active)
    assert all(p.engagement.likes is None for p in active)  # archive scores are frozen: never trusted


def test_bluesky_post_fields():
    item = fixture_json("bsky_search.json")["posts"][0]
    p = bsky_post(item, NOW, "bsky-appview-search", "search:fantasy football")
    assert p.url.startswith("https://bsky.app/profile/") and "/post/" in p.url
    assert p.engagement.likes == item["likeCount"] and p.engagement.comments == item["replyCount"]
    assert p.created_at is not None


def test_fxtwitter_post_fields():
    s = fixture_json("fxtwitter_statuses.json")["results"][0]
    p = fx_post(s, NOW, "AdamSchefter")
    assert p.platform == "x" and p.url.startswith("https://") and p.created_at is not None
    assert p.engagement.views == s.get("views") and p.engagement.likes == s.get("likes")


def test_youtube_channel_feed_and_ytdlp_search():
    posts = parse_channel_feed(fixture_text("youtube_channel.xml"), NOW, "NFL")
    assert len(posts) == 4 and all(p.engagement.views is not None for p in posts)
    rows = [json.loads(line) for line in fixture_text("ytdlp_search.jsonl").splitlines()]
    v = ytdlp_video_post(rows[0], NOW, "yt-dlp-search", "search:q")
    assert v.platform == "youtube" and v.engagement.views == rows[0].get("view_count")
    assert v.engagement.likes is None  # flat search has no likes: stays unknown


def test_news_rss_google_strips_outlet_suffix():
    posts = parse_rss(fixture_text("google_news.xml"), NOW, "google-news-rss", "gnews:fantasy football")
    assert len(posts) == 6
    assert all(" - " not in p.title[-25:] or p.author for p in posts)
    espn = parse_rss(fixture_text("espn_fantasy.xml"), NOW, "outlet-rss", "ESPN")
    assert espn and all(p.platform == "news" and p.url for p in espn)


# ---------------------------------------------------------------- HTTP behaviour

def test_fetcher_retries_transient_then_succeeds():
    seq = iter([httpx.Response(500), httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(200, text="ok")])
    slept = []
    f = Fetcher(transport=httpx.MockTransport(lambda r: next(seq)), sleep=slept.append, min_interval_s=0)
    assert f.get("https://x.test/a").text == "ok"
    assert f.requests_made == 3 and 1.0 in slept  # honoured Retry-After


def test_fetcher_reports_block_after_repeated_403():
    f = Fetcher(transport=httpx.MockTransport(lambda r: httpx.Response(403)), sleep=lambda s: None, min_interval_s=0)
    with pytest.raises(FetchError) as e:
        f.get("https://x.test/a")
    assert e.value.blocked and e.value.status == 403
    assert f.requests_made == 2  # a second refusal is a block; no hammering


def test_fetcher_cache(tmp_path):
    calls = []
    f = fetcher_for({"https://x.test": (200, "body")}, calls)
    f.cache_dir = tmp_path
    f.get("https://x.test/a", cache_ttl_s=60)
    r = f.get("https://x.test/a", cache_ttl_s=60)
    assert r.from_cache and len(calls) == 1


def test_reddit_rss_rate_limited_raises_blocked():
    f = fetcher_for({"https://www.reddit.com": (429, "")})
    f.retries = 0
    with pytest.raises(FetchError) as e:
        RedditCollector().collect(CollectContext(f, SourceConfig("nfl", subreddits=["nfl"]), {}, NOW))
    assert e.value.blocked


# ---------------------------------------------------------------- isolation + incremental

@register
class _Boom(Collector):
    name = "_test_boom"
    optional = True

    def collect(self, ctx):
        raise ValueError("layout changed")


@register
class _Blocked(Collector):
    name = "_test_blocked"
    optional = True

    def collect(self, ctx):
        raise FetchError("403", status=403, blocked=True)


@register
class _Good(Collector):
    name = "_test_good"
    optional = True

    def collect(self, ctx):
        from conftest import make_post
        n = ctx.cursor.get("n", 0)
        return [make_post(f"g{n}", "Jaxson Dart breakout?"), make_post("same", "dup post")], {"n": n + 1}


def test_failed_collectors_do_not_stop_others_and_cool_down(store):
    f = fetcher_for({})
    res = run_collectors(store, f, SourceConfig("nfl"), ["_test_boom", "_test_blocked", "_test_good"], now=NOW)
    assert [r.status for r in res] == ["error", "blocked", "ok"]
    assert res[2].new_posts == 2
    st = store.source_state("_test_blocked")
    assert st["consecutive_failures"] == 1 and st["cooldown_until"] > NOW
    again = run_collectors(store, f, SourceConfig("nfl"), ["_test_blocked"], now=NOW)
    assert again[0].status == "skipped" and "cooling" in again[0].error


def test_incremental_cursor_and_dedup(store):
    f = fetcher_for({})
    run_collectors(store, f, SourceConfig("nfl"), ["_test_good"], now=NOW, force=True)
    res = run_collectors(store, f, SourceConfig("nfl"), ["_test_good"], now=NOW, force=True)
    assert store.get_cursor("_test_good") == {"n": 2}
    assert res[0].new_posts == 1  # "same" was already stored; only g1 is new


def test_refresh_interval_skips_fresh_source(store):
    f = fetcher_for({})
    run_collectors(store, f, SourceConfig("nfl"), ["_test_good"], now=NOW)
    res = run_collectors(store, f, SourceConfig("nfl"), ["_test_good"], now=NOW)
    assert res[0].status == "skipped" and "fresh" in res[0].error


def test_every_registered_collector_declares_metadata():
    for name, cls in REGISTRY.items():
        if name.startswith("_test"):
            continue
        assert cls.platform != "unknown" and cls.interval_minutes > 0 and cls.description

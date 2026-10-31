"""Collector interface and the fault-isolating runner.

A collector turns one public source into ``SocialPost`` rows. It knows nothing about players,
scoring or storage: the runner gives it a ``CollectContext`` (HTTP fetcher, its own cursor, the
sport's search config) and stores whatever it returns. Any exception is caught per collector and
recorded in ``source_state`` with an exponential cooldown, so a blocked X mirror never stops Reddit.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

from social_intel.contract import SocialPost, utcnow
from social_intel.core.http import Fetcher, FetchError
from social_intel.core.store import Store

log = logging.getLogger(__name__)


@dataclass
class SourceConfig:
    """What to look for, per sport. Collectors read only the keys they understand."""

    sport: str
    subreddits: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)  # free-text queries (Bluesky, YouTube, News)
    youtube_channels: dict[str, str] = field(default_factory=dict)  # name -> channel_id (RSS)
    bluesky_actors: list[str] = field(default_factory=list)  # handles of reporters/analysts
    x_accounts: list[str] = field(default_factory=list)
    tiktok_hashtags: list[str] = field(default_factory=list)
    instagram_accounts: list[str] = field(default_factory=list)
    news_feeds: dict[str, str] = field(default_factory=dict)  # name -> RSS URL
    trends_geo: str = "US"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectContext:
    fetcher: Fetcher
    config: SourceConfig
    cursor: dict[str, Any]
    now: datetime
    max_items: int = 200

    def new_cursor(self) -> dict[str, Any]:
        return dict(self.cursor)


class Collector:
    name: ClassVar[str] = "base"
    platform: ClassVar[str] = "unknown"
    method: ClassVar[str] = ""
    interval_minutes: ClassVar[int] = 60  # default refresh cadence
    optional: ClassVar[bool] = False  # optional connectors are off unless enabled
    sports: ClassVar[frozenset[str] | None] = None  # None = any sport
    description: ClassVar[str] = ""

    def collect(self, ctx: CollectContext) -> tuple[list[SocialPost], dict[str, Any]]:
        """Return (posts, new_cursor). Raise FetchError for source failures."""
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        """Cheap local check (binary installed, env var set) before any network call."""
        return True, ""


REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    REGISTRY[cls.name] = cls
    return cls


@dataclass
class RunResult:
    collector: str
    status: str  # ok | error | blocked | skipped
    posts: int = 0
    new_posts: int = 0
    requests: int = 0
    error: str | None = None


def run_collectors(
    store: Store,
    fetcher: Fetcher,
    config: SourceConfig,
    names: list[str],
    annotate: Callable[[SocialPost], None] | None = None,
    force: bool = False,
    now: datetime | None = None,
    max_items: int = 200,
) -> list[RunResult]:
    """Run each named collector in isolation. Respects cooldowns and refresh intervals unless
    ``force``. ``annotate`` (the sport adapter's entity/topic tagger) runs before storage."""
    now = now or utcnow()
    run_id = uuid.uuid4().hex[:12]
    results = []
    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            results.append(RunResult(name, "skipped", error="unknown collector"))
            continue
        if cls.sports is not None and config.sport not in cls.sports:
            results.append(RunResult(name, "skipped", error=f"not a {config.sport} source"))
            continue
        collector = cls()
        ok, why = collector.available()
        if not ok:
            results.append(RunResult(name, "skipped", error=why))
            continue
        state = store.source_state(name)
        if state and not force:
            if state["cooldown_until"] and state["cooldown_until"] > now:
                results.append(RunResult(name, "skipped", error=f"cooling down until {state['cooldown_until'].astimezone(timezone.utc):%H:%M}Z"))
                continue
            last = state["last_success"]
            if last and (now - last).total_seconds() < collector.interval_minutes * 60 * 0.9:
                results.append(RunResult(name, "skipped", error=f"fresh (interval {collector.interval_minutes} min)"))
                continue
        before = fetcher.requests_made
        started = utcnow()
        ctx = CollectContext(fetcher, config, store.get_cursor(name), now, max_items)
        try:
            posts, cursor = collector.collect(ctx)
            if annotate:
                for p in posts:
                    annotate(p)
            new = store.upsert_posts(posts, config.sport)
            store.set_cursor(name, cursor)
            res = RunResult(name, "ok", len(posts), new, fetcher.requests_made - before)
            store.record_run(run_id, name, started, "ok", len(posts), new, res.requests, None, False)
        except FetchError as e:
            status = "blocked" if e.blocked else "error"
            res = RunResult(name, status, requests=fetcher.requests_made - before, error=str(e)[:300])
            store.record_run(run_id, name, started, status, 0, 0, res.requests, res.error, e.blocked)
        except Exception as e:  # a parser broke on a layout change: isolate it, keep the trace
            log.debug("collector %s crashed\n%s", name, traceback.format_exc())
            res = RunResult(name, "error", requests=fetcher.requests_made - before,
                            error=f"{type(e).__name__}: {e}"[:300])
            store.record_run(run_id, name, started, "error", 0, 0, res.requests, res.error, False)
        results.append(res)
    return results

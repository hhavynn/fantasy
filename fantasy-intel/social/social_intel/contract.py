"""Versioned, sport-agnostic JSON contract for social trend intelligence.

Everything a collector produces, everything the ranker emits and every video idea handed to a
generator is one of the dataclasses below. They serialise to plain JSON (``to_dict``) and are
described by ``json_schema()`` so another repository (the soccer generator) can consume the same
files without importing this package. Sport-specific data lives only in the free-form
``enrichment`` / ``entities`` maps, keyed by the sport adapter that produced it.

Bump ``CONTRACT_VERSION`` when a field is removed or changes meaning; adding optional fields is a
minor change (readers must ignore unknown keys).
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CONTRACT_VERSION = "social-intel/1.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def post_key(platform: str, external_id: str) -> str:
    """Stable primary key for a post across collections."""
    return hashlib.sha1(f"{platform}:{external_id}".encode()).hexdigest()[:20]


@dataclass
class Engagement:
    """Public engagement counters at one moment. ``None`` means "not exposed by this source",
    never zero, so scoring can tell a missing metric from a flop."""

    likes: int | None = None  # upvotes / likes / hearts
    comments: int | None = None
    shares: int | None = None  # reposts / retweets / quote posts
    views: int | None = None
    upvote_ratio: float | None = None  # Reddit only; a low ratio is a debate signal

    def total(self) -> int:
        """Weighted interaction count used for velocity (comments weigh most: they are debate)."""
        return int((self.likes or 0) + 3 * (self.comments or 0) + 2 * (self.shares or 0) + 0.01 * (self.views or 0))


@dataclass
class SocialPost:
    platform: str  # reddit | bluesky | youtube | x | tiktok | instagram | google_trends | news | sleeper | wikipedia
    external_id: str
    url: str
    created_at: datetime | None
    collected_at: datetime
    text: str  # title + body / caption; the analysable discussion text
    author: str | None = None  # public handle only; no other personal data is stored
    community: str | None = None  # subreddit, channel, feed, hashtag, search query
    title: str | None = None
    hashtags: list[str] = field(default_factory=list)
    engagement: Engagement = field(default_factory=Engagement)
    collection_method: str = ""  # e.g. "reddit-public-json", "bsky-appview-search"
    confidence: float = 1.0  # how much we trust the fields (0-1); scraped mirrors < official JSON
    lang: str | None = None
    parent_id: str | None = None  # comment -> post, quote/repost -> original
    is_repost: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific raw fields worth keeping
    # filled by the sport adapter, not the collector
    entities: dict[str, list[str]] = field(default_factory=dict)  # {"players": [...ids], "teams": [...]}
    topics: list[str] = field(default_factory=list)  # e.g. ["waivers", "injury", "trade"]

    @property
    def key(self) -> str:
        return post_key(self.platform, self.external_id)

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.created_at is None:
            return None
        now = now or utcnow()
        return max(0.0, (now - self.created_at).total_seconds() / 3600)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["key"] = self.key
        d["created_at"] = iso(self.created_at)
        d["collected_at"] = iso(self.collected_at)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SocialPost":
        d = {k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}}
        d["engagement"] = Engagement(**(d.get("engagement") or {}))
        d["created_at"] = parse_iso(d.get("created_at"))
        d["collected_at"] = parse_iso(d.get("collected_at")) or utcnow()
        return cls(**d)


@dataclass
class ScoreComponent:
    name: str
    value: float  # 0-1
    weight: float
    detail: str  # human-readable reason, e.g. "+410 interactions/h, 6.2x r/nfl median"


@dataclass
class TrendScore:
    total: float  # 0-100
    components: list[ScoreComponent]
    flags: list[str]  # stale, rumor, sarcasm, suspicious_engagement, recycled, already_covered
    explanation: str


@dataclass
class TrendCluster:
    cluster_id: str
    label: str  # short human label, e.g. "Jaxson Dart breakout debate"
    sport: str
    post_keys: list[str]
    platforms: list[str]
    independent_sources: int  # distinct authors/communities after repost collapse
    first_seen: datetime | None
    last_seen: datetime | None
    entities: dict[str, list[str]]
    topics: list[str]
    keywords: list[str]
    score: TrendScore | None = None
    stance: dict[str, Any] = field(default_factory=dict)  # {"claims": [...], "pro": n, "con": n}
    enrichment: dict[str, Any] = field(default_factory=dict)  # sport adapter analytics, every number sourced
    top_posts: list[dict[str, Any]] = field(default_factory=list)  # url, platform, text, engagement

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["first_seen"] = iso(self.first_seen)
        d["last_seen"] = iso(self.last_seen)
        return d


@dataclass
class SourcedStat:
    """One number an idea may say out loud, with where it came from."""

    label: str
    value: Any
    source: str  # table/query or URL
    as_of: str | None = None  # point-in-time: the data cut the number was computed from


@dataclass
class VideoIdea:
    idea_id: str
    cluster_id: str
    sport: str
    format: str  # contrarian_breakdown | opinion_vs_numbers | buy_low_sell_high | player_vs_player | ...
    working_title: str
    hooks: list[str]  # three distinct openings, each speakable in < 3 s
    why_trending: str
    source_links: list[str]
    key_stats: list[SourcedStat]
    supporting_args: list[str]
    opposing_args: list[str]
    editorial_angle: str
    entities: dict[str, list[str]]
    duration_s: int
    graphics: list[str]
    memes: list[str]
    engagement_question: str
    trend_score: float
    research_confidence: float  # 0-1: share of the angle backed by our own sourced numbers
    relevance_window: str  # e.g. "until Sunday 1pm ET kickoff (2026-10-19)"
    relevance_expires: datetime | None = None
    notes: list[str] = field(default_factory=list)
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["relevance_expires"] = iso(self.relevance_expires)
        return d


def json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for the export bundle written by ``trends export``."""
    s, n, i, b = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "boolean"}
    ns = {"type": ["string", "null"]}
    ni = {"type": ["integer", "null"]}
    strs = {"type": "array", "items": s}
    ent = {"type": "object", "additionalProperties": strs}
    engagement = {
        "type": "object",
        "properties": {"likes": ni, "comments": ni, "shares": ni, "views": ni,
                       "upvote_ratio": {"type": ["number", "null"]}},
    }
    post = {
        "type": "object",
        "required": ["key", "platform", "external_id", "url", "collected_at", "text", "collection_method"],
        "properties": {
            "key": s, "platform": s, "external_id": s, "url": s, "created_at": ns, "collected_at": s,
            "text": s, "author": ns, "community": ns, "title": ns, "hashtags": strs, "engagement": engagement,
            "collection_method": s, "confidence": n, "lang": ns, "parent_id": ns, "is_repost": b,
            "extra": {"type": "object"}, "entities": ent, "topics": strs,
        },
    }
    component = {"type": "object", "required": ["name", "value", "weight", "detail"],
                 "properties": {"name": s, "value": n, "weight": n, "detail": s}}
    score = {"type": "object", "required": ["total", "components", "flags", "explanation"],
             "properties": {"total": n, "components": {"type": "array", "items": component},
                            "flags": strs, "explanation": s}}
    cluster = {
        "type": "object",
        "required": ["cluster_id", "label", "sport", "post_keys", "platforms", "score"],
        "properties": {
            "cluster_id": s, "label": s, "sport": s, "post_keys": strs, "platforms": strs,
            "independent_sources": i, "first_seen": ns, "last_seen": ns, "entities": ent, "topics": strs,
            "keywords": strs, "score": score, "stance": {"type": "object"}, "enrichment": {"type": "object"},
            "top_posts": {"type": "array", "items": {"type": "object"}},
        },
    }
    stat = {"type": "object", "required": ["label", "value", "source"],
            "properties": {"label": s, "value": {}, "source": s, "as_of": ns}}
    idea = {
        "type": "object",
        "required": ["idea_id", "cluster_id", "sport", "format", "working_title", "hooks", "source_links",
                     "key_stats", "editorial_angle", "trend_score", "research_confidence", "contract_version"],
        "properties": {
            "idea_id": s, "cluster_id": s, "sport": s, "format": s, "working_title": s,
            "hooks": {"type": "array", "items": s, "minItems": 3}, "why_trending": s, "source_links": strs,
            "key_stats": {"type": "array", "items": stat}, "supporting_args": strs, "opposing_args": strs,
            "editorial_angle": s, "entities": ent, "duration_s": i, "graphics": strs, "memes": strs,
            "engagement_question": s, "trend_score": n, "research_confidence": n, "relevance_window": s,
            "relevance_expires": ns, "notes": strs, "contract_version": s,
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://fantasy-intel.local/schemas/{CONTRACT_VERSION}.json",
        "title": "Social trend intelligence bundle",
        "type": "object",
        "required": ["contract_version", "sport", "generated_at", "clusters", "ideas"],
        "properties": {
            "contract_version": {"const": CONTRACT_VERSION},
            "sport": s,
            "generated_at": s,
            "data_label": {"enum": ["live", "fixture"]},
            "source_health": {"type": "array", "items": {"type": "object"}},
            "clusters": {"type": "array", "items": cluster},
            "ideas": {"type": "array", "items": idea},
            "posts": {"type": "array", "items": post},
        },
        "$defs": {"post": post, "cluster": cluster, "idea": idea, "score": score, "stat": stat},
    }

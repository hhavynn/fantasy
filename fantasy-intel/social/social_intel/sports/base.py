"""Sport adapter interface: everything sport-specific the generic engine needs.

The engine (collectors, storage, clustering, scoring, idea assembly) is sport-agnostic. A sport
plugs in by implementing ``SportAdapter``: what to collect, how to recognise entities and topics in
text, how popular an entity is, how to enrich a cluster with its own statistics, and which video
formats/meme references fit. Football lives in ``sports/football``; ``sports/soccer`` is the stub
the soccer generator will fill in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from social_intel.collectors.base import SourceConfig
from social_intel.contract import SocialPost, TrendCluster


@dataclass
class Entity:
    entity_id: str
    kind: str  # player | team | club | competition
    name: str
    team: str | None = None
    position: str | None = None
    popularity: float = 0.0  # 0-1 audience-size proxy
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventWindow:
    """When the sport's next decision point is (football: Sunday kickoff/waivers), so ideas carry
    a relevance window and stale chatter can be recognised."""

    label: str
    expires: datetime | None


class SportAdapter(Protocol):
    sport: str

    def source_config(self) -> SourceConfig: ...

    def annotate(self, post: SocialPost) -> None:
        """Fill ``post.entities`` and ``post.topics`` in place."""

    def entity(self, entity_id: str) -> Entity | None: ...

    def is_relevant(self, post: SocialPost) -> bool:
        """Drop off-topic posts (general news, other sports) before clustering."""

    def topic_weights(self) -> dict[str, float]:
        """How much each topic matters to this audience (fantasy relevance for football)."""

    def enrich(self, cluster: TrendCluster) -> dict[str, Any]:
        """Analytics for the cluster's entities. Every number carries a source and an as-of."""

    def event_window(self, now: datetime) -> EventWindow: ...

    def meme_bank(self) -> dict[str, list[str]]:
        """Recognisable references per angle, e.g. {"overreaction": [...]}."""

    # Optional members. Without them the engine uses the football defaults.
    #   enrich_teams: bool -- also enrich clusters that name teams but no players
    #   choose_formats(cluster) -> list[str] -- the video formats that fit a ranked cluster
    #   build_idea(cluster, fmt, rotation, window, meme_bank, now) -> VideoIdea
    #   format_labels() -> dict[str, str] -- display names for this sport's formats

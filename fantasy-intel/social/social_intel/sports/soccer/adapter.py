"""Soccer adapter: the interface the soccer generator will implement.

Everything sport-agnostic (collectors, storage, clustering, scoring, idea assembly, the JSON
contract) is shared. A working soccer adapter needs, mirroring ``sports/football``:

* ``source_config`` -- r/soccer, r/PremierLeague, club subreddits, football-journalist accounts on X
  (via FxTwitter) and Bluesky, soccer YouTube channels, transfer-news RSS.
* entity recognition -- clubs (with nicknames: "Spurs", "the Gunners"), players, competitions.
* ``enrich`` -- the soccer repository's own statistics (xG, minutes, form), keyed by its player ids,
  with the same ``{"label", "value", "source", "as_of"}`` stat shape and point-in-time rule.
* topic lexicon -- transfers, tactics, injuries, form, refereeing, title race, relegation.
* ``event_window`` -- the next matchday kickoff; ``meme_bank`` -- soccer-specific references.

Until then this stub collects and ranks generic discussion with no entities or analytics, so the
shared pipeline can be exercised end to end: ``trends --sport soccer run --only reddit``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from social_intel.collectors.base import SourceConfig
from social_intel.contract import SocialPost, TrendCluster
from social_intel.sports.base import Entity, EventWindow

TOPICS = {
    "transfer": r"\btransfer|\bsign(ed|ing)?\b|\bfee\b|\bhere we go\b|\bloan\b|\bbid\b",
    "injury": r"\binjur|\bhamstring|\bacl\b|\bruled out\b",
    "tactics": r"\bformation|\bpress(ing)?\b|\btactic|\bxg\b",
    "form": r"\bform\b|\bunbeaten|\bwinless|\bstreak",
    "refereeing": r"\bvar\b|\breferee|\bpenalty|\bred card",
}
_RX = {k: re.compile(v, re.I) for k, v in TOPICS.items()}


class SoccerAdapter:
    sport = "soccer"
    direction_topics = ({"form"}, {"injury"})

    def source_config(self) -> SourceConfig:
        return SourceConfig(sport=self.sport, subreddits=["soccer", "PremierLeague"],
                            search_terms=["premier league", "transfer news"])

    def annotate(self, post: SocialPost) -> None:
        post.entities = {}
        post.topics = self.tag_topics(post.text)

    def tag_topics(self, text: str) -> list[str]:
        return [k for k, rx in _RX.items() if rx.search(text)]

    def entity(self, entity_id: str) -> Entity | None:
        return None

    def name_tokens(self, entity_id: str) -> set[str]:
        return set()

    def entity_popularity(self) -> dict[str, float]:
        return {}

    def is_relevant(self, post: SocialPost) -> bool:
        return True

    def topic_weights(self) -> dict[str, float]:
        return {"transfer": 1.0, "injury": 0.8, "form": 0.7, "tactics": 0.6, "refereeing": 0.6}

    def label(self, c: TrendCluster) -> str:
        return ", ".join(c.keywords[:3]) or "soccer chatter"

    def enrich(self, cluster: TrendCluster) -> dict[str, Any]:
        return {"analytic_coverage": 0.0, "analytic_note": "soccer analytics not connected yet", "players": []}

    def event_window(self, now: datetime) -> EventWindow:
        days = (5 - now.weekday()) % 7 or 7
        return EventWindow("next Saturday matchday", (now + timedelta(days=days)).replace(tzinfo=timezone.utc))

    def meme_bank(self) -> dict[str, list[str]]:
        return {"sfx": ["vine boom", "record scratch"]}

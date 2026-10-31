"""Soccer sport adapter: the top five European leagues and MLS.

Sources and entity recognition live here; club and player lists are in ``clubs.py`` and the cache from
``sync_entities`` (run ``trends --sport soccer sync-entities``). Statistics come from ``stats.py``
(``trends --sport soccer sync-stats``); without them ``enrich`` reports no coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from social_intel.collectors.base import SourceConfig
from social_intel.contract import SocialPost, TrendCluster
from social_intel.sports.base import Entity, EventWindow
from social_intel.sports.soccer import ideas
from social_intel.sports.soccer.clubs import CLUB_BY_ID, CLUB_SUBREDDITS, CLUBS
from social_intel.sports.soccer.entities import SoccerIndex
from social_intel.sports.soccer.stats import SoccerEnricher
from social_intel.sports.soccer.topics import TOPIC_WEIGHTS, tag_topics

SOCCER_SUBREDDITS = ["soccer", "PremierLeague", "football", "MLS"]
TRUSTED_COMMUNITIES = {f"r/{s.lower()}" for s in SOCCER_SUBREDDITS} | {f"r/{s}" for s in CLUB_SUBREDDITS}

MEME_BANK = {
    "parody": ["Trophy-heist movie trailer", "Courtroom drama over one decision", "Nature documentary narrator",
               "Press conference that spirals"],
    "parallel_universe": ["Spider-Man pointing (two timelines)", "Butterfly effect countdown"],
    "explainer": ["Galaxy-brain escalation", "Teacher at the whiteboard"],
    "sfx": ["vine boom on the reveal", "record scratch before the twist", "sad trombone for the collapse",
            "crowd 'ooooh' on the punchline"],
}


class SoccerAdapter:
    sport = "soccer"
    direction_topics = ({"rise"}, {"crisis"})
    enrich_teams = True
    studio_command = "uv run make-soccer --package {package}"
    choose_formats = staticmethod(ideas.choose_formats)
    build_idea = staticmethod(ideas.build_idea)

    def __init__(self, entity_cache: Path | None = None, now: datetime | None = None, index: SoccerIndex | None = None,
                 stats_db: Path | None = None):
        self.entity_cache = entity_cache
        self.stats_db = stats_db or Path("/nonexistent/soccer.duckdb")
        self.now = now or datetime.now(timezone.utc)
        self.season = self.now.year if self.now.month >= 7 else self.now.year - 1
        self._index = index

    @property
    def index(self) -> SoccerIndex:
        if self._index is None:
            has_cache = self.entity_cache and self.entity_cache.exists()
            self._index = SoccerIndex.load(self.entity_cache) if has_cache else SoccerIndex()
        return self._index

    def format_labels(self) -> dict[str, str]:
        return ideas.FORMAT_LABEL

    # ------------------------------------------------------------------ sources

    def source_config(self) -> SourceConfig:
        return SourceConfig(
            sport=self.sport,
            subreddits=SOCCER_SUBREDDITS,
            search_terms=["premier league", "champions league", "transfer news", "la liga", "ballon d'or", "mls soccer"],
            youtube_channels={
                "UEFA": "UCyGa1YEx9ST66rYrJTGIKOw", "Premier League": "UCG5qGWdu8nIRZqJ_GgDwQ-w",
                "Sky Sports Football": "UCZ7wY7MRDSygp63HIEfdQZA", "ESPN FC": "UC6c1z7bA__85CIWZ_jpCK-Q",
                "CBS Sports Golazo": "UCET00YnetHT7tOpu12v8jxg", "The Athletic FC": "UC6ZMmQaL9wZYo4iLw8n8xiA",
                "LaLiga": "UCTv-XvfzLX3i4IGWAm4sbmA", "OneFootball": "UC2-0sEOYbQFuaURd_AU6Krg",
                "COPA90": "UCEPKlFeUjRY_1mpZP8GnkVg", "MLS": "UCSZbXT5TLLW_i-5W8FZpFsg",
            },
            x_accounts=["FabrizioRomano", "David_Ornstein", "Plettigoal", "DiMarzio", "MatteMoretto", "SamLee",
                        "JPercyTelegraph", "Santi_J_FM", "GuillemBalague", "MarioCortegana", "tombogert", "PaulTenorio"],
            tiktok_hashtags=["football", "soccer"],
            news_feeds={
                "BBC Football": "https://feeds.bbci.co.uk/sport/football/rss.xml",
                "Guardian Football": "https://www.theguardian.com/football/rss",
                "Sky Sports Football": "https://www.skysports.com/rss/12040",
                "ESPN Soccer": "https://www.espn.com/espn/rss/soccer/news",
                "90min": "https://www.90min.com/posts.rss",
                "talkSPORT": "https://talksport.com/football/feed/",
            },
            extra={"team_subreddits": [c.subreddit for c in CLUBS if c.subreddit],
                   "activity_subreddits": ["soccer", "PremierLeague", "MLS"],
                   "google_news_queries": ["transfer news", "premier league", "champions league", "MLS transfer"]},
        )

    def dynamic_config(self, cfg: SourceConfig, store) -> None:
        idx = self.index
        top = sorted(idx.players.values(), key=lambda e: e.popularity, reverse=True)[:60]
        cfg.extra["wiki_players"] = [{"name": e.name, "entity_id": e.entity_id, "disambiguator": "footballer"} for e in top]
        hot: list[str] = []
        for c in store.latest_ranking(self.sport)[:8]:
            for pid in (c.get("entities", {}).get("players") or [])[:1]:
                if (e := idx.players.get(pid)) and e.name not in hot:
                    hot.append(e.name)
        cfg.extra["hot_player_queries"] = [f'"{n}" football' for n in hot[:6]]
        cfg.extra["youtube_queries"] = ["football parody", "football what if", "football explained"] + \
                                       [f"{n} football" for n in hot[:2]]

    # ------------------------------------------------------------------ annotation

    def annotate(self, post: SocialPost) -> None:
        text = post.text if not post.title or post.title in post.text else f"{post.title}\n{post.text}"
        community = (post.community or "").lower()
        clubs = self.index.find_clubs(text, trusted=community in TRUSTED_COMMUNITIES)
        players = self.index.find_players(text, clubs)
        wiki_id = post.extra.get("entity_id")
        if wiki_id and wiki_id not in players:
            players.insert(0, wiki_id)
        for pid in players:
            e = self.index.players.get(pid)
            if e and e.team and e.team not in clubs:
                clubs.append(e.team)
        sub_club = CLUB_SUBREDDITS.get(community.removeprefix("r/"))
        if sub_club and sub_club not in clubs:
            clubs.append(sub_club)
        post.entities = {"players": players, "teams": clubs}
        post.topics = tag_topics(text)

    def tag_topics(self, text: str) -> list[str]:
        return tag_topics(text)

    def is_relevant(self, post: SocialPost) -> bool:
        if post.entities.get("players") or post.entities.get("teams"):
            return True
        return (post.community or "").lower() in TRUSTED_COMMUNITIES

    def topic_weights(self) -> dict[str, float]:
        return TOPIC_WEIGHTS

    # ------------------------------------------------------------------ entities

    def entity(self, entity_id: str) -> Entity | None:
        return self.index.entity(entity_id)

    def name_tokens(self, entity_id: str) -> set[str]:
        return self.index.name_tokens(entity_id)

    def entity_popularity(self) -> dict[str, float]:
        return {k: e.popularity for k, e in self.index.players.items()}

    def label(self, c: TrendCluster) -> str:
        names = [e.name for pid in (c.entities.get("players") or [])[:2] if (e := self.index.players.get(pid))]
        if not names:
            names = [CLUB_BY_ID[t].name for t in (c.entities.get("teams") or [])[:2] if t in CLUB_BY_ID]
        topic = (c.topics[0] if c.topics else "chatter").replace("_", " ")
        return f"{' & '.join(names) or ', '.join(c.keywords[:3]) or 'soccer'}: {topic}"

    def enricher(self, now: datetime | None = None) -> SoccerEnricher:
        return SoccerEnricher(self.stats_db, self.index, now or self.now)

    def enrich(self, cluster: TrendCluster) -> dict[str, Any]:
        return self.enricher().enrich(cluster.entities.get("players") or [], cluster.entities.get("teams") or [])

    # ------------------------------------------------------------------ calendar

    def event_window(self, now: datetime) -> EventWindow:
        kick = self.enricher(now).next_kickoff()
        if kick:
            return EventWindow(f"next {kick[0]} kickoff {kick[1]:%a %b %d, %H:%M} UK", kick[1])
        days = (5 - now.weekday()) % 7 or 7
        return EventWindow("next Saturday matchday", (now + timedelta(days=days)).replace(tzinfo=timezone.utc))

    def meme_bank(self) -> dict[str, list[str]]:
        return MEME_BANK

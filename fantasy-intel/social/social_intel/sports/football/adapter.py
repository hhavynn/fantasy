"""NFL / fantasy-football sport adapter."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from social_intel.collectors.base import SourceConfig
from social_intel.contract import SocialPost, TrendCluster
from social_intel.sports.base import Entity, EventWindow
from social_intel.sports.football.entities import TEAMS, EntityIndex
from social_intel.sports.football.topics import NFL_CONTEXT, TOPIC_WEIGHTS, tag_topics

FANTASY_INTEL = Path(__file__).resolve().parents[4]  # .../fantasy-intel
DEFAULT_DB = Path(os.getenv("FANTASY_DB_PATH") or FANTASY_INTEL / "data" / "fantasy.duckdb")
ET = ZoneInfo("America/New_York")

TEAM_SUBREDDITS = {
    "ARI": "AZCardinals", "ATL": "falcons", "BAL": "ravens", "BUF": "buffalobills", "CAR": "panthers",
    "CHI": "CHIBears", "CIN": "bengals", "CLE": "Browns", "DAL": "cowboys", "DEN": "DenverBroncos",
    "DET": "detroitlions", "GB": "GreenBayPackers", "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars",
    "KC": "KansasCityChiefs", "LV": "raiders", "LAC": "Chargers", "LA": "LosAngelesRams", "MIA": "miamidolphins",
    "MIN": "minnesotavikings", "NE": "Patriots", "NO": "Saints", "NYG": "NYGiants", "NYJ": "nyjets",
    "PHI": "eagles", "PIT": "steelers", "SF": "49ers", "SEA": "Seahawks", "TB": "buccaneers",
    "TEN": "Tennesseetitans", "WAS": "Commanders",
}

MEME_BANK = {
    "overreaction": ["'This is fine' dog in the burning room", "Panik / Kalm / Panik", "Fantasy manager hitting the drop button at 1:05 pm",
                     "'Is this a pigeon?' (is this a league winner?)"],
    "breakout": ["Galaxy-brain escalation", "'Who is this guy?' Steph/Jordan side-eye", "Waiver-wire Avengers assemble",
                 "The 'I've been waiting for this' Kermit"],
    "bust": ["'It's over' wojak", "Michael Scott 'NO GOD PLEASE NO'", "Sad Affleck smoking", "Benching him one week too late"],
    "contrarian": ["Drake no/yes (vibes vs. numbers)", "'Well yes, but actually no'", "Distracted boyfriend (managers vs. the data)",
                   "Change My Mind table"],
    "injury": ["Hospital-bed 'Am I a joke to you?'", "Backup RB 'my time has come'", "Domino chain (injury ripple)"],
    "trade": ["'Trade offer: I receive / You receive'", "Pikachu shocked face (veto)", "Robbery in progress siren"],
    "matchup": ["Shadow corner as the final boss", "'You shall not pass' Gandalf", "Shootout cowboy stand-off"],
    "sfx": ["vine boom on the reveal number", "record scratch before the twist", "airhorn on the verdict",
            "sad trombone for the bust case", "crowd 'ooooh' on the contrarian stat"],
}


class FootballAdapter:
    sport = "nfl"
    direction_topics = ({"breakout", "buy_low"}, {"bust", "sell_high"})

    def __init__(self, db_path: Path | None = None, season: int | None = None, entity_cache: Path | None = None,
                 now: datetime | None = None, index: EntityIndex | None = None):
        self.db_path = Path(db_path or DEFAULT_DB)
        self.now = now or datetime.now(timezone.utc)
        self.season = season or (self.now.year if self.now.month >= 3 else self.now.year - 1)
        self.entity_cache = entity_cache
        self._index = index
        self._enricher = None

    # ------------------------------------------------------------------ entities

    @property
    def index(self) -> EntityIndex:
        if self._index is None:
            self._index = self._load_index()
        return self._index

    def _load_index(self) -> EntityIndex:
        cache = self.entity_cache
        if cache and cache.exists() and (not self.db_path.exists() or cache.stat().st_mtime >= self.db_path.stat().st_mtime):
            return EntityIndex.load(cache)
        if self.db_path.exists():
            import duckdb
            con = duckdb.connect(str(self.db_path), read_only=True)
            try:
                idx = EntityIndex.from_db(con, self.season)
            finally:
                con.close()
            if cache:
                idx.dump(cache)
            return idx
        if cache and cache.exists():
            return EntityIndex.load(cache)
        return EntityIndex([])

    def entity(self, entity_id: str) -> Entity | None:
        return self.index.entity(entity_id)

    def name_tokens(self, entity_id: str) -> set[str]:
        return self.index.name_tokens(entity_id)

    def entity_popularity(self) -> dict[str, float]:
        return {k: e.popularity for k, e in self.index.players.items()}

    # ------------------------------------------------------------------ annotation

    def annotate(self, post: SocialPost) -> None:
        text = post.text if not post.title or post.title in post.text else f"{post.title}\n{post.text}"
        sleeper_gsis = post.extra.get("gsis_id")
        teams = self.index.find_teams(text)
        players = self.index.find_players(text, teams)
        if sleeper_gsis and sleeper_gsis not in players:
            players.insert(0, sleeper_gsis)
        for pid in players:  # a player's team counts as mentioned
            t = self.index.players.get(pid)
            if t and t.team and t.team not in teams:
                teams.append(t.team)
        sub_team = next((abbr for abbr, sub in TEAM_SUBREDDITS.items()
                         if post.community and post.community.lower() == f"r/{sub.lower()}"), None)
        if sub_team and sub_team not in teams:
            teams.append(sub_team)
        post.entities = {"players": players, "teams": teams}
        post.topics = tag_topics(text)

    def is_relevant(self, post: SocialPost) -> bool:
        if post.entities.get("players"):
            return True
        if post.platform in ("reddit",) and post.community and post.community.lower() in {
                "r/fantasyfootball", "r/dynastyff", "r/fantasyfootballers", "r/nfl"}:
            return True
        return bool(post.entities.get("teams")) and bool(NFL_CONTEXT.search(post.text))

    def topic_weights(self) -> dict[str, float]:
        return TOPIC_WEIGHTS

    def tag_topics(self, text: str) -> list[str]:
        return tag_topics(text)

    def label(self, c: TrendCluster) -> str:
        names = [e.name for pid in (c.entities.get("players") or [])[:2] if (e := self.index.players.get(pid))]
        if not names:
            names = [TEAMS[t][0] for t in (c.entities.get("teams") or [])[:2] if t in TEAMS]
        topic = (c.topics[0] if c.topics else "chatter").replace("_", " ")
        return f"{' & '.join(names) or ', '.join(c.keywords[:3])}: {topic}"

    # ------------------------------------------------------------------ analytics

    def enricher(self):
        if self._enricher is None:
            from social_intel.sports.football.enrich import FootballEnricher
            self._enricher = FootballEnricher(self.db_path, self.season, self.now)
        return self._enricher

    def enrich(self, cluster: TrendCluster) -> dict[str, Any]:
        return self.enricher().enrich(cluster.entities.get("players") or [], cluster.topics, cluster.stance)

    # ------------------------------------------------------------------ calendar

    def event_window(self, now: datetime) -> EventWindow:
        """Next Sunday 1 pm ET kickoff: start/sit and waiver content is dead after it."""
        local = now.astimezone(ET)
        days = (6 - local.weekday()) % 7
        kickoff = (local + timedelta(days=days)).replace(hour=13, minute=0, second=0, microsecond=0)
        if kickoff <= local:
            kickoff += timedelta(days=7)
        return EventWindow(f"Sunday kickoff {kickoff:%a %b %d, 1:00 pm ET}", kickoff.astimezone(timezone.utc))

    def meme_bank(self) -> dict[str, list[str]]:
        return MEME_BANK

    # ------------------------------------------------------------------ sources

    def source_config(self) -> SourceConfig:
        return SourceConfig(
            sport=self.sport,
            subreddits=["fantasyfootball", "nfl", "DynastyFF", "fantasyfootballers"],
            search_terms=["fantasy football", "waiver wire", "start sit", "fantasy football trade", "nfl injury",
                          "buy low fantasy", "sell high fantasy", "breakout fantasy football"],
            youtube_channels={
                "FantasyPros": "UCWl7MtoOqhHl0Ok3UvTsyIw",
                "The Fantasy Footballers": "UCIy4pLBhRxQ7h3yjhxfQ4zA",
                "Fantasy Football Today": "UCKWKVi31JR4CyaTb9kbKhEA",
                "NFL": "UCDVYQ4Zhbm3S2dlz7P1GBDg",
                "ESPN Fantasy Sports": "UCiWLfSweyRNmLpgEHekhoAg",
            },
            bluesky_actors=[],
            x_accounts=["AdamSchefter", "RapSheet", "TomPelissero", "MatthewBerryTMR", "FantasyLabsNFL",
                        "JJZachariason", "LateRoundQB", "FantasyPros", "Underdog__NFL"],
            tiktok_hashtags=["fantasyfootball", "nfl", "fantasyfootballadvice"],
            instagram_accounts=["fantasypros", "nfl", "fantasyfootballers"],
            news_feeds={
                "ESPN NFL": "https://www.espn.com/espn/rss/nfl/news",
                "CBS NFL": "https://www.cbssports.com/rss/headlines/nfl/",
                "Yahoo NFL": "https://sports.yahoo.com/nfl/rss/",
                "ProFootballTalk": "https://profootballtalk.nbcsports.com/feed/",
            },
            extra={"team_subreddits": list(TEAM_SUBREDDITS.values()),
                   "google_news_queries": ["fantasy football", "NFL injury", "NFL trade", "waiver wire"]},
        )

    def dynamic_config(self, cfg: SourceConfig, store) -> None:
        """Per-run additions: id maps for attention sources and queries for the currently hot players."""
        idx = self.index
        cfg.extra["sleeper_to_gsis"] = {str(e.meta["sleeper_id"]): pid for pid, e in idx.players.items()
                                        if e.meta.get("sleeper_id")}
        cfg.extra["sleeper_names"] = {str(e.meta["sleeper_id"]): e.name for e in idx.players.values()
                                      if e.meta.get("sleeper_id")}
        top = sorted(idx.players.values(), key=lambda e: e.popularity, reverse=True)[:60]
        cfg.extra["wiki_players"] = [{"name": e.name, "gsis_id": e.entity_id, "position": e.position} for e in top]
        hot: list[str] = []
        for c in store.latest_ranking(self.sport)[:8]:
            for pid in (c.get("entities", {}).get("players") or [])[:1]:
                if (e := idx.players.get(pid)) and e.name not in hot:
                    hot.append(e.name)
        cfg.extra["hot_player_queries"] = [f'"{n}" fantasy' for n in hot[:6]]
        cfg.extra["youtube_queries"] = ["fantasy football week " + str(self.enricher().upcoming_week()),
                                        "fantasy football waiver wire", "nfl hot take"] + [f"{n} fantasy" for n in hot[:2]]

    def player_search_terms(self, limit: int = 15) -> list[str]:
        """Most popular players: queries for search-driven sources (Wikipedia, YouTube)."""
        top = sorted(self.index.players.values(), key=lambda e: e.popularity, reverse=True)[:limit]
        return [e.name for e in top]


def wiki_title(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip())

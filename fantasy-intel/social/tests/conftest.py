from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from social_intel.contract import Engagement, SocialPost
from social_intel.core.store import Store
from social_intel.sports.football.adapter import FootballAdapter
from social_intel.sports.football.entities import EntityIndex

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)

# Synthetic roster for entity tests (names are real players; popularity values are made up).
PLAYERS = [
    {"id": "00-0040001", "name": "Jaxson Dart", "team": "NYG", "position": "QB", "popularity": 0.7, "sleeper_id": "12508"},
    {"id": "00-0040002", "name": "Cam Skattebo", "team": "NYG", "position": "RB", "popularity": 0.8},
    {"id": "00-0040003", "name": "Josh Allen", "team": "BUF", "position": "QB", "popularity": 0.99},
    {"id": "00-0040004", "name": "Josh Allen", "team": "JAX", "position": "LB", "popularity": 0.0},
    {"id": "00-0040005", "name": "Keenan Allen", "team": "LAC", "position": "WR", "popularity": 0.6},
    {"id": "00-0040006", "name": "Braelon Allen", "team": "NYJ", "position": "RB", "popularity": 0.5},
    {"id": "00-0040007", "name": "Christian McCaffrey", "team": "SF", "position": "RB", "popularity": 0.99},
    {"id": "00-0040008", "name": "Amon-Ra St. Brown", "team": "DET", "position": "WR", "popularity": 0.99},
    {"id": "00-0040009", "name": "Jordan Love", "team": "GB", "position": "QB", "popularity": 0.6},
    {"id": "00-0040010", "name": "Malik Nabers", "team": "NYG", "position": "WR", "popularity": 0.95},
    {"id": "00-0040011", "name": "Ja'Marr Chase", "team": "CIN", "position": "WR", "popularity": 0.99},
    {"id": "00-0040012", "name": "Travis Kelce", "team": "KC", "position": "TE", "popularity": 0.9},
]


@pytest.fixture
def index() -> EntityIndex:
    return EntityIndex(PLAYERS, 2026, 6)


@pytest.fixture
def adapter(index, tmp_path) -> FootballAdapter:
    return FootballAdapter(db_path=tmp_path / "missing.duckdb", season=2026, index=index, now=NOW)


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "social.duckdb")
    yield s
    s.close()


def make_post(pid: str, text: str, platform: str = "reddit", hours_ago: float = 2, likes=None, comments=None,
              shares=None, views=None, author=None, community="r/fantasyfootball", **kw) -> SocialPost:
    return SocialPost(platform=platform, external_id=pid, url=f"https://example.test/{platform}/{pid}",
                      created_at=NOW - timedelta(hours=hours_ago), collected_at=NOW, text=text,
                      author=author or f"user_{pid}", community=community,
                      engagement=Engagement(likes=likes, comments=comments, shares=shares, views=views), **kw)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str):
    return json.loads(fixture_text(name))

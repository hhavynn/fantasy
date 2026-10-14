"""Configuration loaded from the repo-root `.env` file / environment variables.

Secrets (ESPN_S2, SWID) are only ever read from the environment and are never
logged or written to the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
DATA_DIR = REPO_ROOT / "data"

load_dotenv(REPO_ROOT / ".env")


def _default_season() -> int:
    # NFL seasons start in September; Jan-Feb belong to the previous season.
    today = date.today()
    return today.year if today.month >= 3 else today.year - 1


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


@dataclass(frozen=True)
class Settings:
    league_id: int | None
    season: int
    nfl_season: int
    espn_s2: str | None
    swid: str | None
    my_team_id: int | None
    db_path: Path

    @property
    def has_espn_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)


def get_settings() -> Settings:
    season = _int_or_none(os.getenv("ESPN_SEASON")) or _default_season()
    db_path = Path(os.getenv("FANTASY_DB_PATH") or "data/fantasy.duckdb")
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    return Settings(
        league_id=_int_or_none(os.getenv("ESPN_LEAGUE_ID")),
        season=season,
        nfl_season=_int_or_none(os.getenv("NFL_SEASON")) or season,
        espn_s2=(os.getenv("ESPN_S2") or "").strip() or None,
        swid=(os.getenv("SWID") or "").strip() or None,
        my_team_id=_int_or_none(os.getenv("ESPN_MY_TEAM_ID")),
        db_path=db_path,
    )

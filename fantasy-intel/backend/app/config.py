"""Configuration loaded from the repo-root `.env` file / environment variables.

Secrets (ESPN_S2, SWID) are only ever read from the environment and are never
logged or written to the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _str_or_none(value: str | None) -> str | None:
    return (value or "").strip() or None


_MIN_ASSERTION_SECRET = 32


@dataclass(frozen=True)
class Settings:
    league_id: int | None
    season: int
    nfl_season: int
    espn_s2: str | None = field(repr=False)   # secrets never appear in repr(), logs or tracebacks
    swid: str | None = field(repr=False)
    my_team_id: int | None
    db_path: Path
    external_preferred_provider: str | None = None
    trade_max_players_per_side: int = 6     # manual trade analyzer guard (TRADE_MAX_PLAYERS_PER_SIDE, 1-10)
    motherduck_database: str | None = None  # hosted mode: the API reads md:<name> instead of db_path
    api_key: str | None = field(default=None, repr=False)   # hosted mode: required `X-API-Key` header on every request
    # --- multi-league platform (Phase 1). Every flag defaults OFF, so today's behaviour is unchanged. ---
    multileague_enabled: bool = False       # MULTILEAGUE_ENABLED=1: serves /v2/*; otherwise those routes 404
    tenancy_db_path: Path = field(default_factory=lambda: DATA_DIR / "tenancy.duckdb")   # FANTASY_TENANCY_DB_PATH
    user_assertion_secret: str | None = field(default=None, repr=False)  # FANTASY_USER_ASSERTION_SECRET, >= 32 chars or treated as unset
    owner_user_id: str | None = None        # FANTASY_OWNER_USER_ID: the one user allowed AccessMethod.OWNER_ENV
    sleeper_source: str = "disabled"        # SLEEPER_SOURCE = disabled | fixture | live
    espn_public_import_enabled: bool = False    # ESPN_PUBLIC_IMPORT_ENABLED=1
    yahoo_client_id: str | None = None      # YAHOO_CLIENT_ID (presence only; OAuth is not implemented)

    @property
    def has_espn_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)


def get_settings() -> Settings:
    season = _int_or_none(os.getenv("ESPN_SEASON")) or _default_season()
    db_path = Path(os.getenv("FANTASY_DB_PATH") or "data/fantasy.duckdb")
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    tenancy_db_path = Path(os.getenv("FANTASY_TENANCY_DB_PATH") or "data/tenancy.duckdb")
    if not tenancy_db_path.is_absolute():
        tenancy_db_path = REPO_ROOT / tenancy_db_path
    # A short secret is no secret: treat it as unset so assertions are refused rather than forgeable.
    assertion_secret = _str_or_none(os.getenv("FANTASY_USER_ASSERTION_SECRET"))
    if assertion_secret is not None and len(assertion_secret) < _MIN_ASSERTION_SECRET:
        assertion_secret = None
    api_key = (os.getenv("FANTASY_API_KEY") or "").strip() or None
    # The API key travels on every request; if it also signed user assertions, any key holder could forge any user.
    if assertion_secret is not None and assertion_secret == api_key:
        assertion_secret = None
    return Settings(
        league_id=_int_or_none(os.getenv("ESPN_LEAGUE_ID")),
        season=season,
        nfl_season=_int_or_none(os.getenv("NFL_SEASON")) or season,
        espn_s2=(os.getenv("ESPN_S2") or "").strip() or None,
        swid=(os.getenv("SWID") or "").strip() or None,
        my_team_id=_int_or_none(os.getenv("ESPN_MY_TEAM_ID")),
        db_path=db_path,
        external_preferred_provider=(os.getenv("EXTERNAL_USAGE_PREFERRED_PROVIDER") or "").strip() or None,
        trade_max_players_per_side=max(1, min(10, _int_or_none(os.getenv("TRADE_MAX_PLAYERS_PER_SIDE")) or 6)),
        motherduck_database=(os.getenv("MOTHERDUCK_DATABASE") or "").strip() or None,
        api_key=api_key,
        multileague_enabled=_flag(os.getenv("MULTILEAGUE_ENABLED")),
        tenancy_db_path=tenancy_db_path,
        user_assertion_secret=assertion_secret,
        owner_user_id=_str_or_none(os.getenv("FANTASY_OWNER_USER_ID")),
        sleeper_source=(_str_or_none(os.getenv("SLEEPER_SOURCE")) or "disabled").lower(),
        espn_public_import_enabled=_flag(os.getenv("ESPN_PUBLIC_IMPORT_ENABLED")),
        yahoo_client_id=_str_or_none(os.getenv("YAHOO_CLIENT_ID")),
    )

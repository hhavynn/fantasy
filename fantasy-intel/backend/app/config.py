"""Configuration loaded from the repo-root `.env` file / environment variables.

Secrets (ESPN_S2, SWID) are only ever read from the environment and are never
logged or written to the database.
"""

from __future__ import annotations

import os
import re
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
    # Hosted mode on free tiers: with SERVE_SNAPSHOT=1 (set on Vercel only) the API serves the snapshot that
    # scripts/publish_snapshot.py put in R2, taking precedence over motherduck_database. The publisher needs
    # only the bucket + a read/write token; the API's token should be scoped to Object Read.
    serve_snapshot: bool = False            # SERVE_SNAPSHOT=1
    snapshot_bucket: str | None = None      # SNAPSHOT_BUCKET
    snapshot_prefix: str = "snapshots"      # SNAPSHOT_PREFIX
    r2_account_id: str | None = None        # R2_ACCOUNT_ID
    r2_access_key_id: str | None = field(default=None, repr=False)      # R2_ACCESS_KEY_ID
    r2_secret_access_key: str | None = field(default=None, repr=False)  # R2_SECRET_ACCESS_KEY
    r2_endpoint: str | None = None          # R2_ENDPOINT: override the account endpoint (tests, local S3)
    api_key: str | None = field(default=None, repr=False)   # hosted mode: required `X-API-Key` header on every request
    # --- multi-league platform. Every flag defaults OFF, so today's behaviour is unchanged. ---
    multileague_enabled: bool = False       # MULTILEAGUE_ENABLED=1: serves /v2/*; otherwise those routes 404
    # Supabase control plane (docs/multileague/ADR-0002). Read only by /v2 routes; never opened with the flag off.
    supabase_url: str | None = None         # SUPABASE_URL: https://<ref>.supabase.co (JWKS + token issuer)
    supabase_db_url: str | None = field(default=None, repr=False)   # SUPABASE_DB_URL (the fi_api DSN, via the Supavisor pooler)
    credential_public_keys: dict = field(default_factory=dict, repr=False)   # CREDENTIALS_PUBKEY_V<n>: RSA public PEMs
    credential_wrap_key_version: str | None = None                            # CREDENTIALS_WRAP_KEY_VERSION or the highest
    sleeper_source: str = "disabled"        # SLEEPER_SOURCE = disabled | fixture | live
    espn_public_import_enabled: bool = False    # ESPN_PUBLIC_IMPORT_ENABLED=1
    yahoo_client_id: str | None = None      # YAHOO_CLIENT_ID (presence only; OAuth is not implemented)

    @property
    def has_espn_credentials(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    @property
    def supabase_issuer(self) -> str | None:
        return f"{self.supabase_url}/auth/v1" if self.supabase_url else None

    @property
    def r2_configured(self) -> bool:
        return bool(self.snapshot_bucket and self.r2_account_id and self.r2_access_key_id and self.r2_secret_access_key)

    @property
    def snapshot_enabled(self) -> bool:
        """The API serves the R2 snapshot (hosted mode)."""
        return self.serve_snapshot and self.r2_configured

    def r2_config(self):
        from .db.r2 import R2Config
        if not self.r2_configured:
            raise ValueError("SNAPSHOT_BUCKET, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must all be set")
        return R2Config(self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key,
                        bucket=self.snapshot_bucket, endpoint=self.r2_endpoint)


def get_settings() -> Settings:
    season = _int_or_none(os.getenv("ESPN_SEASON")) or _default_season()
    db_path = Path(os.getenv("FANTASY_DB_PATH") or "data/fantasy.duckdb")
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    api_key = (os.getenv("FANTASY_API_KEY") or "").strip() or None
    pubkeys = {f"v{m.group(1)}": v.strip() for k, v in os.environ.items()
               if (m := re.fullmatch(r"CREDENTIALS_PUBKEY_V([0-9]{1,4})", k)) and v.strip()}
    wrap_version = _str_or_none(os.getenv("CREDENTIALS_WRAP_KEY_VERSION")) or (
        max(pubkeys, key=lambda v: int(v[1:])) if pubkeys else None)
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
        serve_snapshot=_flag(os.getenv("SERVE_SNAPSHOT")),
        snapshot_bucket=_str_or_none(os.getenv("SNAPSHOT_BUCKET")),
        snapshot_prefix=_str_or_none(os.getenv("SNAPSHOT_PREFIX")) or "snapshots",
        r2_account_id=_str_or_none(os.getenv("R2_ACCOUNT_ID")),
        r2_access_key_id=_str_or_none(os.getenv("R2_ACCESS_KEY_ID")),
        r2_secret_access_key=_str_or_none(os.getenv("R2_SECRET_ACCESS_KEY")),
        r2_endpoint=_str_or_none(os.getenv("R2_ENDPOINT")),
        api_key=api_key,
        multileague_enabled=_flag(os.getenv("MULTILEAGUE_ENABLED")),
        supabase_url=(_str_or_none(os.getenv("SUPABASE_URL")) or "").rstrip("/") or None,
        supabase_db_url=_str_or_none(os.getenv("SUPABASE_DB_URL")),
        credential_public_keys=pubkeys,
        credential_wrap_key_version=wrap_version,
        sleeper_source=(_str_or_none(os.getenv("SLEEPER_SOURCE")) or "disabled").lower(),
        espn_public_import_enabled=_flag(os.getenv("ESPN_PUBLIC_IMPORT_ENABLED")),
        yahoo_client_id=_str_or_none(os.getenv("YAHOO_CLIENT_ID")),
    )

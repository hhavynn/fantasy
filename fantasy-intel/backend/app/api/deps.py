from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import duckdb
from fastapi import HTTPException

from ..config import get_settings
from ..db.connection import MIN_READ_SCHEMA_VERSION, SCHEMA_VERSION, DatabaseBusy, connect, connect_motherduck, fetch_dicts, schema_version

log = logging.getLogger(__name__)

JSON_COLUMNS = {"components", "reasons", "competitors", "roster_slots", "owner_ids", "matchup_acquisitions",
                "stats", "details", "eligible_slots", "scoring_items", "waiver_process_days",
                "labels", "evidence", "ranks", "examples", "context_labels", "weeks", "previous",
                "bye_weeks", "missing_weeks", "assignment", "bench", "missing_projection_ids",
                "teamless_unprojected_ids", "missing_players"}


# Hosted mode keeps one MotherDuck connection per process (opening one is a network round trip);
# each request gets its own cursor on it.
_MD_LOCK = threading.Lock()
_MD_CONS: dict[str, duckdb.DuckDBPyConnection] = {}


def _motherduck_cursor(database: str) -> duckdb.DuckDBPyConnection:
    with _MD_LOCK:
        con = _MD_CONS.get(database)
        if con is not None:
            try:
                return con.cursor()
            except duckdb.Error:  # dropped connection: reopen below
                _MD_CONS.pop(database, None)
        try:
            con = connect_motherduck(database)
        except duckdb.Error as err:     # the detail reaches visitors: no database name or driver text in it
            missing = not (os.getenv("MOTHERDUCK_TOKEN") or os.getenv("motherduck_token"))
            log.warning("motherduck connect failed: %s%s", type(err).__name__,
                        " (MOTHERDUCK_TOKEN is not set)" if missing else "")     # for operators; never the token
            raise HTTPException(503, "Cannot reach the hosted database.") from None
        _MD_CONS[database] = con
        return con.cursor()


# Snapshot mode keeps one read-only connection per process on the downloaded file and swaps it when a
# newer publish has been downloaded; each request gets its own cursor. Superseded connections are left
# for garbage collection rather than closed, so a request still running on one is never cut off.
_SNAP_LOCK = threading.Lock()
_SNAP: dict[str, Any] = {}


def _snapshot_cursor(settings) -> duckdb.DuckDBPyConnection:
    from ..db.snapshot import SnapshotCache

    with _SNAP_LOCK:
        cache = _SNAP.get("cache")
        if cache is None:
            cache = _SNAP["cache"] = SnapshotCache(settings.r2_config(), prefix=settings.snapshot_prefix)
    try:
        path, _ = cache.local_path()
    except Exception as err:  # R2Error, a bad manifest: nothing has been served yet in this instance
        log.warning("snapshot load failed: %s", type(err).__name__)     # never the object key or bucket in the detail
        raise HTTPException(503, "Cannot load the published snapshot.") from None
    with _SNAP_LOCK:
        if _SNAP.get("path") != path:
            try:
                _SNAP["con"], _SNAP["path"] = duckdb.connect(str(path), read_only=True), path
            except duckdb.Error as err:   # e.g. written by a newer DuckDB than this bundle's
                if _SNAP.get("con") is None:
                    log.warning("cannot open snapshot: %s", type(err).__name__)
                    raise HTTPException(503, "Cannot open the published snapshot.") from None
                log.warning("cannot open snapshot %s (%s); still serving %s", path.name, err, _SNAP["path"].name)
        return _SNAP["con"].cursor()


@dataclass(frozen=True)
class LeagueScope:
    """The league a /v2/leagues/{lg}/... request was authorized for (app/api/league_scope.py) and the files it reads.
    Set only after authorization; read by db() and data_version()."""
    league_id: str
    league_path: Path
    global_path: Path
    data_version: str
    global_version: str
    visibility_epoch: int
    perspective: int | None
    provider: str = "espn"          # "sleeper": responses use Sleeper's wording (app/api/provider_copy.py)

    @property
    def cache_key(self) -> str:
        """Every input that changes an answer for the same route and params (spec §1: league, data and global
        versions, visibility epoch, perspective)."""
        return (f"league:{self.league_id}:{self.data_version}:{self.global_version}:{self.visibility_epoch}:"
                f"{'none' if self.perspective is None else self.perspective}")


LEAGUE_SCOPE: ContextVar[LeagueScope | None] = ContextVar("league_scope", default=None)
_LAYERED: dict[str, Any] = {}


def layered_databases():
    from ..db.league_conn import LayeredDatabases
    key = ("dbs", os.getpid())     # per process: DuckDB instances never cross a fork
    with _SNAP_LOCK:
        inst = _LAYERED.get(key)
        if inst is None:
            inst = _LAYERED[key] = LayeredDatabases()
        return inst


@contextmanager
def db() -> Iterator[Any]:
    scope = LEAGUE_SCOPE.get()
    if scope is not None:
        # A /v2/leagues/{lg}/... request: this league's file and the global file it was built against, read-only,
        # with the request's perspective as fantasy_teams.is_mine. Immutable files: no transaction needed.
        con = layered_databases().cursor(scope.league_path, scope.global_path, perspective=scope.perspective)
        try:
            version = schema_version(con)
            if version is None or version < MIN_READ_SCHEMA_VERSION:
                raise HTTPException(503, f"This league's data (schema v{version or 1}) is older than v{SCHEMA_VERSION}; "
                                         "it is rebuilt at its next sync.")
            yield con
        finally:
            con.close()
        return
    settings = get_settings()
    hosted_md = bool(settings.motherduck_database) and not settings.snapshot_enabled
    if settings.snapshot_enabled:
        con = _snapshot_cursor(settings)   # an immutable file: no publish can commit under a request
    elif hosted_md:
        con = _motherduck_cursor(settings.motherduck_database)
    else:
        if not settings.db_path.exists():
            raise HTTPException(503, "No database yet. Run scripts/sync_espn.py and scripts/sync_nfl.py first.")
        try:
            con = connect(settings.db_path, read_only=True, retries=8)
        except DatabaseBusy:
            raise HTTPException(503, "Database is busy (a sync is probably running). Retry in a few seconds.")
    try:
        if hosted_md:
            # One snapshot per request: a publish (scripts/publish_motherduck.py) can commit while a
            # request runs, and every query in the request must see the same version, including the
            # data_version stamp that keys the trade-search cache. The local file needs no such
            # guard: the API's read-only connection holds DuckDB's file lock, so no sync can commit.
            con.execute("BEGIN TRANSACTION")
        version = schema_version(con)
        if version is not None and version < MIN_READ_SCHEMA_VERSION or (version is None and _has_tables(con)):
            stale = f"Database schema v{version or 1} is older than v{SCHEMA_VERSION}."
            fix = "Run `uv run python ../scripts/sync_nfl.py` (and sync_espn.py) to migrate and rebuild."
            if hosted_md or settings.snapshot_enabled:
                # A hosted visitor can see this detail (a page printing it inline): no command; the operator's fix
                # goes to the log. The "Database schema" wording stays, as the frontend classifies the error by it.
                log.error("%s %s", stale, fix)
                raise HTTPException(503, f"{stale} The league data is being rebuilt; try again later.")
            raise HTTPException(503, f"{stale} {fix}")
        yield con
    except duckdb.Error:
        if hosted_md:  # possibly a dropped connection: the next request reconnects
            with _MD_LOCK:
                _MD_CONS.pop(settings.motherduck_database, None)
        raise
    finally:
        if hosted_md:
            try:
                con.execute("ROLLBACK")  # read-only request: nothing to commit
            except duckdb.Error:
                pass
        con.close()


def data_version(con) -> str:
    """Changes whenever the served data changes: the local file's mtime, or the publish stamp that
    scripts/publish_snapshot.py (or publish_motherduck.py) writes into the hosted copy."""
    scope = LEAGUE_SCOPE.get()
    if scope is not None:
        return scope.cache_key    # per league, file versions, visibility epoch and perspective: never shared
    settings = get_settings()
    if settings.snapshot_enabled or settings.motherduck_database:
        row = con.execute("SELECT value FROM schema_meta WHERE key = 'published_at'").fetchone()
        stamp = row[0] if row else ""
        return f"snapshot:{stamp}" if settings.snapshot_enabled else f"md:{settings.motherduck_database}:{stamp}"
    return f"{settings.db_path}:{settings.db_path.stat().st_mtime_ns}"


def _has_tables(con) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'leagues'").fetchone()[0])


def rows(con, sql: str, params=()) -> list[dict]:
    out = fetch_dicts(con, sql, params)
    for r in out:
        for k in JSON_COLUMNS & r.keys():
            if isinstance(r[k], str):
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return out


def one(con, sql: str, params=()) -> dict | None:
    r = rows(con, sql, params)
    return r[0] if r else None

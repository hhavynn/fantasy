"""Serve the existing pages for one league: `/v2/leagues/{league_id}/api/...` (multi-league Phase 2).

Every legacy router is mounted a second time under that prefix with the `league_scope` dependency, which runs before
the handler and before any cache lookup:

  1. authorize the caller for the league in Postgres (app/control_plane/authz.py): unknown and unauthorized are 404;
  2. no published snapshot yet -> 409 "first sync in progress" (never another league's data);
  3. fetch the league's file and the global file it was built against into the per-instance cache (verified);
  4. pick the perspective ("my team"): the signed-in viewer's saved team, else `?team_id=`, else none;
  5. set the LEAGUE_SCOPE ContextVar, which `deps.db()` turns into a layered read-only cursor.

The handlers are unchanged. Responses carry `Cache-Control: private, no-store` and the versions actually read.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from .deps import LEAGUE_SCOPE, LeagueScope, layered_databases
from .leagues_v2 import NOT_FOUND, current_principal, valid_league_id

log = logging.getLogger(__name__)
FIRST_SYNC = "First sync in progress."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def league_file_cache():
    """One cache per process: R2 (read-only token) in production, LEAGUE_FILES_DIR locally."""
    from ..db.league_files import DEFAULT_MAX_BYTES, DirStore, LeagueFileCache, R2Store
    with _CACHE_LOCK:
        cache = _CACHE.get("files")
        if cache is None:
            settings = get_settings()
            files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
            store = DirStore(Path(files_dir)) if files_dir else R2Store(settings.r2_config())
            max_bytes = int(os.environ.get("LEAGUE_CACHE_MAX_BYTES") or DEFAULT_MAX_BYTES)
            cache = _CACHE["files"] = LeagueFileCache(
                store, Path(tempfile.gettempdir()) / "fantasy-leagues", max_bytes=max_bytes,
                on_evict=layered_databases().forget)
        return cache


def _resolve(principal, league_id: str, team_param: str | None) -> LeagueScope:
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .leagues_v2 import SIGN_IN, UNAVAILABLE
    try:
        with get_control_plane().request_tx(principal) as con:
            access = authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if access.snapshot is None:
        raise HTTPException(409, {"detail": FIRST_SYNC, "sync_state": access.sync_state})
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        log.warning("league %s has a pointer in an unsupported format", league_id)
        raise HTTPException(503, FILES_UNAVAILABLE)
    cache = league_file_cache()
    try:
        league_path = cache.league_file(access.league_id, snap.data_version)
        global_path = cache.global_file(snap.global_version)
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object: never a 500 with details
        log.warning("league files unavailable for %s: %s", league_id, type(err).__name__)
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    # "My team" (fantasy_teams.is_mine): the signed-in viewer's saved team, as the owner's team was on the legacy
    # site; an explicit ?team_id= stays an explicit choice the routes already handle. A viewer with no saved team
    # (anonymous, or not chosen yet) takes the URL's team as their perspective everywhere.
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, league_path, global_path, snap.data_version, snap.global_version,
                       access.visibility_epoch, chosen)


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    scope = await run_in_threadpool(_resolve, principal, league_id, request.query_params.get("team_id"))
    request.state.fi_scope = scope
    token = LEAGUE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        LEAGUE_SCOPE.reset(token)


LEAGUE_PREFIX = "/v2/leagues/{league_id}"


def mount_legacy_routers(app, routers, require_enabled) -> None:
    """Mount each legacy router again under /v2/leagues/{league_id}. Not in the OpenAPI schema: the legacy entries
    already document the operations."""
    for r in routers:
        app.include_router(r, prefix=LEAGUE_PREFIX, include_in_schema=False,
                           dependencies=[Depends(require_enabled), Depends(league_scope, scope="function")])


async def add_scope_headers(request: Request, call_next):
    response = await call_next(request)
    scope = getattr(request.state, "fi_scope", None)
    if scope is not None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-FI-Data-Version"] = scope.data_version
        response.headers["X-FI-Global-Version"] = scope.global_version
    return response

"""Serve the existing pages for one league: `/v2/leagues/{league_id}/api/...` (multi-league Phase 2).

Every legacy router is mounted a second time under that prefix with the `league_scope` dependency, which runs before
the handler and before any cache lookup:

  1. authorize the caller for the league in Postgres (app/control_plane/authz.py): unknown and unauthorized are 404;
  2. no published snapshot yet -> 409 "first sync in progress" (never another league's data);
  3. fetch the league's file and the global file it was built against into the per-instance cache (verified);
  4. pick the perspective ("my team"): the signed-in viewer's saved team, else `?team_id=`, else none;
  5. set the LEAGUE_SCOPE ContextVar, which `deps.db()` turns into a layered read-only cursor.

The owner-only research routers (projection performance, Ours promotion, research) are mounted with
`require_platform_admin` as well: spec §6 keeps them platform-admin only, on every league (anyone else gets 404).

The handlers are unchanged. Responses carry `Cache-Control: private, no-store` and the versions actually read.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

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
        key = ("files", os.getpid())       # per process: a forked worker never reuses its parent's directory
        cache = _CACHE.get(key)
        if cache is None:
            settings = get_settings()
            files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
            store = DirStore(Path(files_dir)) if files_dir else R2Store(settings.r2_config())
            max_bytes = int(os.environ.get("LEAGUE_CACHE_MAX_BYTES") or DEFAULT_MAX_BYTES)
            cache = _CACHE[key] = LeagueFileCache(
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
        from .connect import nudge_probe
        nudge_probe(principal, league_id)      # an expired public league starts a probe; the answer stays 404
        raise HTTPException(404, NOT_FOUND) from None
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if access.snapshot is None:
        # The detail stays a plain string (what the dashboard shows); the state travels in a header.
        raise HTTPException(409, FIRST_SYNC, headers={"X-FI-Sync-State": access.sync_state})
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        log.warning("league %s has a pointer in an unsupported format", league_id)
        raise HTTPException(503, FILES_UNAVAILABLE)
    cache = league_file_cache()
    league_path = None
    try:
        # Pinned until the request ends (league_scope releases them): never evicted before the handler opens them.
        league_path = cache.pin_league(access.league_id, snap.data_version)
        global_path = cache.pin_global(snap.global_version)
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object, a full cache: never a 500 with details
        if league_path is not None:
            cache.release(league_path)
        log.warning("league files unavailable for %s: %s %s %s", league_id, type(err).__name__,
                    getattr(err, "status", ""), getattr(err, "code", ""))
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    # "My team" (fantasy_teams.is_mine) is who the viewer is, exactly as on the legacy site: the signed-in viewer's saved
    # team, even while a route analyses another team through ?team_id= (legacy: is_mine stays the owner's team then).
    # A viewer with no saved team (anonymous, or not chosen yet) takes the URL's team. Golden parity on real Los
    # Subies depends on this order (the __team cases).
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, league_path, global_path, snap.data_version, snap.global_version,
                       access.visibility_epoch, chosen)


# ---- per-instance response cache ------------------------------------------------------------------------------------

class ResponseCache:
    """Successful GET responses keyed by (scope cache key, path, sorted query): league, data and global versions,
    visibility epoch and perspective are all in the scope key, so two leagues, two versions or two perspectives never
    share an entry. Looked up only after authorization (inside `league_scope`). Bounded by bytes, LRU."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, bytes, str]] = OrderedDict()   # key -> (status, body, media type)
        self.hits = 0

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(len(body) for _, body, _ in self._entries.values())

    def statuses(self) -> list[int]:
        with self._lock:
            return [status for status, _, _ in self._entries.values()]

    def get(self, key: str):
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                self._entries.move_to_end(key)
                self.hits += 1
            return hit

    def put(self, key: str, status: int, body: bytes, media_type: str) -> None:
        if status != 200 or len(body) > self.max_bytes // 4:          # one answer never takes most of the cache
            return
        with self._lock:
            self._entries[key] = (status, body, media_type)
            self._entries.move_to_end(key)
            used = sum(len(b) for _, b, _ in self._entries.values())
            while self._entries and used > self.max_bytes:
                _, (_, old, _) = self._entries.popitem(last=False)
                used -= len(old)


_RESPONSES: dict[str, ResponseCache] = {}


def response_cache() -> ResponseCache:
    with _CACHE_LOCK:
        cache = _RESPONSES.get("cache")
        if cache is None:
            cache = _RESPONSES["cache"] = ResponseCache(int(os.environ.get("LEAGUE_RESPONSE_CACHE_BYTES") or (32 << 20)))
        return cache


class CachedResponse(Exception):
    """Raised by league_scope on a cache hit (after authorization) to answer without running the handler."""

    def __init__(self, entry):
        self.entry = entry


async def serve_cached(request: Request, exc: CachedResponse) -> Response:
    status, body, media_type = exc.entry
    return Response(content=body, status_code=status, media_type=media_type, headers={"X-FI-Cache": "hit"})


def _response_key(scope: LeagueScope, request: Request) -> str | None:
    if request.method != "GET":
        return None
    query = "&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))
    return f"{scope.cache_key}|{request.url.path}|{query}"


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    scope = await run_in_threadpool(_resolve, principal, league_id, request.query_params.get("team_id"))
    request.state.fi_scope = scope
    key = _response_key(scope, request)
    if key is not None:
        hit = response_cache().get(key)
        if hit is not None:
            cache = league_file_cache()
            cache.release(scope.league_path)
            cache.release(scope.global_path)
            raise CachedResponse(hit)
        request.state.fi_cache_key = key
    token = LEAGUE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        LEAGUE_SCOPE.reset(token)
        cache = league_file_cache()
        cache.release(scope.league_path)
        cache.release(scope.global_path)


LEAGUE_PREFIX = "/v2/leagues/{league_id}"


def _is_platform_admin(principal) -> bool:
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    from .leagues_v2 import SIGN_IN, UNAVAILABLE
    try:
        with get_control_plane().request_tx(principal) as con:
            row = con.execute("SELECT is_platform_admin FROM fi.app_users WHERE id = %s", [principal.user_id]).fetchone()
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    return bool(row and row[0])


async def require_platform_admin(principal=Depends(current_principal)) -> None:
    """Owner-only research pages: platform admins only. Everyone else gets the same 404 as an unknown league, and
    the check runs before `league_scope` touches any file."""
    if not principal.is_authenticated or not await run_in_threadpool(_is_platform_admin, principal):
        raise HTTPException(404, NOT_FOUND)


def mount_legacy_routers(app, routers, require_enabled, *, owner_only=()) -> None:
    """Mount each legacy router again under /v2/leagues/{league_id}; the `owner_only` ones also require a platform
    admin. Not in the OpenAPI schema: the legacy entries already document the operations."""
    for r in routers:
        admin = [Depends(require_platform_admin)] if any(r is o for o in owner_only) else []
        app.include_router(r, prefix=LEAGUE_PREFIX, include_in_schema=False,
                           dependencies=[Depends(require_enabled), *admin, Depends(league_scope, scope="function")])


async def add_scope_headers(request: Request, call_next):
    """Private, uncached by browsers and proxies, naming the versions read; successful GETs go into the response
    cache on the way out."""
    response = await call_next(request)
    scope = getattr(request.state, "fi_scope", None)
    if scope is None:
        return response
    key = getattr(request.state, "fi_cache_key", None)
    if key is not None and response.status_code == 200 and "x-fi-cache" not in response.headers:
        body = b"".join([chunk async for chunk in response.body_iterator])
        response_cache().put(key, response.status_code, body, response.media_type or "application/json")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        response = Response(content=body, status_code=response.status_code, headers=headers,
                            media_type=response.media_type)
        response.headers["X-FI-Cache"] = "miss"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-FI-Data-Version"] = scope.data_version
    response.headers["X-FI-Global-Version"] = scope.global_version
    return response

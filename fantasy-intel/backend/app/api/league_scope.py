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

Performance (issue #201): the request's phases go into `Server-Timing` (app/monitor/timing.py, no league ids). When a
league's pointer has moved to files this instance does not hold yet, the request waits at most
LEAGUE_COLD_LOAD_BUDGET_S (3 s) for them and otherwise reads the league's own previous files, marked
`X-FI-Degraded: stale-files` (app/db/league_files.py `pin_pair`). Identical concurrent GETs (same response-cache key:
league, versions, visibility epoch, perspective, path and query) run the handler once; the others wait for its cached
answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..config import get_settings
from . import provider_copy
from ..monitor.timing import Timing, request_timing, size_bucket, version_bytes
from .deps import LEAGUE_SCOPE, LeagueScope, layered_databases
from .leagues_v2 import NOT_FOUND, current_principal, valid_league_id

log = logging.getLogger(__name__)
FIRST_SYNC = "First sync in progress."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
DEGRADED_HEADER = "X-FI-Degraded"
FLIGHT_WAIT_S = 20.0           # longest an identical request waits for the first one's answer before running its own


def cold_load_budget_s() -> float:
    try:
        return max(0.0, float(os.environ.get("LEAGUE_COLD_LOAD_BUDGET_S") or 3.0))
    except ValueError:
        return 3.0


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


def _resolve(principal, league_id: str, team_param: str | None, timing: Timing | None = None):
    """(scope, pinned pair): the caller releases the pair (`league_file_cache().release_pair`) when the request ends."""
    timing = timing or Timing()
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .leagues_v2 import SIGN_IN, UNAVAILABLE
    try:
        with timing.phase("auth"), get_control_plane().request_tx(principal) as con:
            access = authorize_league_in(con, principal, league_id)
    except LeagueNotFound:
        from .connect import nudge_probe
        nudge_probe(principal, league_id)      # an expired public league starts a probe; the answer stays 404
        raise HTTPException(404, NOT_FOUND) from None
    except AccountGone:
        raise HTTPException(401, SIGN_IN) from None
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if access.provider == "sleeper" and get_settings().sleeper_source == "disabled":
        from .connect import SLEEPER_UNAVAILABLE
        raise HTTPException(503, SLEEPER_UNAVAILABLE)     # the rollback switch (SLEEPER_SOURCE=disabled)
    if access.snapshot is None:
        # The detail stays a plain string (what the dashboard shows); the state travels in a header.
        raise HTTPException(409, FIRST_SYNC, headers={"X-FI-Sync-State": access.sync_state})
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        log.warning("league %s has a pointer in an unsupported format", league_id)
        raise HTTPException(503, FILES_UNAVAILABLE)
    cache = league_file_cache()
    try:
        # Pinned until the request ends (league_scope releases them): never evicted before the handler opens them.
        # With a newer pointer than this instance holds, at most the budget is spent waiting; then this league's own
        # previous files are read and the response says so.
        with timing.phase("files"):
            pair = cache.pin_pair(access.league_id, snap.data_version, snap.global_version,
                                  budget_s=cold_load_budget_s())
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object, a full cache: never a 500 with details
        log.warning("league files unavailable for %s: %s %s %s", league_id, type(err).__name__,
                    getattr(err, "status", ""), getattr(err, "code", ""))
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    timing.set_code("files", "stale" if pair.degraded else "cold" if pair.cold else "warm")
    timing.size = size_bucket(version_bytes(pair.data_version))
    if pair.degraded:
        log.warning("league %s served from its previous files (%s)", league_id, pair.degraded)
    # "My team" (fantasy_teams.is_mine) is who the viewer is, exactly as on the legacy site: the signed-in viewer's saved
    # team, even while a route analyses another team through ?team_id= (legacy: is_mine stays the owner's team then).
    # A viewer with no saved team (anonymous, or not chosen yet) takes the URL's team. Golden parity on real Los
    # Subies depends on this order (the __team cases).
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, pair.league_path, pair.global_path, pair.data_version, pair.global_version,
                       access.visibility_epoch, chosen, access.provider), pair


# ---- per-instance response cache ------------------------------------------------------------------------------------

class ResponseCache:
    """Successful GET responses keyed by (scope cache key, path, sorted query): league, data and global versions,
    visibility epoch and perspective are all in the scope key, so two leagues, two versions or two perspectives never
    share an entry. Looked up only after authorization (inside `league_scope`). Bounded by bytes, LRU."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, bytes, str]] = OrderedDict()   # key -> (status, body, media type)
        self._flights: dict[str, tuple[Any, asyncio.Event]] = {}                 # key -> (event loop, done)
        self.hits = 0
        self.shared = 0              # answers a request received from an identical request it waited for

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

    def begin(self, key: str) -> tuple[str, asyncio.Event | None]:
        """Single flight for one key, as (role, event):

          ("lead", None)   the caller computes the answer and must call `land(key)` once its response is complete
                           (whether or not it was cached);
          ("wait", event)  an identical request on this event loop is computing it: wait, then look the key up again;
          ("solo", None)   an identical request is in flight on another event loop (another worker thread): compute
                           alone, never land (the flight is not this caller's to end)."""
        loop = asyncio.get_running_loop()
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                self._flights[key] = (loop, asyncio.Event())
                return "lead", None
            return ("wait", flight[1]) if flight[0] is loop else ("solo", None)

    def land(self, key: str) -> None:
        """End this loop's flight for `key` and wake its waiters. A flight owned by another loop is left alone."""
        loop = asyncio.get_running_loop()
        with self._lock:
            flight = self._flights.get(key)
            if flight is None or flight[0] is not loop:
                return
            del self._flights[key]
        flight[1].set()

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
    # Re-encode the decoded pairs: joining them raw would let `?a%3D1%26b=2` collide with `?a=1&b=2`. Sort by name only
    # (a stable sort), so different names in any order share a key while a repeated name keeps its values' order.
    query = urlencode(sorted(request.query_params.multi_items(), key=lambda kv: kv[0]))
    return f"{scope.cache_key}|{request.url.path}|{query}"


async def _lookup(request: Request, scope: LeagueScope, timing: Timing) -> None:
    """Raise CachedResponse with a cached answer (after waiting for an identical in-flight request, if any); otherwise
    note the key for add_scope_headers to fill, leading the flight when no identical request is running."""
    key = _response_key(scope, request)
    if key is None:
        return
    cache = response_cache()
    hit = cache.get(key)
    if hit is None:
        role, flight = cache.begin(key)
        if role == "lead":
            request.state.fi_flight = key            # add_scope_headers lands it once the answer is complete
        elif role == "wait":
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                await asyncio.wait_for(flight.wait(), FLIGHT_WAIT_S)
            except TimeoutError:
                pass
            timing.add("wait", loop.time() - t0)
            hit = cache.get(key)
            if hit is not None:
                cache.shared += 1
    if hit is not None:
        timing.add("cache", 0.0, "hit")
        raise CachedResponse(hit)
    request.state.fi_cache_key = key


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    timing = request_timing(request) or Timing()
    scope, pair = await run_in_threadpool(_resolve, principal, league_id, request.query_params.get("team_id"), timing)
    request.state.fi_scope = scope
    request.state.fi_degraded = pair.degraded
    try:
        await _lookup(request, scope, timing)
    except BaseException:                          # a cache hit, or a cancelled wait: unpin before leaving
        league_file_cache().release_pair(pair)
        raise
    token = LEAGUE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        LEAGUE_SCOPE.reset(token)
        league_file_cache().release_pair(pair)


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


def _json_response(response) -> bool:
    return (response.media_type or response.headers.get("content-type", "")).startswith("application/json")


async def add_scope_headers(request: Request, call_next):
    """Private, uncached by browsers and proxies, naming the versions read; successful GETs go into the response
    cache on the way out, and identical requests waiting on this one are woken once it is complete."""
    try:
        return await _add_scope_headers(request, call_next)
    finally:
        flight = getattr(request.state, "fi_flight", None)
        if flight is not None:
            response_cache().land(flight)


async def _add_scope_headers(request: Request, call_next):
    response = await call_next(request)
    scope = getattr(request.state, "fi_scope", None)
    if scope is None:
        return response
    key = getattr(request.state, "fi_cache_key", None)
    fresh = response.status_code == 200 and "x-fi-cache" not in response.headers
    reword = fresh and scope.provider != "espn" and _json_response(response) and \
        provider_copy.applies(request.url.path.split(f"/v2/leagues/{scope.league_id}", 1)[-1])
    if (key is not None and fresh) or reword:
        body = b"".join([chunk async for chunk in response.body_iterator])
        if reword:          # before caching, so a cache hit is worded the same way
            body = provider_copy.dumps(provider_copy.rewrite(json.loads(body)))
        if key is not None and fresh:
            response_cache().put(key, response.status_code, body, response.media_type or "application/json")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        response = Response(content=body, status_code=response.status_code, headers=headers,
                            media_type=response.media_type)
        if key is not None and fresh:
            response.headers["X-FI-Cache"] = "miss"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-FI-Data-Version"] = scope.data_version
    response.headers["X-FI-Global-Version"] = scope.global_version
    if getattr(request.state, "fi_degraded", None):
        response.headers[DEGRADED_HEADER] = "stale-files"     # this league's previous files: data may be older
    return response

"""Serve the existing pages for one league: `/v2/leagues/{league_id}/api/...` (multi-league Phase 2).

Every legacy router is mounted a second time under that prefix with the `league_scope` dependency, which runs before
the handler and before any cache lookup:

  1. authorize the caller for the league in Postgres (app/control_plane/authz.py): unknown and unauthorized are 404;
  2. no published snapshot yet -> 409 "first sync in progress" (never another league's data);
  3. fetch the league's file and the global file it was built against into the per-instance cache (verified);
  4. pick the perspective ("my team"): the signed-in viewer's saved team, else `?team_id=`, else none;
  5. set the LEAGUE_SCOPE ContextVar, which `deps.db()` turns into a layered read-only cursor.

The owner-only routers (projection performance and the Ours promotion tracker, app/main.py) are mounted with
`require_platform_admin` as well: spec §6 keeps them platform-admin only, on every league (anyone else gets 404). The
research lab, game splits and pace lab are league-member tools, authorized like every other router.

The handlers are unchanged. Responses carry `Cache-Control: private, no-store` and the versions actually read.

Performance (issue #201): the request's phases go into `Server-Timing` (app/monitor/timing.py, no league ids). When a
league's pointer has moved to files this instance does not hold yet, the request waits at most
LEAGUE_COLD_LOAD_BUDGET_S (3 s) for them and otherwise reads the league's own previous files, marked
`X-FI-Degraded: stale-files` (app/db/league_files.py `pin_pair`). Identical concurrent GETs (same response-cache key:
league, versions, visibility epoch, perspective, path and query) run the handler once; the others wait for its cached
answer.

Time bound (STATUS 1k): many answers depend on the wall clock as well as the files (the trade window starts at the first
week not kicked off, Start/Sit and add/drop lock players whose game has kicked off, provenance and waiver snapshots turn
stale with age). Every cached answer therefore expires at the next scheduled NFL kickoff after it was computed (the
global file's `nfl_schedules`, the same kickoffs the handlers read), and at the latest RESPONSE_TTL after it: the cap
bounds the age-based labels and covers a schedule without kickoff times. This applies to every route, so a route that
reads the clock never has to opt in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import duckdb
from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..config import get_settings
from . import provider_copy
from ..monitor.timing import Timing, request_timing, size_bucket, version_bytes
from .deps import LEAGUE_FILES, LEAGUE_SCOPE, HeldFiles, LeagueScope, layered_databases
from .leagues_v2 import NOT_FOUND, current_principal, valid_league_id

log = logging.getLogger(__name__)
FIRST_SYNC = "First sync in progress."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
DEGRADED_HEADER = "X-FI-Degraded"
FLIGHT_WAIT_S = 20.0           # longest an identical request waits for the first one's answer before running its own
RESPONSE_TTL = timedelta(minutes=15)    # the longest any cached answer is served, whatever the schedule says


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


COLD_LOADS = 4                 # leagues one event loop fetches files for at once (LEAGUE_COLD_LOADS overrides)


def cold_loads_per_loop() -> int:
    try:
        return max(1, int(os.environ.get("LEAGUE_COLD_LOADS") or COLD_LOADS))
    except ValueError:
        return COLD_LOADS


def _authorize(principal, league_id: str, timing: Timing):
    """The caller's access to the league (Postgres), before any file is touched: unknown or unauthorized is 404."""
    from ..control_plane.authz import LeagueNotFound, authorize_league_in
    from ..control_plane.pg import AccountGone, ControlPlaneUnavailable, get_control_plane
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
    return access


def _pin(access, team_param: str | None, timing: Timing, *, wait: bool = True, since: float | None = None):
    """(scope, pinned pair) for an authorized league, or None when `wait` is False and the files are not cached here.
    `since` (a perf_counter time) is when a cold request started waiting: the files phase counts from it and is coded
    cold. The caller releases the pair (`league_file_cache().release_pair`) when the request ends."""
    from ..db.league_conn import perspective_team
    snap = access.snapshot
    cache = league_file_cache()
    try:
        # Pinned until the request ends (league_scope releases them): never evicted before the handler opens them.
        # With a newer pointer than this instance holds, at most the budget is spent waiting; then this league's own
        # previous files are read and the response says so.
        t0 = time.perf_counter() if since is None else since
        if wait:
            pair = cache.pin_pair(access.league_id, snap.data_version, snap.global_version,
                                  budget_s=cold_load_budget_s())
        else:
            pair = cache.pin_pair_if_cached(access.league_id, snap.data_version, snap.global_version)
        if pair is not None:                       # a miss records nothing: the cold load that follows is the phase
            timing.add("files", time.perf_counter() - t0)
    except Exception as err:   # noqa: BLE001 - R2 errors, a missing object, a full cache: never a 500 with details
        timing.add("files", time.perf_counter() - t0)
        log.warning("league files unavailable for %s: %s %s %s", access.league_id, type(err).__name__,
                    getattr(err, "status", ""), getattr(err, "code", ""))
        raise HTTPException(503, FILES_UNAVAILABLE) from None
    if pair is None:
        return None
    timing.set_code("files", "stale" if pair.degraded else "cold" if pair.cold or since is not None else "warm")
    timing.size = size_bucket(version_bytes(pair.data_version))
    if pair.degraded:
        log.warning("league %s served from its previous files (%s)", access.league_id, pair.degraded)
    # "My team" (fantasy_teams.is_mine) is who the viewer is, exactly as on the legacy site: the signed-in viewer's saved
    # team, even while a route analyses another team through ?team_id= (legacy: is_mine stays the owner's team then).
    # A viewer with no saved team (anonymous, or not chosen yet) takes the URL's team. Golden parity on real Los
    # Subies depends on this order (the __team cases).
    chosen = perspective_team(access.perspective_team_id)
    if chosen is None and team_param is not None:
        chosen = perspective_team(team_param)
    return LeagueScope(access.league_id, pair.league_path, pair.global_path, pair.data_version, pair.global_version,
                       access.visibility_epoch, chosen, access.provider), pair


def _resolve(principal, league_id: str, team_param: str | None, timing: Timing | None = None):
    """(scope, pinned pair), waiting for the files in this thread: the caller releases the pair."""
    timing = timing or Timing()
    return _pin(_authorize(principal, league_id, timing), team_param, timing)


def _resolve_if_cached(principal, league_id: str, team_param: str | None, timing: Timing):
    """(access, (scope, pair) or None): authorization, then the pair only if it is already cached (no download)."""
    access = _authorize(principal, league_id, timing)
    return access, _pin(access, team_param, timing, wait=False)


# Cold loads (issue #201 risk 2). A request whose files are not cached here waits for them on the event loop, not in a
# thread-pool slot: per pair, one request (the leader) takes one of COLD_LOADS permits and loads in the pool; the
# others for the same pair wait for it, then pin from the cache, read the same previous files when the leader was
# served those (pin_pair returns within what is left of the budget, so no permit), or answer 503 when it failed: one
# wait for room per pair, never one after another. A request waiting for room in a full cache therefore never holds a
# thread that a request holding pinned files needs to finish and release them. Waiting for a permit or for the leader
# ends after COLD_WAIT_S with a 503; a thread hop is never cancelled (its pinned files would never be released).
COLD_WAIT_S = 20.0
_COLD: dict[asyncio.AbstractEventLoop, tuple[asyncio.Semaphore, dict]] = {}
_COLD_LOCK = threading.Lock()


def _cold_state() -> tuple[asyncio.Semaphore, dict]:
    loop = asyncio.get_running_loop()
    with _COLD_LOCK:                                # test clients run loops in threads: never iterate while inserting
        state = _COLD.get(loop)
        if state is None:
            for old in [lp for lp in _COLD if lp.is_closed()]:
                del _COLD[old]
            state = _COLD[loop] = (asyncio.Semaphore(cold_loads_per_loop()), {})
    return state


async def _pin_cold(access, team_param: str | None, timing: Timing):
    since = time.perf_counter()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COLD_WAIT_S
    gate, flights = _cold_state()
    snap = access.snapshot
    key = (access.league_id, snap.data_version, snap.global_version)
    if league_file_cache().has_cached_fallback(*key):
        # The league's previous files are cached: pin_pair serves them within its budget (or the new pair, if it
        # arrives in time) and never waits for room, so it needs no permit and never queues behind other downloads.
        return await run_in_threadpool(_pin, access, team_param, timing, since=since)

    async def within(aw):
        try:
            return await asyncio.wait_for(aw, max(0.0, deadline - loop.time()))
        except TimeoutError:
            timing.add("files", time.perf_counter() - since, "slow")
            raise HTTPException(503, FILES_UNAVAILABLE) from None

    while (leader := flights.get(key)) is not None:
        outcome = await within(asyncio.shield(leader))  # "loaded", "degraded", "failed", or None (leader cancelled)
        if outcome == "failed":
            timing.add("files", time.perf_counter() - since, "error")
            raise HTTPException(503, FILES_UNAVAILABLE)  # a constant: nothing of the leader's request crosses over
        if outcome == "degraded":
            return await run_in_threadpool(_pin, access, team_param, timing, since=since)
        found = await run_in_threadpool(_pin, access, team_param, timing, wait=False, since=since)
        if found is not None:
            return found                                # else evicted again since: lead a load of its own
    done = flights[key] = loop.create_future()
    outcome = None
    try:
        await within(gate.acquire())
        try:
            found = await run_in_threadpool(_pin, access, team_param, timing, since=since)
        finally:
            gate.release()
        outcome = "degraded" if found[1].degraded else "loaded"
        return found
    except HTTPException:
        outcome = "failed"
        raise
    finally:
        if flights.get(key) is done:
            del flights[key]
        done.set_result(outcome)


# ---- per-instance response cache ------------------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResponseCache:
    """Successful GET responses keyed by (scope cache key, path, sorted query): league, data and global versions,
    visibility epoch and perspective are all in the scope key, so two leagues, two versions or two perspectives never
    share an entry. Looked up only after authorization (inside `league_scope`). Bounded by bytes, LRU.

    Bounded in time too: an entry is served until `valid_until` (the next kickoff, see `next_kickoff`) and never longer
    than `ttl` after it was stored, whether or not the caller passes a `valid_until`. `clock` (aware UTC) is injectable
    so tests can move time; an expired entry is dropped when it is looked up and whenever an answer is stored."""

    def __init__(self, max_bytes: int, *, ttl: timedelta = RESPONSE_TTL, clock=_utc_now):
        self.max_bytes, self.ttl, self.clock = max_bytes, ttl, clock
        self._lock = threading.Lock()
        # key -> (status, body, media type, expires at)
        self._entries: OrderedDict[str, tuple[int, bytes, str, datetime]] = OrderedDict()
        self._flights: dict[str, tuple[Any, asyncio.Event]] = {}                 # key -> (event loop, done)
        self.hits = 0
        self.shared = 0              # answers a request received from an identical request it waited for
        self.expired = 0             # entries dropped because their time bound passed

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(len(e[1]) for e in self._entries.values())

    def statuses(self) -> list[int]:
        with self._lock:
            return [e[0] for e in self._entries.values()]

    def get(self, key: str):
        """(status, body, media type), or None when absent or past its time bound (then it is dropped)."""
        now = self.clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            if now >= hit[3]:
                del self._entries[key]
                self.expired += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return hit[:3]

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

    def put(self, key: str, status: int, body: bytes, media_type: str, valid_until: datetime | None = None) -> None:
        """Store an answer until `valid_until`, capped at `ttl` from now. An answer whose bound has already passed (a
        kickoff came while it was computed) is not stored."""
        if status != 200 or len(body) > self.max_bytes // 4:          # one answer never takes most of the cache
            return
        now = self.clock()
        expires = now + self.ttl if valid_until is None else min(valid_until, now + self.ttl)
        if expires <= now:
            return
        with self._lock:
            for old_key in [k for k, e in self._entries.items() if now >= e[3]]:   # expired: never holds room
                del self._entries[old_key]
                self.expired += 1
            self._entries[key] = (status, body, media_type, expires)
            self._entries.move_to_end(key)
            used = sum(len(e[1]) for e in self._entries.values())
            while self._entries and used > self.max_bytes:
                _, old = self._entries.popitem(last=False)
                used -= len(old[1])


def next_kickoff(scope: LeagueScope, now: datetime) -> datetime | None:
    """The first scheduled NFL kickoff after `now` in the global file this league's answer was read from, or None
    (no schedule, no kickoff times, or none ahead). Every game type counts: an extra boundary only costs a recompute.
    One small query per cache miss, in the thread pool while the request holds the pair pinned, over the rows dated
    from a day before `now` on. Rows are selected by date, never by `season`: a season label need not match its
    calendar dates (a season's playoffs run into the next year; synthetic schedules date future weeks far ahead)."""
    from ..analytics.trade_data import kickoff_utc
    con = layered_databases().cursor(scope.league_path, scope.global_path, perspective=scope.perspective)
    try:
        # gameday is the US Eastern date: a day of margin keeps a late game (after midnight UTC) in view.
        rows = con.execute("SELECT DISTINCT gameday, gametime FROM gl.main.nfl_schedules "
                           "WHERE CAST(gameday AS VARCHAR) >= ?", [(now - timedelta(days=1)).date().isoformat()]).fetchall()
    except (duckdb.CatalogException, duckdb.BinderException):     # no schedule, or one without kickoff columns
        return None
    finally:
        con.close()
    return min((k for gd, gt in rows if (k := kickoff_utc(gd, gt)) is not None and k > now), default=None)


def _valid_until(scope: LeagueScope, now: datetime) -> datetime | None:
    try:
        return next_kickoff(scope, now)
    except Exception as err:   # noqa: BLE001 - an unreadable schedule leaves the RESPONSE_TTL cap as the bound
        log.warning("next kickoff unavailable: %s", type(err).__name__)
        return None


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
    # The bound is taken before the handler runs: a clock read by the handler is never earlier than this one.
    request.state.fi_valid_until = await run_in_threadpool(_valid_until, scope, cache.clock())
    request.state.fi_cache_key = key


async def league_scope(request: Request, league_id: str = Depends(valid_league_id),
                       principal=Depends(current_principal)):
    """Async on purpose: the ContextVar is set in the request's own context, which FastAPI copies into the thread
    pool that runs the (sync) legacy handler. The blocking work runs in the pool."""
    timing = request_timing(request) or Timing()
    team_param = request.query_params.get("team_id")
    access, found = await run_in_threadpool(_resolve_if_cached, principal, league_id, team_param, timing)
    scope, pair = found if found is not None else await _pin_cold(access, team_param, timing)
    request.state.fi_scope = scope
    request.state.fi_degraded = pair.degraded
    try:
        await _lookup(request, scope, timing)
    except BaseException:                          # a cache hit, or a cancelled wait: unpin before leaving
        league_file_cache().release_pair(pair)
        raise
    # Released at the end of the request, or earlier by a handler done with its files (deps.release_league_files).
    held = HeldFiles(lambda: league_file_cache().release_pair(pair))
    token, files_token = LEAGUE_SCOPE.set(scope), LEAGUE_FILES.set(held)
    try:
        yield scope
    finally:
        try:
            LEAGUE_FILES.reset(files_token)
            LEAGUE_SCOPE.reset(token)
        finally:
            held.release()                         # even if a reset raised: a leaked pin fills the file cache


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
            response_cache().put(key, response.status_code, body, response.media_type or "application/json",
                                 getattr(request.state, "fi_valid_until", None))
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

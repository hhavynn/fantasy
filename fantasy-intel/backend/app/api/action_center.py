"""All-leagues action center (issue #166): a signed-in user's critical decisions across their leagues, one section per
league, without redoing any per-league analysis.

Authorization comes first and happens in the /v2 route (leagues_v2.action_center): it enumerates the caller's own
leagues (memberships and pins, `mutations.list_my_leagues`) and keeps only what `authorize_leagues_in` grants, exactly
as GET /v2/leagues does. No league id is ever taken from the client, so a league the caller cannot see is never named,
opened or timed here. This module then evaluates each granted league on its own:

  * no published snapshot -> "not_ready" (the sync state says why): never another league's data, never a clean roster;
  * no saved team, or a saved team the data does not have -> "needs_team": the engines would otherwise read the
    league's first team;
  * otherwise the league's files are pinned exactly as `league_scope` pins them (the #201 cold-load budget: when the
    newest files are slow, the league's previous files are read and the section is marked `stale`), LEAGUE_SCOPE is set
    for the worker thread, and the existing handlers run unchanged: /api/overview (the roster with its availability
    flags), /api/start-sit (lock-aware moves, each player's kickoff) and /api/league-rules (the lineup-lock rule). Their
    signal-provenance.v1 blocks pass through untouched.

The page turns these payloads into decisions with the Overview's own contract (lib/weekly-decisions.ts), so nothing here
ranks, scores or recommends. Fan-out is capped (MAX_LEAGUES evaluated, the rest listed in `more`), bounded (CONCURRENCY
of a request's leagues at once on one process-wide pool of POOL_WORKERS threads, BUDGET_S overall: a league still
running then is reported as not ready, never dropped; files not already here are fetched in the background, never in a
pool thread), admitted (one build per user at a time, at most POOL_WORKERS // CONCURRENCY in the process, a short wait for a slot,
else 503) and cached per (user,
league, data and global versions, visibility epoch, perspective, provider) for at most CACHE_TTL_S and never past the
next kickoff it saw. Nothing here contacts a provider or queues a sync.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from ..config import get_settings
from . import provider_copy
from .deps import LEAGUE_SCOPE, LeagueScope

log = logging.getLogger(__name__)

CONTRACT = "action-center.v1"
MAX_LEAGUES = 8              # leagues evaluated per request; the rest are listed, not opened
CONCURRENCY = 3              # leagues evaluated at once
BUDGET_S = 20.0              # the whole fan-out; a league not done by then is "not_ready" (still loading)
CACHE_TTL_S = 120.0
ERROR_TTL_S = 15.0           # a section with a deterministic error (4xx, data schema): kept briefly, re-read soon
CACHE_MAX_ENTRIES = 256

NOT_SYNCED = {
    "first_sync_queued": "The first sync is queued.",
    "first_sync_running": "The first sync is running.",
    "first_sync_failed": "The first sync failed.",
    "no_data": "This league has no synced data yet.",
}
NO_TEAM = "No team is saved for you in this league, so there is no lineup to check."
TEAM_GONE = "Your saved team is not in this league's synced data. Choose your team again."
FILES_UNAVAILABLE = "This league's data could not be loaded. Try again shortly."
TOO_SLOW = "This league's data is still loading. Reload in a moment."
PROVIDER_OFF = "This league's provider is switched off in this deployment."
UNREADABLE = "This league's data could not be read."
_OK_DETAIL = 200


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return min(hi, max(lo, value))


def limits() -> tuple[int, int, float]:
    """(max leagues, concurrency, budget seconds): bounded whatever the environment says."""
    return (int(_env_number("ACTION_CENTER_MAX_LEAGUES", MAX_LEAGUES, 1, 25)),
            int(_env_number("ACTION_CENTER_CONCURRENCY", CONCURRENCY, 1, 6)),
            _env_number("ACTION_CENTER_BUDGET_S", BUDGET_S, 1.0, 60.0))


# ---- the bounded per-user cache -------------------------------------------------------------------------------------

class SummaryCache:
    """Evaluated league sections, keyed by everything that changes one: the user, the league, its data and global
    versions, visibility epoch, perspective and provider. Never shared between users; looked up only after the route has
    authorized the league for this request. LRU-bounded by entries; an entry expires after `ttl_s` or once `valid_until`
    (the next kickoff it saw: lock states change then) has passed."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, ttl_s: float = CACHE_TTL_S, clock=time.monotonic):
        self.max_entries, self.ttl_s, self.clock = max_entries, ttl_s, clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[float, datetime | None, dict]] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: tuple, now: datetime) -> dict | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            expires, valid_until, value = hit
            if self.clock() >= expires or (valid_until is not None and now >= valid_until):
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: tuple, value: dict, valid_until: datetime | None, ttl_s: float | None = None) -> None:
        with self._lock:
            self._entries[key] = (self.clock() + (self.ttl_s if ttl_s is None else ttl_s), valid_until, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)


_CACHE_LOCK = threading.Lock()
_CACHES: dict[str, SummaryCache] = {}


def summary_cache() -> SummaryCache:
    with _CACHE_LOCK:
        cache = _CACHES.get("cache")
        if cache is None:
            cache = _CACHES["cache"] = SummaryCache()
        return cache


# ---- one league -------------------------------------------------------------------------------------------------------

def league_json(access) -> dict:
    """What GET /v2/leagues already tells this caller about the league (authz.Access.to_json), nothing more."""
    return access.to_json()


def _section(access, state: str, reason: str | None = None, **extra) -> dict:
    return {"league": league_json(access), "state": state, "reason": reason, "stale": False, "degraded": None,
            "versions": None, "team": None, "week": None, "overview": None, "start_sit": None, "lock": None,
            "errors": {}, **extra}


def _detail(err: HTTPException) -> str:
    return err.detail if isinstance(err.detail, str) and err.detail else UNREADABLE


def deterministic(err: HTTPException) -> bool:
    """An answer the same files will give again: a 4xx (the offseason's 422 "outside valid fantasy season") or the
    league file's schema being too old (a 503 that names the schema). Anything else (a 5xx, a busy database) is
    transient and is never cached."""
    return 400 <= err.status_code < 500 or (err.status_code == 503 and "schema" in _detail(err).lower())


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


_ROSTER_KEYS = ("espn_player_id", "full_name", "position", "pro_team", "lineup_slot", "is_starter", "on_bye",
                "availability", "injury_status")
_OVERVIEW_LEAGUE_KEYS = ("name", "season", "current_week")


def _trim_overview(ov: dict) -> dict:
    team = ov.get("team") or {}
    return {
        "league": {k: (ov.get("league") or {}).get(k) for k in _OVERVIEW_LEAGUE_KEYS},
        "team": {"team_id": team.get("team_id"), "name": team.get("name")},
        "roster": [{k: p.get(k) for k in _ROSTER_KEYS} for p in ov.get("roster") or []],
        "provenance": ov.get("provenance"),
    }


def _trim_start_sit(ss: dict) -> dict:
    summary = ss.get("summary") or {}
    return {
        "scoring_period": ss.get("scoring_period"),
        "is_future_preview": ss.get("is_future_preview"),
        "projection_source": ss.get("projection_source"),
        "summary": {k: summary.get(k) for k in ("starters_count", "status", "projection_complete")},
        "decisions": ss.get("decisions") or [],
        "schedule_warnings": ss.get("schedule_warnings") or [],
        "provenance": ss.get("provenance"),
    }


def lock_summary(ss: dict, rule: dict | None) -> dict:
    """Each rostered player's lock state and kickoff as Start/Sit computed them (IR players cannot move into a lineup and
    are left out), the earliest kickoff still ahead, and the league's lineup-lock rule as synced. Nothing is inferred:
    a player whose lock state is unknown is counted as such."""
    players: dict[str, dict] = {}
    starters = {"unlocked": 0, "locked": 0, "unknown": 0, "no_game": 0}
    for row in ss.get("actual_lineup") or []:
        pid = row.get("player_id")
        status = row.get("lock_status")
        key = {"UNLOCKED": "unlocked", "LOCKED": "locked", "BYE": "no_game", "TEAMLESS": "no_game"}.get(status, "unknown")
        starters[key] += 1
        if pid is not None:
            players[str(pid)] = {"lock_status": status, "kickoff_utc": row.get("kickoff_utc")}
    for row in ss.get("current_bench") or []:
        pid = row.get("player_id")
        if pid is not None and not row.get("on_ir"):
            players.setdefault(str(pid), {"lock_status": row.get("lock_status"), "kickoff_utc": row.get("kickoff_utc")})
    ahead = sorted(k for p in players.values() if p["lock_status"] == "UNLOCKED"
                   and (k := _parse_utc(p["kickoff_utc"])) is not None)
    return {"next_kickoff_utc": ahead[0].isoformat() if ahead else None, "starters": starters, "players": players,
            "rule": rule}


def _read(scope: LeagueScope, team: int) -> dict:
    """The three existing handlers for this league and team, each failing on its own."""
    from .league_rules import league_rules
    from .routes import overview
    from .start_sit import get_start_sit
    out: dict[str, Any] = {"overview": None, "start_sit": None, "lock": None, "errors": {}, "transient": False}
    try:
        ov = overview(team_id=team)
    except HTTPException as err:
        if err.status_code == 404:
            return {**out, "team_gone": True}
        out["errors"]["overview"] = _detail(err)
        out["transient"] |= not deterministic(err)
    else:
        if ov.get("team") is None:
            return {**out, "team_gone": True}
        out["overview"] = ov
    try:
        ss = get_start_sit(team_id=team, week=None, source=None)
    except HTTPException as err:
        if err.status_code == 404:
            return {**out, "team_gone": True}
        out["errors"]["start_sit"] = _detail(err)
        out["transient"] |= not deterministic(err)
        ss = None
    rule = None
    try:
        rule = league_rules().get("lineup_lock")
    except HTTPException as err:
        out["errors"]["league_rules"] = _detail(err)     # the lock rule is then unknown
        out["transient"] |= not deterministic(err)
    if ss is not None:
        out["start_sit"] = ss
        out["lock"] = lock_summary(ss, rule)
    return out


def _pin_without_download(files, league_id: str, data_version: str, global_version: str, budget_s: float):
    """The league's pinned pair, or None. Never a download in this (pool) thread: files already here are pinned; with
    an older pair to fall back on, `pin_pair` waits at most `budget_s`; otherwise the pair is fetched on the cache's own
    bounded background threads and this thread waits for that fetch at most `budget_s`, then looks once more."""
    pair = files.pin_pair(league_id, data_version, global_version, budget_s=budget_s, blocking=False)
    if pair is not None:
        return pair
    fetch = files.prefetch(league_id, data_version, global_version)     # the fetch pin_pair started (or None: busy)
    if fetch is None:
        return None
    try:
        fetch.result(timeout=budget_s)
    except Exception:  # noqa: BLE001 - slow (TimeoutError) or failed: the league is "still loading" this time
        return None
    return files.pin_pair(league_id, data_version, global_version, budget_s=budget_s, blocking=False)


def _cache_key(principal, access, team: int) -> tuple:
    snap = access.snapshot
    return (principal.user_id, access.league_id, snap.data_version, snap.global_version, access.visibility_epoch, team,
            access.provider)


def evaluate_league(principal, access, now: datetime) -> dict:
    """One league's section. Runs in a worker thread and never raises: anything unexpected is that league's own
    "unavailable" state (only the exception type is logged), so one league can never fail the whole response."""
    try:
        return _evaluate(principal, access, now)
    except Exception as err:  # noqa: BLE001
        log.warning("action center: league %s failed: %s", getattr(access, "league_id", "?"), type(err).__name__)
        return _section(access, "unavailable", UNREADABLE)


def _evaluate(principal, access, now: datetime) -> dict:
    from ..db.league_conn import perspective_team
    from ..db.league_split import VERSION_RE
    from .league_scope import cold_load_budget_s, league_file_cache
    if access.snapshot is None:
        return _section(access, "not_ready", NOT_SYNCED.get(access.sync_state, NOT_SYNCED["no_data"]))
    if access.provider == "sleeper" and get_settings().sleeper_source == "disabled":
        return _section(access, "unavailable", PROVIDER_OFF)
    team = perspective_team(access.perspective_team_id)
    if team is None:
        return _section(access, "needs_team", NO_TEAM)
    snap = access.snapshot
    if not VERSION_RE.fullmatch(snap.data_version) or not VERSION_RE.fullmatch(snap.global_version):
        return _section(access, "unavailable", FILES_UNAVAILABLE)
    key = _cache_key(principal, access, team)
    cache = summary_cache()
    hit = cache.get(key, now)
    if hit is not None:
        return {**hit, "league": league_json(access)}
    files = league_file_cache()
    try:
        pair = _pin_without_download(files, access.league_id, snap.data_version, snap.global_version,
                                     cold_load_budget_s())
    except Exception as err:  # noqa: BLE001 - R2 errors, a missing object, a full cache: that league's state only
        log.warning("action center: files unavailable for %s: %s", access.league_id, type(err).__name__)
        return _section(access, "unavailable", FILES_UNAVAILABLE)
    if pair is None:
        return _section(access, "not_ready", TOO_SLOW)
    try:
        scope = LeagueScope(access.league_id, pair.league_path, pair.global_path, pair.data_version, pair.global_version,
                            access.visibility_epoch, team, access.provider)
        token = LEAGUE_SCOPE.set(scope)
        try:
            read = _read(scope, team)
        finally:
            LEAGUE_SCOPE.reset(token)
    except Exception as err:  # noqa: BLE001 - an engine bug in one league must not take the others down
        log.warning("action center: league %s failed: %s", access.league_id, type(err).__name__)
        return _section(access, "unavailable", UNREADABLE)
    finally:
        files.release_pair(pair)
    if read.get("team_gone"):
        return _section(access, "needs_team", TEAM_GONE)
    ov, ss = read["overview"], read["start_sit"]
    if ov is None and ss is None:
        return _section(access, "unavailable", next(iter(read["errors"].values()), UNREADABLE))
    section = _section(
        access, "ready", None,
        stale=bool(pair.degraded), degraded=pair.degraded,
        versions={"data_version": pair.data_version, "global_version": pair.global_version},
        team={"team_id": team, "name": ((ov or {}).get("team") or {}).get("name")},
        week=(ss or {}).get("scoring_period") or ((ov or {}).get("league") or {}).get("current_week"),
        overview=_trim_overview(ov) if ov is not None else None,
        start_sit=_trim_start_sit(ss) if ss is not None else None,
        lock=read["lock"],
        errors=read["errors"],
    )
    if access.provider != "espn":                         # Sleeper's wording, as the league's own pages get it
        for part in ("overview", "start_sit", "lock", "errors"):
            section[part] = provider_copy.rewrite(section[part])
    # Never cached: a degraded read (fresh files may arrive) or a transient failure. A deterministic error is cached
    # briefly, so the offseason's 422 does not re-run every league on every request.
    if not pair.degraded and not read["transient"]:
        lock = section.get("lock") or {}
        cache.put(key, section, _parse_utc(lock.get("next_kickoff_utc")), ERROR_TTL_S if read["errors"] else None)
    return section


# ---- the whole response -------------------------------------------------------------------------------------------

_POOL_LOCK = threading.Lock()
_POOLS: dict[str, ThreadPoolExecutor] = {}
POOL_WORKERS = 6             # process-wide: every request's league evaluations share these threads
RETRY_AFTER_S = 5
BUSY = "Your leagues are being checked for other requests right now. Try again in a few seconds."


def worker_pool() -> ThreadPoolExecutor:
    """One pool per process, so concurrent requests cannot multiply the threads (or open league files) without bound."""
    with _POOL_LOCK:
        key = f"pool:{os.getpid()}"
        pool = _POOLS.get(key)
        if pool is None:
            pool = _POOLS[key] = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="action-center")
        return pool


async def run_bounded(items: list, fn, concurrency: int, budget: float, on_error=None) -> list:
    """`fn(item)` for every item on the shared pool, at most `concurrency` at once, for at most `budget` seconds; the
    results in order, None for an item still running (or never started) when the budget ran out. `fn` must not raise;
    if it does anyway, that item's result is `on_error(item)` (None without one). Awaiting holds no thread: the event
    loop waits. Shared by the action center and the portfolio (app/api/portfolio.py), so both stay on the one
    process-wide pool."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    results: list = [None] * len(items)
    pending = list(enumerate(items))
    running: dict[asyncio.Future, int] = {}
    pool = worker_pool()
    try:
        while pending or running:
            while pending and len(running) < concurrency:
                i, item = pending.pop(0)
                running[asyncio.wrap_future(pool.submit(fn, item))] = i
            left = deadline - loop.time()
            if left <= 0:
                break
            done, _ = await asyncio.wait(running, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                i = running.pop(fut)
                try:
                    results[i] = fut.result()
                except Exception as err:  # noqa: BLE001 - fn does not raise; a replacement still must not
                    log.warning("bounded fan-out: an item failed: %s", type(err).__name__)
                    results[i] = on_error(items[i]) if on_error is not None else None
    finally:
        # Also when the caller is cancelled mid-wait: a queued item never runs, a running one finishes and releases
        # its own files.
        for fut in running:
            fut.cancel()
    return results


async def build(principal, granted: list, now: datetime | None = None) -> tuple[dict, bool]:
    """(response body, whether any league was read from older files). `granted` is the authorized list, in the order
    GET /v2/leagues gives it; the first `max_leagues` are evaluated on the shared pool, at most `concurrency` at once,
    for at most `budget` seconds; the others are only listed."""
    now = now or datetime.now(timezone.utc)
    max_leagues, concurrency, budget = limits()
    evaluated, extra = granted[:max_leagues], granted[max_leagues:]
    results = await run_bounded(evaluated, lambda access: evaluate_league(principal, access, now), concurrency, budget,
                                on_error=lambda access: _section(access, "unavailable", UNREADABLE))
    out = [s if s is not None else _section(evaluated[i], "not_ready", TOO_SLOW) for i, s in enumerate(results)]
    body = {
        "contract": CONTRACT,
        "generated_at": now.isoformat(),
        "limit": max_leagues,
        "leagues": out,
        "more": {"count": len(extra), "leagues": [league_json(a) for a in extra]},
    }
    return body, any(s.get("stale") for s in out)


# ---- admission and single flight ------------------------------------------------------------------------------------

class Busy(Exception):
    """Every build slot of this process is taken: the route answers 503 with Retry-After."""


def admission_slots() -> int:
    return max(1, POOL_WORKERS // limits()[1])


_ADMISSION_LOCK = threading.Lock()
_ADMISSION: dict[str, Any] = {}
_FLIGHTS: dict[str, tuple[tuple, Future]] = {}      # user id (or kind:user id) -> (its authorized league ids, its build)
_HOLDERS: dict[str, Future] = {}                    # user id -> the build of whichever kind holds (or awaits) its slot


def _admission() -> threading.BoundedSemaphore:
    with _ADMISSION_LOCK:
        slots = admission_slots()
        sem = _ADMISSION.get("sem")
        if sem is None or _ADMISSION.get("slots") != slots:
            sem = _ADMISSION["sem"] = threading.BoundedSemaphore(slots)
            _ADMISSION["slots"] = slots
        return sem


ADMISSION_WAIT_S = 1.75     # how long a request waits for a free build slot before the 503
_POLL_S = 0.05


async def _acquire_slot(sem: threading.BoundedSemaphore, wait_s: float) -> bool:
    """A build slot within `wait_s`, polled on the event loop (never a blocking wait that would stall it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while not sem.acquire(blocking=False):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_POLL_S)
    return True


async def serve(principal, granted: list, *, kind: str = "action-center", build_fn=None,
                budget_s: float | None = None) -> tuple[dict, bool]:
    """`build`, with two process-wide guards (the pool is shared by every user):

      * one build per user: a request from a user whose build is running joins it when it is for exactly the same
        authorized leagues (its own authorization has already run), and otherwise waits for it to finish and then
        builds once, so one user never holds two slots. Flights are keyed by the verified user id: two users never
        share a build, whatever their leagues;
      * one slot per user across kinds: a user's build of another kind (the portfolio beside the action center) waits for
        the first to finish before it takes a slot, so one user can never hold every slot;
      * admission: at most POOL_WORKERS // CONCURRENCY builds at once in the process. A request waits up to
        ADMISSION_WAIT_S for a slot (on the event loop), then gets Busy (503 with Retry-After): never a long queue
        behind other users' leagues.

    `kind`, `build_fn` and `budget_s` let another all-leagues view (the portfolio, app/api/portfolio.py) share the
    pool's admission slots with its own single flight per (kind, user) and its own `build_fn(principal, granted)`."""
    ids = tuple(a.league_id for a in granted)
    user = principal.user_id if kind == "action-center" else f"{kind}:{principal.user_id}"
    build_fn = build_fn or build
    loop = asyncio.get_running_loop()
    give_up = loop.time() + (limits()[2] if budget_s is None else budget_s) + 10
    while True:
        with _ADMISSION_LOCK:
            flight = _FLIGHTS.get(user)
            if flight is None:
                mine: Future = Future()
                mine.set_running_or_notify_cancel()    # a running future cannot be cancelled by anyone who joins it
                _FLIGHTS[user] = (ids, mine)           # claimed before a slot: the user's other requests wait on it
                break
        same, theirs = flight[0] == ids, flight[1]
        left = give_up - loop.time()
        if left <= 0:
            raise Busy()
        try:
            # Shielded: this request going away never cancels the build other requests share.
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(theirs)), left)
        except Busy:
            raise
        except (Exception, TimeoutError):  # noqa: BLE001 - that build failed or ran long: look again
            if loop.time() >= give_up:
                raise Busy() from None
            continue
        if same:
            return result
        # Different leagues (a membership changed in between): that build is over; build our own next round.
    holder = False
    try:
        while True:
            with _ADMISSION_LOCK:
                other = _HOLDERS.get(principal.user_id)
                if other is None:
                    _HOLDERS[principal.user_id] = mine
                    holder = True
                    break
            left = give_up - loop.time()
            if left <= 0:
                raise Busy()
            if other.done():                           # finished, its holder entry about to go: look again shortly
                await asyncio.sleep(_POLL_S)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(other)), left)
            except (Exception, TimeoutError):  # noqa: BLE001 - that build failed or ran long: look again
                if loop.time() >= give_up:
                    raise Busy() from None
        sem = _admission()
        if not await _acquire_slot(sem, ADMISSION_WAIT_S):
            raise Busy()
        try:
            result = await build_fn(principal, granted)
        finally:
            sem.release()
        mine.set_result(result)
        return result
    except BaseException as err:
        # Joiners see Busy or a plain error, never this request's cancellation.
        mine.set_exception(err if isinstance(err, Exception) else RuntimeError("the shared build was cancelled"))
        raise
    finally:
        with _ADMISSION_LOCK:
            if _FLIGHTS.get(user, (None, None))[1] is mine:
                del _FLIGHTS[user]
            if holder and _HOLDERS.get(principal.user_id) is mine:
                del _HOLDERS[principal.user_id]

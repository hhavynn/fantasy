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
running then is reported as not ready, never dropped) and cached per (user,
league, data and global versions, visibility epoch, perspective, provider) for at most CACHE_TTL_S and never past the
next kickoff it saw. Nothing here contacts a provider or queues a sync.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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

    def put(self, key: tuple, value: dict, valid_until: datetime | None) -> None:
        with self._lock:
            self._entries[key] = (self.clock() + self.ttl_s, valid_until, value)
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
    out: dict[str, Any] = {"overview": None, "start_sit": None, "lock": None, "errors": {}}
    try:
        ov = overview(team_id=team)
    except HTTPException as err:
        if err.status_code == 404:
            return {**out, "team_gone": True}
        out["errors"]["overview"] = _detail(err)
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
        ss = None
    rule = None
    try:
        rule = league_rules().get("lineup_lock")
    except HTTPException:
        rule = None
    if ss is not None:
        out["start_sit"] = ss
        out["lock"] = lock_summary(ss, rule)
    return out


def _cache_key(principal, access, team: int) -> tuple:
    snap = access.snapshot
    return (principal.user_id, access.league_id, snap.data_version, snap.global_version, access.visibility_epoch, team,
            access.provider)


def evaluate_league(principal, access, now: datetime) -> dict:
    """One league's section. Runs in a worker thread; never raises (a failure is that league's own state)."""
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
        pair = files.pin_pair(access.league_id, snap.data_version, snap.global_version, budget_s=cold_load_budget_s())
    except Exception as err:  # noqa: BLE001 - R2 errors, a missing object, a full cache: that league's state only
        log.warning("action center: files unavailable for %s: %s", access.league_id, type(err).__name__)
        return _section(access, "unavailable", FILES_UNAVAILABLE)
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
    if not pair.degraded:                                # a degraded read is retried for fresh files next time
        lock = section.get("lock") or {}
        cache.put(key, section, _parse_utc(lock.get("next_kickoff_utc")))
    return section


# ---- the whole response -------------------------------------------------------------------------------------------

_POOL_LOCK = threading.Lock()
_POOLS: dict[str, ThreadPoolExecutor] = {}
POOL_WORKERS = 6             # process-wide: every request's league evaluations share these threads


def worker_pool() -> ThreadPoolExecutor:
    """One pool per process, so concurrent requests cannot multiply the threads (or open league files) without bound."""
    with _POOL_LOCK:
        key = f"pool:{os.getpid()}"
        pool = _POOLS.get(key)
        if pool is None:
            pool = _POOLS[key] = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="action-center")
        return pool


def _fan_out(principal, leagues: list, now: datetime, concurrency: int, budget: float) -> list[dict | None]:
    """Evaluate `leagues` on the shared pool with at most `concurrency` of this request's leagues in flight, until all
    are done or `budget` seconds have passed. A league not finished by then is None (the caller reports it); one not
    started is never started."""
    results: list[dict | None] = [None] * len(leagues)
    pending = list(enumerate(leagues))
    running: dict = {}
    deadline = time.monotonic() + budget
    pool = worker_pool()
    while pending or running:
        while pending and len(running) < concurrency:
            i, access = pending.pop(0)
            running[pool.submit(evaluate_league, principal, access, now)] = i
        left = deadline - time.monotonic()
        if left <= 0:
            break
        done, _ = wait(running, timeout=left, return_when=FIRST_COMPLETED)
        if not done:
            break
        for fut in done:
            results[running.pop(fut)] = fut.result()
    for fut in running:
        fut.cancel()                 # queued ones never run; a running one releases its files when it finishes
    return results


def build(principal, granted: list, now: datetime | None = None) -> tuple[dict, bool]:
    """(response body, whether any league was read from older files). `granted` is the authorized list, in the order
    GET /v2/leagues gives it; the first `max_leagues` are evaluated, the others only listed."""
    now = now or datetime.now(timezone.utc)
    max_leagues, concurrency, budget = limits()
    evaluated, extra = granted[:max_leagues], granted[max_leagues:]
    sections = _fan_out(principal, evaluated, now, concurrency, budget)
    out = [s if s is not None else _section(evaluated[i], "not_ready", TOO_SLOW) for i, s in enumerate(sections)]
    body = {
        "contract": CONTRACT,
        "generated_at": now.isoformat(),
        "limit": max_leagues,
        "leagues": out,
        "more": {"count": len(extra), "leagues": [league_json(a) for a in extra]},
    }
    return body, any(s.get("stale") for s in out)

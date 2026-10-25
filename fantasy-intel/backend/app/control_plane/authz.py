"""Server-side authorization for one league request (spec §2).

authorize_league runs as the request's Postgres role, so RLS already hides private leagues from non-members and
deleted leagues from everyone; the Python layer adds the rules RLS cannot express per row: public access is valid only
while the last anonymous visibility probe is younger than 24 h (fail closed), and the response carries the current
snapshot pointer so the read path never looks anything up a second time. Unknown and unauthorized are the same
LeagueNotFound; nothing here distinguishes them to the caller."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from typing import TYPE_CHECKING

from ..leagues.context import Principal

if TYPE_CHECKING:                                 # pg imports the Postgres driver; keep this module light
    from .pg import ControlPlane

PUBLIC_VISIBILITY_TTL = timedelta(hours=24)
# A queued job due, or a first run started, this long ago is stalled: four of the sync workflow's 15-minute runs
# (.github/workflows/sync-drain.yml, each capped at 25 minutes) plus margin.
STALL_AFTER = timedelta(hours=1)
_LEAGUE_ID = re.compile(r"lg_[A-Za-z0-9_-]{16,64}")


class LeagueNotFound(LookupError):
    """The caller may not see this league, or it does not exist. HTTP 404, never 403."""


def check_league_id(value: object) -> str:
    if not isinstance(value, str) or not _LEAGUE_ID.fullmatch(value):
        raise LeagueNotFound("unknown league")
    return value


@dataclass(frozen=True)
class SnapshotPointer:
    object_key: str
    data_version: str
    global_version: str
    committed_at: datetime


@dataclass(frozen=True)
class Access:
    league_id: str
    provider: str
    external_id: str
    season: int
    name: str | None
    visibility: str
    status: str
    visibility_epoch: int
    role: str | None
    pinned: bool
    perspective_team_id: str | None
    snapshot: SnapshotPointer | None
    sync_state: str
    principal: Principal
    sync_issue: str | None = None        # why the latest sync job failed, as a coarse category (_sync_issue)
    sync_stalled: bool = False           # the latest job is overdue by far more than the worker cadence (_sync_stalled)

    @property
    def is_member(self) -> bool:
        return self.role is not None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_json(self) -> dict:
        return {
            "league_id": self.league_id, "provider": self.provider, "external_league_id": self.external_id,
            "season": self.season, "name": self.name, "visibility": self.visibility, "status": self.status,
            "role": self.role, "pinned": self.pinned, "perspective_team_id": self.perspective_team_id,
            "sync_state": self.sync_state, "sync_issue": self.sync_issue, "sync_stalled": self.sync_stalled,
            "data": None if self.snapshot is None else {
                "data_version": self.snapshot.data_version, "global_version": self.snapshot.global_version,
                "committed_at": self.snapshot.committed_at.isoformat()},
        }


_BASE = """
SELECT l.id, l.provider, l.external_id, l.season, l.name, l.visibility, l.visibility_checked_at, l.visibility_epoch, l.status,
       {member_cols},
       s.object_key, s.data_version, s.global_version, s.committed_at,
       j.status AS job_status, j.error_code AS job_error, j.run_after AS job_run_after, j.started_at AS job_started_at,
       j.attempt AS job_attempt, now() AS db_now
FROM fi.leagues l
LEFT JOIN fi.league_snapshots s ON s.league_id = l.id
LEFT JOIN LATERAL (SELECT status, error_code, run_after, started_at, attempt FROM fi.sync_jobs x WHERE x.league_id = l.id
                   ORDER BY created_at DESC LIMIT 1) j ON true
{member_joins}
WHERE l.id = ANY(%s)
"""
# anon has no grants on the per-user tables, so its query must not touch them.
_SQL_ANON = _BASE.format(member_cols="NULL::text AS member_role, false AS pinned, NULL::text AS perspective",
                         member_joins="")
_SQL_USER = _BASE.format(
    member_cols="m.role AS member_role, (p.user_id IS NOT NULL) AS pinned, t.team_external_id AS perspective",
    member_joins="""
LEFT JOIN fi.league_memberships m ON m.league_id = l.id AND m.user_id = auth.uid() AND m.revoked_at IS NULL
LEFT JOIN fi.league_pins p ON p.league_id = l.id AND p.user_id = auth.uid()
LEFT JOIN fi.team_selections t ON t.league_id = l.id AND t.user_id = auth.uid()""")


def _sync_state(has_snapshot: bool, job_status: str | None) -> str:
    if has_snapshot:
        return "ready"
    return {"queued": "first_sync_queued", "running": "first_sync_running",
            "failed": "first_sync_failed", "dead": "first_sync_failed"}.get(job_status or "", "no_data")


# The worker's error codes (app/sync/failures.py, app/sync/worker.py) folded into what a reader can act on. Only the
# category leaves the API: never the code itself, and never error_detail (which request roles cannot read anyway).
_ISSUES = {
    "network": "provider_unreachable", "provider_429": "provider_unreachable", "provider_5xx": "provider_unreachable",
    "provider_response": "provider_unreachable", "probe_unknown": "provider_unreachable",
    "provider_auth": "access", "provider_private": "access", "no_credential": "access",
    "provider_not_found": "not_found",
    "provider_empty": "empty",
}


def _sync_issue(job_status: str | None, error_code: str | None) -> str | None:
    """Why the latest job is waiting to retry (`queued` after a failure) or gave up (`failed`, `dead`): provider_unreachable
    | access | not_found | internal. None while nothing went wrong."""
    if job_status not in ("queued", "failed", "dead") or not error_code:
        return None
    return _ISSUES.get(error_code, "internal")


def _sync_stalled(job_status: str | None, run_after, started_at, attempt, db_now) -> bool:
    """The latest job has waited far longer than the worker cadence allows (STALL_AFTER, against the database clock):
    - queued: due that long ago and no worker claimed it (a job waiting out its retry backoff is due in the future);
    - running its first attempt since that long ago: one drain run is capped well under it (sync-drain.yml).
    A later attempt keeps the first attempt's started_at (queue.claim), so its age says nothing: never called stalled."""
    if job_status == "queued" and run_after is not None:
        return db_now - run_after > STALL_AFTER
    if job_status == "running" and started_at is not None and attempt == 1:
        return db_now - started_at > STALL_AFTER
    return False


def _access_from_row(row, principal: Principal) -> Access | None:
    """The Access a row grants, or None when it grants nothing (the caller turns that into LeagueNotFound)."""
    (lid, provider, external_id, season, name, visibility, checked_at, epoch, status,
     member_role, pinned, perspective, object_key, data_version, global_version, committed_at, job_status, job_error,
     job_run_after, job_started_at, job_attempt, db_now) = row
    if visibility != "public" and member_role is None:
        return None                                      # RLS already hides this; defence in depth
    if member_role is None:                              # public reader: the last anonymous probe must be fresh
        if checked_at is None or db_now - checked_at > PUBLIC_VISIBILITY_TTL:
            return None                                  # Phase 3: trigger a probe here (<= 1 per league per 5 min)
    snapshot = None
    if object_key is not None:
        snapshot = SnapshotPointer(object_key, data_version, global_version, committed_at)
    return Access(lid, provider, external_id, int(season), name, visibility, status, int(epoch), member_role,
                  bool(pinned), perspective, snapshot, _sync_state(snapshot is not None, job_status), principal,
                  _sync_issue(job_status, job_error), _sync_stalled(job_status, job_run_after, job_started_at, job_attempt, db_now))


def authorize_leagues_in(con, principal: Principal, league_ids: list[str]) -> list[Access]:
    """Every league in `league_ids` the caller may see, in the given order, in one query. Malformed, unknown and
    unauthorized ids are simply absent."""
    ids = [i for i in league_ids if isinstance(i, str) and _LEAGUE_ID.fullmatch(i)]
    if not ids:
        return []
    sql = _SQL_USER if principal.is_authenticated else _SQL_ANON
    found = {}
    for row in con.execute(sql, [ids]).fetchall():
        access = _access_from_row(row, principal)
        if access is not None:
            found[access.league_id] = access
    return [found[i] for i in ids if i in found]


def authorize_league_in(con, principal: Principal, league_id: str) -> Access:
    league_id = check_league_id(league_id)
    sql = _SQL_USER if principal.is_authenticated else _SQL_ANON
    row = con.execute(sql, [[league_id]]).fetchone()
    access = _access_from_row(row, principal) if row is not None else None
    if access is None:
        raise LeagueNotFound("unknown league")
    return access


def authorize_league(plane: "ControlPlane", principal: Principal, league_id: str) -> Access:
    league_id = check_league_id(league_id)               # before any connection is opened
    with plane.request_tx(principal) as con:
        return authorize_league_in(con, principal, league_id)

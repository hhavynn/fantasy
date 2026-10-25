"""The drain's maintenance step, run once per drain before the workers start (spec §2 retention, §3.2 dispatch,
§3.7 recompute). Everything here only enqueues jobs or deletes objects whose retention ran out in Postgres
(fi.league_timers); it never decides on provider data.

    scheduled   an active league with a published version older than its refresh interval (1 h on NFL game days
                Thu/Sun/Mon US Eastern, 6 h otherwise), staggered by a few minutes per league
    probe       a public league whose anonymous visibility check is older than 6 h (spec §2 "Dynamic visibility")
    recompute   an active league whose version was built against an older global file than the current one
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import queue

GAME_DAYS = {3, 6, 0}                 # Thursday, Sunday, Monday (datetime.weekday())
PROBE_EVERY = timedelta(hours=6)
STAGGER_MIN = 10
EASTERN = ZoneInfo("America/New_York")


def refresh_interval(now: datetime | None = None) -> timedelta:
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    return timedelta(hours=1) if now.weekday() in GAME_DAYS else timedelta(hours=6)


def _stagger(league_id: str) -> timedelta:
    return timedelta(minutes=int(hashlib.sha256(league_id.encode()).hexdigest(), 16) % STAGGER_MIN)


_NO_OPEN_JOB = ("NOT EXISTS (SELECT 1 FROM fi.sync_jobs j WHERE j.league_id = l.id "
                "AND j.status IN ('queued', 'running'))")


def enqueue_due(con) -> dict:
    """Queue the scheduled refreshes, probes and recomputes that are due. Idempotent (keys collapse repeats)."""
    counts = {"scheduled": 0, "probe": 0, "recompute": 0}
    current = con.execute("SELECT global_version FROM fi.global_snapshot").fetchone()
    due = con.execute(f"""
        SELECT l.id FROM fi.leagues l JOIN fi.league_snapshots s ON s.league_id = l.id
        WHERE l.deleted_at IS NULL AND l.status = 'active' AND s.committed_at < now() - %s AND {_NO_OPEN_JOB}
        ORDER BY s.committed_at""", [refresh_interval()]).fetchall()
    probes = con.execute(f"""
        SELECT l.id FROM fi.leagues l
        WHERE l.deleted_at IS NULL AND l.visibility = 'public' AND l.status IN ('active', 'unavailable')
          AND (l.visibility_checked_at IS NULL OR l.visibility_checked_at < now() - %s) AND {_NO_OPEN_JOB}
        ORDER BY l.visibility_checked_at NULLS FIRST""", [PROBE_EVERY]).fetchall()
    stale = con.execute(f"""
        SELECT l.id FROM fi.leagues l JOIN fi.league_snapshots s ON s.league_id = l.id
        WHERE l.deleted_at IS NULL AND l.status = 'active' AND s.global_version <> %s AND {_NO_OPEN_JOB}""",
                        [current[0] if current else ""]).fetchall() if current else []
    con.rollback()
    scheduled = {r[0] for r in due}
    for (lid,) in due:
        counts["scheduled"] += queue.enqueue(con, lid, "scheduled", key=f"scheduled:{lid}", run_after=_stagger(lid)) is not None
    for (lid,) in probes:
        counts["probe"] += queue.enqueue(con, lid, "probe", key=f"probe:{lid}") is not None
    for (lid,) in stale:
        if lid not in scheduled:          # a refresh builds against the current global file anyway
            counts["recompute"] += queue.enqueue(con, lid, "recompute", key=f"recompute:{lid}") is not None
    return counts


def purge_expired(con, objects) -> list[str]:
    """Delete the objects of leagues whose retention ran out (fi.league_timers), then mark them purged."""
    due = [r[0] for r in con.execute("SELECT league_id FROM fi.league_timers()").fetchall()]
    con.commit()
    for lid in due:
        for prefix in (f"leagues/{lid}/", f"raw/{lid}/"):
            for key in objects.keys(prefix):
                objects.delete(key)
        con.execute("SELECT fi.league_purged(%s)", [lid])
        con.commit()
    return due


def prune_audit_log(con) -> int:
    """Delete audit rows older than a year (the privacy policy's retention; fi.prune_audit_log)."""
    n = con.execute("SELECT fi.prune_audit_log()").fetchone()[0]
    con.commit()
    return int(n)

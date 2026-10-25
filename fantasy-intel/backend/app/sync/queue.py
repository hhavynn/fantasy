"""The sync queue in Postgres (spec §3.3–3.4), used by the GitHub Actions workers as `fi_sync`.

claim: one transaction picks a due job (or a running job whose lease expired: reclaimed in place, the same row),
locks its league, takes a free provider slot, and increments the league's fence, which becomes the job's lease
token. The fence is the only thing that lets a worker publish (app/db/league_publish.commit_league); a superseded
worker's heartbeat and completion find a different token and do nothing.

Lock order follows the control plane's: the league row, then the league's own rows (here the job), then the slot.
Every function commits on the connection it is given.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

LEASE = timedelta(minutes=2)
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=20), timedelta(hours=1), timedelta(hours=3))
JITTER = 0.2
CANDIDATES = 8          # due jobs looked at per claim attempt (skipping locked or slot-less ones)
# Outcomes Postgres confirms only after failures spanning 6 h (spec §3.6: a credential's 401s, a league's 404s) keep
# retrying hourly past max_attempts, for about a day more, so the confirmation can happen at all.
EXTENDED_ATTEMPTS = 24
EXTENDED_DELAY = timedelta(hours=1)
PROVIDER_PAUSE = timedelta(minutes=5)


@dataclass(frozen=True)
class Claim:
    job_id: str
    league_id: str
    kind: str
    provider: str
    fence: int
    attempt: int
    max_attempts: int
    slot: int


def enqueue(con, league_id: str, kind: str, *, key: str, run_after: timedelta | None = None) -> str | None:
    """Queue a job unless one with the same idempotency key is queued or running. Returns the new job's id, or None
    when it collapsed into an existing one."""
    with con.transaction():
        row = con.execute("""
            INSERT INTO fi.sync_jobs (league_id, kind, idempotency_key, run_after)
            VALUES (%s, %s, %s, now() + %s)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running')
            DO NOTHING RETURNING id::text""", [league_id, kind, key, run_after or timedelta(0)]).fetchone()
    return row[0] if row else None


_DUE = """
    SELECT j.id::text, j.league_id, l.provider FROM fi.sync_jobs j JOIN fi.leagues l ON l.id = j.league_id
    WHERE l.deleted_at IS NULL AND (
          (j.status = 'queued' AND j.run_after <= now()
           AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs r WHERE r.league_id = j.league_id AND r.status = 'running'))
       OR (j.status = 'running' AND j.lease_expires_at < now()))
    ORDER BY j.run_after, j.created_at
    LIMIT %s"""


def claim(con, owner: str, *, providers: tuple[str, ...] = ("espn", "sleeper")) -> Claim | None:
    """Claim one due job, or return None when nothing is due or no provider slot is free."""
    full: set[str] = set()                      # providers with no free slot in this attempt
    for job_id, league_id, provider in con.execute(_DUE, [CANDIDATES]).fetchall():
        con.rollback()
        if provider in full or provider not in providers:
            continue
        with con.transaction():
            # Lock order: the league first (account deletion locks it before cascading to its jobs) ...
            if con.execute("SELECT 1 FROM fi.leagues WHERE id = %s AND deleted_at IS NULL FOR UPDATE SKIP LOCKED",
                           [league_id]).fetchone() is None:
                continue
            # ... then the job, re-checked under the lock: another worker may have taken it meanwhile.
            job = con.execute("""
                SELECT j.kind, j.attempt, j.max_attempts FROM fi.sync_jobs j WHERE j.id = %s AND (
                      (j.status = 'queued' AND j.run_after <= now()
                       AND NOT EXISTS (SELECT 1 FROM fi.sync_jobs r WHERE r.league_id = j.league_id AND r.status = 'running'))
                   OR (j.status = 'running' AND j.lease_expires_at < now()))
                FOR UPDATE SKIP LOCKED""", [job_id]).fetchone()
            if job is None:
                continue
            # Provider cap: row locks on the slots make it race-safe (counting running rows would not be).
            slot = con.execute("""
                SELECT slot_no FROM fi.provider_slots
                WHERE provider = %s AND (job_id IS NULL OR job_id = %s OR lease_expires_at < now())
                  AND (paused_until IS NULL OR paused_until < now())
                ORDER BY slot_no FOR UPDATE SKIP LOCKED LIMIT 1""", [provider, job_id]).fetchone()
            if slot is None:            # leaving the block commits a transaction that changed nothing
                full.add(provider)
                continue
            fence = con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                                [league_id]).fetchone()[0]
            con.execute("""
                UPDATE fi.sync_jobs SET status = 'running', lease_owner = %s, lease_token = %s,
                       lease_expires_at = now() + %s, heartbeat_at = now(), attempt = attempt + 1, provider_slot = %s,
                       started_at = coalesce(started_at, now()), error_code = NULL, error_detail = NULL
                WHERE id = %s""", [owner, fence, LEASE, slot[0], job_id])
            con.execute("UPDATE fi.provider_slots SET job_id = %s, lease_expires_at = now() + %s "
                        "WHERE provider = %s AND slot_no = %s", [job_id, LEASE, provider, slot[0]])
            return Claim(job_id, league_id, job[0], provider, fence, job[1] + 1, job[2], slot[0])
    con.rollback()
    return None


def heartbeat(con, c: Claim) -> bool:
    """Extend the lease and the slot. False: the lease is lost (reclaimed by another worker); abort, never publish."""
    with con.transaction():
        n = con.execute("""UPDATE fi.sync_jobs SET lease_expires_at = now() + %s, heartbeat_at = now()
                           WHERE id = %s AND lease_token = %s AND status = 'running'""", [LEASE, c.job_id, c.fence]).rowcount
        if n:
            con.execute("UPDATE fi.provider_slots SET lease_expires_at = now() + %s "
                        "WHERE provider = %s AND slot_no = %s AND job_id = %s", [LEASE, c.provider, c.slot, c.job_id])
    return n == 1


def _finish(con, c: Claim, sql: str, params: list) -> bool:
    with con.transaction():
        n = con.execute(sql + " WHERE id = %s AND lease_token = %s AND status = 'running'",
                        [*params, c.job_id, c.fence]).rowcount
        if n:
            con.execute("UPDATE fi.provider_slots SET job_id = NULL, lease_expires_at = NULL WHERE job_id = %s",
                        [c.job_id])
    return n == 1


def complete(con, c: Claim, *, global_version: str | None = None) -> bool:
    return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'succeeded', finished_at = now(), error_code = NULL,
                              error_detail = NULL, lease_expires_at = NULL, global_version = coalesce(%s, global_version)""",
                   [global_version])


def backoff(attempt: int) -> timedelta:
    base = BACKOFF[min(attempt, len(BACKOFF)) - 1]
    return base * (1 + random.uniform(-JITTER, JITTER))


def pause_provider(con, provider: str, for_: timedelta = PROVIDER_PAUSE) -> None:
    """Every worker stops claiming this provider's jobs for a while (it answered 429)."""
    with con.transaction():
        con.execute("UPDATE fi.provider_slots SET paused_until = greatest(coalesce(paused_until, now()), now() + %s) "
                    "WHERE provider = %s", [for_, provider])


def fail(con, c: Claim, code: str, *, retry: bool, detail: str | None = None, retry_after: timedelta | None = None,
         extended: bool = False) -> bool:
    """Record a failure. `retry`: queue the job again after the backoff for this attempt (or `retry_after`, e.g. a
    Retry-After), until max_attempts, then `dead`; `extended` keeps retrying hourly for EXTENDED_ATTEMPTS more (an
    outcome Postgres confirms only after 6 h). Not retryable: `dead` at once. `detail` must already be redacted
    (app/sync/redact.py)."""
    limit = c.max_attempts + (EXTENDED_ATTEMPTS if extended else 0)
    if retry and c.attempt < limit:
        delay = retry_after or (backoff(c.attempt) if c.attempt < c.max_attempts else EXTENDED_DELAY)
        return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'queued', run_after = now() + %s, error_code = %s,
                                  error_detail = %s, lease_expires_at = NULL, lease_owner = NULL""",
                       [delay, code, detail])
    return _finish(con, c, """UPDATE fi.sync_jobs SET status = 'dead', finished_at = now(), error_code = %s,
                              error_detail = %s, lease_expires_at = NULL""", [code, detail])

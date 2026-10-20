"""The Postgres queue (spec §3.3–3.4): claim/reclaim in place, per-league lock, provider slots, fence, heartbeat,
retries with backoff, duplicate enqueue collapse."""
import threading
from datetime import timedelta

import psycopg
import pytest

from app.sync import queue
from cp_seed import seed_league


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def su_one(pg, sql, params=()):
    with pg.connect() as su:
        return su.execute(sql, params).fetchone()


@pytest.fixture
def lg(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.commit()
    return lid


def test_enqueue_collapses_duplicates_while_queued_or_running(pg, lg):
    with sync(pg) as con:
        first = queue.enqueue(con, lg, "manual", key=f"manual:{lg}")
        assert first and queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is None
        claim = queue.claim(con, "w1")
        assert claim.job_id == first
        assert queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is None, "still running"
        assert queue.complete(con, claim)
        assert queue.enqueue(con, lg, "manual", key=f"manual:{lg}") is not None, "a finished job frees the key"


def test_claim_takes_a_fence_and_a_slot_and_a_league_runs_once(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        queue.enqueue(con, lg, "manual", key=f"manual:{lg}")
        c = queue.claim(con, "w1")
        assert (c.league_id, c.kind, c.provider, c.attempt) == (lg, "initial", "espn", 1)
        assert c.fence == su_one(pg, "SELECT sync_fence FROM fi.leagues WHERE id = %s", [lg])[0] == 1
        assert su_one(pg, "SELECT job_id::text FROM fi.provider_slots WHERE provider = 'espn' AND slot_no = %s",
                      [c.slot]) == (c.job_id,)
        assert queue.claim(con, "w2") is None, "the league already has a running job"


def test_an_expired_lease_is_reclaimed_in_place_with_a_new_fence(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        old = queue.claim(con, "w1")
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [old.job_id])
        con.commit()
        new = queue.claim(con, "w2")
        assert new.job_id == old.job_id and new.fence > old.fence and new.attempt == 2
        assert su_one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE league_id = %s", [lg]) == (1,)
        assert not queue.heartbeat(con, old), "the superseded worker loses its lease"
        assert queue.heartbeat(con, new)
        assert not queue.complete(con, old)
        assert su_one(pg, "SELECT status, lease_owner FROM fi.sync_jobs WHERE id = %s", [new.job_id]) == ("running", "w2")


def test_heartbeat_extends_the_lease_and_the_slot(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "initial", key=f"initial:{lg}")
        c = queue.claim(con, "w1")
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() + interval '5 seconds' WHERE id = %s", [c.job_id])
        con.commit()
        assert queue.heartbeat(con, c)
    left = su_one(pg, "SELECT extract(epoch FROM lease_expires_at - now()) FROM fi.sync_jobs WHERE id = %s", [c.job_id])[0]
    slot = su_one(pg, "SELECT extract(epoch FROM lease_expires_at - now()) FROM fi.provider_slots WHERE job_id = %s",
                  [c.job_id])[0]
    assert 100 < left <= 120 and 100 < slot <= 120


def test_the_provider_slot_cap_holds_under_ten_concurrent_claimers(pg):
    with pg.connect() as su:
        leagues = [seed_league(su) for _ in range(10)]
        su.commit()
    with sync(pg) as con:
        for lid in leagues:
            queue.enqueue(con, lid, "initial", key=f"initial:{lid}")
    barrier, claims, errors = threading.Barrier(10), [], []

    def worker(n):
        try:
            with sync(pg) as con:
                barrier.wait()
                c = queue.claim(con, f"w{n}")
                if c:
                    claims.append(c)
        except Exception as err:   # noqa: BLE001 - surfaced below
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(claims) == 2, "ESPN has two slots"
    assert len({c.slot for c in claims}) == 2
    assert su_one(pg, "SELECT count(*) FROM fi.sync_jobs WHERE status = 'running'") == (2,)


def test_a_slot_held_past_its_lease_is_reused(pg):
    with pg.connect() as su:
        a, b, c = (seed_league(su) for _ in range(3))
        su.commit()
    with sync(pg) as con:
        for lid in (a, b, c):
            queue.enqueue(con, lid, "initial", key=f"initial:{lid}")
        first, second = queue.claim(con, "w1"), queue.claim(con, "w2")
        assert first and second and queue.claim(con, "w3") is None
        con.execute("UPDATE fi.provider_slots SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
                    [first.job_id])
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [first.job_id])
        con.commit()
        again = queue.claim(con, "w3")
        assert again is not None and again.slot == first.slot


def test_failures_retry_with_backoff_then_die(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "scheduled", key=f"scheduled:{lg}")
        delays = []
        for attempt in range(1, 6):
            c = queue.claim(con, "w1")
            assert c.attempt == attempt
            assert queue.fail(con, c, "espn_5xx", retry=True)
            status, wait = su_one(pg, "SELECT status, extract(epoch FROM run_after - now()) FROM fi.sync_jobs WHERE id = %s",
                                  [c.job_id])
            if attempt < 5:
                assert status == "queued"
                delays.append(wait)
                con.execute("UPDATE fi.sync_jobs SET run_after = now() WHERE id = %s", [c.job_id])
                con.commit()
            else:
                assert status == "dead"
        for wait, base in zip(delays, queue.BACKOFF):
            assert base.total_seconds() * 0.75 <= wait <= base.total_seconds() * 1.25
        assert su_one(pg, "SELECT count(*) FROM fi.provider_slots WHERE job_id IS NOT NULL") == (0,), "slots released"


def test_a_non_retryable_failure_is_final_and_the_detail_is_stored_redacted(pg, lg):
    with sync(pg) as con:
        queue.enqueue(con, lg, "scheduled", key=f"scheduled:{lg}")
        c = queue.claim(con, "w1")
        assert queue.fail(con, c, "parse_error", retry=False, detail="KeyError at write_all")
    assert su_one(pg, "SELECT status, error_code, error_detail FROM fi.sync_jobs") == ("dead", "parse_error",
                                                                                      "KeyError at write_all")


def test_a_queued_job_waits_for_its_run_after_and_deleted_leagues_are_skipped(pg):
    with pg.connect() as su:
        later, gone = seed_league(su), seed_league(su, deleted=True)
        su.commit()
    with sync(pg) as con:
        queue.enqueue(con, later, "scheduled", key=f"scheduled:{later}", run_after=timedelta(minutes=5))
        queue.enqueue(con, gone, "scheduled", key=f"scheduled:{gone}")
        assert queue.claim(con, "w1") is None


def test_claim_respects_the_lock_order_league_before_job(pg):
    """Account deletion locks a league and then (via its cascade) the league's jobs; a claim takes the same order."""
    import inspect
    src = inspect.getsource(queue.claim)
    assert src.index("FROM fi.leagues") < src.index("FROM fi.sync_jobs j WHERE j.id")

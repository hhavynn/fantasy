"""Phase 3 queue support in Postgres: the global pointer, status timestamps, the §3.6 confirmation rules and the
retention timers (supabase/migrations/20261021120000_sync_queue.sql)."""
import uuid

import psycopg
import pytest

from cp_seed import U1, U2, as_role, seed_league, seed_membership, seed_snapshot, seed_user

CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384
GV = "20261021T120000Z-" + "a" * 24 + "-1000"


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


@pytest.fixture
def private(pg):
    """A private league with U1's credential and a running job leased with token 7."""
    cid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_user(su, U1)
        lid = seed_league(su, visibility="private")
        seed_membership(su, U1, lid, "admin")
        su.commit()
    with pg.connect(user="fi_api") as con, as_role(con, "authenticated", U1) as c:
        c.execute("SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')", [cid, lid, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        job = su.execute("""INSERT INTO fi.sync_jobs (league_id, kind, status, lease_token, lease_expires_at)
                            VALUES (%s, 'scheduled', 'running', 7, now() + interval '2 minutes') RETURNING id::text""",
                         [lid]).fetchone()[0]
        su.commit()
    return {"league": lid, "cid": cid, "job": job}


def age_first_failure(pg, cid, hours):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_credentials SET first_failed_at = now() - %s * interval '1 hour' WHERE id = %s",
                   [hours, cid])
        su.commit()


FAIL = "SELECT fi.record_credential_failure(%s, %s, %s, %s)"


def test_a_credential_is_invalid_only_after_three_failures_over_six_hours_with_a_control_success(pg, private):
    j, cid = private["job"], private["cid"]
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect", "3 failures but within 6 hours"
        con.commit()
    age_first_failure(pg, cid, 7)
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, False]).fetchone()[0] == "suspect", "ESPN unreachable: never confirm"
        assert con.execute(FAIL, [j, 6, cid, True]).fetchone()[0] is None, "a stale lease marks nothing"
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "invalid"
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == ("stale",)
        assert su.execute("SELECT count(*) FROM fi.league_credentials").fetchone() == (1,), "never deleted"


def test_a_success_resets_the_failure_count(pg, private):
    j, cid = private["job"], private["cid"]
    with sync(pg) as con:
        con.execute(FAIL, [j, 7, cid, True])
        con.execute(FAIL, [j, 7, cid, True])
        con.execute("SELECT fi.mark_credential(%s, 7, %s, 'active')", [j, cid])
        con.commit()
    age_first_failure(pg, cid, 7)
    with sync(pg) as con:
        assert con.execute(FAIL, [j, 7, cid, True]).fetchone()[0] == "suspect"
        con.commit()


MISSING = "SELECT fi.record_league_missing(%s, %s, %s)"


def test_a_league_is_unavailable_only_after_three_404s_over_six_hours_with_a_control_success(pg, private):
    j, lid = private["job"], private["league"]
    with sync(pg) as con:
        for _ in range(3):
            assert con.execute(MISSING, [j, 7, True]).fetchone()[0] == "active"
        con.commit()
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET missing_since = now() - interval '7 hours' WHERE id = %s", [lid])
        su.commit()
    with sync(pg) as con:
        assert con.execute(MISSING, [j, 7, False]).fetchone()[0] == "active"
        assert con.execute(MISSING, [j, 7, True]).fetchone()[0] == "unavailable"
        con.execute("SELECT fi.record_league_found(%s, %s)", [j, 7])
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT missing_count, missing_since FROM fi.leagues WHERE id = %s", [lid]).fetchone() == (0, None)


def test_status_changes_are_timestamped(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.execute("UPDATE fi.leagues SET status_changed_at = now() - interval '3 days' WHERE id = %s", [lid])
        su.execute("UPDATE fi.leagues SET name = 'x' WHERE id = %s", [lid])
        assert su.execute("SELECT status_changed_at < now() - interval '2 days' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,), "only a status change moves it"
        su.execute("UPDATE fi.leagues SET status = 'dormant' WHERE id = %s", [lid])
        assert su.execute("SELECT status_changed_at > now() - interval '1 minute' FROM fi.leagues WHERE id = %s",
                          [lid]).fetchone() == (True,)


def test_the_global_pointer_is_written_by_fi_sync_only(pg):
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET global_version = EXCLUDED.global_version, committed_at = now()",
                    [GV, "b" * 24])
        con.commit()
        assert con.execute("SELECT global_version FROM fi.global_snapshot").fetchone() == (GV,)
        with pytest.raises(psycopg.errors.CheckViolation):
            con.execute("INSERT INTO fi.global_snapshot (id, global_version, content_hash) VALUES (false, %s, %s)",
                        [GV, "b" * 24])
    with pg.connect(user="fi_api") as con:
        for role in ("anon", "authenticated"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(con, role, U1 if role != "anon" else None) as c:
                c.execute("SELECT * FROM fi.global_snapshot")


TIMERS = "SELECT league_id FROM fi.league_timers() ORDER BY 1"


def test_timers_mark_unwatched_public_leagues_dormant_and_wake_them_on_a_view(pg):
    with pg.connect() as su:
        seed_user(su, U2)
        old = seed_league(su)
        pinned = seed_league(su)
        fresh = seed_league(su)
        su.execute("UPDATE fi.leagues SET created_at = now() - interval '40 days' WHERE id IN (%s, %s)", [old, pinned])
        su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U2, pinned])
        su.commit()
    with sync(pg) as con:
        assert con.execute(TIMERS).fetchall() == []
        con.commit()
    with pg.connect() as su:
        status = dict(su.execute("SELECT id, status FROM fi.leagues").fetchall())
        assert (status[old], status[pinned], status[fresh]) == ("dormant", "active", "active")
        su.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [old])
        su.commit()
    with sync(pg) as con:
        con.execute(TIMERS)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [old]).fetchone() == ("active",)


def test_timers_return_leagues_whose_objects_are_due_for_deletion(pg):
    with pg.connect() as su:
        long_dormant = seed_league(su, status="dormant")
        recently_dormant = seed_league(su, status="dormant")
        stale = seed_league(su, visibility="private", status="stale")
        flipped = seed_league(su, visibility="private", status="unavailable")
        deleted = seed_league(su)
        for lid, days in ((long_dormant, 91), (recently_dormant, 10), (stale, 61), (flipped, 31)):
            su.execute("UPDATE fi.leagues SET status_changed_at = now() - %s * interval '1 day' WHERE id = %s", [days, lid])
        su.execute("UPDATE fi.leagues SET deleted_at = now() - interval '8 days' WHERE id = %s", [deleted])
        for lid in (long_dormant, recently_dormant, stale, flipped, deleted):
            seed_snapshot(su, lid)
        su.commit()
    with sync(pg) as con:
        due = {r[0] for r in con.execute(TIMERS).fetchall()}
        con.commit()
    assert due == {long_dormant, deleted}, "stale/flipped leagues are soft-deleted first and purged 7 days later"
    with pg.connect() as su:
        gone = dict(su.execute("SELECT id, deleted_at IS NOT NULL FROM fi.leagues").fetchall())
        assert gone[stale] and gone[flipped] and not gone[recently_dormant]
    with sync(pg) as con:
        con.execute("SELECT fi.league_purged(%s)", [long_dormant])
        con.commit()
        assert {r[0] for r in con.execute(TIMERS).fetchall()} == {deleted}, "purged once"
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshots WHERE league_id = %s", [long_dormant]).fetchone() == (0,)
        assert su.execute("SELECT objects_purged_at IS NOT NULL FROM fi.leagues WHERE id = %s",
                          [long_dormant]).fetchone() == (True,)


def test_a_private_league_with_a_live_credential_is_never_expired(pg, private):
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET status = 'stale' WHERE id = %s", [private["league"]])
        su.execute("UPDATE fi.leagues SET status_changed_at = now() - interval '90 days' WHERE id = %s", [private["league"]])
        su.commit()
    with sync(pg) as con:
        con.execute(TIMERS)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT deleted_at FROM fi.leagues WHERE id = %s", [private["league"]]).fetchone() == (None,)


@pytest.mark.parametrize("fn", ["record_credential_failure", "record_league_missing", "league_timers", "league_purged"])
def test_new_functions_are_for_fi_sync_only(pg, fn):
    with pg.connect() as con:
        grantees = {r[0] for r in con.execute(
            "SELECT grantee FROM information_schema.routine_privileges WHERE routine_schema = 'fi' AND routine_name = %s",
            [fn]).fetchall()}
    assert grantees - {"postgres"} == {"fi_sync"}, grantees


def test_record_credential_failure_locks_the_league_before_the_credential(pg):
    with pg.connect() as con:
        src = con.execute("SELECT pg_get_functiondef('fi.record_credential_failure(uuid,bigint,uuid,boolean)'::regprocedure)"
                          ).fetchone()[0]
    body = " ".join(src.split())
    lock = body.find("FROM fi.leagues")
    assert lock != -1 and "FOR UPDATE" in body[lock:lock + 200]
    assert lock < body.find("UPDATE fi.league_credentials")


def test_a_purged_league_that_wakes_up_gets_a_first_sync(pg):
    with pg.connect() as su:
        seed_user(su, U2)
        lid = seed_league(su, status="dormant")
        su.execute("UPDATE fi.leagues SET objects_purged_at = now(), status_changed_at = now() - interval '100 days' "
                   "WHERE id = %s", [lid])
        su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U2, lid])
        su.commit()
    with sync(pg) as con:
        con.execute(TIMERS)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status, objects_purged_at FROM fi.leagues WHERE id = %s", [lid]).fetchone() == ("active", None)
        assert su.execute("SELECT kind, status FROM fi.sync_jobs WHERE league_id = %s", [lid]).fetchall() == [("initial", "queued")]

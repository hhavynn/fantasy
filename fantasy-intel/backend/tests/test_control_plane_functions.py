"""SECURITY DEFINER functions: each one checks auth.uid() itself and refuses other users; the audit log only grows."""
import hashlib
import secrets
import uuid

import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from cp_seed import U1, U2, U3, as_role, seed_auth_user, seed_league, seed_membership, seed_user

TOKEN = secrets.token_bytes(16)
HASH = hashlib.sha256(TOKEN).digest()
CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384
STORE = "SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')::text"


@pytest.fixture
def league(pg):
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_user(con, U3)
        lid = seed_league(con, visibility="private")
        seed_membership(con, U1, lid, "admin")
        seed_membership(con, U2, lid, "viewer")
        con.commit()
    return lid


def call(con, role, uid, sql, params=()):
    with as_role(con, role, uid) as c:
        return c.execute(sql, params).fetchall()


def raises(con, role, uid, sql, params=(), match=None):
    with pytest.raises((RaiseException, InsufficientPrivilege), match=match):
        call(con, role, uid, sql, params)


def test_ensure_app_user_inserts_once_and_refuses_anonymous(pg):
    uid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_auth_user(su, uid)
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert call(con, "authenticated", uid, "SELECT fi.ensure_app_user()::text") == [(uid,)]
        assert call(con, "authenticated", uid, "SELECT fi.ensure_app_user()::text") == [(uid,)]
        assert call(con, "authenticated", uid, "SELECT count(*) FROM fi.app_users WHERE id = %s", [uid]) == [(1,)]
        raises(con, "anon", None, "SELECT fi.ensure_app_user()")


def test_only_admins_create_invites_and_ttl_is_capped(pg, league):
    with pg.connect(user="fi_api") as con:
        (invite_id,), = call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        assert uuid.UUID(str(invite_id))
        raises(con, "authenticated", U2, "SELECT fi.create_invite(%s, %s)", [league, hashlib.sha256(b"x").digest()])
        raises(con, "authenticated", U3, "SELECT fi.create_invite(%s, %s)", [league, hashlib.sha256(b"y").digest()])
        raises(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s, interval '8 days')",
               [league, hashlib.sha256(b"z").digest()], "ttl")
        assert call(con, "authenticated", U1, "SELECT count(*) FROM fi.league_invites WHERE league_id = %s", [league]) == [(1,)]
        with pytest.raises(InsufficientPrivilege):
            call(con, "authenticated", U1, "SELECT token_hash FROM fi.league_invites")


def test_redeem_is_single_use_signed_in_only_and_audited(pg, league):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        assert call(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH]) == [(league,)]
        assert call(con, "authenticated", U3, "SELECT role FROM fi.league_memberships WHERE league_id = %s", [league]) == [("viewer",)]
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH], "invite_invalid")      # used
        raises(con, "authenticated", U2, "SELECT fi.redeem_invite(%s)", [hashlib.sha256(b"nope").digest()], "invite_invalid")
        raises(con, "anon", None, "SELECT fi.redeem_invite(%s)", [HASH])
    with pg.connect() as su:
        actions = [r[0] for r in su.execute("SELECT action FROM fi.audit_log ORDER BY id")]
    assert actions == ["invite.create", "invite.redeem"]


def test_expired_and_revoked_invites_are_the_same_failure(pg, league):
    h2 = hashlib.sha256(b"two").digest()
    with pg.connect(user="fi_api") as con:
        (inv,), = call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, h2])
        call(con, "authenticated", U1, "SELECT fi.revoke_invite(%s)", [inv])
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH], "invite_invalid")
    with pg.connect() as su:
        su.execute("UPDATE fi.league_invites SET expires_at = now() - interval '1 minute' WHERE token_hash = %s", [h2])
        su.commit()
    with pg.connect(user="fi_api") as con:
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [h2], "invite_invalid")


def test_redeeming_never_grants_admin_and_a_revoked_member_comes_back_as_viewer(pg, league):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s", [U2])
        su.commit()
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        call(con, "authenticated", U2, "SELECT fi.redeem_invite(%s)", [HASH])
        assert call(con, "authenticated", U2, "SELECT role, revoked_at FROM fi.league_memberships WHERE league_id = %s",
                    [league]) == [("viewer", None)]


def test_admin_promotion_and_the_last_admin_rule(pg, league):
    with pg.connect(user="fi_api") as con:
        raises(con, "authenticated", U2, "SELECT fi.promote_admin(%s, %s)", [league, U2])     # viewers cannot
        raises(con, "authenticated", U1, "SELECT fi.demote_admin(%s, %s)", [league, U1], "last_admin")
        raises(con, "authenticated", U1, "SELECT fi.promote_admin(%s, %s)", [league, U3])     # not a member
        call(con, "authenticated", U1, "SELECT fi.promote_admin(%s, %s)", [league, U2])
        call(con, "authenticated", U2, "SELECT fi.demote_admin(%s, %s)", [league, U1])
        assert call(con, "authenticated", U1, "SELECT role FROM fi.league_memberships WHERE league_id = %s", [league]) == [("viewer",)]
        raises(con, "authenticated", U2, "SELECT fi.demote_admin(%s, %s)", [league, U2], "last_admin")


def test_store_credential_requires_membership_and_one_live_credential_per_league(pg, league):
    with pg.connect(user="fi_api") as con:
        c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
        raises(con, "authenticated", U3, STORE, [c1, league, CT, NONCE, WRAPPED])                 # stranger
        assert call(con, "authenticated", U2, STORE, [c1, league, CT, NONCE, WRAPPED]) == [(c1,)]    # viewer may
        raises(con, "authenticated", U1, STORE, [c2, league, CT, NONCE, WRAPPED], "credential_exists")
        assert call(con, "authenticated", U2, STORE, [c2, league, CT, NONCE, WRAPPED]) == [(c2,)]    # owner replaces
        raises(con, "anon", None, STORE, [c1, league, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        rows = su.execute("SELECT id::text, owner_user_id::text, status FROM fi.league_credentials").fetchall()
    assert rows == [(c2, U2, "active")]


def test_fi_api_itself_cannot_call_the_user_functions(pg, league):
    with pg.connect(user="fi_api") as con, pytest.raises(InsufficientPrivilege):
        con.execute(STORE, [str(uuid.uuid4()), league, CT, NONCE, WRAPPED])


def test_revoke_credential_is_owner_only_deletes_the_row_and_marks_the_league_stale(pg, league):
    cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U2, STORE, [cid, league, CT, NONCE, WRAPPED])
        raises(con, "authenticated", U1, "SELECT fi.revoke_credential(%s)", [league])
        call(con, "authenticated", U2, "SELECT fi.revoke_credential(%s)", [league])
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_credentials").fetchone() == (0,)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [league]).fetchone() == ("stale",)


@pytest.fixture
def leased_job(pg, league):
    cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U2, STORE, [cid, league, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        job = su.execute("""INSERT INTO fi.sync_jobs (league_id, kind, status, lease_token, lease_expires_at)
                            VALUES (%s, 'scheduled', 'running', 7, now() + interval '2 minutes') RETURNING id::text""",
                         [league]).fetchone()[0]
        su.commit()
    return {"job": job, "cid": cid}


def test_sync_credential_needs_the_current_lease(pg, league, leased_job):
    q = "SELECT credential_id::text, league_id, wrap_key_version FROM fi.sync_credential(%s, %s)"
    with pg.connect(user="fi_sync") as con:
        assert con.execute(q, [leased_job["job"], 7]).fetchall() == [(leased_job["cid"], league, "v1")]
        assert con.execute(q, [leased_job["job"], 6]).fetchall() == []
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [leased_job["job"]])
        assert con.execute(q, [leased_job["job"], 7]).fetchall() == []
        con.rollback()
    with pg.connect(user="fi_api") as con:
        with pytest.raises(InsufficientPrivilege):
            call(con, "authenticated", U2, q, [leased_job["job"], 7])


MARK = "SELECT fi.mark_credential(%s, %s, %s, %s)"


def test_mark_credential_transitions(pg, league, leased_job):
    with pg.connect(user="fi_sync") as con:
        def mark(status, token=7, cid=None):
            return con.execute(MARK, [leased_job["job"], token, cid or leased_job["cid"], status]).fetchone()[0]
        assert mark("suspect") is True
        assert mark("active") is True
        assert mark("suspect") is True and mark("invalid") is True
        assert mark("active", token=6) is False
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.league_credentials").fetchone() == ("invalid",)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [league]).fetchone() == ("stale",)


def test_audit_log_is_append_only_even_for_the_owner(pg, league):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
    with pg.connect() as su:
        for sql in ("UPDATE fi.audit_log SET action = 'x'", "DELETE FROM fi.audit_log"):
            with pytest.raises(RaiseException, match="append-only"):
                su.execute(sql)
            su.rollback()



def test_a_deleted_supabase_account_crypto_erases_its_credentials_and_memberships(pg, league):
    cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U2, STORE, [cid, league, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        su.execute("DELETE FROM auth.users WHERE id = %s", [U2])      # what Supabase does on account deletion
        su.commit()
        assert su.execute("SELECT count(*) FROM fi.league_credentials").fetchone() == (0,)
        assert su.execute("SELECT count(*) FROM fi.app_users WHERE id = %s", [U2]).fetchone() == (0,)
        assert su.execute("SELECT count(*) FROM fi.league_memberships WHERE user_id = %s", [U2]).fetchone() == (0,)


def test_a_token_for_a_deleted_account_cannot_recreate_its_user_row(pg, league):
    ghost = str(uuid.uuid4())                       # a still-valid JWT whose auth.users row is gone
    with pg.connect(user="fi_api") as con:
        raises(con, "authenticated", ghost, "SELECT fi.ensure_app_user()", match="sign_in_required")
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        raises(con, "authenticated", ghost, "SELECT fi.redeem_invite(%s)", [HASH], "sign_in_required")


def test_a_stale_worker_cannot_mark_the_credential_that_replaced_the_one_it_fetched(pg, league, leased_job):
    new_cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:            # the member re-enters cookies while the job runs
        call(con, "authenticated", U2, STORE, [new_cid, league, CT, NONCE, WRAPPED])
    with pg.connect(user="fi_sync") as con:
        assert con.execute(MARK, [leased_job["job"], 7, leased_job["cid"], "invalid"]).fetchone() == (False,)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT id::text, status FROM fi.league_credentials").fetchall() == [(new_cid, "active")]
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [league]).fetchone() == ("active",)


def test_a_lease_reclaimed_while_mark_credential_waits_is_honoured(pg, league, leased_job):
    """The reclaim holds the job row; mark_credential must wait for it and then see the new lease token."""
    import threading
    import time

    result = {}
    reclaimer = pg.connect()
    t = None
    try:
        reclaimer.execute("UPDATE fi.sync_jobs SET lease_token = 8 WHERE id = %s", [leased_job["job"]])  # locked, uncommitted

        def stale_worker():
            try:
                with pg.connect(user="fi_sync") as con:
                    result["marked"] = con.execute(MARK, [leased_job["job"], 7, leased_job["cid"], "invalid"]).fetchone()[0]
                    con.commit()
            except Exception as err:   # noqa: BLE001 - reported through `result`
                result["error"] = repr(err)

        t = threading.Thread(target=stale_worker)
        t.start()
        time.sleep(0.5)
        assert t.is_alive(), f"mark_credential must block on the job row while a reclaim holds it: {result}"
        reclaimer.commit()
    finally:
        reclaimer.rollback()      # never leave the row lock behind (the next test's reset would wait on it)
        reclaimer.close()
        if t is not None:
            t.join(10)
    assert result == {"marked": False}
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.league_credentials").fetchone() == ("active",)

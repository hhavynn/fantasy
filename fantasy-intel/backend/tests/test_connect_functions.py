"""Phase 3b Postgres functions: public import, private connect, the first private sync, manual refresh, probe on
expiry, admin delete (supabase/migrations/20261021130000_connect_flows.sql)."""
import uuid

import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from cp_seed import U1, U2, U3, as_role, seed_league, seed_membership, seed_snapshot, seed_user

CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384
IMPORT = "SELECT fi.import_public_league('espn', %s, 2026, 'Name')"
CONNECT = "SELECT fi.connect_private_league('espn', %s, 2026, 'Name', %s)"


def call(con, role, uid, sql, params=()):
    with as_role(con, role, uid) as c:
        return c.execute(sql, params).fetchall()


def refused(con, role, uid, sql, params=(), match=None):
    with pytest.raises((RaiseException, InsufficientPrivilege), match=match):
        call(con, role, uid, sql, params)


@pytest.fixture
def users(pg):
    with pg.connect() as su:
        for u in (U1, U2, U3):
            seed_user(su, u)
        su.commit()


def jobs(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT kind, status FROM fi.sync_jobs WHERE league_id = %s ORDER BY created_at", [lid]).fetchall()


def test_import_registers_once_pins_and_queues_one_first_sync(pg, users):
    with pg.connect(user="fi_api") as con:
        a = call(con, "authenticated", U1, IMPORT, ["123"])[0][0]
        b = call(con, "authenticated", U2, IMPORT, ["123"])[0][0]
        refused(con, "anon", None, IMPORT, ["124"])
    assert a == b and a.startswith("lg_")
    assert jobs(pg, a) == [("initial", "queued")]
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_pins WHERE league_id = %s", [a]).fetchone() == (2,)
        assert su.execute("SELECT visibility, visibility_checked_at > now() - interval '1 minute' FROM fi.leagues "
                          "WHERE id = %s", [a]).fetchone() == ("public", True)


def test_a_league_connected_privately_is_never_imported_as_public(pg, users):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, CONNECT, ["555", True])
        refused(con, "authenticated", U2, IMPORT, ["555"], match="league_unavailable")


def test_import_wakes_a_dormant_league(pg, users):
    with pg.connect() as su:
        lid = seed_league(su, external_id="777", status="dormant")
        su.execute("UPDATE fi.leagues SET objects_purged_at = now() WHERE id = %s", [lid])
        su.commit()
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, IMPORT, ["777"])
    with pg.connect() as su:
        assert su.execute("SELECT status, objects_purged_at FROM fi.leagues WHERE id = %s", [lid]).fetchone() == ("active", None)
    assert jobs(pg, lid) == [("initial", "queued")], "purged: no version left, so a first sync"


def test_imports_are_capped_per_account_per_day(pg, users):
    with pg.connect(user="fi_api") as con:
        for n in range(20):
            call(con, "authenticated", U1, IMPORT, [str(1000 + n)])
        refused(con, "authenticated", U1, IMPORT, ["2000"], match="league_quota")
        call(con, "authenticated", U2, IMPORT, ["2000"])


def test_private_connect_makes_the_first_user_admin_and_later_owners_viewers(pg, users):
    with pg.connect(user="fi_api") as con:
        lid = call(con, "authenticated", U1, CONNECT, ["888", False])[0][0]
        assert call(con, "authenticated", U2, CONNECT, ["888", True])[0][0] == lid
        refused(con, "authenticated", U3, CONNECT, ["888", False], match="not_a_league_member")
    with pg.connect() as su:
        roles = dict(su.execute("SELECT user_id::text, role FROM fi.league_memberships WHERE league_id = %s", [lid]).fetchall())
    assert roles == {U1: "admin", U2: "viewer"}


def test_private_connect_refuses_a_public_league(pg, users):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, IMPORT, ["999"])
        refused(con, "authenticated", U2, CONNECT, ["999", True], match="league_public")


def test_the_first_private_sync_needs_membership_and_a_live_credential(pg, users):
    with pg.connect(user="fi_api") as con:
        lid = call(con, "authenticated", U1, CONNECT, ["4321", False])[0][0]
        refused(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid], match="no_credential")
        call(con, "authenticated", U1, "SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')",
             [str(uuid.uuid4()), lid, CT, NONCE, WRAPPED])
        refused(con, "authenticated", U2, "SELECT fi.start_private_sync(%s)", [lid], match="league_unavailable")
        call(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid])
        call(con, "authenticated", U1, "SELECT fi.start_private_sync(%s)", [lid])
    assert jobs(pg, lid) == [("initial", "queued")]


def test_manual_refresh_is_for_members_or_public_and_at_most_every_fifteen_minutes(pg, users):
    with pg.connect() as su:
        pub, priv = seed_league(su), seed_league(su, visibility="private")
        seed_membership(su, U1, priv, "viewer")
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert call(con, "authenticated", U2, "SELECT fi.request_sync(%s)::text", [pub])[0][0]
        assert call(con, "authenticated", U3, "SELECT fi.request_sync(%s)::text", [pub])[0][0] is None, "already queued"
        refused(con, "authenticated", U2, "SELECT fi.request_sync(%s)", [priv], match="league_unavailable")
        assert call(con, "authenticated", U1, "SELECT fi.request_sync(%s)::text", [priv])[0][0]
        refused(con, "anon", None, "SELECT fi.request_sync(%s)", [pub])
    with pg.connect() as su:
        su.execute("UPDATE fi.sync_jobs SET status = 'succeeded' WHERE league_id = %s", [pub])
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert call(con, "authenticated", U2, "SELECT fi.request_sync(%s)::text", [pub])[0][0] is None, "ran < 15 min ago"


def test_an_expired_public_league_is_probed_once_per_five_minutes_and_nothing_is_revealed(pg):
    with pg.connect() as su:
        expired, fresh, private = (seed_league(su, checked_at="25 hours"), seed_league(su),
                                   seed_league(su, visibility="private", checked_at="25 hours"))
        su.commit()
    with pg.connect(user="fi_api") as con:
        answers = {str(call(con, "anon", None, "SELECT fi.request_probe(%s)", [lid]))
                   for lid in (expired, expired, fresh, private, "lg_" + "z" * 24)}
    assert len(answers) == 1, "the same answer whatever the league is"
    assert jobs(pg, expired) == [("probe", "queued")]
    assert jobs(pg, fresh) == [] and jobs(pg, private) == []


def test_only_an_admin_deletes_and_only_a_private_league(pg, users):
    with pg.connect() as su:
        priv, pub = seed_league(su, visibility="private"), seed_league(su)
        seed_membership(su, U1, priv, "admin")
        seed_membership(su, U2, priv, "viewer")
        seed_membership(su, U1, pub, "admin")
        su.commit()
    with pg.connect(user="fi_api") as con:
        refused(con, "authenticated", U2, "SELECT fi.delete_league(%s)", [priv], match="not_admin")
        refused(con, "authenticated", U1, "SELECT fi.delete_league(%s)", [pub], match="league_public")
        call(con, "authenticated", U1, "SELECT fi.delete_league(%s)", [priv])
    with pg.connect() as su:
        assert su.execute("SELECT deleted_at IS NOT NULL FROM fi.leagues WHERE id = %s", [priv]).fetchone() == (True,)
        assert su.execute("SELECT count(*) FROM fi.audit_log WHERE action = 'league.delete'").fetchone() == (1,)


def test_queue_initial_is_not_callable_by_request_roles(pg, users):
    with pg.connect() as su:
        lid = seed_league(su)
        seed_snapshot(su, lid)
        su.commit()
    with pg.connect(user="fi_api") as con:
        refused(con, "authenticated", U1, "SELECT fi.queue_initial(%s)", [lid])


@pytest.mark.parametrize("fn", ["import_public_league", "connect_private_league", "start_private_sync", "request_sync",
                                "delete_league"])
def test_new_functions_lock_the_account_before_the_league(pg, fn):
    with pg.connect() as con:
        src = con.execute("SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                          "WHERE n.nspname = 'fi' AND p.proname = %s", [fn]).fetchone()[0]
    body = " ".join(src.split())
    assert body.find("ensure_app_user") < body.find("FROM fi.leagues"), fn

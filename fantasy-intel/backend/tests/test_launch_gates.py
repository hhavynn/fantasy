"""Phase 6 launch gates: the server-side sign-up gate, the 25-league cap and account deletion, in SQL and through
the API, plus the whole deletion flow down to the league's objects (docs/superpowers/plans/2026-10-19-multi-league-
phase-6.md, Task 2)."""
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from app.db.league_publish import DirWriter
from app.sync import maintenance
from cp_seed import U1, U2, as_role, seed_auth_user, seed_league, seed_membership, seed_snapshot, seed_user
from test_connect_api import COOKIES, client, dispatched, espn, one, users  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)


def ensure(con, uid, gate):
    with as_role(con, "authenticated", uid) as c:
        if gate is not None:
            c.execute("SELECT set_config('fi.signups_open', %s, true)", [gate])
        return c.execute("SELECT fi.ensure_app_user()::text").fetchone()[0]


@pytest.mark.parametrize("gate", ["on", None])
def test_a_new_account_is_created_when_sign_ups_are_open_or_the_gate_is_unset(pg, gate):
    uid = str(uuid.uuid4())
    with pg.connect() as su:
        seed_auth_user(su, uid)
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert ensure(con, uid, gate) == uid


def test_a_closed_gate_refuses_new_accounts_but_not_existing_ones(pg):
    new = str(uuid.uuid4())
    with pg.connect() as su:
        seed_auth_user(su, new)
        seed_user(su, U1)
        su.commit()
    with pg.connect(user="fi_api") as con:
        with pytest.raises(RaiseException, match="signups_closed"):
            ensure(con, new, "off")
        assert ensure(con, U1, "off") == U1
    assert one(pg, "SELECT count(*) FROM fi.app_users WHERE id = %s", [new]) == (0,)


def test_the_league_cap_counts_pins_and_memberships_once_per_league(pg):
    with pg.connect() as su:
        seed_user(su, U1)
        leagues = [seed_league(su) for _ in range(26)]
        for lid in leagues[:20]:
            su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, lid])
        for lid in leagues[15:25]:                         # 5 of these are also pinned: 25 distinct leagues
            seed_membership(su, U1, lid, "viewer")
        su.commit()
    with pg.connect() as su:
        # A league the account already has is never counted twice: pinning a member league still works at the cap.
        su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, leagues[24]])
        with pytest.raises(RaiseException, match="league_limit"):
            su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, leagues[25]])
        su.rollback()
        with pytest.raises(RaiseException, match="league_limit"):
            seed_membership(su, U1, leagues[25], "viewer")
        su.rollback()
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s AND league_id = %s",
                   [U1, leagues[22]])                     # a revoked membership frees its slot
        su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, leagues[25]])
        su.commit()


def test_delete_my_account_deletes_only_the_callers_account(pg):
    with pg.connect() as su:
        seed_user(su, U1)
        seed_user(su, U2)
        su.commit()
    with pg.connect(user="fi_api") as con:
        with as_role(con, "authenticated", U1) as c:
            c.execute("SELECT fi.delete_my_account()")
        with pytest.raises(InsufficientPrivilege):          # not granted to anonymous callers at all
            with as_role(con, "anon") as c:
                c.execute("SELECT fi.delete_my_account()")
    assert one(pg, "SELECT count(*) FROM auth.users WHERE id = %s", [U1]) == (0,)
    assert one(pg, "SELECT count(*) FROM auth.users WHERE id = %s", [U2]) == (1,)


# ---- through the API -----------------------------------------------------------------------------------------

def test_sign_ups_closed_keep_new_accounts_out_of_every_write_and_me(client, pg, users, monkeypatch):
    new = str(uuid.uuid4())
    with pg.connect() as su:
        seed_auth_user(su, new)
        su.commit()
    monkeypatch.delenv("SIGNUPS_ENABLED", raising=False)
    r = client.get("/v2/me", headers=bearer(new))
    assert r.status_code == 403 and r.json()["detail"] == "Sign-ups are not open yet."
    assert client.post("/v2/leagues/import", json={"league": "4242"}, headers=bearer(new)).status_code == 403
    assert client.get("/v2/me", headers=bearer(U1)).status_code == 200, "existing accounts are unaffected"
    monkeypatch.setenv("SIGNUPS_ENABLED", "1")
    assert client.get("/v2/me", headers=bearer(new)).status_code == 200


def test_the_25th_league_is_the_last(client, pg, users):
    with pg.connect() as su:
        for _ in range(25):
            su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, seed_league(su)])
        extra = seed_league(su)
        su.commit()
    r = client.post("/v2/leagues/import", json={"league": "4242"}, headers=bearer(U1))
    assert r.status_code == 409 and "25 leagues" in r.json()["detail"]
    r = client.put(f"/v2/leagues/{extra}/pin", headers=bearer(U1))
    assert r.status_code == 409 and "25 leagues" in r.json()["detail"]


def test_deleting_an_account_erases_cookies_and_its_private_league_is_purged_a_week_later(client, pg, users, espn,
                                                                                          tmp_path):
    espn.anonymous = "private"
    r = client.post("/v2/leagues/connect", json={"league": "4242", **COOKIES}, headers=bearer(U1))
    assert r.status_code == 201, r.text
    lid = r.json()["league_id"]
    with pg.connect() as su:
        seed_snapshot(su, lid)
        su.commit()
    assert one(pg, "SELECT count(*) FROM fi.league_credentials WHERE league_id = %s", [lid]) == (1,)
    objects = DirWriter(tmp_path / "objects")
    (tmp_path / "f").write_bytes(b"x")
    objects.put(f"leagues/{lid}/v1.duckdb", tmp_path / "f")
    objects.put(f"raw/{lid}/v1.json.gz", tmp_path / "f")

    assert client.delete("/v2/me").status_code == 401
    assert client.delete("/v2/me", headers=bearer(U1)).status_code == 204
    # Immediately: the account, its cookies (ciphertext and wrapped key) and its membership are gone, and the league
    # it was the only member of is soft-deleted, so nobody can read it.
    assert one(pg, "SELECT count(*) FROM auth.users WHERE id = %s", [U1]) == (0,)
    assert one(pg, "SELECT count(*) FROM fi.league_credentials WHERE league_id = %s", [lid]) == (0,)
    assert one(pg, "SELECT count(*) FROM fi.league_memberships WHERE user_id = %s", [U1]) == (0,)
    assert one(pg, "SELECT deleted_at IS NOT NULL FROM fi.leagues WHERE id = %s", [lid]) == (True,)
    assert one(pg, "SELECT count(*) FROM fi.audit_log WHERE action = 'account.delete'") == (1,)
    assert client.delete("/v2/me", headers=bearer(U1)).status_code == 401, "the token is signed out"

    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        assert maintenance.purge_expired(con, objects) == [], "kept for the 7-day soft delete"
    with pg.connect() as su:
        su.execute("UPDATE fi.leagues SET deleted_at = now() - interval '8 days' WHERE id = %s", [lid])
        su.commit()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        assert maintenance.purge_expired(con, objects) == [lid]
    assert objects.keys(f"leagues/{lid}/") == [] and objects.keys(f"raw/{lid}/") == []


def test_deleting_one_member_leaves_a_shared_league_and_promotes_the_heir(client, pg, users):
    with pg.connect() as su:
        lid = seed_league(su, visibility="private")
        seed_membership(su, U1, lid, "admin")
        seed_membership(su, U2, lid, "viewer")
        su.commit()
    assert client.delete("/v2/me", headers=bearer(U1)).status_code == 204
    assert one(pg, "SELECT role FROM fi.league_memberships WHERE user_id = %s AND league_id = %s", [U2, lid]) == ("admin",)
    assert one(pg, "SELECT deleted_at FROM fi.leagues WHERE id = %s", [lid]) == (None,)



def test_concurrent_inserts_for_one_account_cannot_pass_the_cap_together(pg):
    import threading
    with pg.connect() as su:
        seed_user(su, U1)
        leagues = [seed_league(su) for _ in range(26)]
        for lid in leagues[:24]:
            su.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, lid])
        su.commit()
    first = pg.connect()
    first.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, leagues[24]])  # 25th, open
    outcome = {}

    def second():
        with pg.connect() as con:
            try:
                con.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [U1, leagues[25]])
                con.commit()
                outcome["result"] = "inserted"
            except RaiseException as err:
                outcome["result"] = err.diag.message_primary

    t = threading.Thread(target=second)
    t.start()
    t.join(1.0)
    assert t.is_alive(), "the second insert waits for the account lock"
    first.commit()
    first.close()
    t.join(10)
    assert outcome == {"result": "league_limit"}
    assert one(pg, "SELECT count(*) FROM fi.league_pins WHERE user_id = %s", [U1]) == (25,)


def test_audit_rows_older_than_a_year_are_pruned_and_newer_ones_stay_append_only(pg):
    with pg.connect() as su:
        su.execute("INSERT INTO fi.audit_log (at, action, target_type, target_id) VALUES "
                   "(now() - interval '366 days', 'old.action', 'league', 'lg_x'), (now(), 'new.action', 'league', 'lg_x')")
        su.commit()
        with pytest.raises(RaiseException, match="append-only"):
            su.execute("DELETE FROM fi.audit_log WHERE action = 'new.action'")
        su.rollback()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as con:
        assert maintenance.prune_audit_log(con) == 1
        assert maintenance.prune_audit_log(con) == 0
    assert one(pg, "SELECT array_agg(action) FROM fi.audit_log") == (["new.action"],)

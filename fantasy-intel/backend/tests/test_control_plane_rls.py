"""Least privilege in the control plane: what each role can see and touch, straight against Postgres."""
import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from cp_seed import U1, U2, as_role, seed_job, seed_league, seed_membership, seed_snapshot, seed_user

SERVICE_ROLES = ("fi_api", "fi_sync", "anon", "authenticated")


def test_no_role_we_created_bypasses_rls_or_is_superuser(pg):
    with pg.connect() as con:
        rows = con.execute("SELECT rolname, rolbypassrls, rolsuper, rolinherit FROM pg_roles WHERE rolname = ANY(%s)",
                           [list(SERVICE_ROLES)]).fetchall()
    assert {r[0] for r in rows} == set(SERVICE_ROLES)
    assert all(not bypass and not su for _, bypass, su, _ in rows)
    assert all(not inherit for name, _, _, inherit in rows if name in ("fi_api", "fi_sync"))


def test_fi_api_is_a_member_of_both_request_roles_and_nothing_else(pg):
    with pg.connect() as con:
        members = {r[0] for r in con.execute(
            "SELECT r.rolname FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid "
            "JOIN pg_roles m ON m.oid = am.member WHERE m.rolname = 'fi_api'")}
    assert members == {"anon", "authenticated"}


@pytest.fixture
def world(pg):
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        pub = seed_league(con, visibility="public", name="Public")
        priv = seed_league(con, visibility="private", name="Private")
        gone = seed_league(con, visibility="public", deleted=True, name="Deleted")
        seed_membership(con, U1, priv, "viewer")
        seed_snapshot(con, pub)
        seed_snapshot(con, priv)
        seed_snapshot(con, gone)
        seed_job(con, pub)
        seed_job(con, priv)
        con.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now()), (%s, %s, now())", [U1, pub, U2, pub])
        con.execute("INSERT INTO fi.team_selections VALUES (%s, %s, '6', now()), (%s, %s, '7', now())", [U1, pub, U2, pub])
        con.commit()
    return {"pub": pub, "priv": priv, "gone": gone}


def names(con, role, uid=None):
    with as_role(con, role, uid) as c:
        return {r[0] for r in c.execute("SELECT name FROM fi.leagues ORDER BY name")}


def test_anon_sees_only_live_public_leagues(pg, world):
    with pg.connect(user="fi_api") as con:
        assert names(con, "anon") == {"Public"}


def test_authenticated_sees_public_plus_own_private(pg, world):
    with pg.connect(user="fi_api") as con:
        assert names(con, "authenticated", U1) == {"Public", "Private"}
        assert names(con, "authenticated", U2) == {"Public"}


def test_revoked_membership_hides_the_private_league(pg, world):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s", [U1])
        su.commit()
    with pg.connect(user="fi_api") as con:
        assert names(con, "authenticated", U1) == {"Public"}


def test_authenticated_sees_only_own_pins_selections_and_memberships(pg, world):
    with pg.connect(user="fi_api") as con:
        with as_role(con, "authenticated", U1) as c:
            assert c.execute("SELECT user_id::text FROM fi.league_pins").fetchall() == [(U1,)]
            assert c.execute("SELECT user_id::text FROM fi.team_selections").fetchall() == [(U1,)]
            assert c.execute("SELECT user_id::text FROM fi.league_memberships").fetchall() == [(U1,)]
        for table in ("league_pins", "team_selections", "league_memberships", "app_users"):
            with pytest.raises(InsufficientPrivilege):
                with as_role(con, "anon") as c:
                    c.execute(f"SELECT count(*) FROM fi.{table}")


def test_snapshots_and_jobs_follow_league_visibility(pg, world):
    with pg.connect(user="fi_api") as con:
        with as_role(con, "anon") as c:
            assert {r[0] for r in c.execute("SELECT league_id FROM fi.league_snapshots")} == {world["pub"]}
            assert {r[0] for r in c.execute("SELECT league_id FROM fi.sync_jobs")} == {world["pub"]}
        with as_role(con, "authenticated", U1) as c:
            assert {r[0] for r in c.execute("SELECT league_id FROM fi.league_snapshots")} == {world["pub"], world["priv"]}


@pytest.mark.parametrize("role,uid", [("fi_api", None), ("anon", None), ("authenticated", U1)])
def test_fi_api_can_never_read_credentials(pg, world, role, uid):
    with pg.connect(user="fi_api") as con:
        with pytest.raises(InsufficientPrivilege):
            if role == "fi_api":
                con.execute("SELECT count(*) FROM fi.league_credentials")
            else:
                with as_role(con, role, uid) as c:
                    c.execute("SELECT count(*) FROM fi.league_credentials")


@pytest.mark.parametrize("table", ["league_memberships", "league_pins", "team_selections", "league_invites", "app_users",
                                   "league_credentials", "audit_log"])
def test_fi_sync_cannot_read_people_tables(pg, world, table):
    with pg.connect(user="fi_sync") as con, pytest.raises(InsufficientPrivilege):
        con.execute(f"SELECT count(*) FROM fi.{table}")


def test_fi_sync_reads_and_writes_only_the_sync_tables(pg, world):
    with pg.connect(user="fi_sync") as con:
        assert con.execute("SELECT count(*) FROM fi.sync_jobs").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM fi.leagues").fetchone() == (3,)      # sync sees every league row
        con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s", [world["pub"]])
        con.commit()
        con.execute("UPDATE fi.leagues SET name = 'x' WHERE id = %s", [world["pub"]])   # refreshed from the provider
        con.commit()
        for column, value in (("external_id", "1"), ("deleted_at", None), ("last_viewed_at", None)):
            with pytest.raises(InsufficientPrivilege):
                con.execute(f"UPDATE fi.leagues SET {column} = %s WHERE id = %s", [value, world["pub"]])
            con.rollback()


def test_request_roles_can_only_touch_last_viewed_at_on_leagues(pg, world):
    with pg.connect(user="fi_api") as con:
        with as_role(con, "anon") as c:
            assert c.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [world["pub"]]).rowcount == 1
            assert c.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [world["priv"]]).rowcount == 0
        with pytest.raises(InsufficientPrivilege):
            with as_role(con, "authenticated", U1) as c:
                c.execute("UPDATE fi.leagues SET visibility = 'public' WHERE id = %s", [world["priv"]])


def test_a_user_cannot_pin_or_select_in_an_invisible_league_or_for_someone_else(pg, world):
    with pg.connect(user="fi_api") as con:
        with pytest.raises(Exception) as err:
            with as_role(con, "authenticated", U2) as c:
                c.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now())", [U2, world["priv"]])
        assert "row-level security" in str(err.value)
        with pytest.raises(Exception) as err:
            with as_role(con, "authenticated", U2) as c:
                c.execute("INSERT INTO fi.team_selections VALUES (%s, %s, '1', now())", [U1, world["pub"]])
        assert "row-level security" in str(err.value)


def test_a_user_cannot_promote_themselves_to_platform_admin(pg, world):
    with pg.connect(user="fi_api") as con, pytest.raises(InsufficientPrivilege):
        with as_role(con, "authenticated", U1) as c:
            c.execute("UPDATE fi.app_users SET is_platform_admin = true WHERE id = %s", [U1])


def test_anon_has_no_write_grants_at_all(pg, world):
    with pg.connect(user="fi_api") as con:
        for sql in ("INSERT INTO fi.league_pins VALUES (%s, %s, now())",
                    "INSERT INTO fi.team_selections VALUES (%s, %s, '1', now())"):
            with pytest.raises(InsufficientPrivilege):
                with as_role(con, "anon") as c:
                    c.execute(sql, [U1, world["pub"]])


def test_invite_token_hashes_and_job_error_details_are_not_readable_by_request_roles(pg, world):
    with pg.connect(user="fi_api") as con:
        for sql in ("SELECT token_hash FROM fi.league_invites", "SELECT error_detail FROM fi.sync_jobs"):
            with pytest.raises(InsufficientPrivilege):
                with as_role(con, "authenticated", U1) as c:
                    c.execute(sql)


def test_snapshot_object_keys_are_bound_to_their_league(pg, world):
    with pg.connect() as con, pytest.raises(CheckViolation):
        con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) "
                    "VALUES (%s, %s, 'v9', 9, 'g')", [world["pub"], f"leagues/{world['priv']}/v9.duckdb"])


def test_functions_are_not_executable_by_public_or_anon(pg):
    """Except the membership predicates RLS uses, and request_probe (Phase 3b): the first anonymous viewer of a public
    league whose visibility check expired starts a probe (spec §2). It is rate-limited in Postgres and its answer goes
    only to the API (the fi schema is not exposed through Supabase's Data API)."""
    with pg.connect() as con:
        rows = con.execute("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'fi' AND has_function_privilege('anon', p.oid, 'EXECUTE')
              AND p.proname NOT IN ('is_member', 'is_admin', 'request_probe')""").fetchall()
    assert rows == []


def test_every_control_plane_function_pins_its_search_path(pg):
    """Supabase's advisor flags a mutable search_path; a SECURITY DEFINER function without one can be hijacked."""
    with pg.connect() as con:
        rows = con.execute("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'fi' AND NOT EXISTS (
              SELECT 1 FROM unnest(coalesce(p.proconfig, '{}')) c WHERE c LIKE 'search_path=%')""").fetchall()
    assert rows == []


def test_rls_is_enabled_on_every_control_plane_table(pg):
    with pg.connect() as con:
        rows = con.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'fi' AND c.relkind = 'r' AND NOT c.relrowsecurity""").fetchall()
    assert rows == []


def test_a_user_cannot_delete_preferences_in_a_league_they_can_no_longer_see(pg, world):
    """DELETE policies have no WITH CHECK, so visibility must be part of USING."""
    with pg.connect() as su:
        su.execute("INSERT INTO fi.team_selections VALUES (%s, %s, '6', now())", [U1, world["priv"]])
        su.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now())", [U1, world["priv"]])
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s", [U1])
        su.commit()
    with pg.connect(user="fi_api") as con:
        with as_role(con, "authenticated", U1) as c:
            assert c.execute("DELETE FROM fi.league_pins WHERE league_id = %s", [world["priv"]]).rowcount == 0
            assert c.execute("DELETE FROM fi.team_selections WHERE league_id = %s", [world["priv"]]).rowcount == 0
            assert c.execute("UPDATE fi.team_selections SET team_external_id = '7' WHERE league_id = %s",
                             [world["priv"]]).rowcount == 0

"""The platform monitor's Postgres side (supabase/migrations/20261022130000_usage_samples.sql): fi.usage_samples is
written by fi_sync only, fi.usage_overview() answers platform admins only, fi.dead_jobs() and fi.usage_counters()
are for fi_sync only."""
import psycopg
import pytest

from cp_seed import U1, U2, as_role, seed_job, seed_league, seed_user
from test_leagues_api import bearer, env  # noqa: F401  (fixture)


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def add_sample(con, metric, used, quota, unit="bytes", ago="0 hours"):
    con.execute("INSERT INTO fi.usage_samples (metric, used, quota, unit, sampled_at) "
                "VALUES (%s, %s, %s, %s, now() - %s::interval)", [metric, used, quota, unit, ago])


def test_fi_sync_inserts_selects_and_prunes_samples(pg):
    with sync(pg) as con:
        add_sample(con, "supabase_db_bytes", 10, 500)
        add_sample(con, "supabase_db_bytes", 9, 500, ago="91 days")
        assert con.execute("SELECT count(*) FROM fi.usage_samples").fetchone() == (2,)
        con.execute("DELETE FROM fi.usage_samples WHERE sampled_at < now() - interval '90 days'")
        assert con.execute("SELECT used FROM fi.usage_samples").fetchall() == [(10.0,)]
        con.commit()


@pytest.mark.parametrize("role", ["anon", "authenticated", "fi_api"])
def test_request_roles_cannot_read_or_write_samples(pg, role):
    with pg.connect() as su:
        seed_user(su, U1, admin=True)
        su.commit()
    with pg.connect(user="fi_api") as con:
        for sql in ("SELECT * FROM fi.usage_samples",
                    "INSERT INTO fi.usage_samples (metric, used, unit) VALUES ('x', 1, 'bytes')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                if role == "fi_api":
                    with con.transaction():
                        con.execute(sql)
                else:
                    with as_role(con, role, U1 if role == "authenticated" else None) as c:
                        c.execute(sql)


def test_samples_reject_unsafe_metric_names(pg):
    with sync(pg) as con, pytest.raises(psycopg.errors.CheckViolation):
        add_sample(con, "League of Extraordinary Gentlemen", 1, 2)


def test_usage_overview_returns_the_latest_sample_and_seven_day_max_for_admins(pg):
    with pg.connect() as su:
        seed_user(su, U1, admin=True)
        su.commit()
    with sync(pg) as con:
        add_sample(con, "r2_storage_bytes", 900, 1000, ago="3 days")
        add_sample(con, "r2_storage_bytes", 300, 1000, ago="1 hour")
        add_sample(con, "r2_storage_bytes", 5000, 1000, ago="8 days")      # outside the 7-day max
        add_sample(con, "supabase_accounts", 2, 50000, unit="accounts")
        con.commit()
    with pg.connect(user="fi_api") as con, as_role(con, "authenticated", U1) as c:
        rows = c.execute("SELECT metric, used, quota, unit, max_used_7d FROM fi.usage_overview() ORDER BY metric").fetchall()
    assert rows == [("r2_storage_bytes", 300.0, 1000.0, "bytes", 900.0),
                    ("supabase_accounts", 2.0, 50000.0, "accounts", 2.0)]


@pytest.mark.parametrize("who", ["member", "anon"])
def test_usage_overview_refuses_everyone_else(pg, who):
    with pg.connect() as su:
        seed_user(su, U2)                      # an ordinary account
        su.commit()
    with sync(pg) as con:
        add_sample(con, "r2_storage_bytes", 1, 10)
        con.commit()
    with pg.connect(user="fi_api") as con:
        with pytest.raises(psycopg.Error):
            with as_role(con, "authenticated" if who == "member" else "anon", U2 if who == "member" else None) as c:
                c.execute("SELECT * FROM fi.usage_overview()").fetchall()


def test_dead_jobs_lists_only_dead_jobs_inside_the_window(pg):
    with pg.connect() as su:
        lid = seed_league(su, name="Secret League Name")
        dead = seed_job(su, lid, status="dead", kind="scheduled", error_code="http_500")
        old = seed_job(su, lid, status="dead", error_code="timeout")
        seed_job(su, lid, status="failed", error_code="http_500")
        seed_job(su, lid, status="succeeded")
        su.execute("UPDATE fi.sync_jobs SET finished_at = now() - interval '1 hour' WHERE id = %s", [dead])
        su.execute("UPDATE fi.sync_jobs SET finished_at = now() - interval '8 days', created_at = now() - interval '9 days' "
                   "WHERE id = %s", [old])
        su.commit()
    with sync(pg) as con:
        rows = con.execute("SELECT job_id::text, league_id, kind, error_code FROM fi.dead_jobs(interval '7 days')").fetchall()
        cols = [d.name for d in con.execute("SELECT * FROM fi.dead_jobs(interval '7 days')").description]
    assert rows == [(dead, lid, "scheduled", "http_500")]
    assert cols == ["job_id", "league_id", "kind", "error_code", "finished_at"]     # never a name or detail


def test_usage_counters_reports_database_size_and_accounts(pg):
    with pg.connect() as su:
        seed_user(su, U1)
        seed_user(su, U2)
        su.commit()
    with sync(pg) as con:
        db_bytes, accounts = con.execute("SELECT db_bytes, accounts FROM fi.usage_counters()").fetchone()
    assert db_bytes > 1_000_000 and accounts == 2


@pytest.mark.parametrize("fn", ["dead_jobs", "usage_counters"])
def test_monitor_functions_are_for_fi_sync_only(pg, fn):
    with pg.connect() as con:
        grantees = {r[0] for r in con.execute(
            "SELECT grantee FROM information_schema.routine_privileges WHERE routine_schema = 'fi' AND routine_name = %s",
            [fn]).fetchall()}
    assert grantees - {"postgres"} == {"fi_sync"}, grantees


def test_usage_overview_is_for_authenticated_only(pg):
    with pg.connect() as con:
        grantees = {r[0] for r in con.execute(
            "SELECT grantee FROM information_schema.routine_privileges WHERE routine_schema = 'fi' "
            "AND routine_name = 'usage_overview'").fetchall()}
    assert grantees - {"postgres"} == {"authenticated"}, grantees


def test_the_admin_usage_route_is_404_for_everyone_but_platform_admins(pg, env):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import app
    with pg.connect() as su:
        seed_user(su, U1, admin=True)
        seed_user(su, U2)
        su.commit()
    with sync(pg) as con:
        add_sample(con, "r2_storage_bytes", 7.5e9, 1e10)
        con.commit()
    client = TestClient(app)
    assert client.get("/v2/admin/usage").status_code == 404
    assert client.get("/v2/admin/usage", headers=bearer(U2)).status_code == 404
    r = client.get("/v2/admin/usage", headers=bearer(U1))
    assert r.status_code == 200
    (row,) = r.json()["metrics"]
    assert row["metric"] == "r2_storage_bytes" and row["percent"] == 75.0

"""The throwaway Postgres: shim behaves like Supabase's auth helpers; migrations apply cleanly and idempotently."""
import json


def test_shim_auth_uid_reads_transaction_local_claims(pg):
    with pg.connect(autocommit=True) as con:      # autocommit: `transaction()` below is a real transaction, not a savepoint
        assert con.execute("SELECT auth.uid()").fetchone() == (None,)
        with con.transaction():
            con.execute("SELECT set_config('request.jwt.claims', %s, true)",
                        [json.dumps({"sub": "11111111-1111-4111-8111-111111111111", "role": "authenticated"})])
            uid, role = con.execute("SELECT auth.uid()::text, auth.role()").fetchone()
        assert (uid, role) == ("11111111-1111-4111-8111-111111111111", "authenticated")
        assert con.execute("SELECT auth.uid()").fetchone() == (None,)


def test_migrations_are_idempotent(pg):
    with pg.connect(autocommit=True) as con:
        for path in pg.MIGRATIONS:
            con.execute(path.read_text())           # second application must not fail
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'fi'")}
    assert set(pg.FI_TABLES) <= tables


def test_every_migration_rolls_back_in_reverse_order_and_reapplies(pg_cluster):
    """The documented rollback: down scripts in reverse order. Each must run, restore the previous definitions (so
    the next one down still applies), and leave a schema the migrations can rebuild. Uses its own database; the
    final 0000 down (which drops the cluster-wide roles) is replaced by its schema drop, since other test databases
    in this cluster still hold grants to those roles."""
    import psycopg
    from pg_harness import MIGRATIONS_DIR, SHIM

    down_dir = MIGRATIONS_DIR.parent / "down"
    ups = sorted(MIGRATIONS_DIR.glob("*.sql"))
    downs = {p.name: p for p in down_dir.glob("*.sql")}
    assert set(downs) == {p.name for p in ups}, "every migration needs a down-migration with the same name"
    with psycopg.connect(pg_cluster.superuser_dsn(), autocommit=True) as con:
        con.execute("DROP DATABASE IF EXISTS fi_rollback WITH (FORCE)")
        con.execute("CREATE DATABASE fi_rollback")
    try:
        with psycopg.connect(pg_cluster.superuser_dsn("fi_rollback"), autocommit=True) as con:
            con.execute(SHIM.read_text())
            for up in ups:
                con.execute(up.read_text())
            for up in reversed(ups[1:]):
                con.execute(downs[up.name].read_text())
            assert con.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                               "WHERE n.nspname = 'fi' AND p.proname IN ('mark_credential', 'redeem_invite', "
                               "'on_app_user_delete', 'current_account_exists')").fetchone() == (0,)
            con.execute("DROP SCHEMA fi CASCADE")
            for up in ups:                                        # and the whole chain applies again
                con.execute(up.read_text())
            assert con.execute("SELECT count(*) FROM fi.provider_slots").fetchone() == (6,)
    finally:
        with psycopg.connect(pg_cluster.superuser_dsn(), autocommit=True) as con:
            con.execute("DROP DATABASE IF EXISTS fi_rollback WITH (FORCE)")


def test_unverified_external_dsn_rejected(monkeypatch):
    import pytest
    from pg_harness import _verify_safe_test_dsn

    monkeypatch.delenv("FI_ALLOW_REMOTE_TEST_PG", raising=False)
    with pytest.raises(ValueError, match="Refusing to run destructive test setup against unverified external DSN"):
        _verify_safe_test_dsn("postgresql://postgres:secret@db.supabase.co:5432/postgres")

    with pytest.raises(ValueError, match="Refusing to run destructive test setup against unverified external DSN"):
        _verify_safe_test_dsn("postgresql://postgres:secret@production.rds.amazonaws.com:5432/postgres")

    with pytest.raises(ValueError, match="Refusing to run destructive test setup against unverified external DSN"):
        _verify_safe_test_dsn("postgresql://postgres:secret@internal-db.local:5432/postgres")

    with pytest.raises(ValueError, match="Refusing to run destructive test setup against unverified external DSN"):
        _verify_safe_test_dsn("postgresql://postgres:secret@prod.corp.internal:5432/postgres")

    # Safe test hosts should pass without error
    _verify_safe_test_dsn("postgresql://postgres:postgres@localhost:5432/postgres")
    _verify_safe_test_dsn("postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    _verify_safe_test_dsn("host=/tmp/fipg user=postgres dbname=postgres")
    _verify_safe_test_dsn(None)

    # Explicit override allows intentional remote test clusters
    monkeypatch.setenv("FI_ALLOW_REMOTE_TEST_PG", "1")
    _verify_safe_test_dsn("postgresql://postgres:secret@db.supabase.co:5432/postgres")


def test_tracked_connections_and_fixture_cleanup(pg):
    con1 = pg.connect()
    con2 = pg.connect()
    assert not con1.closed and not con2.closed
    closed = pg.close_open_connections()
    assert closed >= 2
    assert con1.closed and con2.closed


def test_reset_cleans_up_abandoned_uncommitted_transaction(pg):
    """When a failed test leaves an uncommitted transaction holding a row or table lock,
    pg.reset() must not hang; it must safely clean up and reset."""
    con = pg.connect()
    con.execute("BEGIN")
    con.execute("INSERT INTO fi.leagues (id, provider, external_id, season, name, status, visibility) VALUES "
                "('lg_1234567890123456', 'espn', '123', 2026, 'Leak Test', 'active', 'public')")
    # Leave transaction uncommitted on purpose
    pg.reset(timeout=5.0)
    with pg.connect() as check:
        assert check.execute("SELECT count(*) FROM fi.leagues WHERE id = 'lg_1234567890123456'").fetchone() == (0,)


def test_reset_terminates_orphaned_background_lock(pg):
    """When an orphaned thread holds a table lock on the test DB, pg.reset() terminates
    the lingering connection to the test database and succeeds cleanly within timeout."""
    import threading
    import time

    lock_acquired = threading.Event()

    def background_holder():
        try:
            with pg.connect() as con:
                con.execute("BEGIN")
                con.execute("LOCK TABLE fi.leagues IN EXCLUSIVE MODE")
                lock_acquired.set()
                # Simulate a hanging or orphaned thread
                time.sleep(30)
        except Exception:
            pass  # Expected to be terminated by pg.reset()

    t = threading.Thread(target=background_holder, daemon=True)
    t.start()
    assert lock_acquired.wait(timeout=5.0)

    # pg.reset must terminate the blocking backend and complete quickly without hanging
    start = time.monotonic()
    pg.reset(timeout=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 4.0
    t.join(timeout=1.0)


def test_is_exact_postgres_instance_rejects_unrelated_process():
    import os
    from pg_harness import _is_exact_postgres_instance

    # Current process is python, not postgres
    assert not _is_exact_postgres_instance(os.getpid(), "/tmp/fake_dir")
    # Non-existent PID
    assert not _is_exact_postgres_instance(99999999, "/tmp/fake_dir")


def test_pg_cluster_stop_protects_against_pid_reuse(monkeypatch, tmp_path):
    """If cluster._pid is replaced or reused by an unrelated process, PgCluster.stop()
    must not terminate it."""
    import subprocess
    import sys
    from pg_harness import PgCluster

    # Start a benign dummy subprocess
    dummy_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        # Create a mock PgCluster object without starting a real postgres server
        cluster = object.__new__(PgCluster)
        cluster._dir = str(tmp_path)
        cluster._data = str(tmp_path / "data")
        cluster._pg_ctl = None
        cluster._pid = dummy_proc.pid

        # Call stop(); dummy process must not be killed because it is python, not postgres
        cluster.stop()

        # Check that dummy_proc is still alive
        assert dummy_proc.poll() is None, "dummy process should not have been terminated by PgCluster.stop()"
    finally:
        dummy_proc.terminate()
        dummy_proc.wait(timeout=2)


def test_concurrent_database_isolation(pg_cluster):
    """Demonstrate that two concurrent test invocations (with isolated database identities)
    cannot terminate each other's connections."""
    import psycopg
    from pg_harness import PgDatabase

    db_alpha = PgDatabase(pg_cluster, name="fi_test_alpha")
    db_beta = PgDatabase(pg_cluster, name="fi_test_beta")
    db_alpha.create()
    db_beta.create()

    try:
        con_alpha = db_alpha.connect()
        con_beta = db_beta.connect()

        assert con_alpha.execute("SELECT 1").fetchone() == (1,)
        assert con_beta.execute("SELECT 1").fetchone() == (1,)

        # Calling terminate_other_backends on db_alpha terminates db_alpha's connections
        # but MUST NOT touch db_beta's connections.
        terminated_alpha = db_alpha.terminate_other_backends()
        assert terminated_alpha >= 1
        # con_beta must still be completely unaffected and usable!
        assert not con_beta.closed
        assert con_beta.execute("SELECT 1").fetchone() == (1,)

        # Reconnect db_alpha and now call terminate_other_backends on db_beta:
        # db_beta terminates its own connections, but db_alpha's connection is untouched!
        con_alpha2 = db_alpha.connect()
        terminated_beta = db_beta.terminate_other_backends()
        assert terminated_beta >= 1
        # con_alpha2 must still be completely unaffected and usable!
        assert not con_alpha2.closed
        assert con_alpha2.execute("SELECT 1").fetchone() == (1,)

        con_alpha2.close()
    finally:
        with psycopg.connect(pg_cluster.superuser_dsn(), autocommit=True) as con:
            con.execute("DROP DATABASE IF EXISTS fi_test_alpha WITH (FORCE)")
            con.execute("DROP DATABASE IF EXISTS fi_test_beta WITH (FORCE)")



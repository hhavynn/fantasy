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

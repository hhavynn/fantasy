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

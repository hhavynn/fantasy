"""Each request is one transaction that runs as anon|authenticated with transaction-local claims. Claims must never
survive the transaction or leak between two transactions that share a pooled connection (Supavisor transaction mode)."""
import json
import threading

import psycopg
import pytest

from app.control_plane.pg import ControlPlane, ControlPlaneUnavailable
from app.leagues.context import Principal
from cp_seed import U1, U2


def principal(uid):
    return Principal.from_claims({"sub": uid, "role": "authenticated", "aud": "authenticated", "exp": 2_000_000_000})


@pytest.fixture(autouse=True)
def accounts(pg):
    from cp_seed import seed_auth_user
    with pg.connect() as con:
        seed_auth_user(con, U1)
        seed_auth_user(con, U2)
        con.commit()


@pytest.fixture
def cp(pg):
    plane = ControlPlane(pg.dsn_for("fi_api"), max_size=1)      # one physical connection: every tx shares it
    yield plane
    plane.close()


def who(con):
    uid, user, claims = con.execute(
        "SELECT auth.uid()::text, current_user, current_setting('request.jwt.claims', true)").fetchone()
    return uid, user, claims


def test_claims_and_role_are_transaction_local_on_a_shared_connection(cp):
    with cp.request_tx(principal(U1)) as con:
        assert who(con)[:2] == (U1, "authenticated")
    with cp.request_tx(Principal.anonymous()) as con:
        assert who(con) == (None, "anon", json.dumps({"role": "anon"}))
    with cp.request_tx(principal(U2)) as con:
        assert who(con)[:2] == (U2, "authenticated")
    with cp._pool.connection() as raw:                           # the same physical connection, outside any request
        with raw.transaction():
            user, claims = raw.execute("SELECT current_user, current_setting('request.jwt.claims', true)").fetchone()
            assert (user, claims) in (("fi_api", ""), ("fi_api", None))


def test_a_failed_transaction_leaves_nothing_behind(cp):
    with pytest.raises(RuntimeError):
        with cp.request_tx(principal(U1)) as con:
            con.execute("SELECT 1")
            raise RuntimeError("boom")
    with cp.request_tx(Principal.anonymous()) as con:
        assert who(con)[:2] == (None, "anon")


def test_two_interleaved_requests_never_see_each_others_claims(pg):
    plane = ControlPlane(pg.dsn_for("fi_api"), max_size=2)
    a_in, b_done = threading.Event(), threading.Event()
    seen = {}

    def a():
        with plane.request_tx(principal(U1)) as con:
            seen["a1"] = who(con)[0]
            a_in.set()
            b_done.wait(5)
            seen["a2"] = who(con)[0]

    def b():
        a_in.wait(5)
        with plane.request_tx(principal(U2)) as con:
            seen["b"] = who(con)[0]
        b_done.set()

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start()
    tb.start()
    ta.join(10)
    tb.join(10)
    plane.close()
    assert seen == {"a1": U1, "a2": U1, "b": U2}


def test_the_request_role_cannot_escape_rls_with_set_role(cp):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with cp.request_tx(Principal.anonymous()) as con:
            con.execute("SET LOCAL ROLE postgres")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with cp.request_tx(principal(U1)) as con:
            con.execute("RESET ROLE")                              # back to fi_api, which has no grant either
            con.execute("SELECT count(*) FROM fi.league_credentials")


def test_unreachable_database_is_unavailable_not_a_crash(tmp_path):
    plane = ControlPlane(f"host={tmp_path} user=fi_api dbname=nope connect_timeout=1", max_size=1)
    with pytest.raises(ControlPlaneUnavailable):
        with plane.request_tx(Principal.anonymous()):
            pass
    plane.close()


def test_principal_from_claims_requires_a_uuid_subject():
    for sub in (None, "", "alice", 42):
        with pytest.raises(ValueError):
            Principal.from_claims({"sub": sub})
    p = principal(U1)
    assert p.user_id == U1 and "sub" in p.claims_json and p.claims_json not in repr(p)
    assert Principal.from_claims({"sub": U1.upper()}).user_id == U1


def test_get_control_plane_requires_the_dsn(monkeypatch):
    from app.control_plane import pg as module
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(module, "_PLANES", {})
    with pytest.raises(ControlPlaneUnavailable):
        module.get_control_plane()



def test_setup_is_one_round_trip_and_a_deleted_account_is_refused(cp, pg):
    import uuid

    from app.control_plane.pg import AccountGone
    ghost = principal(str(uuid.uuid4()))                        # valid claims, no auth.users row
    with pytest.raises(AccountGone):
        with cp.request_tx(ghost):
            pass
    with cp.request_tx(principal(U1)) as con:                  # timeout set transaction-locally with the claims
        assert con.execute("SHOW statement_timeout").fetchone() == ("5s",)
    with cp._pool.connection() as raw, raw.transaction():
        assert raw.execute("SHOW statement_timeout").fetchone() != ("5s",)

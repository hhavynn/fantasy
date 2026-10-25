"""Every /v2 league request starts here. The matrix below is the spec's access model: visibility x membership x
status, decided from Postgres under RLS before any league file is touched."""
import pytest

from app.control_plane.authz import Access, LeagueNotFound, authorize_league
from app.control_plane.pg import ControlPlane
from app.leagues.context import Principal
from cp_seed import U1, U2, U3, seed_job, seed_league, seed_membership, seed_snapshot, seed_user

ANON = Principal.anonymous()


def user(uid):
    return Principal.from_claims({"sub": uid, "role": "authenticated"})


@pytest.fixture
def plane(pg):
    p = ControlPlane(pg.dsn_for("fi_api"), max_size=2)
    yield p
    p.close()


@pytest.fixture
def states(pg):
    """league name -> id, with U1 = admin and U2 = viewer of every league that has members; U3 is a stranger."""
    out = {}
    with pg.connect() as con:
        for u in (U1, U2, U3):
            seed_user(con, u)
        out["public_fresh"] = seed_league(con, visibility="public")
        out["public_expired"] = seed_league(con, visibility="public", checked_at="25 hours")
        out["public_unchecked"] = seed_league(con, visibility="public", checked_at=None)
        out["public_dormant"] = seed_league(con, visibility="public", status="dormant")
        out["private"] = seed_league(con, visibility="private", checked_at=None)
        out["private_stale"] = seed_league(con, visibility="private", status="stale", checked_at=None)
        out["flipped_private"] = seed_league(con, visibility="private", epoch=1, checked_at="1 hour")
        out["deleted"] = seed_league(con, visibility="public", deleted=True)
        for name in ("public_fresh", "public_expired", "public_unchecked", "public_dormant", "private",
                     "private_stale", "deleted"):
            seed_membership(con, U1, out[name], "admin")
            seed_membership(con, U2, out[name], "viewer")
        con.commit()
    return out


ACTORS = {"anon": ANON, "admin": user(U1), "viewer": user(U2), "stranger": user(U3)}
# expected: role for a 200, or 404
MATRIX = {
    "public_fresh":     {"anon": None, "admin": "admin", "viewer": "viewer", "stranger": None},
    "public_dormant":   {"anon": None, "admin": "admin", "viewer": "viewer", "stranger": None},
    "public_expired":   {"anon": 404, "admin": "admin", "viewer": "viewer", "stranger": 404},
    "public_unchecked": {"anon": 404, "admin": "admin", "viewer": "viewer", "stranger": 404},
    "private":          {"anon": 404, "admin": "admin", "viewer": "viewer", "stranger": 404},
    "private_stale":    {"anon": 404, "admin": "admin", "viewer": "viewer", "stranger": 404},
    "flipped_private":  {"anon": 404, "admin": 404, "viewer": 404, "stranger": 404},
    "deleted":          {"anon": 404, "admin": 404, "viewer": 404, "stranger": 404},
}


@pytest.mark.parametrize("state", list(MATRIX))
@pytest.mark.parametrize("actor", list(ACTORS))
def test_access_matrix(plane, states, state, actor):
    expected = MATRIX[state][actor]
    if expected == 404:
        with pytest.raises(LeagueNotFound):
            authorize_league(plane, ACTORS[actor], states[state])
    else:
        access = authorize_league(plane, ACTORS[actor], states[state])
        assert isinstance(access, Access) and access.role == expected and access.league_id == states[state]
        assert access.status == ("stale" if state == "private_stale" else "dormant" if state == "public_dormant" else "active")


@pytest.mark.parametrize("bad", ["", "lg_short", "lg_" + "a" * 24 + "/..", "x" * 40, "lg_" + "é" * 24, None, 5])
def test_malformed_ids_are_404_without_touching_the_database(bad):
    plane = ControlPlane("host=/nonexistent user=fi_api dbname=nope connect_timeout=1", max_size=1)
    with pytest.raises(LeagueNotFound):
        authorize_league(plane, ANON, bad)        # a ControlPlaneUnavailable here would mean the id reached Postgres
    plane.close()


def test_unknown_id_is_the_same_404(plane, states):
    with pytest.raises(LeagueNotFound):
        authorize_league(plane, user(U1), "lg_" + "z" * 24)


def test_pointer_perspective_and_pin_come_back_with_the_access(pg, plane, states):
    lid = states["public_fresh"]
    with pg.connect() as con:
        seed_snapshot(con, lid, data_version="20261020T1200", global_version="g7", fence=3)
        con.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now())", [U2, lid])
        con.execute("INSERT INTO fi.team_selections VALUES (%s, %s, '6', now())", [U2, lid])
        con.commit()
    a = authorize_league(plane, user(U2), lid)
    assert a.snapshot.object_key == f"leagues/{lid}/20261020T1200.duckdb" and a.snapshot.global_version == "g7"
    assert a.sync_state == "ready" and a.pinned and a.perspective_team_id == "6"
    anon = authorize_league(plane, ANON, lid)
    assert anon.snapshot == a.snapshot and not anon.pinned and anon.perspective_team_id is None and anon.role is None
    body = a.to_json()
    assert "object_key" not in str(body)
    assert body["data"] == {"data_version": "20261020T1200", "global_version": "g7",
                            "committed_at": a.snapshot.committed_at.isoformat()}


@pytest.mark.parametrize("job,expected", [(None, "no_data"), ("queued", "first_sync_queued"),
                                          ("running", "first_sync_running"), ("failed", "first_sync_failed"),
                                          ("dead", "first_sync_failed")])
def test_sync_state_before_the_first_publish(pg, plane, states, job, expected):
    lid = states["public_fresh"]
    if job:
        with pg.connect() as con:
            seed_job(con, lid, status=job)
            con.commit()
    a = authorize_league(plane, ANON, lid)
    assert a.snapshot is None and a.sync_state == expected and a.to_json()["data"] is None


@pytest.mark.parametrize("job,code,expected", [
    ("queued", None, None),                                     # a first attempt: nothing went wrong yet
    ("running", "network", None),                               # running again: the old code is not an issue
    ("queued", "provider_5xx", "provider_unreachable"),         # waiting to retry
    ("queued", "probe_unknown", "provider_unreachable"),
    ("dead", "provider_auth", "access"),
    ("dead", "no_credential", "access"),
    ("dead", "provider_not_found", "not_found"),
    ("dead", "provider_empty", "empty"),                        # the provider returned the league with no teams
    ("dead", "parse_error", "internal"),
    ("failed", "something_new", "internal"),                    # an unknown code is never shown, only its category
    ("succeeded", "network", None),
])
def test_sync_issue_is_a_category_never_the_worker_code(pg, plane, states, job, code, expected):
    lid = states["public_fresh"]
    with pg.connect() as con:
        seed_job(con, lid, status=job, error_code=code)
        con.commit()
    a = authorize_league(plane, ANON, lid)
    assert a.sync_issue == expected and a.to_json()["sync_issue"] == expected
    body = str(a.to_json())
    assert "redacted detail" not in body and (code is None or expected == code or f"'{code}'" not in body)


@pytest.mark.parametrize("job,due_min,started_min,attempt,expected", [
    ("queued", 10, None, 0, False),            # due 10 minutes ago: a worker runs every 15
    ("queued", 61, None, 0, True),             # due over an hour ago and nobody claimed it
    ("queued", -30, None, 2, False),           # waiting out a retry backoff: due in the future
    ("running", 0, 30, 1, False),
    ("running", 0, 61, 1, True),               # a first run started over an hour ago: one drain run is capped at 25 min
    ("running", 0, 300, 3, False),             # a retry keeps the first attempt's started_at: its age says nothing
    ("failed", 600, 600, 5, False),            # finished: failed, not stalled
])
def test_a_sync_overdue_far_beyond_the_worker_cadence_is_stalled(pg, plane, states, job, due_min, started_min, attempt,
                                                               expected):
    lid = states["public_fresh"]
    with pg.connect() as con:
        jid = seed_job(con, lid, status=job)
        con.execute("UPDATE fi.sync_jobs SET run_after = now() - make_interval(mins => %s), attempt = %s, "
                    "started_at = CASE WHEN %s::int IS NULL THEN NULL ELSE now() - make_interval(mins => %s) END "
                    "WHERE id = %s::uuid", [due_min, attempt, started_min, started_min or 0, jid])
        con.commit()
    a = authorize_league(plane, ANON, lid)
    assert a.sync_stalled is expected and a.to_json()["sync_stalled"] is expected


def test_stalled_is_judged_on_the_database_clock_from_columns_request_roles_may_read():
    """Unit check of the rule itself (no Postgres): queued past due, or a first run, by more than STALL_AFTER."""
    from datetime import datetime, timedelta

    from app.control_plane.authz import STALL_AFTER, _sync_stalled
    now = datetime(2026, 10, 23, 12)
    over, under = now - STALL_AFTER - timedelta(minutes=1), now - STALL_AFTER + timedelta(minutes=1)
    assert _sync_stalled("queued", over, None, 0, now) and not _sync_stalled("queued", under, None, 0, now)
    assert _sync_stalled("running", None, over, 1, now) and not _sync_stalled("running", None, over, 2, now)
    assert not _sync_stalled("queued", None, None, 0, now) and not _sync_stalled(None, over, over, 1, now)


def test_a_revoked_member_of_a_private_league_is_a_stranger(pg, plane, states):
    with pg.connect() as con:
        con.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s AND league_id = %s",
                    [U2, states["private"]])
        con.commit()
    with pytest.raises(LeagueNotFound):
        authorize_league(plane, user(U2), states["private"])

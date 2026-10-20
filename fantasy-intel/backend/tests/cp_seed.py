"""Seeding and role switching for control-plane tests. Seeding runs as the superuser (bypasses RLS); `as_role` runs
one transaction exactly the way the API does: SET LOCAL ROLE + transaction-local request.jwt.claims."""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"


def new_league_id() -> str:
    return "lg_" + secrets.token_urlsafe(18)


def seed_auth_user(con, uid: str) -> str:
    """A Supabase account (auth.users row). fi.app_users rows reference it and are deleted with it."""
    con.execute("INSERT INTO auth.users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", [uid])
    return uid


def seed_user(con, uid: str, *, admin: bool = False) -> str:
    seed_auth_user(con, uid)
    con.execute("INSERT INTO fi.app_users (id, is_platform_admin) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", [uid, admin])
    return uid


def seed_league(con, league_id: str | None = None, *, provider="espn", external_id=None, season=2026, name="League",
                visibility="public", status="active", checked_at="now()", deleted=False, epoch=0) -> str:
    """`checked_at`: "now()" (fresh probe), None (never probed) or an interval string like "25 hours" (that long ago)."""
    league_id = league_id or new_league_id()
    external_id = external_id or str(secrets.randbelow(10**9))
    if checked_at is None:
        checked_sql, extra = "NULL", []
    elif checked_at == "now()":
        checked_sql, extra = "now()", []
    else:
        checked_sql, extra = "now() - %s::interval", [checked_at]
    con.execute(f"""INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, status, visibility_epoch,
                        visibility_checked_at, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {checked_sql}, CASE WHEN %s THEN now() ELSE NULL END)""",
                [league_id, provider, external_id, season, name, visibility, status, epoch, *extra, deleted])
    return league_id


def seed_membership(con, uid: str, league_id: str, role: str = "viewer", *, revoked: bool = False) -> None:
    con.execute("INSERT INTO fi.league_memberships (user_id, league_id, role, revoked_at) "
                "VALUES (%s, %s, %s, CASE WHEN %s THEN now() END)", [uid, league_id, role, revoked])


def seed_snapshot(con, league_id: str, *, data_version="v1", global_version="g1", fence=1) -> None:
    con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) "
                "VALUES (%s, %s, %s, %s, %s)",
                [league_id, f"leagues/{league_id}/{data_version}.duckdb", data_version, fence, global_version])


def seed_job(con, league_id: str, status="queued", kind="initial", error_code=None) -> str:
    return con.execute("INSERT INTO fi.sync_jobs (league_id, kind, status, error_code, error_detail) "
                       "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
                       [league_id, kind, status, error_code, "redacted detail"]).fetchone()[0]


@contextmanager
def as_role(con, role: str, user_id: str | None = None):
    assert role in ("anon", "authenticated", "fi_sync", "fi_api")
    with con.transaction():
        con.execute(f"SET LOCAL ROLE {role}")
        claims = {"sub": user_id, "role": "authenticated", "aud": "authenticated"} if user_id else {"role": role}
        con.execute("SELECT set_config('request.jwt.claims', %s, true)", [json.dumps(claims)])
        yield con

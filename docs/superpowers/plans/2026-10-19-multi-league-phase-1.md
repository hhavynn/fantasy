# Multi-league Phase 1: Control plane (invisible) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Supabase control plane (schema, RLS, least-privilege roles, SECURITY DEFINER functions), verify Supabase JWTs in FastAPI, authorize every `/v2` league request server-side before anything else, and ship the credential envelope crypto, with nothing user-visible and production unchanged while `MULTILEAGUE_ENABLED` is off.

**Architecture:** A new Postgres schema `fi` in the Supabase project `fantasy-intel` (ref `kdikrcmvfuzqsgahqaxf`) holds users, leagues, memberships, pins, team selections, invites, encrypted credentials, the sync queue and snapshot pointers. The API connects as `fi_api` (member of `anon` and `authenticated`, `NOINHERIT`); each request runs one transaction with `SET LOCAL ROLE` and transaction-local `request.jwt.claims`, so Supabase's `auth.uid()` and RLS apply; `authorize_league()` reads through that transaction and returns an `Access` with the snapshot pointer. The old DuckDB tenancy store and the HMAC user assertion are deleted; the `/v2` router is rebuilt on the control plane with a minimal route set (providers, me, leagues, league view, pin, perspective). Tests run against a throwaway local Postgres cluster (or a CI service container) with a small shim for Supabase's `auth.uid()` and roles.

**Tech Stack:** FastAPI, psycopg 3 + psycopg_pool, PyJWT (ES256 via `cryptography`), `cryptography` (AES-256-GCM, RSA-OAEP-3072), Postgres 17 (Supabase) / Postgres 18 (local `initdb`), pytest, Supabase MCP (`apply_migration`) for the hosted project.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md). Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md).

## Global Constraints

- Free tiers only (Supabase free, Vercel Hobby). Nothing in this phase adds a paid feature.
- Flags default off. `MULTILEAGUE_ENABLED` unset ⇒ every `/v2` path answers 404 (existing middleware stays) and the API never opens a Postgres connection. Legacy routes and the Phase 1a snapshot read path are untouched.
- Every `/v2/leagues/{league_id}/…` request calls `authorize_league` before any other work. Unknown and unauthorized are both `404 {"detail": "League not found."}`. Anonymous callers may read public leagues; mutations by anonymous callers are `401` only after the league was found visible.
- Supabase JWTs are verified in FastAPI against the project JWKS (`<SUPABASE_URL>/auth/v1/.well-known/jwks.json`): `iss = <SUPABASE_URL>/auth/v1`, `aud = authenticated`, `exp` required, algorithms `ES256`/`RS256` only. No header ⇒ anonymous. Present-but-invalid ⇒ `401`. Header values are never logged.
- Roles: `fi_api` and `fi_sync` are `NOINHERIT`, `NOBYPASSRLS`, created `NOLOGIN` by migration (Havyn sets `LOGIN PASSWORD` out of band). `fi_api` has no grants on `fi.league_credentials`; `fi_sync` has no grants on `fi.league_memberships`, `fi.league_pins`, `fi.team_selections`, `fi.league_invites`, `fi.app_users`. The Supabase service-role key is never used by any code in this repo.
- Credential crypto: AES-256-GCM with a random 256-bit data key and 96-bit nonce; AAD = `b"fi-cred-v1" + b"\x1f" + league_id + b"\x1f" + credential_id + b"\x1f" + owner_user_id` (no key version); data key wrapped with RSA-OAEP (SHA-256, MGF1-SHA-256, 3072-bit) under `CREDENTIALS_PUBKEY_V<n>`; rewrap changes only `wrapped_data_key` + `wrap_key_version`.
- No secrets in commits, logs, PR text or chat: test keys are generated at test time; the repo credential scan (`tests/test_no_credential_leaks.py`) keeps running.
- The control plane lives in schema `fi`, never `public`, so Supabase's PostgREST (which exposes `public` with the anon key) never serves it.
- Use `uv` for every Python command from `fantasy-intel/backend`; use `npm` from `fantasy-intel/frontend`. Commit after every task on branch `h/multileague-phase-1`.

## Dependencies (Task 1 adds them)

| Package | Why |
|---|---|
| `psycopg[binary]>=3.2` | Postgres driver (binary wheel; standard-library-only was ruled out: SigV4 was fine for R2, but Postgres needs a real driver) |
| `psycopg-pool>=3.2` | Small per-process pool with lazy open (Vercel keeps instances warm) |
| `PyJWT>=2.9` | JWKS client + ES256/RS256 verification |
| `cryptography>=43` | AES-GCM, RSA-OAEP, EC key handling for PyJWT |

## File structure

| File | Responsibility |
|---|---|
| `fantasy-intel/supabase/migrations/20261020120000_control_plane.sql` | Schema `fi`, roles, tables, indexes, grants, RLS policies |
| `fantasy-intel/supabase/migrations/20261020120100_control_plane_functions.sql` | SECURITY DEFINER functions + audit immutability trigger |
| `fantasy-intel/supabase/down/20261020120000_control_plane.sql`, `…120100_….sql` | Down-migrations (drop functions / drop schema + roles) |
| `fantasy-intel/supabase/README.md` | How migrations are applied (MCP / `supabase db push`), the `fi_api` password runbook |
| `backend/app/control_plane/__init__.py` | Package docstring |
| `backend/app/control_plane/pg.py` | `ControlPlane` pool, `request_tx(principal)` with `SET LOCAL ROLE` + claims, `ControlPlaneUnavailable` |
| `backend/app/control_plane/jwt_auth.py` | Supabase JWT verification, `current_principal` dependency |
| `backend/app/control_plane/authz.py` | `Access`, `SnapshotPointer`, `authorize_league`, `LeagueNotFound` |
| `backend/app/control_plane/mutations.py` | `ensure_app_user`, pins, perspective, wrappers for the SQL functions |
| `backend/app/control_plane/credential_crypto.py` | Envelope encryption, rewrap, keyring |
| `backend/app/leagues/redaction.py` | `sanitize_error` (moved out of the deleted store) |
| `backend/app/leagues/context.py` | Trimmed: `Provider`, `AccessMethod` (`PUBLIC`, `COOKIES`), `LeagueKey`, `Principal`, id checks |
| `backend/app/leagues/credentials.py` | Trimmed: `Secret`, `EspnCookies`, errors |
| `backend/app/api/leagues_v2.py` | Rebuilt `/v2` router on the control plane |
| `backend/app/config.py` | New settings; three old ones removed |
| `backend/scripts/gen_credential_keypair.py` | RSA-3072 keypair generator (private key to a file, public PEM to stdout) |
| `backend/tests/pg_harness.py`, `backend/tests/pg_shim.sql` | Throwaway Postgres cluster / CI DSN, Supabase shim, migration runner |
| `backend/tests/test_control_plane_rls.py` | RLS + grant + no-BYPASSRLS tests |
| `backend/tests/test_control_plane_functions.py` | SECURITY DEFINER function tests |
| `backend/tests/test_control_plane_pg.py` | Request transaction + claims isolation |
| `backend/tests/test_jwt_auth.py` | JWT verification |
| `backend/tests/test_credential_crypto.py` | Crypto tests |
| `backend/tests/test_authorize_league.py` | `authorize_league` state matrix |
| `backend/tests/test_control_plane_mutations.py` | Pins, perspective, wrappers |
| `backend/tests/test_leagues_api.py` | Rewritten `/v2` API tests |
| `backend/tests/test_v2_authorization_matrix.py` | Router-enumerated matrix |
| `backend/tests/test_multileague_isolation.py`, `test_no_credential_leaks.py` | Trimmed to store-independent tests + new leak checks |
| `frontend/lib/user-assertion*.ts` | Deleted |
| `frontend/app/leagues/load.ts`, `actions.ts`, `frontend/lib/leagues.ts` | No identity plumbing until sign-in lands (Phase 2) |
| `docs/multileague/ADR-0002-supabase-control-plane.md`, `threat-model.md` | Decision record + addendum |
| `.github/workflows/ci.yml` | Postgres 17 service for the backend job |
| `fantasy-intel/README.md`, `fantasy-intel/.env.example` | Control-plane env vars + runbook |

Deleted: `backend/app/leagues/auth.py`, `store.py`, `cache.py`, `views.py`, `tenancy_schema.sql`; `backend/tests/test_leagues_auth.py`, `test_leagues_store.py`, `test_leagues_cache.py`; `frontend/lib/user-assertion.ts`, `user-assertion.server.ts`, `user-assertion.test.ts`.

---

### Task 1: Dependencies and the Postgres test harness

**Files:**
- Modify: `fantasy-intel/backend/pyproject.toml` (dependencies)
- Create: `fantasy-intel/backend/tests/pg_shim.sql`
- Create: `fantasy-intel/backend/tests/pg_harness.py`
- Modify: `fantasy-intel/backend/tests/conftest.py`
- Create: `fantasy-intel/backend/tests/test_pg_harness.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `tests/pg_harness.py` → `class PgCluster` with `.superuser_dsn(db)`, `.dsn(db, user)`; conftest fixtures `pg_cluster` (session), `pg_db` (session: database `fi_test` with shim + migrations applied, returns `PgDatabase(dsn_for(user) -> str)`), `pg` (function: truncated tables, returns the same `PgDatabase`). `PgDatabase.connect(user="postgres") -> psycopg.Connection` (autocommit=False, `prepare_threshold=None`). `PgDatabase.MIGRATIONS` = sorted list of `fantasy-intel/supabase/migrations/*.sql`.

- [ ] **Step 1: Add the runtime dependencies**

```bash
cd fantasy-intel/backend && git checkout -b h/multileague-phase-1 && uv add "psycopg[binary]>=3.2" "psycopg-pool>=3.2" "PyJWT>=2.9" "cryptography>=43"
```
Expected: `pyproject.toml` `dependencies` gains the four entries and `uv.lock` updates. Run `uv run python -c "import psycopg, psycopg_pool, jwt, cryptography; print('ok')"` → `ok`.

- [ ] **Step 2: Write the Supabase shim**

`tests/pg_shim.sql` (applied only by the test harness, never on Supabase, where these objects already exist):

```sql
-- Test-only stand-in for the parts of a Supabase project the control plane relies on.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN NOINHERIT; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN NOINHERIT; END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY, email text);
CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$;
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT nullif(auth.jwt() ->> 'sub', '')::uuid $$;
CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT nullif(auth.jwt() ->> 'role', '')::text $$;
GRANT USAGE ON SCHEMA auth TO anon, authenticated;
GRANT EXECUTE ON FUNCTION auth.jwt(), auth.uid(), auth.role() TO anon, authenticated;
```

- [ ] **Step 3: Write the harness**

`tests/pg_harness.py`:

```python
"""A throwaway Postgres for the control-plane tests.

Order of preference: FI_TEST_PG_DSN (CI: a postgres:17 service container, superuser DSN) → a local cluster made
with `initdb` + `pg_ctl` in a temp dir (unix socket only, trust auth, torn down at session end) → skip. In CI the
DSN is mandatory: tests must never silently skip there.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "fantasy-intel" / "supabase" / "migrations"
SHIM = Path(__file__).with_name("pg_shim.sql")
_PG_BIN_DIRS = ("", "/opt/homebrew/opt/postgresql@18/bin", "/opt/homebrew/opt/postgresql@17/bin",
                "/usr/lib/postgresql/17/bin", "/usr/lib/postgresql/16/bin")


def _find(binary: str) -> str | None:
    for d in _PG_BIN_DIRS:
        path = shutil.which(binary) if not d else (Path(d) / binary if (Path(d) / binary).exists() else None)
        if path:
            return str(path)
    return None


class PgCluster:
    def __init__(self):
        self.external_dsn = os.environ.get("FI_TEST_PG_DSN")
        self._dir: str | None = None

    def start(self) -> None:
        if self.external_dsn:
            return
        initdb, pg_ctl = _find("initdb"), _find("pg_ctl")
        if not (initdb and pg_ctl):
            if os.environ.get("CI"):
                pytest.fail("CI must provide FI_TEST_PG_DSN (postgres service) for the control-plane tests")
            pytest.skip("no Postgres available: set FI_TEST_PG_DSN or install postgresql (initdb/pg_ctl)")
        self._dir = tempfile.mkdtemp(prefix="fipg")        # short path: unix socket paths are limited to ~100 chars
        data = os.path.join(self._dir, "data")
        subprocess.run([initdb, "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)
        opts = f"-c listen_addresses='' -c unix_socket_directories='{self._dir}' -c fsync=off -c synchronous_commit=off"
        subprocess.run([pg_ctl, "-D", data, "-o", opts, "-l", os.path.join(self._dir, "log"), "-w", "start"],
                       check=True, capture_output=True)
        self._pg_ctl, self._data = pg_ctl, data

    def stop(self) -> None:
        if self._dir:
            subprocess.run([self._pg_ctl, "-D", self._data, "-m", "immediate", "-w", "stop"], capture_output=True)
            shutil.rmtree(self._dir, ignore_errors=True)

    def superuser_dsn(self, db: str = "postgres") -> str:
        if self.external_dsn:
            return psycopg.conninfo.make_conninfo(self.external_dsn, dbname=db)
        return f"host={self._dir} user=postgres dbname={db}"

    def dsn(self, db: str, user: str) -> str:
        return psycopg.conninfo.make_conninfo(self.superuser_dsn(db), user=user)


class PgDatabase:
    """One database with the shim + migrations applied. Connections for any role of the test cluster."""
    MIGRATIONS = sorted(MIGRATIONS_DIR.glob("*.sql"))
    FI_TABLES = ("audit_log", "league_credentials", "league_invites", "team_selections", "league_pins",
                 "league_memberships", "league_snapshot_history", "league_snapshots", "sync_jobs", "provider_slots",
                 "leagues", "app_users")

    def __init__(self, cluster: PgCluster, name: str = "fi_test"):
        self.cluster, self.name = cluster, name

    def create(self) -> None:
        with psycopg.connect(self.cluster.superuser_dsn(), autocommit=True) as con:
            con.execute(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)")
            con.execute(f"CREATE DATABASE {self.name}")
        with psycopg.connect(self.cluster.superuser_dsn(self.name), autocommit=True) as con:
            con.execute(SHIM.read_text())
            for path in self.MIGRATIONS:
                con.execute(path.read_text())
            # The migration creates the service roles NOLOGIN (Havyn sets LOGIN + a password on Supabase).
            con.execute("ALTER ROLE fi_api LOGIN; ALTER ROLE fi_sync LOGIN")

    def dsn_for(self, user: str = "postgres") -> str:
        return self.cluster.dsn(self.name, user)

    def connect(self, user: str = "postgres", **kw) -> psycopg.Connection:
        return psycopg.connect(self.dsn_for(user), prepare_threshold=None, **kw)

    def reset(self) -> None:
        with self.connect(autocommit=True) as con:
            con.execute("TRUNCATE " + ", ".join(f"fi.{t}" for t in self.FI_TABLES) + " RESTART IDENTITY CASCADE")
            con.execute("SELECT fi.seed_provider_slots()")

    def wait_ready(self, timeout: float = 20) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                psycopg.connect(self.cluster.superuser_dsn(), connect_timeout=2).close()
                return
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.25)
```

- [ ] **Step 4: Register the fixtures in conftest**

Append to `tests/conftest.py`:

```python
from pg_harness import PgCluster, PgDatabase


@pytest.fixture(scope="session")
def pg_cluster():
    cluster = PgCluster()
    cluster.start()
    try:
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(scope="session")
def pg_db(pg_cluster):
    db = PgDatabase(pg_cluster)
    db.wait_ready()
    db.create()
    return db


@pytest.fixture
def pg(pg_db):
    pg_db.reset()
    return pg_db
```

- [ ] **Step 5: Write the harness test**

`tests/test_pg_harness.py`:

```python
"""The throwaway Postgres: shim behaves like Supabase's auth helpers; migrations apply cleanly and idempotently."""
import json


def test_shim_auth_uid_reads_transaction_local_claims(pg):
    with pg.connect() as con:
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
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_pg_harness.py -v`
Expected: FAIL (no migrations directory yet → `fi.seed_provider_slots` / `fi` schema missing). The cluster itself must start: the failure must be a SQL error, not "no Postgres available".

- [ ] **Step 7: Add the CI service**

In `.github/workflows/ci.yml`, in job `backend`, add after `env:`:

```yaml
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: postgres
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s --health-timeout 5s --health-retries 10
        ports:
          - 5432:5432
```
and extend the job's `env:` with `FI_TEST_PG_DSN: postgresql://postgres:postgres@localhost:5432/postgres`.

- [ ] **Step 8: Commit**

```bash
git add fantasy-intel/backend/pyproject.toml fantasy-intel/backend/uv.lock fantasy-intel/backend/tests/pg_shim.sql fantasy-intel/backend/tests/pg_harness.py fantasy-intel/backend/tests/conftest.py fantasy-intel/backend/tests/test_pg_harness.py .github/workflows/ci.yml
git commit -m "Phase 1: Postgres test harness, control-plane dependencies, CI postgres service"
```
(The harness test goes green in Task 2.)

### Task 2: Migration 0001: schema `fi`, roles, grants, RLS

**Files:**
- Create: `fantasy-intel/supabase/migrations/20261020120000_control_plane.sql`
- Create: `fantasy-intel/supabase/down/20261020120000_control_plane.sql`
- Create: `fantasy-intel/backend/tests/cp_seed.py`
- Create: `fantasy-intel/backend/tests/test_control_plane_rls.py`

**Interfaces:**
- Produces: schema `fi` (tables listed in `PgDatabase.FI_TABLES`), roles `fi_api`, `fi_sync`, helper functions `fi.is_member(text)`, `fi.is_admin(text)`, `fi.seed_provider_slots()`. Test helpers in `tests/cp_seed.py`: `U1, U2, U3` (fixed uuids), `seed_user(con, uid, *, admin=False)`, `seed_league(con, league_id, *, visibility="public", status="active", checked_at="now()", deleted=False, epoch=0)`, `seed_membership(con, uid, league_id, role="viewer", revoked=False)`, `seed_snapshot(con, league_id, *, data_version="v1", global_version="g1", fence=1)`, `seed_job(con, league_id, status="queued", kind="initial")`, `as_role(con, role, user_id=None)` context manager, `new_league_id()`.

- [ ] **Step 1: Write the seed helpers**

`tests/cp_seed.py`:

```python
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


def seed_user(con, uid: str, *, admin: bool = False) -> str:
    con.execute("INSERT INTO fi.app_users (id, is_platform_admin) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", [uid, admin])
    return uid


def seed_league(con, league_id: str | None = None, *, provider="espn", external_id=None, season=2026, name="League",
                visibility="public", status="active", checked_at="now()", deleted=False, epoch=0) -> str:
    league_id = league_id or new_league_id()
    external_id = external_id or str(secrets.randbelow(10**9))
    checked_sql = "NULL" if checked_at is None else ("now()" if checked_at == "now()" else "now() - %s::interval")
    params = [league_id, provider, external_id, season, name, visibility, status, epoch]
    if checked_sql.endswith("interval"):
        params.append(checked_at)
    params.append(deleted)
    con.execute(f"""INSERT INTO fi.leagues (id, provider, external_id, season, name, visibility, status, visibility_epoch,
                        visibility_checked_at, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {checked_sql}, CASE WHEN %s THEN now() ELSE NULL END)""", params)
    return league_id


def seed_membership(con, uid: str, league_id: str, role: str = "viewer", *, revoked: bool = False) -> None:
    con.execute("INSERT INTO fi.league_memberships (user_id, league_id, role, revoked_at) VALUES (%s, %s, %s, CASE WHEN %s THEN now() END)",
                [uid, league_id, role, revoked])


def seed_snapshot(con, league_id: str, *, data_version="v1", global_version="g1", fence=1) -> None:
    con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) VALUES (%s, %s, %s, %s, %s)",
                [league_id, f"leagues/{league_id}/{data_version}.duckdb", data_version, fence, global_version])


def seed_job(con, league_id: str, status="queued", kind="initial", error_code=None) -> str:
    return con.execute("INSERT INTO fi.sync_jobs (league_id, kind, status, error_code, error_detail) VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
                       [league_id, kind, status, error_code, "redacted detail"]).fetchone()[0]


@contextmanager
def as_role(con, role: str, user_id: str | None = None):
    assert role in ("anon", "authenticated", "fi_sync", "fi_api")
    with con.transaction():
        con.execute(f"SET LOCAL ROLE {role}")
        claims = {"sub": user_id, "role": "authenticated", "aud": "authenticated"} if user_id else {"role": role}
        con.execute("SELECT set_config('request.jwt.claims', %s, true)", [json.dumps(claims)])
        yield con
```

- [ ] **Step 2: Write the failing RLS tests**

`tests/test_control_plane_rls.py`:

```python
"""Least privilege in the control plane: what each role can see and touch, straight against Postgres."""
import pytest
from psycopg.errors import InsufficientPrivilege, CheckViolation

from cp_seed import U1, U2, as_role, seed_league, seed_membership, seed_snapshot, seed_job, seed_user

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
        seed_user(con, U1); seed_user(con, U2)
        pub = seed_league(con, visibility="public", name="Public")
        priv = seed_league(con, visibility="private", name="Private")
        gone = seed_league(con, visibility="public", deleted=True, name="Deleted")
        seed_membership(con, U1, priv, "viewer")
        seed_snapshot(con, pub); seed_snapshot(con, priv); seed_snapshot(con, gone)
        seed_job(con, pub); seed_job(con, priv)
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
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s", [U1]); su.commit()
    with pg.connect(user="fi_api") as con:
        assert names(con, "authenticated", U1) == {"Public"}


def test_authenticated_sees_only_own_pins_selections_and_memberships(pg, world):
    with pg.connect(user="fi_api") as con:
        with as_role(con, "authenticated", U1) as c:
            assert c.execute("SELECT user_id::text FROM fi.league_pins").fetchall() == [(U1,)]
            assert c.execute("SELECT user_id::text FROM fi.team_selections").fetchall() == [(U1,)]
            assert c.execute("SELECT user_id::text FROM fi.league_memberships").fetchall() == [(U1,)]
        with as_role(con, "anon") as c:
            assert c.execute("SELECT count(*) FROM fi.team_selections").fetchone() == (0,) if False else True


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
        with pytest.raises(InsufficientPrivilege):
            con.execute("UPDATE fi.leagues SET name = 'x' WHERE id = %s", [world["pub"]])


def test_request_roles_can_only_touch_last_viewed_at_on_leagues(pg, world):
    with pg.connect(user="fi_api") as con:
        with as_role(con, "anon") as c:
            assert c.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [world["pub"]]).rowcount == 1
            assert c.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [world["priv"]]).rowcount == 0
        with pytest.raises(InsufficientPrivilege):
            with as_role(con, "authenticated", U1) as c:
                c.execute("UPDATE fi.leagues SET visibility = 'public' WHERE id = %s", [world["priv"]])


def test_a_user_cannot_pin_or_select_in_an_invisible_league(pg, world):
    from psycopg.errors import RaiseException, InsufficientPrivilege as IP  # noqa: F401
    with pg.connect(user="fi_api") as con:
        with pytest.raises(Exception) as err:
            with as_role(con, "authenticated", U2) as c:
                c.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now())", [U2, world["priv"]])
        assert "row-level security" in str(err.value)
        with pytest.raises(Exception):
            with as_role(con, "authenticated", U2) as c:
                c.execute("INSERT INTO fi.league_pins VALUES (%s, %s, now())", [U1, world["pub"]])   # someone else's pin


def test_a_user_cannot_promote_themselves_to_platform_admin(pg, world):
    with pg.connect(user="fi_api") as con, pytest.raises(InsufficientPrivilege):
        with as_role(con, "authenticated", U1) as c:
            c.execute("UPDATE fi.app_users SET is_platform_admin = true WHERE id = %s", [U1])


def test_anon_has_no_write_grants_at_all(pg, world):
    with pg.connect(user="fi_api") as con:
        for sql in ("INSERT INTO fi.league_pins VALUES (%s, %s, now())", "INSERT INTO fi.team_selections VALUES (%s, %s, '1', now())"):
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
        con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) VALUES (%s, %s, 'v9', 9, 'g')",
                    [world["pub"], f"leagues/{world['priv']}/v9.duckdb"])


def test_functions_are_not_executable_by_public(pg):
    with pg.connect() as con:
        rows = con.execute("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'fi' AND has_function_privilege('anon', p.oid, 'EXECUTE')
              AND p.proname NOT IN ('is_member', 'is_admin')""").fetchall()
    assert rows == []
```
(Remove the no-op `if False else True` line in `test_authenticated_sees_only_own_pins_selections_and_memberships` when writing the file: the anon branch should be `assert c.execute("SELECT count(*) FROM fi.league_pins").fetchone() == (0,)` guarded by `pytest.raises(InsufficientPrivilege)` since anon has no grant. Write it as a separate expectation.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_control_plane_rls.py tests/test_pg_harness.py -q`
Expected: errors from the session fixture (`fi.seed_provider_slots` does not exist) until the migration exists.

- [ ] **Step 4: Write the migration**

`fantasy-intel/supabase/migrations/20261020120000_control_plane.sql`:

```sql
-- Multi-league control plane (Phase 1). Schema `fi` is deliberately not `public`: Supabase's PostgREST serves
-- `public` to anyone holding the anon key, and nothing here may be reachable that way. Every grant is explicit.
-- Idempotent: safe to re-run. Down-migration: supabase/down/20261020120000_control_plane.sql.

CREATE SCHEMA IF NOT EXISTS fi;
REVOKE ALL ON SCHEMA fi FROM PUBLIC;

-- ---- roles -------------------------------------------------------------------------------------------
-- fi_api: the request path. NOINHERIT member of the two Supabase request roles; every request transaction does
-- SET LOCAL ROLE anon|authenticated, so RLS and auth.uid() apply. No grants of its own on fi.* tables.
-- fi_sync: GitHub Actions workers. Sync tables only; credentials only via fi.sync_credential (next migration).
-- Both are created NOLOGIN: the project owner sets LOGIN + a password out of band (never in a migration).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fi_api') THEN
    CREATE ROLE fi_api NOLOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fi_sync') THEN
    CREATE ROLE fi_sync NOLOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT anon, authenticated TO fi_api;
GRANT USAGE ON SCHEMA fi TO anon, authenticated, fi_api, fi_sync;

-- ---- tables ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fi.app_users (
  id                uuid PRIMARY KEY,                 -- auth.users.id = the JWT `sub`
  is_platform_admin boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fi.leagues (
  id                    text PRIMARY KEY CHECK (id ~ '^lg_[A-Za-z0-9_-]{16,64}$'),
  provider              text NOT NULL CHECK (provider IN ('espn', 'sleeper')),
  external_id           text NOT NULL CHECK (external_id ~ '^[0-9]{1,64}$'),
  season                integer NOT NULL CHECK (season BETWEEN 1990 AND 2100),
  name                  text CHECK (name IS NULL OR length(name) <= 200),
  visibility            text NOT NULL CHECK (visibility IN ('public', 'private')),
  visibility_checked_at timestamptz,
  visibility_epoch      bigint NOT NULL DEFAULT 0,
  status                text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'unavailable', 'dormant')),
  sync_fence            bigint NOT NULL DEFAULT 0,
  created_at            timestamptz NOT NULL DEFAULT now(),
  last_viewed_at        timestamptz,
  deleted_at            timestamptz,
  UNIQUE (provider, external_id, season)
);

CREATE TABLE IF NOT EXISTS fi.league_memberships (
  user_id    uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id  text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('admin', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  PRIMARY KEY (user_id, league_id)
);
CREATE INDEX IF NOT EXISTS league_memberships_league ON fi.league_memberships (league_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS fi.league_pins (
  user_id    uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id  text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, league_id)
);

CREATE TABLE IF NOT EXISTS fi.team_selections (
  user_id          uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  league_id        text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  team_external_id text NOT NULL CHECK (team_external_id ~ '^[A-Za-z0-9._:-]{1,64}$'),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, league_id)
);

CREATE TABLE IF NOT EXISTS fi.league_invites (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  league_id   text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  token_hash  bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),   -- sha256(token); the token is shown once
  created_by  uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  redeemed_by uuid REFERENCES fi.app_users(id) ON DELETE SET NULL,
  redeemed_at timestamptz,
  revoked_at  timestamptz
);

CREATE TABLE IF NOT EXISTS fi.league_credentials (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  league_id         text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  owner_user_id     uuid NOT NULL REFERENCES fi.app_users(id) ON DELETE CASCADE,   -- account deletion crypto-erases
  status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspect', 'invalid', 'revoked')),
  ciphertext        bytea NOT NULL,
  nonce             bytea NOT NULL CHECK (octet_length(nonce) = 12),
  aad_format        text NOT NULL DEFAULT 'fi-cred-v1' CHECK (aad_format = 'fi-cred-v1'),
  wrapped_data_key  bytea NOT NULL,
  wrap_key_version  text NOT NULL CHECK (wrap_key_version ~ '^v[0-9]{1,4}$'),
  failure_count     integer NOT NULL DEFAULT 0,
  first_failed_at   timestamptz,
  last_validated_at timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  revoked_at        timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS league_credentials_one_live ON fi.league_credentials (league_id)
  WHERE status IN ('active', 'suspect');

CREATE TABLE IF NOT EXISTS fi.sync_jobs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  league_id        text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  kind             text NOT NULL CHECK (kind IN ('initial', 'scheduled', 'manual', 'probe', 'recompute', 'rewrap')),
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead')),
  attempt          integer NOT NULL DEFAULT 0,
  max_attempts     integer NOT NULL DEFAULT 5,
  run_after        timestamptz NOT NULL DEFAULT now(),
  lease_owner      text,
  lease_token      bigint,
  lease_expires_at timestamptz,
  heartbeat_at     timestamptz,
  provider_slot    integer,
  idempotency_key  text,
  error_code       text,
  error_detail     text,                                 -- redacted before it is written; never shown to request roles
  global_version   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_running_per_league ON fi.sync_jobs (league_id) WHERE status = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_idempotency ON fi.sync_jobs (idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS sync_jobs_due ON fi.sync_jobs (run_after) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS sync_jobs_by_league ON fi.sync_jobs (league_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fi.provider_slots (
  provider         text NOT NULL CHECK (provider IN ('espn', 'sleeper')),
  slot_no          integer NOT NULL,
  job_id           uuid REFERENCES fi.sync_jobs(id) ON DELETE SET NULL,
  lease_expires_at timestamptz,
  PRIMARY KEY (provider, slot_no)
);
CREATE OR REPLACE FUNCTION fi.seed_provider_slots() RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
  INSERT INTO fi.provider_slots (provider, slot_no)
  SELECT 'espn', g FROM generate_series(1, 2) g UNION ALL SELECT 'sleeper', g FROM generate_series(1, 4) g
  ON CONFLICT DO NOTHING
$$;
SELECT fi.seed_provider_slots();

CREATE TABLE IF NOT EXISTS fi.league_snapshots (
  league_id      text PRIMARY KEY REFERENCES fi.leagues(id) ON DELETE CASCADE,
  object_key     text NOT NULL,
  data_version   text NOT NULL CHECK (data_version ~ '^[A-Za-z0-9._-]{1,64}$'),
  fence          bigint NOT NULL,
  global_version text NOT NULL CHECK (global_version ~ '^[A-Za-z0-9._-]{1,64}$'),
  committed_at   timestamptz NOT NULL DEFAULT now(),
  -- The object key is derived from the league id and version only: never from anything a user typed.
  CONSTRAINT league_snapshots_key_matches_league CHECK (object_key = 'leagues/' || league_id || '/' || data_version || '.duckdb')
);

CREATE TABLE IF NOT EXISTS fi.league_snapshot_history (
  id             bigserial PRIMARY KEY,
  league_id      text NOT NULL REFERENCES fi.leagues(id) ON DELETE CASCADE,
  object_key     text NOT NULL,
  data_version   text NOT NULL,
  fence          bigint NOT NULL,
  global_version text NOT NULL,
  committed_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS league_snapshot_history_league ON fi.league_snapshot_history (league_id, fence DESC);

CREATE TABLE IF NOT EXISTS fi.audit_log (
  id            bigserial PRIMARY KEY,
  at            timestamptz NOT NULL DEFAULT now(),
  actor_user_id uuid,
  action        text NOT NULL,
  target_type   text NOT NULL,
  target_id     text NOT NULL,
  meta          jsonb NOT NULL DEFAULT '{}'::jsonb        -- metadata only: never league data, names or secrets
);

-- ---- helper predicates (SECURITY DEFINER so anon never needs a grant on memberships) --------------------
CREATE OR REPLACE FUNCTION fi.is_member(p_league_id text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT EXISTS (SELECT 1 FROM fi.league_memberships m
                 WHERE m.league_id = p_league_id AND m.user_id = auth.uid() AND m.revoked_at IS NULL)
$$;
CREATE OR REPLACE FUNCTION fi.is_admin(p_league_id text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT EXISTS (SELECT 1 FROM fi.league_memberships m
                 WHERE m.league_id = p_league_id AND m.user_id = auth.uid() AND m.role = 'admin' AND m.revoked_at IS NULL)
$$;
REVOKE ALL ON FUNCTION fi.is_member(text), fi.is_admin(text), fi.seed_provider_slots() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.is_member(text), fi.is_admin(text) TO anon, authenticated;

-- ---- grants (column lists where a column must stay unreadable or unwritable) -----------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA fi FROM PUBLIC, anon, authenticated, fi_api, fi_sync;
GRANT SELECT ON fi.leagues, fi.league_snapshots TO anon, authenticated;
GRANT UPDATE (last_viewed_at) ON fi.leagues TO anon, authenticated;
GRANT SELECT (id, league_id, kind, status, attempt, run_after, error_code, created_at, started_at, finished_at)
  ON fi.sync_jobs TO anon, authenticated;
GRANT SELECT, INSERT ON fi.app_users TO authenticated;
GRANT UPDATE (last_seen_at) ON fi.app_users TO authenticated;
GRANT SELECT ON fi.league_memberships TO authenticated;
GRANT SELECT, INSERT, DELETE ON fi.league_pins TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON fi.team_selections TO authenticated;
GRANT SELECT (id, league_id, created_by, created_at, expires_at, redeemed_at, revoked_at) ON fi.league_invites TO authenticated;
GRANT SELECT ON fi.leagues TO fi_sync;
GRANT UPDATE (visibility, visibility_checked_at, visibility_epoch, status, sync_fence) ON fi.leagues TO fi_sync;
GRANT SELECT, INSERT, UPDATE, DELETE ON fi.sync_jobs, fi.provider_slots TO fi_sync;
GRANT SELECT, INSERT, UPDATE ON fi.league_snapshots TO fi_sync;
GRANT SELECT, INSERT, DELETE ON fi.league_snapshot_history TO fi_sync;
GRANT USAGE, SELECT ON SEQUENCE fi.league_snapshot_history_id_seq TO fi_sync;

-- ---- row-level security --------------------------------------------------------------------------------
ALTER TABLE fi.app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.leagues ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.team_selections ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_credentials ENABLE ROW LEVEL SECURITY;    -- no policies: nobody but the owner role reads it
ALTER TABLE fi.sync_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.provider_slots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.league_snapshot_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE fi.audit_log ENABLE ROW LEVEL SECURITY;             -- no policies: written by SECURITY DEFINER functions only

DROP POLICY IF EXISTS league_read ON fi.leagues;
CREATE POLICY league_read ON fi.leagues FOR SELECT TO anon, authenticated
  USING (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)));
DROP POLICY IF EXISTS league_touch ON fi.leagues;
CREATE POLICY league_touch ON fi.leagues FOR UPDATE TO anon, authenticated
  USING (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)))
  WITH CHECK (deleted_at IS NULL AND (visibility = 'public' OR fi.is_member(id)));
DROP POLICY IF EXISTS league_sync ON fi.leagues;
CREATE POLICY league_sync ON fi.leagues FOR ALL TO fi_sync USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS app_users_self_read ON fi.app_users;
CREATE POLICY app_users_self_read ON fi.app_users FOR SELECT TO authenticated USING (id = auth.uid());
DROP POLICY IF EXISTS app_users_self_insert ON fi.app_users;
CREATE POLICY app_users_self_insert ON fi.app_users FOR INSERT TO authenticated
  WITH CHECK (id = auth.uid() AND is_platform_admin = false);
DROP POLICY IF EXISTS app_users_self_update ON fi.app_users;
CREATE POLICY app_users_self_update ON fi.app_users FOR UPDATE TO authenticated USING (id = auth.uid()) WITH CHECK (id = auth.uid());

DROP POLICY IF EXISTS memberships_own ON fi.league_memberships;
CREATE POLICY memberships_own ON fi.league_memberships FOR SELECT TO authenticated USING (user_id = auth.uid());

DROP POLICY IF EXISTS pins_own ON fi.league_pins;
CREATE POLICY pins_own ON fi.league_pins FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS selections_own ON fi.team_selections;
CREATE POLICY selections_own ON fi.team_selections FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid() AND EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));

DROP POLICY IF EXISTS invites_admin_read ON fi.league_invites;
CREATE POLICY invites_admin_read ON fi.league_invites FOR SELECT TO authenticated USING (fi.is_admin(league_id));

DROP POLICY IF EXISTS snapshots_visible ON fi.league_snapshots;
CREATE POLICY snapshots_visible ON fi.league_snapshots FOR SELECT TO anon, authenticated
  USING (EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
DROP POLICY IF EXISTS snapshots_sync ON fi.league_snapshots;
CREATE POLICY snapshots_sync ON fi.league_snapshots FOR ALL TO fi_sync USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS snapshot_history_sync ON fi.league_snapshot_history;
CREATE POLICY snapshot_history_sync ON fi.league_snapshot_history FOR ALL TO fi_sync USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS jobs_visible ON fi.sync_jobs;
CREATE POLICY jobs_visible ON fi.sync_jobs FOR SELECT TO anon, authenticated
  USING (EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = league_id));
DROP POLICY IF EXISTS jobs_sync ON fi.sync_jobs;
CREATE POLICY jobs_sync ON fi.sync_jobs FOR ALL TO fi_sync USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS slots_sync ON fi.provider_slots;
CREATE POLICY slots_sync ON fi.provider_slots FOR ALL TO fi_sync USING (true) WITH CHECK (true);
```

- [ ] **Step 5: Write the down-migration**

`fantasy-intel/supabase/down/20261020120000_control_plane.sql`:

```sql
-- Reverses 20261020120000_control_plane.sql. Run only when rolling Phase 1 back entirely.
DROP SCHEMA IF EXISTS fi CASCADE;
REVOKE anon, authenticated FROM fi_api;
DROP ROLE IF EXISTS fi_api;
DROP ROLE IF EXISTS fi_sync;
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_pg_harness.py tests/test_control_plane_rls.py -q`
Expected: all PASS. If `GRANT anon, authenticated TO fi_api` fails locally, the shim did not create the roles before the migration ran: check `PgDatabase.create` order.

- [ ] **Step 7: Commit**

```bash
git add fantasy-intel/supabase fantasy-intel/backend/tests/cp_seed.py fantasy-intel/backend/tests/test_control_plane_rls.py
git commit -m "Phase 1: control-plane schema fi with least-privilege roles, grants and RLS"
```

### Task 3: Migration 0002: SECURITY DEFINER functions and the append-only audit log

**Files:**
- Create: `fantasy-intel/supabase/migrations/20261020120100_control_plane_functions.sql`
- Create: `fantasy-intel/supabase/down/20261020120100_control_plane_functions.sql`
- Create: `fantasy-intel/backend/tests/test_control_plane_functions.py`

**Interfaces:**
- Produces SQL functions (all `SECURITY DEFINER`, `SET search_path = ''`, EXECUTE revoked from PUBLIC):
  - `fi.ensure_app_user() RETURNS uuid` (authenticated)
  - `fi.create_invite(p_league_id text, p_token_hash bytea, p_ttl interval DEFAULT '7 days') RETURNS uuid` (authenticated, league admin)
  - `fi.revoke_invite(p_invite_id uuid) RETURNS void` (authenticated, league admin)
  - `fi.redeem_invite(p_token_hash bytea) RETURNS text` → league id (authenticated); every failure raises `invite_invalid`
  - `fi.promote_admin(p_league_id text, p_user uuid) RETURNS void`, `fi.demote_admin(p_league_id text, p_user uuid) RETURNS void` (authenticated, league admin; demoting the last admin raises `last_admin`)
  - `fi.store_credential(p_credential_id uuid, p_league_id text, p_ciphertext bytea, p_nonce bytea, p_wrapped_data_key bytea, p_wrap_key_version text) RETURNS uuid` (authenticated, active member; raises `credential_exists` when another member's live credential exists; the caller's own live credential is deleted and replaced in the same transaction)
  - `fi.revoke_credential(p_league_id text) RETURNS void` (authenticated, credential owner; deletes the row; league → `stale`)
  - `fi.sync_credential(p_job_id uuid, p_lease_token bigint) RETURNS TABLE (credential_id uuid, league_id text, owner_user_id uuid, ciphertext bytea, nonce bytea, aad_format text, wrapped_data_key bytea, wrap_key_version text)` (fi_sync only; rows only for a `running` job whose `lease_token` matches and whose lease has not expired)
  - `fi.mark_credential(p_job_id uuid, p_lease_token bigint, p_status text) RETURNS boolean` (fi_sync only; `active`↔`suspect`→`invalid`; `invalid` sets the league `stale`)
  - Every function writes `fi.audit_log` via `fi.audit(...)` (owner-only). `fi.audit_log` rejects UPDATE/DELETE by trigger.

- [ ] **Step 1: Write the failing tests**

`tests/test_control_plane_functions.py`:

```python
"""SECURITY DEFINER functions: each one checks auth.uid() itself and refuses other users; the audit log only grows."""
import hashlib
import secrets
import uuid

import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from cp_seed import U1, U2, U3, as_role, seed_league, seed_membership, seed_user

TOKEN = secrets.token_bytes(16)
HASH = hashlib.sha256(TOKEN).digest()
CT, NONCE, WRAPPED = b"\x01" * 48, b"\x02" * 12, b"\x03" * 384


@pytest.fixture
def league(pg):
    with pg.connect() as con:
        seed_user(con, U1); seed_user(con, U2); seed_user(con, U3)
        lid = seed_league(con, visibility="private")
        seed_membership(con, U1, lid, "admin")
        seed_membership(con, U2, lid, "viewer")
        con.commit()
    return lid


def call(con, role, uid, sql, params=()):
    with as_role(con, role, uid) as c:
        return c.execute(sql, params).fetchall()


def raises(con, role, uid, sql, params=(), match=None):
    with pytest.raises((RaiseException, InsufficientPrivilege), match=match):
        call(con, role, uid, sql, params)


def test_ensure_app_user_inserts_once_and_refuses_anonymous(pg):
    with pg.connect(user="fi_api") as con:
        uid = str(uuid.uuid4())
        assert call(con, "authenticated", uid, "SELECT fi.ensure_app_user()::text") == [(uid,)]
        assert call(con, "authenticated", uid, "SELECT fi.ensure_app_user()::text") == [(uid,)]
        assert call(con, "authenticated", uid, "SELECT count(*) FROM fi.app_users WHERE id = %s", [uid]) == [(1,)]
        raises(con, "anon", None, "SELECT fi.ensure_app_user()")


def test_only_admins_create_invites_and_ttl_is_capped(pg, league):
    with pg.connect(user="fi_api") as con:
        (invite_id,), = call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        assert uuid.UUID(str(invite_id))
        raises(con, "authenticated", U2, "SELECT fi.create_invite(%s, %s)", [league, hashlib.sha256(b"x").digest()])
        raises(con, "authenticated", U3, "SELECT fi.create_invite(%s, %s)", [league, hashlib.sha256(b"y").digest()])
        raises(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s, interval '8 days')", [league, hashlib.sha256(b"z").digest()], "ttl")
        # admins see invite metadata but never the hash
        assert call(con, "authenticated", U1, "SELECT count(*) FROM fi.league_invites WHERE league_id = %s", [league]) == [(1,)]
        with pytest.raises(InsufficientPrivilege):
            call(con, "authenticated", U1, "SELECT token_hash FROM fi.league_invites")


def test_redeem_is_single_use_signed_in_only_and_audited(pg, league):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        assert call(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH]) == [(league,)]
        assert call(con, "authenticated", U3, "SELECT role FROM fi.league_memberships WHERE league_id = %s", [league]) == [("viewer",)]
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH], "invite_invalid")      # used
        raises(con, "authenticated", U2, "SELECT fi.redeem_invite(%s)", [hashlib.sha256(b"nope").digest()], "invite_invalid")
        raises(con, "anon", None, "SELECT fi.redeem_invite(%s)", [HASH])
    with pg.connect() as su:
        actions = [r[0] for r in su.execute("SELECT action FROM fi.audit_log ORDER BY id")]
    assert actions == ["invite.create", "invite.redeem"]


def test_expired_and_revoked_invites_are_the_same_failure(pg, league):
    h2 = hashlib.sha256(b"two").digest()
    with pg.connect(user="fi_api") as con:
        (inv,), = call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, h2])
        call(con, "authenticated", U1, "SELECT fi.revoke_invite(%s)", [inv])
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [HASH], "invite_invalid")
    with pg.connect() as su:
        su.execute("UPDATE fi.league_invites SET expires_at = now() - interval '1 minute' WHERE token_hash = %s", [h2]); su.commit()
    with pg.connect(user="fi_api") as con:
        raises(con, "authenticated", U3, "SELECT fi.redeem_invite(%s)", [h2], "invite_invalid")


def test_redeeming_never_grants_admin_and_a_revoked_member_comes_back_as_viewer(pg, league):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s", [U2]); su.commit()
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
        call(con, "authenticated", U2, "SELECT fi.redeem_invite(%s)", [HASH])
        assert call(con, "authenticated", U2, "SELECT role, revoked_at FROM fi.league_memberships WHERE league_id = %s", [league]) == [("viewer", None)]


def test_admin_promotion_and_the_last_admin_rule(pg, league):
    with pg.connect(user="fi_api") as con:
        raises(con, "authenticated", U2, "SELECT fi.promote_admin(%s, %s)", [league, U2])     # viewers cannot
        raises(con, "authenticated", U1, "SELECT fi.demote_admin(%s, %s)", [league, U1], "last_admin")
        raises(con, "authenticated", U1, "SELECT fi.promote_admin(%s, %s)", [league, U3])     # not a member
        call(con, "authenticated", U1, "SELECT fi.promote_admin(%s, %s)", [league, U2])
        call(con, "authenticated", U2, "SELECT fi.demote_admin(%s, %s)", [league, U1])
        assert call(con, "authenticated", U1, "SELECT role FROM fi.league_memberships WHERE league_id = %s", [league]) == [("viewer",)]
        raises(con, "authenticated", U2, "SELECT fi.demote_admin(%s, %s)", [league, U2], "last_admin")


STORE = "SELECT fi.store_credential(%s, %s, %s, %s, %s, 'v1')::text"


def test_store_credential_requires_membership_and_one_live_credential_per_league(pg, league):
    with pg.connect(user="fi_api") as con:
        c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
        raises(con, "authenticated", U3, STORE, [c1, league, CT, NONCE, WRAPPED])                 # stranger
        assert call(con, "authenticated", U2, STORE, [c1, league, CT, NONCE, WRAPPED]) == [(c1,)]    # viewer may
        raises(con, "authenticated", U1, STORE, [c2, league, CT, NONCE, WRAPPED], "credential_exists")
        assert call(con, "authenticated", U2, STORE, [c2, league, CT, NONCE, WRAPPED]) == [(c2,)]    # owner replaces
        raises(con, "anon", None, STORE, [c1, league, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        rows = su.execute("SELECT id::text, owner_user_id::text, status FROM fi.league_credentials").fetchall()
    assert rows == [(c2, U2, "active")]


def test_fi_api_itself_cannot_call_the_user_functions(pg, league):
    with pg.connect(user="fi_api") as con, pytest.raises(InsufficientPrivilege):
        con.execute(STORE, [str(uuid.uuid4()), league, CT, NONCE, WRAPPED])


def test_revoke_credential_is_owner_only_deletes_the_row_and_marks_the_league_stale(pg, league):
    cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U2, STORE, [cid, league, CT, NONCE, WRAPPED])
        raises(con, "authenticated", U1, "SELECT fi.revoke_credential(%s)", [league])
        call(con, "authenticated", U2, "SELECT fi.revoke_credential(%s)", [league])
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_credentials").fetchone() == (0,)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [league]).fetchone() == ("stale",)


@pytest.fixture
def leased_job(pg, league):
    cid = str(uuid.uuid4())
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U2, STORE, [cid, league, CT, NONCE, WRAPPED])
    with pg.connect() as su:
        job = su.execute("""INSERT INTO fi.sync_jobs (league_id, kind, status, lease_token, lease_expires_at)
                            VALUES (%s, 'scheduled', 'running', 7, now() + interval '2 minutes') RETURNING id::text""", [league]).fetchone()[0]
        su.commit()
    return {"job": job, "cid": cid}


def test_sync_credential_needs_the_current_lease(pg, league, leased_job):
    q = "SELECT credential_id::text, league_id, wrap_key_version FROM fi.sync_credential(%s, %s)"
    with pg.connect(user="fi_sync") as con:
        assert con.execute(q, [leased_job["job"], 7]).fetchall() == [(leased_job["cid"], league, "v1")]
        assert con.execute(q, [leased_job["job"], 6]).fetchall() == []
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [leased_job["job"]])
        assert con.execute(q, [leased_job["job"], 7]).fetchall() == []
        con.rollback()
    with pg.connect(user="fi_api") as con:
        with pytest.raises(InsufficientPrivilege):
            call(con, "authenticated", U2, q, [leased_job["job"], 7])


def test_mark_credential_transitions(pg, league, leased_job):
    with pg.connect(user="fi_sync") as con:
        m = lambda s: con.execute("SELECT fi.mark_credential(%s, %s, %s)", [leased_job["job"], 7, s]).fetchone()[0]
        assert m("suspect") is True
        assert m("active") is True
        assert m("suspect") is True and m("invalid") is True
        assert con.execute("SELECT fi.mark_credential(%s, %s, 'active')", [leased_job["job"], 6]).fetchone() == (False,)
        con.commit()
    with pg.connect() as su:
        assert su.execute("SELECT status FROM fi.league_credentials").fetchone() == ("invalid",)
        assert su.execute("SELECT status FROM fi.leagues WHERE id = %s", [league]).fetchone() == ("stale",)


def test_audit_log_is_append_only_even_for_the_owner(pg, league):
    with pg.connect(user="fi_api") as con:
        call(con, "authenticated", U1, "SELECT fi.create_invite(%s, %s)", [league, HASH])
    with pg.connect() as su:
        for sql in ("UPDATE fi.audit_log SET action = 'x'", "DELETE FROM fi.audit_log"):
            with pytest.raises(RaiseException, match="append-only"):
                su.execute(sql)
            su.rollback()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_control_plane_functions.py -q`
Expected: FAIL with `function fi.ensure_app_user() does not exist` and similar.

- [ ] **Step 3: Write the functions migration**

`fantasy-intel/supabase/migrations/20261020120100_control_plane_functions.sql`:

```sql
-- SECURITY DEFINER functions for mutations RLS cannot express. Each one checks auth.uid() itself, pins search_path,
-- and is executable only by the role named below. Errors are raised with short machine-readable messages that the
-- API maps to 404/409 without echoing details.

CREATE OR REPLACE FUNCTION fi.audit(p_actor uuid, p_action text, p_target_type text, p_target_id text, p_meta jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = '' AS $$
  INSERT INTO fi.audit_log (actor_user_id, action, target_type, target_id, meta) VALUES (p_actor, p_action, p_target_type, p_target_id, coalesce(p_meta, '{}'::jsonb))
$$;
REVOKE ALL ON FUNCTION fi.audit(uuid, text, text, text, jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION fi.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'fi.audit_log is append-only';
END $$;
DROP TRIGGER IF EXISTS audit_log_immutable ON fi.audit_log;
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON fi.audit_log FOR EACH ROW EXECUTE FUNCTION fi.audit_log_immutable();

CREATE OR REPLACE FUNCTION fi.require_uid() RETURNS uuid LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := auth.uid();
BEGIN
  IF v IS NULL THEN RAISE EXCEPTION 'sign_in_required'; END IF;
  RETURN v;
END $$;
REVOKE ALL ON FUNCTION fi.require_uid() FROM PUBLIC;

CREATE OR REPLACE FUNCTION fi.ensure_app_user() RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  RETURN v;
END $$;

CREATE OR REPLACE FUNCTION fi.create_invite(p_league_id text, p_token_hash bytea, p_ttl interval DEFAULT interval '7 days')
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_id uuid;
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  IF p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '7 days' THEN RAISE EXCEPTION 'ttl must be within 7 days'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = p_league_id AND l.deleted_at IS NULL) THEN RAISE EXCEPTION 'not_admin'; END IF;
  INSERT INTO fi.league_invites (league_id, token_hash, created_by, expires_at) VALUES (p_league_id, p_token_hash, v, now() + p_ttl) RETURNING id INTO v_id;
  PERFORM fi.audit(v, 'invite.create', 'league', p_league_id, jsonb_build_object('invite_id', v_id));
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION fi.revoke_invite(p_invite_id uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_league text;
BEGIN
  SELECT league_id INTO v_league FROM fi.league_invites WHERE id = p_invite_id FOR UPDATE;
  IF v_league IS NULL OR NOT fi.is_admin(v_league) THEN RAISE EXCEPTION 'not_admin'; END IF;
  UPDATE fi.league_invites SET revoked_at = coalesce(revoked_at, now()) WHERE id = p_invite_id;
  PERFORM fi.audit(v, 'invite.revoke', 'league', v_league, jsonb_build_object('invite_id', p_invite_id));
END $$;

CREATE OR REPLACE FUNCTION fi.redeem_invite(p_token_hash bytea) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_inv fi.league_invites%ROWTYPE;
BEGIN
  SELECT * INTO v_inv FROM fi.league_invites WHERE token_hash = p_token_hash FOR UPDATE;
  IF v_inv.id IS NULL OR v_inv.redeemed_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL OR v_inv.expires_at <= now()
     OR NOT EXISTS (SELECT 1 FROM fi.leagues l WHERE l.id = v_inv.league_id AND l.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'invite_invalid';
  END IF;
  INSERT INTO fi.app_users (id) VALUES (v) ON CONFLICT (id) DO UPDATE SET last_seen_at = now();
  -- Viewer only. An existing admin keeps admin; a revoked member comes back as a viewer.
  INSERT INTO fi.league_memberships (user_id, league_id, role) VALUES (v, v_inv.league_id, 'viewer')
  ON CONFLICT (user_id, league_id) DO UPDATE
    SET role = CASE WHEN fi.league_memberships.revoked_at IS NULL THEN fi.league_memberships.role ELSE 'viewer' END,
        revoked_at = NULL;
  UPDATE fi.league_invites SET redeemed_by = v, redeemed_at = now() WHERE id = v_inv.id;
  PERFORM fi.audit(v, 'invite.redeem', 'league', v_inv.league_id, jsonb_build_object('invite_id', v_inv.id));
  RETURN v_inv.league_id;
END $$;

CREATE OR REPLACE FUNCTION fi.promote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid();
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  UPDATE fi.league_memberships SET role = 'admin' WHERE league_id = p_league_id AND user_id = p_user AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.promote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;

CREATE OR REPLACE FUNCTION fi.demote_admin(p_league_id text, p_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_admins integer;
BEGIN
  IF NOT fi.is_admin(p_league_id) THEN RAISE EXCEPTION 'not_admin'; END IF;
  PERFORM 1 FROM fi.leagues WHERE id = p_league_id FOR UPDATE;      -- serialise last-admin checks per league
  SELECT count(*) INTO v_admins FROM fi.league_memberships WHERE league_id = p_league_id AND role = 'admin' AND revoked_at IS NULL;
  IF v_admins <= 1 THEN RAISE EXCEPTION 'last_admin'; END IF;
  UPDATE fi.league_memberships SET role = 'viewer' WHERE league_id = p_league_id AND user_id = p_user AND role = 'admin' AND revoked_at IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM fi.audit(v, 'membership.demote', 'league', p_league_id, jsonb_build_object('user_id', p_user));
END $$;

CREATE OR REPLACE FUNCTION fi.store_credential(p_credential_id uuid, p_league_id text, p_ciphertext bytea, p_nonce bytea,
                                               p_wrapped_data_key bytea, p_wrap_key_version text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_owner uuid;
BEGIN
  IF NOT fi.is_member(p_league_id) THEN RAISE EXCEPTION 'not_a_member'; END IF;
  PERFORM 1 FROM fi.leagues WHERE id = p_league_id AND deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_a_member'; END IF;
  SELECT owner_user_id INTO v_owner FROM fi.league_credentials WHERE league_id = p_league_id AND status IN ('active', 'suspect');
  IF v_owner IS NOT NULL AND v_owner <> v THEN RAISE EXCEPTION 'credential_exists'; END IF;
  -- The caller's previous credential (any status) is destroyed in the same transaction: ciphertext and wrapped key go.
  DELETE FROM fi.league_credentials WHERE league_id = p_league_id AND owner_user_id = v;
  INSERT INTO fi.league_credentials (id, league_id, owner_user_id, ciphertext, nonce, wrapped_data_key, wrap_key_version, last_validated_at)
  VALUES (p_credential_id, p_league_id, v, p_ciphertext, p_nonce, p_wrapped_data_key, p_wrap_key_version, now());
  PERFORM fi.audit(v, 'credential.store', 'league', p_league_id, jsonb_build_object('credential_id', p_credential_id, 'wrap_key_version', p_wrap_key_version));
  RETURN p_credential_id;
END $$;

CREATE OR REPLACE FUNCTION fi.revoke_credential(p_league_id text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v uuid := fi.require_uid(); v_id uuid;
BEGIN
  DELETE FROM fi.league_credentials WHERE league_id = p_league_id AND owner_user_id = v RETURNING id INTO v_id;
  IF v_id IS NULL THEN RAISE EXCEPTION 'not_owner'; END IF;
  IF NOT EXISTS (SELECT 1 FROM fi.league_credentials WHERE league_id = p_league_id AND status IN ('active', 'suspect')) THEN
    UPDATE fi.leagues SET status = 'stale' WHERE id = p_league_id AND visibility = 'private';
  END IF;
  PERFORM fi.audit(v, 'credential.revoke', 'league', p_league_id, jsonb_build_object('credential_id', v_id));
END $$;

CREATE OR REPLACE FUNCTION fi.sync_credential(p_job_id uuid, p_lease_token bigint)
RETURNS TABLE (credential_id uuid, league_id text, owner_user_id uuid, ciphertext bytea, nonce bytea, aad_format text,
               wrapped_data_key bytea, wrap_key_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
  SELECT c.id, c.league_id, c.owner_user_id, c.ciphertext, c.nonce, c.aad_format, c.wrapped_data_key, c.wrap_key_version
  FROM fi.sync_jobs j JOIN fi.league_credentials c ON c.league_id = j.league_id
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now()
    AND c.status IN ('active', 'suspect')
$$;

CREATE OR REPLACE FUNCTION fi.mark_credential(p_job_id uuid, p_lease_token bigint, p_status text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_league text; v_old text;
BEGIN
  IF p_status NOT IN ('active', 'suspect', 'invalid') THEN RAISE EXCEPTION 'bad_status'; END IF;
  SELECT j.league_id INTO v_league FROM fi.sync_jobs j
  WHERE j.id = p_job_id AND j.status = 'running' AND j.lease_token = p_lease_token AND j.lease_expires_at > now();
  IF v_league IS NULL THEN RETURN false; END IF;
  SELECT status INTO v_old FROM fi.league_credentials WHERE league_id = v_league AND status IN ('active', 'suspect') FOR UPDATE;
  IF v_old IS NULL THEN RETURN false; END IF;
  UPDATE fi.league_credentials SET
    status = p_status,
    failure_count = CASE WHEN p_status = 'active' THEN 0 ELSE failure_count + 1 END,
    first_failed_at = CASE WHEN p_status = 'active' THEN NULL ELSE coalesce(first_failed_at, now()) END,
    last_validated_at = CASE WHEN p_status = 'active' THEN now() ELSE last_validated_at END
  WHERE league_id = v_league AND status IN ('active', 'suspect');
  IF p_status = 'invalid' THEN UPDATE fi.leagues SET status = 'stale' WHERE id = v_league; END IF;
  PERFORM fi.audit(NULL, 'credential.' || p_status, 'league', v_league, jsonb_build_object('job_id', p_job_id));
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION fi.ensure_app_user(), fi.create_invite(text, bytea, interval), fi.revoke_invite(uuid), fi.redeem_invite(bytea),
  fi.promote_admin(text, uuid), fi.demote_admin(text, uuid), fi.store_credential(uuid, text, bytea, bytea, bytea, text),
  fi.revoke_credential(text), fi.sync_credential(uuid, bigint), fi.mark_credential(uuid, bigint, text), fi.audit_log_immutable() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fi.ensure_app_user(), fi.create_invite(text, bytea, interval), fi.revoke_invite(uuid), fi.redeem_invite(bytea),
  fi.promote_admin(text, uuid), fi.demote_admin(text, uuid), fi.store_credential(uuid, text, bytea, bytea, bytea, text),
  fi.revoke_credential(text) TO authenticated;
GRANT EXECUTE ON FUNCTION fi.sync_credential(uuid, bigint), fi.mark_credential(uuid, bigint, text) TO fi_sync;
```

Down-migration `supabase/down/20261020120100_control_plane_functions.sql`:

```sql
DROP TRIGGER IF EXISTS audit_log_immutable ON fi.audit_log;
DROP FUNCTION IF EXISTS fi.mark_credential(uuid, bigint, text), fi.sync_credential(uuid, bigint), fi.revoke_credential(text),
  fi.store_credential(uuid, text, bytea, bytea, bytea, text), fi.demote_admin(text, uuid), fi.promote_admin(text, uuid),
  fi.redeem_invite(bytea), fi.revoke_invite(uuid), fi.create_invite(text, bytea, interval), fi.ensure_app_user(),
  fi.require_uid(), fi.audit_log_immutable(), fi.audit(uuid, text, text, text, jsonb);
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_control_plane_functions.py tests/test_control_plane_rls.py -q`
Expected: PASS. Note `test_functions_are_not_executable_by_public` in the RLS file now covers the new functions too (anon must have EXECUTE on none of them).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/supabase fantasy-intel/backend/tests/test_control_plane_functions.py
git commit -m "Phase 1: SECURITY DEFINER functions (invites, admin transfer, credentials, sync_credential) and append-only audit"
```

### Task 4: Request transactions: `ControlPlane.request_tx` with `SET LOCAL ROLE` and claims isolation

**Files:**
- Create: `fantasy-intel/backend/app/control_plane/__init__.py`
- Create: `fantasy-intel/backend/app/control_plane/pg.py`
- Modify: `fantasy-intel/backend/app/leagues/context.py` (`Principal` gains `claims_json`)
- Create: `fantasy-intel/backend/tests/test_control_plane_pg.py`

**Interfaces:**
- Consumes: `Principal` from `app/leagues/context.py`.
- Produces: `Principal(user_id: str | None, claims_json: str = ANON_CLAIMS)` (frozen dataclass; `claims_json` has `repr=False`; `Principal.anonymous()`; `Principal.from_claims(claims: dict) -> Principal` requiring a uuid `sub`). `class ControlPlaneUnavailable(RuntimeError)`. `class ControlPlane(dsn: str, *, max_size: int = 4)` with `request_tx(principal) -> ContextManager[psycopg.Connection]`, `sync_tx()` (not in this phase), `close()`. Module function `get_control_plane() -> ControlPlane` (one per DSN per process; raises `ControlPlaneUnavailable` when `SUPABASE_DB_URL` is unset).

- [ ] **Step 1: Write the failing tests**

`tests/test_control_plane_pg.py`:

```python
"""Each request is one transaction that runs as anon|authenticated with transaction-local claims. Claims must never
survive the transaction or leak between two transactions that share a pooled connection (Supavisor transaction mode)."""
import json
import threading

import pytest

from app.control_plane.pg import ControlPlane, ControlPlaneUnavailable
from app.leagues.context import Principal
from cp_seed import U1, U2


def principal(uid):
    return Principal.from_claims({"sub": uid, "role": "authenticated", "aud": "authenticated", "exp": 2_000_000_000})


@pytest.fixture
def cp(pg):
    plane = ControlPlane(pg.dsn_for("fi_api"), max_size=1)      # one physical connection: every tx shares it
    yield plane
    plane.close()


def who(con):
    uid, user, claims = con.execute("SELECT auth.uid()::text, current_user, current_setting('request.jwt.claims', true)").fetchone()
    return uid, user, claims


def test_claims_and_role_are_transaction_local_on_a_shared_connection(cp):
    with cp.request_tx(principal(U1)) as con:
        assert who(con)[:2] == (U1, "authenticated")
    with cp.request_tx(Principal.anonymous()) as con:
        assert who(con) == (None, "anon", json.dumps({"role": "anon"}))
    with cp.request_tx(principal(U2)) as con:
        assert who(con)[:2] == (U2, "authenticated")
    with cp._pool.connection() as raw:                           # the same physical connection, outside any request
        assert who(raw) in ((None, "fi_api", ""), (None, "fi_api", None))


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
            a_in.set(); b_done.wait(5)
            seen["a2"] = who(con)[0]

    def b():
        a_in.wait(5)
        with plane.request_tx(principal(U2)) as con:
            seen["b"] = who(con)[0]
        b_done.set()

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start(); tb.start(); ta.join(10); tb.join(10)
    plane.close()
    assert seen == {"a1": U1, "a2": U1, "b": U2}


def test_the_request_role_cannot_escape_rls_with_set_role(cp):
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with cp.request_tx(Principal.anonymous()) as con:
            con.execute("SET LOCAL ROLE postgres")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with cp.request_tx(principal(U1)) as con:
            con.execute("RESET ROLE; SELECT count(*) FROM fi.league_credentials")


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
    assert p.user_id == U1 and "sub" in p.claims_json and U1 not in repr(p)


def test_get_control_plane_requires_the_dsn(monkeypatch):
    from app.control_plane import pg as module
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(module, "_PLANES", {})
    with pytest.raises(ControlPlaneUnavailable):
        module.get_control_plane()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_control_plane_pg.py -q` → FAIL: `No module named app.control_plane`.

- [ ] **Step 3: Extend `Principal`**

In `app/leagues/context.py` replace the `Principal` dataclass with:

```python
ANON_CLAIMS = '{"role": "anon"}'
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Principal:
    """Who is asking. `user_id` is the verified Supabase JWT `sub` (a uuid), never a query parameter.
    `claims_json` is the verified claim set as compact JSON, set as transaction-local `request.jwt.claims` so
    Postgres's auth.uid() / RLS see exactly what the API verified. It is excluded from repr()."""
    user_id: str | None
    claims_json: str = field(default=ANON_CLAIMS, repr=False, compare=False)

    def __post_init__(self):
        if self.user_id is not None and not _UUID.fullmatch(self.user_id):
            raise ValueError("malformed user id")

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls(None)

    @classmethod
    def from_claims(cls, claims: dict) -> "Principal":
        sub = claims.get("sub") if isinstance(claims, dict) else None
        if not isinstance(sub, str) or not _UUID.fullmatch(sub.lower()):
            raise ValueError("claims must carry a uuid subject")
        return cls(sub.lower(), json.dumps(claims, separators=(",", ":"), sort_keys=True, default=str))
```
(add `import json` at the top; keep `_USER_ID` removed.)

- [ ] **Step 4: Write `pg.py`**

`app/control_plane/__init__.py`:

```python
"""Supabase control plane: authorization, preferences and the sync queue for the multi-league platform.

Nothing here runs unless MULTILEAGUE_ENABLED is on; the legacy single-league routes never import it. See
docs/multileague/ADR-0002-supabase-control-plane.md."""
```

`app/control_plane/pg.py`:

```python
"""Postgres access for the request path.

The API connects as `fi_api` (NOINHERIT, member of anon + authenticated, no table grants of its own). Every request
runs exactly one transaction: `SET LOCAL ROLE anon|authenticated` and transaction-local `request.jwt.claims`, so
Supabase's auth.uid() and the RLS policies decide what the request can see. Both settings end with the transaction,
which is what makes a Supavisor transaction-mode pool safe: the next transaction on the same physical connection
starts as plain fi_api with no claims. Server-side prepared statements are off (Supavisor transaction mode)."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ..config import get_settings
from ..leagues.context import Principal

log = logging.getLogger(__name__)

_ROLE_SQL = {True: "SET LOCAL ROLE authenticated", False: "SET LOCAL ROLE anon"}   # constants, never built from input
STATEMENT_TIMEOUT_MS = 5000


class ControlPlaneUnavailable(RuntimeError):
    """Postgres cannot be reached or is not configured. Routes answer 503; nothing is ever treated as 'denied'."""


class ControlPlane:
    def __init__(self, dsn: str, *, max_size: int = 4):
        self._pool = ConnectionPool(dsn, min_size=0, max_size=max_size, open=False, timeout=5,
                                    kwargs={"prepare_threshold": None, "autocommit": False, "connect_timeout": 5},
                                    check=ConnectionPool.check_connection)
        self._lock = threading.Lock()
        self._opened = False

    def _ensure_open(self) -> None:
        with self._lock:
            if not self._opened:
                self._pool.open(wait=False)
                self._opened = True

    @contextmanager
    def request_tx(self, principal: Principal) -> Iterator[psycopg.Connection]:
        if not isinstance(principal, Principal):
            raise TypeError("request_tx needs a Principal")
        self._ensure_open()
        try:
            with self._pool.connection() as con:
                with con.transaction():
                    con.execute(_ROLE_SQL[principal.is_authenticated])
                    con.execute("SELECT set_config('request.jwt.claims', %s, true)", [principal.claims_json])
                    con.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                    yield con
        except (psycopg.OperationalError, PoolTimeout) as err:
            log.warning("control plane unavailable: %s", type(err).__name__)
            raise ControlPlaneUnavailable("The control-plane database is unavailable.") from None

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self._pool.close()
                self._opened = False


_PLANES: dict[str, ControlPlane] = {}
_PLANES_LOCK = threading.Lock()


def get_control_plane() -> ControlPlane:
    dsn = get_settings().supabase_db_url
    if not dsn:
        raise ControlPlaneUnavailable("SUPABASE_DB_URL is not configured.")
    with _PLANES_LOCK:
        plane = _PLANES.get(dsn)
        if plane is None:
            plane = _PLANES[dsn] = ControlPlane(dsn)
        return plane
```

Add to `Settings` in `app/config.py`: `supabase_db_url: str | None = field(default=None, repr=False)` and in `get_settings()`: `supabase_db_url=_str_or_none(os.getenv("SUPABASE_DB_URL"))`. (Task 9 removes the old settings.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_control_plane_pg.py -q` → PASS. If `test_the_request_role_cannot_escape_rls_with_set_role` fails on the `RESET ROLE` case, note that `RESET ROLE` inside the transaction returns to `fi_api`, which has no grant on `league_credentials`, so the SELECT must still raise `InsufficientPrivilege`; keep the assertion.

- [ ] **Step 6: Commit**

```bash
git add fantasy-intel/backend/app/control_plane fantasy-intel/backend/app/leagues/context.py fantasy-intel/backend/app/config.py fantasy-intel/backend/tests/test_control_plane_pg.py
git commit -m "Phase 1: ControlPlane request transactions with SET LOCAL ROLE and transaction-local claims"
```

### Task 5: Supabase JWT verification (JWKS) and the `current_principal` dependency

**Files:**
- Create: `fantasy-intel/backend/app/control_plane/jwt_auth.py`
- Modify: `fantasy-intel/backend/app/config.py` (`supabase_url`)
- Create: `fantasy-intel/backend/tests/test_jwt_auth.py`

**Interfaces:**
- Produces: `class InvalidToken(ValueError)`; `verify_supabase_jwt(token: str, *, jwks: jwt.PyJWKClient, issuer: str, now: float | None = None) -> dict` (verified claims); `jwks_client(supabase_url: str) -> jwt.PyJWKClient` (cached per URL, `lifespan=3600`); `current_principal(request: Request) -> Principal` FastAPI dependency; `ISSUER_SUFFIX = "/auth/v1"`, `AUDIENCE = "authenticated"`, `ALGORITHMS = ("ES256", "RS256")`.
- Settings: `supabase_url: str | None` (`SUPABASE_URL`, trailing slash stripped) and property `supabase_issuer` → `f"{supabase_url}/auth/v1"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_jwt_auth.py`:

```python
"""Supabase access tokens are verified here, not trusted from the frontend: signature against the project JWKS,
issuer, audience, expiry. Absent header = anonymous; present-but-bad = 401, never anonymous, never logged."""
import json
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.control_plane.jwt_auth import InvalidToken, current_principal, jwks_client, verify_supabase_jwt
from app.leagues.context import Principal

URL = "https://example-project.supabase.co"
ISS = URL + "/auth/v1"
SUB = str(uuid.uuid4())


class FakeJwks:
    """A PyJWKClient whose fetch is served from memory; records how often the key set was fetched."""
    def __init__(self, keys: dict[str, object]):
        self.keys, self.fetches = keys, 0
        self.client = jwt.PyJWKClient(URL + "/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600)
        self.client.fetch_data = self._fetch        # type: ignore[method-assign]

    def _fetch(self):
        self.fetches += 1
        return {"keys": [{**jwt.algorithms.ECAlgorithm.to_jwk(k.public_key(), as_dict=True), "kid": kid, "alg": "ES256", "use": "sig"}
                         if isinstance(k, ec.EllipticCurvePrivateKey) else
                         {**jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key(), as_dict=True), "kid": kid, "alg": "RS256", "use": "sig"}
                         for kid, k in self.keys.items()]}


@pytest.fixture(scope="module")
def keys():
    return {"k1": ec.generate_private_key(ec.SECP256R1()), "k2": ec.generate_private_key(ec.SECP256R1()),
            "r1": rsa.generate_private_key(65537, 2048)}


@pytest.fixture
def jwks(keys):
    return FakeJwks({"k1": keys["k1"], "r1": keys["r1"]})


def mint(keys, kid="k1", alg="ES256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "sub": SUB, "aud": "authenticated", "role": "authenticated", "iat": now, "exp": now + 3600, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, keys[kid], algorithm=alg, headers={"kid": kid})


def test_valid_tokens_verify_for_es256_and_rs256(keys, jwks):
    assert verify_supabase_jwt(mint(keys), jwks=jwks.client, issuer=ISS)["sub"] == SUB
    assert verify_supabase_jwt(mint(keys, kid="r1", alg="RS256"), jwks=jwks.client, issuer=ISS)["sub"] == SUB
    assert jwks.fetches == 1                     # key set cached across calls


@pytest.mark.parametrize("bad", [
    dict(aud="anon"), dict(aud=None), dict(iss="https://other.supabase.co/auth/v1"), dict(exp=int(time.time()) - 10),
    dict(exp=None), dict(sub=None), dict(sub="not-a-uuid"), dict(nbf=int(time.time()) + 600),
])
def test_wrong_claims_are_rejected(keys, jwks, bad):
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(mint(keys, **bad), jwks=jwks.client, issuer=ISS)


def test_unknown_kid_refreshes_once_then_rejects(keys, jwks):
    token = mint(keys, kid="k2")
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)
    assert jwks.fetches >= 1
    jwks.keys["k2"] = keys["k2"]                 # key rotation: the new key appears in the published set
    assert verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)["sub"] == SUB


def test_symmetric_and_none_algorithms_are_never_accepted(keys, jwks):
    now = int(time.time())
    claims = {"iss": ISS, "sub": SUB, "aud": "authenticated", "exp": now + 60}
    for token in (jwt.encode(claims, "secret", algorithm="HS256", headers={"kid": "k1"}),
                  jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})):   # type: ignore[arg-type]
        with pytest.raises(InvalidToken):
            verify_supabase_jwt(token, jwks=jwks.client, issuer=ISS)


@pytest.mark.parametrize("garbage", ["", "a.b", "a.b.c", "x" * 5000, "Bearer", mint.__doc__ or "doc"])
def test_garbage_is_rejected(jwks, garbage):
    with pytest.raises(InvalidToken):
        verify_supabase_jwt(garbage, jwks=jwks.client, issuer=ISS)


@pytest.fixture
def client(keys, jwks, monkeypatch):
    import app.control_plane.jwt_auth as module
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setattr(module, "jwks_client", lambda url: jwks.client)
    api = FastAPI()

    @api.get("/whoami")
    def whoami(p: Principal = Depends(current_principal)):
        return {"user_id": p.user_id}

    return TestClient(api)


def test_dependency_maps_header_to_principal(client, keys, caplog):
    assert client.get("/whoami").json() == {"user_id": None}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {mint(keys)}"}).json() == {"user_id": SUB}
    with caplog.at_level(logging.DEBUG):
        bad = mint(keys, aud="anon")
        r = client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid or expired session."}
    assert bad not in caplog.text and bad[-20:] not in caplog.text
    for header in ("Basic abc", "Bearer", "Bearer  ", "Token x", f"bearer {mint(keys)}"):
        assert client.get("/whoami", headers={"Authorization": header}).status_code == 401, header


def test_dependency_is_401_when_supabase_url_is_not_configured(client, keys, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    assert client.get("/whoami").json() == {"user_id": None}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {mint(keys)}"}).status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_jwt_auth.py -q` → FAIL: `No module named app.control_plane.jwt_auth`.

- [ ] **Step 3: Implement**

`app/control_plane/jwt_auth.py`:

```python
"""Supabase access-token verification.

The Next.js server forwards the signed-in user's Supabase access token as `Authorization: Bearer <jwt>`; the service
API key (X-API-Key) only proves the caller is our dashboard. This module verifies the token itself against the
project's JWKS (ES256 keys today; RS256 accepted for a future rotation), checks `iss`, `aud` and `exp`, and turns the
verified claims into a Principal. No header is anonymous. A header that is present but unusable is a 401 and is never
downgraded to anonymous. Token values are never logged or echoed."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

import jwt
from fastapi import HTTPException, Request

from ..config import get_settings
from ..leagues.context import Principal

log = logging.getLogger(__name__)

ISSUER_SUFFIX = "/auth/v1"
JWKS_PATH = "/auth/v1/.well-known/jwks.json"
AUDIENCE = "authenticated"
ALGORITHMS = ("ES256", "RS256")
_MAX_TOKEN = 4096
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_.-]{20,4096})")      # exact scheme, one space, compact JWS alphabet
_CLIENTS: dict[str, jwt.PyJWKClient] = {}
_CLIENTS_LOCK = threading.Lock()


class InvalidToken(ValueError):
    """Deliberately vague; never carries the token."""


def jwks_client(supabase_url: str) -> jwt.PyJWKClient:
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(supabase_url)
        if client is None:
            client = _CLIENTS[supabase_url] = jwt.PyJWKClient(supabase_url + JWKS_PATH, cache_keys=True, lifespan=3600, timeout=5)
        return client


def verify_supabase_jwt(token: str, *, jwks: jwt.PyJWKClient, issuer: str, now: float | None = None) -> dict:
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN or token.count(".") != 2:
        raise InvalidToken("malformed")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken("algorithm")
        key = jwks.get_signing_key_from_jwt(token).key   # refreshes the key set once on an unknown kid
        claims = jwt.decode(token, key, algorithms=list(ALGORITHMS), audience=AUDIENCE, issuer=issuer,
                            options={"require": ["exp", "sub", "iss", "aud"], "verify_signature": True},
                            leeway=30)
    except InvalidToken:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise InvalidToken("rejected") from None
    sub = claims.get("sub")
    try:
        uuid.UUID(str(sub))
    except (ValueError, TypeError, AttributeError):
        raise InvalidToken("subject") from None
    if now is not None and claims["exp"] < now - 30:
        raise InvalidToken("expired")
    return claims


def current_principal(request: Request) -> Principal:
    """FastAPI dependency. Anonymous when there is no Authorization header; 401 when it is present but unusable."""
    raw = request.headers.get("authorization")
    if raw is None:
        return Principal.anonymous()
    settings = get_settings()
    m = _BEARER.fullmatch(raw.strip()) if settings.supabase_url else None
    if m is None:
        raise HTTPException(401, "Invalid or expired session.")
    try:
        claims = verify_supabase_jwt(m.group(1), jwks=jwks_client(settings.supabase_url), issuer=settings.supabase_issuer, now=time.time())
        return Principal.from_claims(claims)
    except (InvalidToken, ValueError):
        raise HTTPException(401, "Invalid or expired session.") from None
```

In `app/config.py`: add `supabase_url: str | None = None` to `Settings` with
```python
    @property
    def supabase_issuer(self) -> str | None:
        return f"{self.supabase_url}/auth/v1" if self.supabase_url else None
```
and in `get_settings()`: `supabase_url=(_str_or_none(os.getenv("SUPABASE_URL")) or "").rstrip("/") or None`.

- [ ] **Step 4: Run the tests** → `uv run pytest tests/test_jwt_auth.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/control_plane/jwt_auth.py fantasy-intel/backend/app/config.py fantasy-intel/backend/tests/test_jwt_auth.py
git commit -m "Phase 1: verify Supabase JWTs against the project JWKS in FastAPI"
```

### Task 6: Credential envelope crypto and the keypair generator

**Files:**
- Create: `fantasy-intel/backend/app/control_plane/credential_crypto.py`
- Create: `fantasy-intel/backend/scripts/gen_credential_keypair.py`
- Modify: `fantasy-intel/backend/app/config.py` (`credential_public_keys`, `credential_wrap_key_version`)
- Create: `fantasy-intel/backend/tests/test_credential_crypto.py`

**Interfaces:**
- Produces: `AAD_FORMAT = "fi-cred-v1"`; `build_aad(league_id: str, credential_id: str, owner_user_id: str) -> bytes`; `@dataclass(frozen=True) EncryptedCredential(ciphertext: bytes, nonce: bytes, aad_format: str, wrapped_data_key: bytes, wrap_key_version: str)` whose `repr` shows only lengths and the version; `class PublicKeyring(keys: dict[str, str])` (version → PEM) with `.current_version` and `.encrypt(payload: dict, *, league_id, credential_id, owner_user_id) -> EncryptedCredential`; `class PrivateKeyring(keys: dict[str, str])` with `.decrypt(enc, *, league_id, credential_id, owner_user_id) -> dict` and `.rewrap(enc, *, to_version: str, public: PublicKeyring) -> EncryptedCredential`; `class CredentialCryptoError(Exception)`; `generate_keypair() -> tuple[str, str]` (private PEM, public PEM); `Settings.credential_public_keys: dict[str, str]` from every `CREDENTIALS_PUBKEY_V<n>` env var and `Settings.credential_wrap_key_version` (`CREDENTIALS_WRAP_KEY_VERSION` or the highest `v<n>` present).

- [ ] **Step 1: Write the failing tests**

`tests/test_credential_crypto.py`:

```python
"""Envelope encryption for ESPN cookies: AES-256-GCM under a per-credential data key, the data key wrapped with
RSA-OAEP under a versioned public key the API holds; only GitHub Actions holds private keys."""
import logging
import pickle

import pytest
from cryptography.hazmat.primitives import serialization

from app.control_plane.credential_crypto import (
    AAD_FORMAT, CredentialCryptoError, EncryptedCredential, PrivateKeyring, PublicKeyring, build_aad, generate_keypair,
)

LEAGUE, CRED, OWNER = "lg_" + "a" * 24, "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "11111111-1111-4111-8111-111111111111"
COOKIES = {"espn_s2": "AEBtestcookie" + "x" * 60, "swid": "{11111111-2222-4333-8444-555555555555}"}


@pytest.fixture(scope="module")
def pairs():
    return {"v1": generate_keypair(), "v2": generate_keypair()}


@pytest.fixture
def pub(pairs):
    return PublicKeyring({"v1": pairs["v1"][1]})


@pytest.fixture
def priv(pairs):
    return PrivateKeyring({"v1": pairs["v1"][0], "v2": pairs["v2"][0]})


def test_round_trip_and_shape(pub, priv):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert (enc.aad_format, enc.wrap_key_version, len(enc.nonce), len(enc.wrapped_data_key)) == (AAD_FORMAT, "v1", 12, 384)
    assert priv.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    again = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert again.ciphertext != enc.ciphertext and again.nonce != enc.nonce      # fresh key + nonce every time


def test_aad_binds_the_row_and_excludes_the_key_version(pub, priv):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    for swap in (dict(league_id="lg_" + "b" * 24), dict(credential_id="1f1f1f1f-1f1f-4f1f-8f1f-1f1f1f1f1f1f"),
                 dict(owner_user_id="22222222-2222-4222-8222-222222222222")):
        kw = {"league_id": LEAGUE, "credential_id": CRED, "owner_user_id": OWNER, **swap}
        with pytest.raises(CredentialCryptoError):
            priv.decrypt(enc, **kw)
    assert b"v1" not in build_aad(LEAGUE, CRED, OWNER) and build_aad(LEAGUE, CRED, OWNER).startswith(b"fi-cred-v1\x1f")


def test_the_api_holds_only_public_keys_and_cannot_decrypt(pub, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert not hasattr(pub, "decrypt")
    with pytest.raises(CredentialCryptoError):
        PrivateKeyring({"v1": pairs["v1"][1]})          # a public PEM is not a private key
    with pytest.raises(CredentialCryptoError):
        PublicKeyring({"v1": pairs["v1"][0]})           # and the API refuses to be handed a private key


def test_rewrap_changes_only_the_wrapped_key_and_version(pub, priv, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    pub2 = PublicKeyring({"v1": pairs["v1"][1], "v2": pairs["v2"][1]})
    assert pub2.current_version == "v2"
    enc2 = priv.rewrap(enc, to_version="v2", public=pub2)
    assert (enc2.ciphertext, enc2.nonce, enc2.aad_format) == (enc.ciphertext, enc.nonce, enc.aad_format)
    assert enc2.wrap_key_version == "v2" and enc2.wrapped_data_key != enc.wrapped_data_key
    assert priv.decrypt(enc2, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    only_v2 = PrivateKeyring({"v2": pairs["v2"][0]})     # v1's private key deleted after rotation
    assert only_v2.decrypt(enc2, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    with pytest.raises(CredentialCryptoError):
        only_v2.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_a_row_is_only_ever_unwrapped_with_the_key_its_version_names(pub, priv, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    mislabeled = EncryptedCredential(enc.ciphertext, enc.nonce, enc.aad_format, enc.wrapped_data_key, "v2")
    with pytest.raises(CredentialCryptoError):
        priv.decrypt(mislabeled, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    wrong_format = EncryptedCredential(enc.ciphertext, enc.nonce, "fi-cred-v0", enc.wrapped_data_key, "v1")
    with pytest.raises(CredentialCryptoError):
        priv.decrypt(wrong_format, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_nothing_secret_appears_in_reprs_logs_or_pickles(pub, priv, caplog, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    text = repr(enc) + str(enc) + f"{enc}" + repr(pub) + repr(priv)
    for secret in (COOKIES["espn_s2"], COOKIES["swid"], enc.ciphertext.hex(), pairs["v1"][0][:40]):
        assert secret not in text
    assert "PRIVATE KEY" not in text
    with pytest.raises(TypeError):
        pickle.dumps(priv)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CredentialCryptoError):
            priv.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id="22222222-2222-4222-8222-222222222222")
    assert COOKIES["espn_s2"] not in caplog.text and "PRIVATE KEY" not in caplog.text


def test_payload_must_be_the_two_cookie_fields(pub):
    for bad in ({}, {"espn_s2": "x"}, {"espn_s2": "x", "swid": "y", "extra": 1}, {"espn_s2": 1, "swid": "y"}):
        with pytest.raises(CredentialCryptoError):
            pub.encrypt(bad, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_settings_collect_versioned_public_keys(monkeypatch, pairs):
    from app.config import get_settings
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V1", pairs["v1"][1])
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V3", pairs["v2"][1])
    monkeypatch.delenv("CREDENTIALS_WRAP_KEY_VERSION", raising=False)
    s = get_settings()
    assert set(s.credential_public_keys) == {"v1", "v3"} and s.credential_wrap_key_version == "v3"
    monkeypatch.setenv("CREDENTIALS_WRAP_KEY_VERSION", "v1")
    assert get_settings().credential_wrap_key_version == "v1"
    assert "BEGIN PUBLIC KEY" not in repr(get_settings())


def test_keypair_generator_writes_private_key_to_a_file_only(tmp_path, capsys):
    import subprocess, sys
    out = tmp_path / "k.pem"
    r = subprocess.run([sys.executable, "scripts/gen_credential_keypair.py", "--private-key-file", str(out)],
                       capture_output=True, text=True, cwd=__import__("pathlib").Path(__file__).resolve().parents[1])
    assert r.returncode == 0, r.stderr
    assert "BEGIN PUBLIC KEY" in r.stdout and "PRIVATE KEY" not in r.stdout
    assert "BEGIN PRIVATE KEY" in out.read_text() and (out.stat().st_mode & 0o777) == 0o600
    serialization.load_pem_private_key(out.read_bytes(), password=None)
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_credential_crypto.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

`app/control_plane/credential_crypto.py`:

```python
"""Envelope encryption for user-supplied ESPN cookies (spec §2 "Credential security").

  plaintext  = canonical JSON {"espn_s2": ..., "swid": ...}
  data key   = 32 random bytes per credential;  nonce = 12 random bytes
  ciphertext = AES-256-GCM(data key, nonce, plaintext, AAD)
  AAD        = b"fi-cred-v1" ‖ 0x1f ‖ league_id ‖ 0x1f ‖ credential_id ‖ 0x1f ‖ owner_user_id   (never the key version)
  wrapped    = RSA-OAEP-3072/SHA-256(public key CREDENTIALS_PUBKEY_V<n>, data key);  wrap_key_version = "v<n>"

The API process holds a PublicKeyring only: it can encrypt but not decrypt. GitHub Actions holds the PrivateKeyring.
Rotation (`rewrap`) unwraps with the version the row names and rewraps under the new one; ciphertext, nonce and
AAD stay byte-identical. Nothing in this module logs; reprs show lengths and versions only."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD_FORMAT = "fi-cred-v1"
RSA_BITS = 3072
_VERSION = re.compile(r"v[0-9]{1,4}")
_FIELDS = ("espn_s2", "swid")
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class CredentialCryptoError(Exception):
    """Any failure: wrong key, tampered row, bad input. The message never includes key or cookie material."""


def build_aad(league_id: str, credential_id: str, owner_user_id: str) -> bytes:
    parts = (AAD_FORMAT, league_id, credential_id, owner_user_id)
    if not all(isinstance(p, str) and p and "\x1f" not in p for p in parts):
        raise CredentialCryptoError("bad AAD component")
    return "\x1f".join(parts).encode("utf-8")


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes
    aad_format: str
    wrapped_data_key: bytes
    wrap_key_version: str

    def __repr__(self) -> str:
        return (f"EncryptedCredential(ciphertext=<{len(self.ciphertext)} bytes>, nonce=<{len(self.nonce)} bytes>, "
                f"aad_format={self.aad_format!r}, wrapped_data_key=<{len(self.wrapped_data_key)} bytes>, "
                f"wrap_key_version={self.wrap_key_version!r})")

    __str__ = __repr__


def _check_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CredentialCryptoError("bad key version")
    return version


def _version_number(version: str) -> int:
    return int(version[1:])


def _check_payload(payload: object) -> bytes:
    if not isinstance(payload, dict) or set(payload) != set(_FIELDS) or not all(isinstance(payload[f], str) and payload[f] for f in _FIELDS):
        raise CredentialCryptoError("payload must be {'espn_s2': str, 'swid': str}")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class PublicKeyring:
    """Versioned RSA public keys. The API's side: encrypt only."""
    __slots__ = ("_keys",)

    def __init__(self, keys: dict[str, str]):
        loaded = {}
        for version, pem in (keys or {}).items():
            _check_version(version)
            if "PRIVATE KEY" in pem:
                raise CredentialCryptoError("a private key was offered to the public keyring")
            try:
                key = serialization.load_pem_public_key(pem.encode("utf-8"))
            except (ValueError, TypeError):
                raise CredentialCryptoError("bad public key PEM") from None
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < RSA_BITS:
                raise CredentialCryptoError("public key must be RSA-3072 or larger")
            loaded[version] = key
        if not loaded:
            raise CredentialCryptoError("no credential public keys configured")
        object.__setattr__(self, "_keys", loaded)

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys, key=_version_number))

    @property
    def current_version(self) -> str:
        return self.versions[-1]

    def wrap(self, data_key: bytes, version: str) -> bytes:
        try:
            return self._keys[version].encrypt(data_key, _OAEP)
        except KeyError:
            raise CredentialCryptoError("unknown key version") from None

    def encrypt(self, payload: dict, *, league_id: str, credential_id: str, owner_user_id: str,
                version: str | None = None) -> EncryptedCredential:
        version = _check_version(version or self.current_version)
        plaintext = _check_payload(payload)
        data_key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, build_aad(league_id, credential_id, owner_user_id))
        return EncryptedCredential(ciphertext, nonce, AAD_FORMAT, self.wrap(data_key, version), version)

    def __repr__(self) -> str:
        return f"PublicKeyring(versions={self.versions})"

    def __reduce__(self):
        raise TypeError("keyrings cannot be serialized")


class PrivateKeyring:
    """Versioned RSA private keys. Sync workers only (GitHub Actions secrets)."""
    __slots__ = ("_keys",)

    def __init__(self, keys: dict[str, str]):
        loaded = {}
        for version, pem in (keys or {}).items():
            _check_version(version)
            try:
                key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
            except (ValueError, TypeError):
                raise CredentialCryptoError("bad private key PEM") from None
            if not isinstance(key, rsa.RSAPrivateKey):
                raise CredentialCryptoError("private key must be RSA")
            loaded[version] = key
        if not loaded:
            raise CredentialCryptoError("no credential private keys configured")
        object.__setattr__(self, "_keys", loaded)

    def _unwrap(self, enc: EncryptedCredential) -> bytes:
        if enc.aad_format != AAD_FORMAT:
            raise CredentialCryptoError("unknown AAD format")
        key = self._keys.get(_check_version(enc.wrap_key_version))    # exactly the version the row names
        if key is None:
            raise CredentialCryptoError("no private key for this row's version")
        try:
            data_key = key.decrypt(enc.wrapped_data_key, _OAEP)
        except ValueError:
            raise CredentialCryptoError("cannot unwrap data key") from None
        if len(data_key) != 32:
            raise CredentialCryptoError("bad data key")
        return data_key

    def decrypt(self, enc: EncryptedCredential, *, league_id: str, credential_id: str, owner_user_id: str) -> dict:
        data_key = self._unwrap(enc)
        try:
            plaintext = AESGCM(data_key).decrypt(enc.nonce, enc.ciphertext, build_aad(league_id, credential_id, owner_user_id))
            payload = json.loads(plaintext)
        except (InvalidTag, ValueError):
            raise CredentialCryptoError("credential does not authenticate") from None
        _check_payload(payload)
        return payload

    def rewrap(self, enc: EncryptedCredential, *, to_version: str, public: PublicKeyring) -> EncryptedCredential:
        data_key = self._unwrap(enc)
        return EncryptedCredential(enc.ciphertext, enc.nonce, enc.aad_format, public.wrap(data_key, _check_version(to_version)), to_version)

    def __repr__(self) -> str:
        return f"PrivateKeyring(versions={tuple(sorted(self._keys, key=_version_number))})"

    def __reduce__(self):
        raise TypeError("keyrings cannot be serialized")


def generate_keypair() -> tuple[str, str]:
    """(private PEM, public PEM). RSA-3072, PKCS8 / SubjectPublicKeyInfo, unencrypted (the private PEM lives only in
    a GitHub Actions secret)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private.decode("ascii"), public.decode("ascii")
```

`scripts/gen_credential_keypair.py`:

```python
"""Generate a credential wrapping keypair.

    uv run python scripts/gen_credential_keypair.py --private-key-file credentials_v1.private.pem

Writes the private key to that file (mode 0600; never printed) and prints the public PEM, which goes into the API's
CREDENTIALS_PUBKEY_V<n> env var. The private key goes only into the GitHub Actions secret CREDENTIALS_PRIVKEY_V<n>."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.control_plane.credential_crypto import generate_keypair  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private-key-file", required=True, type=Path)
    args = ap.parse_args()
    if args.private_key_file.exists():
        print(f"refusing to overwrite {args.private_key_file}", file=sys.stderr)
        return 2
    private, public = generate_keypair()
    fd = os.open(args.private_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(private)
    print(public, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

In `app/config.py` add to `Settings`: `credential_public_keys: dict = field(default_factory=dict, repr=False)` and `credential_wrap_key_version: str | None = None`; in `get_settings()`:

```python
    pubkeys = {f"v{m.group(1)}": v.strip() for k, v in os.environ.items()
               if (m := re.fullmatch(r"CREDENTIALS_PUBKEY_V([0-9]{1,4})", k)) and v.strip()}
    wrap_version = _str_or_none(os.getenv("CREDENTIALS_WRAP_KEY_VERSION")) or (max(pubkeys, key=lambda v: int(v[1:])) if pubkeys else None)
```
(add `import re`), and pass `credential_public_keys=pubkeys, credential_wrap_key_version=wrap_version`.

- [ ] **Step 4: Run the tests** → `uv run pytest tests/test_credential_crypto.py -q` → PASS (RSA-3072 generation takes a second or two per key; the fixture is module-scoped).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/control_plane/credential_crypto.py fantasy-intel/backend/scripts/gen_credential_keypair.py fantasy-intel/backend/app/config.py fantasy-intel/backend/tests/test_credential_crypto.py
git commit -m "Phase 1: credential envelope crypto (AES-256-GCM + RSA-OAEP-3072) and keypair generator"
```

### Task 7: `authorize_league` and `Access`

**Files:**
- Create: `fantasy-intel/backend/app/control_plane/authz.py`
- Create: `fantasy-intel/backend/tests/test_authorize_league.py`

**Interfaces:**
- Consumes: `ControlPlane.request_tx`, `Principal`, `check_internal_league_id` (from `app/leagues/context.py`, now raising `LeagueNotFound` from this module; keep the `LeagueAccessDenied` name as an alias for callers that still catch it).
- Produces:
```python
class LeagueNotFound(LookupError): ...          # callers answer 404; unknown == unauthorized
@dataclass(frozen=True)
class SnapshotPointer: object_key: str; data_version: str; global_version: str; committed_at: datetime
@dataclass(frozen=True)
class Access:
    league_id: str; provider: str; external_id: str; season: int; name: str | None
    visibility: str; status: str; visibility_epoch: int
    role: str | None            # 'admin' | 'viewer' | None (public reader)
    pinned: bool; perspective_team_id: str | None
    snapshot: SnapshotPointer | None
    sync_state: str             # 'ready' | 'first_sync_queued' | 'first_sync_running' | 'first_sync_failed' | 'no_data'
    principal: Principal
    @property is_member -> bool; is_admin -> bool
    def to_json(self) -> dict   # no principal, no object_key
PUBLIC_VISIBILITY_TTL = timedelta(hours=24)
def authorize_league_in(con, principal: Principal, league_id: str) -> Access      # inside an open request_tx
def authorize_league(plane: ControlPlane, principal: Principal, league_id: str) -> Access   # opens its own tx
```

- [ ] **Step 1: Write the failing tests**

`tests/test_authorize_league.py`:

```python
"""Every /v2 league request starts here. The matrix below is the spec's access model: visibility × membership ×
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
        for name in ("public_fresh", "public_expired", "public_unchecked", "public_dormant", "private", "private_stale", "deleted"):
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
    assert "object_key" not in str(body) and body["data"] == {"data_version": "20261020T1200", "global_version": "g7",
                                                                "committed_at": a.snapshot.committed_at.isoformat()}


@pytest.mark.parametrize("job,expected", [(None, "no_data"), ("queued", "first_sync_queued"), ("running", "first_sync_running"),
                                          ("failed", "first_sync_failed"), ("dead", "first_sync_failed")])
def test_sync_state_before_the_first_publish(pg, plane, states, job, expected):
    lid = states["public_fresh"]
    if job:
        with pg.connect() as con:
            seed_job(con, lid, status=job); con.commit()
    a = authorize_league(plane, ANON, lid)
    assert a.snapshot is None and a.sync_state == expected and a.to_json()["data"] is None


def test_a_revoked_member_of_a_private_league_is_a_stranger(pg, plane, states):
    with pg.connect() as con:
        con.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s AND league_id = %s", [U2, states["private"]]); con.commit()
    with pytest.raises(LeagueNotFound):
        authorize_league(plane, user(U2), states["private"])
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_authorize_league.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `authz.py`**

```python
"""Server-side authorization for one league request (spec §2).

authorize_league runs as the request's Postgres role, so RLS already hides private leagues from non-members and
deleted leagues from everyone; the Python layer adds the rules RLS cannot express per row: public access is valid only
while the last anonymous visibility probe is younger than 24 h (fail closed), and the response carries the current
snapshot pointer so the read path never looks anything up a second time. Unknown and unauthorized are the same
LeagueNotFound; nothing here distinguishes them to the caller."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..leagues.context import Principal
from .pg import ControlPlane

PUBLIC_VISIBILITY_TTL = timedelta(hours=24)
_LEAGUE_ID = re.compile(r"lg_[A-Za-z0-9_-]{16,64}")


class LeagueNotFound(LookupError):
    """The caller may not see this league, or it does not exist. HTTP 404, never 403."""


def check_league_id(value: object) -> str:
    if not isinstance(value, str) or not _LEAGUE_ID.fullmatch(value):
        raise LeagueNotFound("unknown league")
    return value


@dataclass(frozen=True)
class SnapshotPointer:
    object_key: str
    data_version: str
    global_version: str
    committed_at: datetime


@dataclass(frozen=True)
class Access:
    league_id: str
    provider: str
    external_id: str
    season: int
    name: str | None
    visibility: str
    status: str
    visibility_epoch: int
    role: str | None
    pinned: bool
    perspective_team_id: str | None
    snapshot: SnapshotPointer | None
    sync_state: str
    principal: Principal

    @property
    def is_member(self) -> bool:
        return self.role is not None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_json(self) -> dict:
        return {
            "league_id": self.league_id, "provider": self.provider, "external_league_id": self.external_id,
            "season": self.season, "name": self.name, "visibility": self.visibility, "status": self.status,
            "role": self.role, "pinned": self.pinned, "perspective_team_id": self.perspective_team_id,
            "sync_state": self.sync_state,
            "data": None if self.snapshot is None else {
                "data_version": self.snapshot.data_version, "global_version": self.snapshot.global_version,
                "committed_at": self.snapshot.committed_at.isoformat()},
        }


_BASE = """
SELECT l.id, l.provider, l.external_id, l.season, l.name, l.visibility, l.visibility_checked_at, l.visibility_epoch, l.status,
       {member_cols},
       s.object_key, s.data_version, s.global_version, s.committed_at,
       j.status AS job_status, now() AS db_now
FROM fi.leagues l
LEFT JOIN fi.league_snapshots s ON s.league_id = l.id
LEFT JOIN LATERAL (SELECT status FROM fi.sync_jobs x WHERE x.league_id = l.id ORDER BY created_at DESC LIMIT 1) j ON true
{member_joins}
WHERE l.id = %s
"""
# anon has no grants on the per-user tables, so its query must not touch them.
_SQL_ANON = _BASE.format(member_cols="NULL::text AS member_role, false AS pinned, NULL::text AS perspective", member_joins="")
_SQL_USER = _BASE.format(
    member_cols="m.role AS member_role, (p.user_id IS NOT NULL) AS pinned, t.team_external_id AS perspective",
    member_joins="""
LEFT JOIN fi.league_memberships m ON m.league_id = l.id AND m.user_id = auth.uid() AND m.revoked_at IS NULL
LEFT JOIN fi.league_pins p ON p.league_id = l.id AND p.user_id = auth.uid()
LEFT JOIN fi.team_selections t ON t.league_id = l.id AND t.user_id = auth.uid()""")


def _sync_state(has_snapshot: bool, job_status: str | None) -> str:
    if has_snapshot:
        return "ready"
    return {"queued": "first_sync_queued", "running": "first_sync_running",
            "failed": "first_sync_failed", "dead": "first_sync_failed"}.get(job_status or "", "no_data")


def authorize_league_in(con, principal: Principal, league_id: str) -> Access:
    league_id = check_league_id(league_id)
    sql = _SQL_USER if principal.is_authenticated else _SQL_ANON
    row = con.execute(sql, [league_id]).fetchone()
    if row is None:
        raise LeagueNotFound("unknown league")
    (lid, provider, external_id, season, name, visibility, checked_at, epoch, status,
     member_role, pinned, perspective, object_key, data_version, global_version, committed_at, job_status, db_now) = row
    if visibility != "public" and member_role is None:
        raise LeagueNotFound("unknown league")           # RLS already hides this; defence in depth
    if member_role is None:                              # public reader: the last anonymous probe must be fresh
        if checked_at is None or db_now - checked_at > PUBLIC_VISIBILITY_TTL:
            raise LeagueNotFound("unknown league")       # Phase 3: trigger a probe here (≤1 per league per 5 min)
    snapshot = None
    if object_key is not None:
        snapshot = SnapshotPointer(object_key, data_version, global_version, committed_at)
    return Access(lid, provider, external_id, int(season), name, visibility, status, int(epoch), member_role,
                  bool(pinned), perspective, snapshot, _sync_state(snapshot is not None, job_status), principal)


def authorize_league(plane: ControlPlane, principal: Principal, league_id: str) -> Access:
    league_id = check_league_id(league_id)               # before any connection is opened
    with plane.request_tx(principal) as con:
        return authorize_league_in(con, principal, league_id)
```

Also in `app/leagues/context.py`: delete `check_internal_league_id`, `LeagueAccessDenied`, `new_internal_league_id`'s dependents that referred to the store; keep `new_internal_league_id()`. (Task 9 removes the remaining dead code in one sweep.)

- [ ] **Step 4: Run** → `uv run pytest tests/test_authorize_league.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/control_plane/authz.py fantasy-intel/backend/tests/test_authorize_league.py
git commit -m "Phase 1: authorize_league decides visibility, membership and the snapshot pointer under RLS"
```

### Task 8: Mutations: users, pins, perspective, and wrappers for the SQL functions

**Files:**
- Create: `fantasy-intel/backend/app/control_plane/mutations.py`
- Create: `fantasy-intel/backend/tests/test_control_plane_mutations.py`

**Interfaces:**
- Produces (all take an open request-transaction connection `con`; the caller has already run `authorize_league_in` in the same transaction where a league is involved):
```python
class ControlPlaneError(Exception): code: str            # base; str(err) is safe to return
class SignInRequired(ControlPlaneError)                  # 401
class NotAdmin(ControlPlaneError), NotAMember(ControlPlaneError), NotOwner(ControlPlaneError)   # 403 → but routes answer 404 for leagues
class LastAdmin(ControlPlaneError), CredentialExists(ControlPlaneError)                         # 409
class InviteInvalid(ControlPlaneError)                                                           # 404
TEAM_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
def ensure_app_user(con) -> str
def me(con, principal) -> dict                 # {"user_id", "is_platform_admin", "created_at", "pins": [league ids]}
def list_my_leagues(con, principal) -> list[str]   # league ids the user is a member of or has pinned (visible ones)
def set_pin(con, principal, league_id, pinned: bool) -> None
def set_perspective(con, principal, league_id, team_external_id: str | None) -> None   # ValueError on bad format
def touch_league(con, league_id) -> None
def create_invite(con, principal, league_id, ttl_days: int = 7) -> tuple[str, str]      # (invite_id, token shown once)
def revoke_invite(con, principal, invite_id) -> None
def redeem_invite(con, principal, token: str) -> str     # league id
def promote_admin(con, principal, league_id, user_id) -> None; demote_admin(...)
def store_credential(con, principal, league_id, enc: EncryptedCredential, credential_id: str) -> str
def revoke_credential(con, principal, league_id) -> None
```
Tokens: `secrets.token_urlsafe(16)` (128 bits); stored hash `hashlib.sha256(token.encode()).digest()`. Postgres `RaiseException` messages (`err.diag.message_primary`) map: `sign_in_required`→`SignInRequired`, `not_admin`→`NotAdmin`, `not_a_member`→`NotAMember`, `not_owner`→`NotOwner`, `last_admin`→`LastAdmin`, `credential_exists`→`CredentialExists`, `invite_invalid`→`InviteInvalid`; anything else re-raises.

- [ ] **Step 1: Write the failing tests**

`tests/test_control_plane_mutations.py`:

```python
import pytest

from app.control_plane import mutations as m
from app.control_plane.authz import authorize_league_in
from app.control_plane.credential_crypto import PublicKeyring, generate_keypair
from app.control_plane.pg import ControlPlane
from app.leagues.context import Principal
from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user

ANON = Principal.anonymous()
user = lambda uid: Principal.from_claims({"sub": uid, "role": "authenticated"})


@pytest.fixture
def plane(pg):
    p = ControlPlane(pg.dsn_for("fi_api"), max_size=2)
    yield p
    p.close()


@pytest.fixture
def leagues(pg):
    with pg.connect() as con:
        for u in (U1, U2):
            seed_user(con, u)
        pub = seed_league(con, visibility="public")
        priv = seed_league(con, visibility="private", checked_at=None)
        seed_membership(con, U1, priv, "admin"); seed_membership(con, U2, priv, "viewer")
        con.commit()
    return pub, priv


def test_ensure_app_user_and_me(plane):
    with plane.request_tx(user(U3)) as con:
        assert m.ensure_app_user(con) == U3
        assert m.me(con, user(U3)) == {"user_id": U3, "is_platform_admin": False, "pins": [], "created_at": m.me(con, user(U3))["created_at"]}
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.ensure_app_user(con)


def test_pins_and_perspective_are_per_user_and_need_a_visible_league(plane, leagues):
    pub, priv = leagues
    with plane.request_tx(user(U1)) as con:
        m.ensure_app_user(con)
        m.set_pin(con, user(U1), pub, True); m.set_pin(con, user(U1), pub, True)        # idempotent
        m.set_perspective(con, user(U1), pub, "6")
        m.set_perspective(con, user(U1), pub, "7")                                      # upsert
        assert sorted(m.list_my_leagues(con, user(U1))) == sorted([pub, priv])
        a = authorize_league_in(con, user(U1), pub)
        assert a.pinned and a.perspective_team_id == "7"
    with plane.request_tx(user(U2)) as con:
        assert authorize_league_in(con, user(U2), pub).perspective_team_id is None
        with pytest.raises(ValueError):
            m.set_perspective(con, user(U2), pub, "6; DROP")
    with plane.request_tx(user(U1)) as con:
        m.set_pin(con, user(U1), pub, False); m.set_perspective(con, user(U1), pub, None)
        a = authorize_league_in(con, user(U1), pub)
        assert not a.pinned and a.perspective_team_id is None
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.set_pin(con, ANON, pub, True)


def test_touch_league_only_moves_last_viewed_at(pg, plane, leagues):
    pub, priv = leagues
    with plane.request_tx(ANON) as con:
        m.touch_league(con, pub); m.touch_league(con, priv)        # the private one is invisible: silently no-op
    with pg.connect() as su:
        rows = dict(su.execute("SELECT id, last_viewed_at IS NOT NULL FROM fi.leagues").fetchall())
    assert rows[pub] is True and rows[priv] is False


def test_invite_flow_through_the_wrappers(plane, leagues):
    _, priv = leagues
    with plane.request_tx(user(U1)) as con:
        invite_id, token = m.create_invite(con, user(U1), priv)
        assert len(token) >= 22 and token.isprintable()
        with pytest.raises(m.NotAdmin):
            m.create_invite(con, user(U2), priv) if False else None
    with plane.request_tx(user(U2)) as con, pytest.raises(m.NotAdmin):
        m.create_invite(con, user(U2), priv)
    with plane.request_tx(user(U3)) as con:
        assert m.redeem_invite(con, user(U3), token) == priv
        assert authorize_league_in(con, user(U3), priv).role == "viewer"
    with plane.request_tx(user(U3)) as con, pytest.raises(m.InviteInvalid):
        m.redeem_invite(con, user(U3), token)
    with plane.request_tx(user(U3)) as con, pytest.raises(m.InviteInvalid):
        m.redeem_invite(con, user(U3), "not a token")
    with plane.request_tx(ANON) as con, pytest.raises(m.SignInRequired):
        m.redeem_invite(con, ANON, token)


def test_admin_wrappers_map_errors(plane, leagues):
    _, priv = leagues
    with plane.request_tx(user(U1)) as con, pytest.raises(m.LastAdmin):
        m.demote_admin(con, user(U1), priv, U1)
    with plane.request_tx(user(U1)) as con:
        m.promote_admin(con, user(U1), priv, U2)
    with plane.request_tx(user(U2)) as con:
        m.demote_admin(con, user(U2), priv, U1)
    with plane.request_tx(user(U1)) as con, pytest.raises(m.NotAdmin):
        m.promote_admin(con, user(U1), priv, U1)


def test_credential_wrappers(plane, leagues):
    _, priv = leagues
    pub = PublicKeyring({"v1": generate_keypair()[1]})
    cid = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
    enc = pub.encrypt({"espn_s2": "AEBx" + "y" * 60, "swid": "{11111111-2222-4333-8444-555555555555}"},
                      league_id=priv, credential_id=cid, owner_user_id=U2)
    with plane.request_tx(user(U3)) as con, pytest.raises(m.NotAMember):
        m.store_credential(con, user(U3), priv, enc, cid)
    with plane.request_tx(user(U2)) as con:
        assert m.store_credential(con, user(U2), priv, enc, cid) == cid
    with plane.request_tx(user(U1)) as con, pytest.raises(m.CredentialExists):
        m.store_credential(con, user(U1), priv, enc, "1f1f1f1f-1f1f-4f1f-8f1f-1f1f1f1f1f1f")
    with plane.request_tx(user(U1)) as con, pytest.raises(m.NotOwner):
        m.revoke_credential(con, user(U1), priv)
    with plane.request_tx(user(U2)) as con:
        m.revoke_credential(con, user(U2), priv)
        assert authorize_league_in(con, user(U2), priv).status == "stale"
```
(Drop the stray `if False else None` line inside `test_invite_flow_through_the_wrappers` when writing the file.)

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/test_control_plane_mutations.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `mutations.py`**

```python
"""Writes the request path may perform, all inside the caller's request transaction (so RLS applies) and all after the
caller authorized the league in that same transaction. Anything RLS cannot express goes through the SECURITY DEFINER
functions of migration 0002; their short error codes are mapped to typed exceptions here."""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from psycopg.errors import RaiseException

from ..leagues.context import Principal
from .credential_crypto import EncryptedCredential

TEAM_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22,43}")


class ControlPlaneError(Exception):
    code = "error"


class SignInRequired(ControlPlaneError):
    code = "sign_in_required"


class NotAdmin(ControlPlaneError):
    code = "not_admin"


class NotAMember(ControlPlaneError):
    code = "not_a_member"


class NotOwner(ControlPlaneError):
    code = "not_owner"


class LastAdmin(ControlPlaneError):
    code = "last_admin"


class CredentialExists(ControlPlaneError):
    code = "credential_exists"


class InviteInvalid(ControlPlaneError):
    code = "invite_invalid"


_BY_CODE = {cls.code: cls for cls in (SignInRequired, NotAdmin, NotAMember, NotOwner, LastAdmin, CredentialExists, InviteInvalid)}


def _call(con, sql: str, params=()):
    """Run one SECURITY DEFINER call; translate its RAISE code into a typed error."""
    try:
        return con.execute(sql, params).fetchone()
    except RaiseException as err:
        code = (err.diag.message_primary or "").strip()
        if code in _BY_CODE:
            raise _BY_CODE[code](code) from None
        raise


def _require(principal: Principal) -> str:
    if not isinstance(principal, Principal) or not principal.is_authenticated:
        raise SignInRequired("sign_in_required")
    return principal.user_id  # type: ignore[return-value]


def ensure_app_user(con) -> str:
    row = _call(con, "SELECT fi.ensure_app_user()::text")
    return row[0]


def me(con, principal: Principal) -> dict:
    uid = _require(principal)
    row = con.execute("SELECT is_platform_admin, created_at FROM fi.app_users WHERE id = %s", [uid]).fetchone()
    if row is None:
        ensure_app_user(con)
        row = con.execute("SELECT is_platform_admin, created_at FROM fi.app_users WHERE id = %s", [uid]).fetchone()
    pins = [r[0] for r in con.execute("SELECT league_id FROM fi.league_pins WHERE user_id = %s ORDER BY created_at", [uid])]
    return {"user_id": uid, "is_platform_admin": bool(row[0]), "created_at": row[1].isoformat(), "pins": pins}


def list_my_leagues(con, principal: Principal) -> list[str]:
    uid = _require(principal)
    rows = con.execute("""
        SELECT l.id FROM fi.leagues l
        WHERE EXISTS (SELECT 1 FROM fi.league_pins p WHERE p.league_id = l.id AND p.user_id = %s)
           OR EXISTS (SELECT 1 FROM fi.league_memberships m WHERE m.league_id = l.id AND m.user_id = %s AND m.revoked_at IS NULL)
        ORDER BY l.season DESC, l.name, l.id""", [uid, uid]).fetchall()
    return [r[0] for r in rows]


def set_pin(con, principal: Principal, league_id: str, pinned: bool) -> None:
    uid = _require(principal)
    ensure_app_user(con)
    if pinned:
        con.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", [uid, league_id])
    else:
        con.execute("DELETE FROM fi.league_pins WHERE user_id = %s AND league_id = %s", [uid, league_id])


def set_perspective(con, principal: Principal, league_id: str, team_external_id: str | None) -> None:
    uid = _require(principal)
    if team_external_id is None:
        con.execute("DELETE FROM fi.team_selections WHERE user_id = %s AND league_id = %s", [uid, league_id])
        return
    if not isinstance(team_external_id, str) or not TEAM_ID.fullmatch(team_external_id):
        raise ValueError("malformed team id")
    ensure_app_user(con)
    con.execute("""INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, league_id) DO UPDATE SET team_external_id = EXCLUDED.team_external_id, updated_at = now()""",
                [uid, league_id, team_external_id])


def touch_league(con, league_id: str) -> None:
    con.execute("UPDATE fi.leagues SET last_viewed_at = now() WHERE id = %s", [league_id])


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def create_invite(con, principal: Principal, league_id: str, ttl_days: int = 7) -> tuple[str, str]:
    _require(principal)
    if not 1 <= int(ttl_days) <= 7:
        raise ValueError("ttl_days must be 1..7")
    token = secrets.token_urlsafe(16)
    row = _call(con, "SELECT fi.create_invite(%s, %s, make_interval(days => %s))::text", [league_id, _hash(token), int(ttl_days)])
    return row[0], token


def revoke_invite(con, principal: Principal, invite_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.revoke_invite(%s)", [str(uuid.UUID(str(invite_id)))])


def redeem_invite(con, principal: Principal, token: str) -> str:
    _require(principal)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise InviteInvalid("invite_invalid")
    return _call(con, "SELECT fi.redeem_invite(%s)", [_hash(token)])[0]


def promote_admin(con, principal: Principal, league_id: str, user_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.promote_admin(%s, %s)", [league_id, str(uuid.UUID(str(user_id)))])


def demote_admin(con, principal: Principal, league_id: str, user_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.demote_admin(%s, %s)", [league_id, str(uuid.UUID(str(user_id)))])


def store_credential(con, principal: Principal, league_id: str, enc: EncryptedCredential, credential_id: str) -> str:
    _require(principal)
    if not isinstance(enc, EncryptedCredential):
        raise TypeError("store_credential needs an EncryptedCredential")
    row = _call(con, "SELECT fi.store_credential(%s, %s, %s, %s, %s, %s)::text",
                [str(uuid.UUID(str(credential_id))), league_id, enc.ciphertext, enc.nonce, enc.wrapped_data_key, enc.wrap_key_version])
    return row[0]


def revoke_credential(con, principal: Principal, league_id: str) -> None:
    _require(principal)
    _call(con, "SELECT fi.revoke_credential(%s)", [league_id])
```

- [ ] **Step 4: Run** → `uv run pytest tests/test_control_plane_mutations.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/app/control_plane/mutations.py fantasy-intel/backend/tests/test_control_plane_mutations.py
git commit -m "Phase 1: control-plane mutations (pins, perspective, invites, admin transfer, credential store)"
```

### Task 9: Rebuild `/v2` on the control plane; delete the HMAC assertion and the DuckDB tenancy store

**Files:**
- Rewrite: `fantasy-intel/backend/app/api/leagues_v2.py`
- Delete: `fantasy-intel/backend/app/leagues/auth.py`, `store.py`, `cache.py`, `views.py`, `tenancy_schema.sql`
- Create: `fantasy-intel/backend/app/leagues/redaction.py` (move `sanitize_error` + its regexes verbatim from `store.py`)
- Modify: `fantasy-intel/backend/app/leagues/context.py` (remove `LeagueContext`, `issue_context`, `_seal`, `Role`, `SyncStatus`, `Freshness`, `LeagueAccessDenied`, `check_internal_league_id`; rename `AccessMethod.OWNER_ENV` → `AccessMethod.COOKIES = "cookies"`)
- Modify: `fantasy-intel/backend/app/leagues/credentials.py` (remove `CredentialVault`, `DisabledVault`, `OwnerEnvCredentials`, the `get_settings` import; keep `CredentialError`, `CredentialStorageUnavailable`, `CredentialAccessDenied`, `Secret`, `EspnCookies`)
- Modify: `fantasy-intel/backend/app/leagues/providers/espn.py` (`AccessMethod.OWNER_ENV` → `AccessMethod.COOKIES` at lines 6, 279, 308; docstring: "user-supplied cookies released to a sync job")
- Modify: `fantasy-intel/backend/app/leagues/__init__.py` (docstring: layers now `context`, `models`, `identity`, `capabilities`, `providers`, `redaction`; authorization lives in `app/control_plane`)
- Modify: `fantasy-intel/backend/app/config.py` (remove `tenancy_db_path`, `user_assertion_secret`, `owner_user_id`, `_MIN_ASSERTION_SECRET` and their parsing)
- Delete: `fantasy-intel/backend/tests/test_leagues_auth.py`, `test_leagues_store.py`, `test_leagues_cache.py`
- Rewrite: `fantasy-intel/backend/tests/test_leagues_api.py`
- Modify: `fantasy-intel/.env.example` (replace the multi-league block)

**Interfaces:**
- Routes (all under `/v2`, router dependency `require_enabled` kept; the `hide_v2_when_disabled` middleware in `main.py` stays):

| Method | Path | Auth | Body | Answer |
|---|---|---|---|---|
| GET | `/providers` | anonymous ok | – | `{"providers": [...]}` (unchanged) |
| GET | `/me` | signed in | – | `me()` dict |
| GET | `/leagues` | signed in | – | `{"leagues": [Access.to_json()...]}` for `list_my_leagues` |
| GET | `/leagues/{league_id}` | authorize | – | `Access.to_json()` |
| PUT | `/leagues/{league_id}/pin` | authorize, then signed in | – | 204 |
| DELETE | `/leagues/{league_id}/pin` | authorize, then signed in | – | 204 |
| PUT | `/leagues/{league_id}/perspective` | authorize, then signed in | `{"team_external_id": "6"}` | `Access.to_json()` (refreshed) |
| DELETE | `/leagues/{league_id}/perspective` | authorize, then signed in | – | 204 |

Error mapping: `LeagueNotFound` → 404 `"League not found."`; `SignInRequired` → 401 `"Sign in to use this endpoint."`; `ControlPlaneUnavailable` → 503 `"The control plane is unavailable."`; `ValueError` from `set_perspective` → 422. Dependency `league_access(league_id, principal) -> Access` opens the request transaction, authorizes, and stores the open connection on `request.state` so mutations in the same handler reuse the transaction: implement it as one dependency `league_tx` yielding `(con, access)` via a generator dependency (`yield` inside `with plane.request_tx(principal) as con:`).

- [ ] **Step 1: Rewrite the API tests**

`tests/test_leagues_api.py`:

```python
"""/v2 end to end: the feature flag, Supabase-token identity, server-side authorization and the preference routes.
Runs against the throwaway Postgres (fixture `pg`) with the API's ControlPlane pointed at it."""
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from cp_seed import U1, U2, seed_league, seed_membership, seed_snapshot, seed_user

URL = "https://example-project.supabase.co"
KEY = ec.generate_private_key(ec.SECP256R1())


class _Jwks:
    def __init__(self):
        self.client = jwt.PyJWKClient(URL + "/auth/v1/.well-known/jwks.json", cache_keys=True)
        self.client.fetch_data = lambda: {"keys": [{**jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k", "alg": "ES256"}]}


def token(uid: str, **over) -> str:
    now = int(time.time())
    return jwt.encode({"iss": URL + "/auth/v1", "sub": uid, "aud": "authenticated", "role": "authenticated", "iat": now, "exp": now + 600, **over},
                      KEY, algorithm="ES256", headers={"kid": "k"})


def bearer(uid: str, **over) -> dict:
    return {"Authorization": f"Bearer {token(uid, **over)}"}


@pytest.fixture
def env(pg, monkeypatch):
    import app.control_plane.jwt_auth as jwt_auth
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_DB_URL", pg.dsn_for("fi_api"))
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setattr(jwt_auth, "jwks_client", lambda url: _Jwks().client)
    monkeypatch.setattr(cp_pg, "_PLANES", {})
    yield
    for plane in cp_pg._PLANES.values():
        plane.close()


@pytest.fixture
def client(env):
    from app.main import app
    return TestClient(app)


@pytest.fixture
def world(pg):
    with pg.connect() as con:
        seed_user(con, U1); seed_user(con, U2)
        pub = seed_league(con, visibility="public", name="Public League")
        priv = seed_league(con, visibility="private", checked_at=None, name="Private League")
        seed_membership(con, U1, priv, "admin")
        seed_snapshot(con, pub)
        con.commit()
    return {"pub": pub, "priv": priv}


LID = "lg_" + "a" * 24
ALL_ROUTES = [("GET", "/v2/providers"), ("GET", "/v2/me"), ("GET", "/v2/leagues"), ("GET", f"/v2/leagues/{LID}"),
              ("PUT", f"/v2/leagues/{LID}/pin"), ("DELETE", f"/v2/leagues/{LID}/pin"),
              ("PUT", f"/v2/leagues/{LID}/perspective"), ("DELETE", f"/v2/leagues/{LID}/perspective")]


@pytest.mark.parametrize("flag", ["", "0", "false", None])
@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_flag_off_means_404_on_every_v2_route(client, monkeypatch, flag, method, path):
    if flag is None:
        monkeypatch.delenv("MULTILEAGUE_ENABLED")
    else:
        monkeypatch.setenv("MULTILEAGUE_ENABLED", flag)
    r = client.request(method, path, headers=bearer(U1), json={"team_external_id": "1"} if method == "PUT" else None)
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})


def test_flag_off_never_opens_postgres(client, monkeypatch):
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "0")
    monkeypatch.setenv("SUPABASE_DB_URL", "host=/nonexistent user=fi_api dbname=nope")
    assert client.get("/v2/leagues", headers=bearer(U1)).status_code == 404
    assert client.get("/health").status_code == 200
    assert cp_pg._PLANES == {}


def test_providers_is_public_but_a_bad_token_is_still_401(client):
    assert client.get("/v2/providers").status_code == 200
    assert client.get("/v2/providers", headers={"Authorization": "Bearer nope.nope.nope"}).status_code == 401
    assert client.get("/v2/providers", headers=bearer(U1, aud="anon")).status_code == 401


def test_anonymous_rules(client, world):
    assert client.get("/v2/me").status_code == 401
    assert client.get("/v2/leagues").status_code == 401
    assert client.get(f"/v2/leagues/{world['pub']}").status_code == 200
    assert client.get(f"/v2/leagues/{world['priv']}").status_code == 404
    assert client.put(f"/v2/leagues/{world['pub']}/pin").status_code == 401          # visible, but a preference needs sign-in
    assert client.put(f"/v2/leagues/{world['priv']}/pin").status_code == 404         # invisible: never 401
    assert client.put(f"/v2/leagues/{world['pub']}/perspective", json={"team_external_id": "6"}).status_code == 401


def test_me_creates_the_user_row_and_lists_pins(client, world):
    uid = str(uuid.uuid4())
    assert client.get("/v2/me", headers=bearer(uid)).json() == {"user_id": uid, "is_platform_admin": False, "pins": [],
                                                                  "created_at": client.get("/v2/me", headers=bearer(uid)).json()["created_at"]}
    assert client.put(f"/v2/leagues/{world['pub']}/pin", headers=bearer(uid)).status_code == 204
    assert client.get("/v2/me", headers=bearer(uid)).json()["pins"] == [world["pub"]]
    assert [l["league_id"] for l in client.get("/v2/leagues", headers=bearer(uid)).json()["leagues"]] == [world["pub"]]
    assert client.delete(f"/v2/leagues/{world['pub']}/pin", headers=bearer(uid)).status_code == 204
    assert client.get("/v2/leagues", headers=bearer(uid)).json() == {"leagues": []}


def test_league_view_shape_and_membership(client, world):
    body = client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U2)).json()
    assert body == {"league_id": world["pub"], "provider": "espn", "external_league_id": body["external_league_id"], "season": 2026,
                    "name": "Public League", "visibility": "public", "status": "active", "role": None, "pinned": False,
                    "perspective_team_id": None, "sync_state": "ready",
                    "data": {"data_version": "v1", "global_version": "g1", "committed_at": body["data"]["committed_at"]}}
    assert "object_key" not in body and "leagues/" not in str(body)
    priv = client.get(f"/v2/leagues/{world['priv']}", headers=bearer(U1)).json()
    assert priv["role"] == "admin" and priv["sync_state"] == "no_data" and priv["data"] is None
    assert client.get(f"/v2/leagues/{world['priv']}", headers=bearer(U2)).status_code == 404
    leagues = client.get("/v2/leagues", headers=bearer(U1)).json()["leagues"]
    assert [l["league_id"] for l in leagues] == [world["priv"]]


def test_perspective_is_a_preference_only(client, world):
    r = client.put(f"/v2/leagues/{world['pub']}/perspective", json={"team_external_id": "99"}, headers=bearer(U2))
    assert r.status_code == 200 and r.json()["perspective_team_id"] == "99"      # never verified against a roster
    assert client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U1)).json()["perspective_team_id"] is None
    for bad in ({"team_external_id": "6; DROP"}, {"team_external_id": 6}, {"team": "6"}, {}, {"team_external_id": "6", "x": 1}):
        assert client.put(f"/v2/leagues/{world['pub']}/perspective", json=bad, headers=bearer(U2)).status_code == 422
    assert client.delete(f"/v2/leagues/{world['pub']}/perspective", headers=bearer(U2)).status_code == 204
    assert client.get(f"/v2/leagues/{world['pub']}", headers=bearer(U2)).json()["perspective_team_id"] is None
    # a 404 league is a 404 even with a bad body: authorization runs before validation
    assert client.put(f"/v2/leagues/{world['priv']}/perspective", json={"x": 1}, headers=bearer(U2)).status_code == 404


@pytest.mark.parametrize("league_id", ["lg_" + "z" * 24, "lg_short", "not-an-id", "lg_" + "a" * 24 + "%2F..", "lg_" + "a" * 65])
def test_unknown_and_malformed_ids_are_the_same_404(client, world, league_id):
    assert client.get(f"/v2/leagues/{league_id}", headers=bearer(U1)).json() == {"detail": "League not found."}


def test_control_plane_outage_is_503_not_404(client, world, monkeypatch):
    from app.control_plane import pg as cp_pg
    monkeypatch.setenv("SUPABASE_DB_URL", "host=/nonexistent user=fi_api dbname=nope connect_timeout=1")
    monkeypatch.setattr(cp_pg, "_PLANES", {})
    r = client.get(f"/v2/leagues/{world['pub']}")
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]


def test_responses_are_private_and_uncached(client, world):
    r = client.get(f"/v2/leagues/{world['pub']}")
    assert r.headers["cache-control"] == "private, no-store"


def test_legacy_routes_and_service_auth_are_unchanged_with_the_flag_on(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FANTASY_API_KEY", "k" * 40)
    monkeypatch.setenv("FANTASY_DB_PATH", str(tmp_path / "missing.duckdb"))
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"X-API-Key": "k" * 40}).status_code == 503   # no db: same as before
    assert client.get("/v2/providers", headers={"X-API-Key": "k" * 40}).status_code == 200
```

- [ ] **Step 2: Run** → `uv run pytest tests/test_leagues_api.py -q` → FAIL (old router shapes / imports).

- [ ] **Step 3: Rewrite the router**

`app/api/leagues_v2.py`:

```python
"""Multi-league API (/v2). Feature-flagged: every route answers 404 unless MULTILEAGUE_ENABLED is on.

Identity is the Supabase access token verified in app/control_plane/jwt_auth.py; the service API key is never a user.
Every league route authorizes in Postgres first (app/control_plane/authz.py): unknown, someone else's, deleted,
expired-public and malformed league ids are the same 404, never 403. Only then does a mutation check sign-in (401).
Pins and perspective are preferences: never verified against league data, never granting anything.
Phase 2 adds the league-file read routes; Phase 3 adds connect, invites and credentials on top of app/control_plane/mutations.py."""
from __future__ import annotations

import logging
from typing import Any, Iterator, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..control_plane import mutations
from ..control_plane.authz import Access, LeagueNotFound, authorize_league_in, check_league_id
from ..control_plane.jwt_auth import current_principal
from ..control_plane.pg import ControlPlaneUnavailable, get_control_plane
from ..leagues.capabilities import ProviderCapabilities
from ..leagues.context import Principal, Provider

log = logging.getLogger(__name__)
NOT_FOUND = "League not found."
SIGN_IN = "Sign in to use this endpoint."
UNAVAILABLE = "The control plane is unavailable."
NO_STORE = {"Cache-Control": "private, no-store"}


def require_enabled() -> None:
    """Router-level gate, evaluated before every other dependency, so a flag-off deployment answers 404 even for a
    garbage Authorization header and never opens Postgres."""
    if not get_settings().multileague_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/v2", dependencies=[Depends(require_enabled)])


class ProviderRegistry(Protocol):
    def get_provider(self, provider: Provider) -> Any: ...
    def all_capabilities(self) -> list[ProviderCapabilities]: ...


def provider_registry() -> ProviderRegistry:
    try:
        from ..leagues.providers import registry
    except ImportError:
        raise HTTPException(503, "The provider registry is not available in this deployment.") from None
    return registry   # type: ignore[return-value]


def authenticated_principal(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    return principal


def request_tx(principal: Principal = Depends(current_principal)) -> Iterator[tuple[Any, Principal]]:
    """One Postgres transaction per request, as the request's role."""
    try:
        with get_control_plane().request_tx(principal) as con:
            yield con, principal
    except ControlPlaneUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


def league_tx(league_id: str, tx=Depends(request_tx)) -> tuple[Any, Access]:
    """Authorize FIRST. Everything a league route does happens inside this transaction after this check."""
    con, principal = tx
    try:
        return con, authorize_league_in(con, principal, check_league_id(league_id))
    except LeagueNotFound:
        raise HTTPException(404, NOT_FOUND) from None


def _signed_in(access: Access) -> Principal:
    if not access.principal.is_authenticated:
        raise HTTPException(401, SIGN_IN)
    return access.principal


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def _no_content() -> Response:
    return Response(status_code=204, headers=NO_STORE)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "Request body must be a JSON object.") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Request body must be a JSON object.")
    return body


# ---- routes ---------------------------------------------------------------------------------------

@router.get("/providers")
def list_providers(registry: ProviderRegistry = Depends(provider_registry), _principal: Principal = Depends(current_principal)):
    return {"providers": [c.as_dict() for c in registry.all_capabilities()]}


@router.get("/me")
def me(tx=Depends(request_tx)):
    con, principal = tx
    try:
        return _json(mutations.me(con, principal))
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None


@router.get("/leagues")
def list_leagues(tx=Depends(request_tx)):
    con, principal = tx
    try:
        ids = mutations.list_my_leagues(con, principal)
    except mutations.SignInRequired:
        raise HTTPException(401, SIGN_IN) from None
    out = []
    for league_id in ids:
        try:
            out.append(authorize_league_in(con, principal, league_id).to_json())
        except LeagueNotFound:
            continue     # pinned, then went private/deleted: it simply disappears from the list
    return _json({"leagues": out})


@router.get("/leagues/{league_id}")
def get_league(ltx=Depends(league_tx)):
    _, access = ltx
    return _json(access.to_json())


@router.put("/leagues/{league_id}/pin", status_code=204)
def pin_league(ltx=Depends(league_tx)):
    con, access = ltx
    mutations.set_pin(con, _signed_in(access), access.league_id, True)
    return _no_content()


@router.delete("/leagues/{league_id}/pin", status_code=204)
def unpin_league(ltx=Depends(league_tx)):
    con, access = ltx
    mutations.set_pin(con, _signed_in(access), access.league_id, False)
    return _no_content()


@router.put("/leagues/{league_id}/perspective")
async def set_perspective(request: Request, ltx=Depends(league_tx)):
    con, access = ltx
    principal = _signed_in(access)
    body = await _json_object(request)
    team_id = body.get("team_external_id")
    if set(body) != {"team_external_id"} or not isinstance(team_id, str):
        raise HTTPException(422, 'Send {"team_external_id": "<team id as a string>"}.')
    try:
        mutations.set_perspective(con, principal, access.league_id, team_id)
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    return _json(authorize_league_in(con, principal, access.league_id).to_json())


@router.delete("/leagues/{league_id}/perspective", status_code=204)
def clear_perspective(ltx=Depends(league_tx)):
    con, access = ltx
    mutations.set_perspective(con, _signed_in(access), access.league_id, None)
    return _no_content()
```

Note on `league_tx` for the async `set_perspective` handler: FastAPI runs sync generator dependencies in the threadpool and keeps them open for the handler, so the transaction spans the handler; the body is read after authorization, which is what the 404-before-validation test asserts.

- [ ] **Step 4: Delete the old code and trim the leagues package**

```bash
git rm fantasy-intel/backend/app/leagues/auth.py fantasy-intel/backend/app/leagues/store.py fantasy-intel/backend/app/leagues/cache.py fantasy-intel/backend/app/leagues/views.py fantasy-intel/backend/app/leagues/tenancy_schema.sql fantasy-intel/backend/tests/test_leagues_auth.py fantasy-intel/backend/tests/test_leagues_store.py fantasy-intel/backend/tests/test_leagues_cache.py
```
Create `app/leagues/redaction.py` with the module docstring `"""Error text that may be stored or shown: class name + first line, secrets redacted, length capped."""` and the `_BEARER … sanitize_error` block copied verbatim from the old `store.py`. Then apply the edits listed under **Files** for `context.py`, `credentials.py`, `providers/espn.py`, `__init__.py`, `config.py`. In `config.py` the `Settings` fields become:

```python
    # --- multi-league platform. Every flag defaults OFF, so today's behaviour is unchanged. ---
    multileague_enabled: bool = False       # MULTILEAGUE_ENABLED=1: serves /v2/*; otherwise those routes 404
    supabase_url: str | None = None         # SUPABASE_URL: https://<ref>.supabase.co (JWKS + issuer)
    supabase_db_url: str | None = field(default=None, repr=False)   # SUPABASE_DB_URL (the fi_api DSN, via the Supavisor pooler)
    credential_public_keys: dict = field(default_factory=dict, repr=False)   # CREDENTIALS_PUBKEY_V<n> (PEM)
    credential_wrap_key_version: str | None = None                            # CREDENTIALS_WRAP_KEY_VERSION or highest
    sleeper_source: str = "disabled"
    espn_public_import_enabled: bool = False
    yahoo_client_id: str | None = None
```

- [ ] **Step 5: Replace the multi-league block in `fantasy-intel/.env.example`**

```
# ---------------------------------------------------------------------------
# Multi-league platform (docs/superpowers/specs/2026-10-19-multi-league-platform-design.md).
# Every flag defaults OFF; leave this block unset and the dashboard behaves exactly as before.
# ---------------------------------------------------------------------------
# Serve the /v2 multi-league API (otherwise every /v2 route answers 404 and Postgres is never opened).
MULTILEAGUE_ENABLED=
# Supabase project URL (JWKS + token issuer) and the fi_api connection string through the Supavisor
# pooler (transaction mode, port 6543). fi_api is NOINHERIT with no table grants of its own; see
# fantasy-intel/supabase/README.md for the one-time password step. Never the service-role key.
SUPABASE_URL=
SUPABASE_DB_URL=
# RSA-3072 public key(s) that wrap credential data keys (scripts/gen_credential_keypair.py). The matching
# private key lives only in the GitHub Actions secret CREDENTIALS_PRIVKEY_V<n>.
CREDENTIALS_PUBKEY_V1=
# Sleeper source: disabled (default) | fixture (recorded test data) | live (only after its terms are confirmed).
SLEEPER_SOURCE=disabled
# Import public ESPN leagues (ESPN has no documented public API; off until a terms review clears it).
ESPN_PUBLIC_IMPORT_ENABLED=
# Yahoo: presence only. OAuth, token encryption and refresh are not implemented, so Yahoo stays disabled.
YAHOO_CLIENT_ID=
# Frontend (Next.js server env): show the /leagues prototype page.
MULTILEAGUE_UI_ENABLED=
```

- [ ] **Step 6: Run the touched suites**

Run: `uv run pytest tests/test_leagues_api.py tests/test_league_providers*.py tests/test_hosting.py -q`
Expected: PASS. `test_multileague_isolation.py` and `test_no_credential_leaks.py` still fail at import until Task 10.

- [ ] **Step 7: Commit**

```bash
git add -A fantasy-intel/backend/app fantasy-intel/backend/tests/test_leagues_api.py fantasy-intel/.env.example
git commit -m "Phase 1: /v2 on the Supabase control plane; remove the HMAC user assertion and DuckDB tenancy store"
```

### Task 10: Router-enumerated authorization matrix; trim the isolation and leak suites

**Files:**
- Create: `fantasy-intel/backend/tests/test_v2_authorization_matrix.py`
- Rewrite: `fantasy-intel/backend/tests/test_multileague_isolation.py`
- Modify: `fantasy-intel/backend/tests/test_no_credential_leaks.py`

**Interfaces:**
- Consumes: `client`, `env`, `bearer`, `token` fixtures/helpers from `tests/test_leagues_api.py` (import them: `from test_leagues_api import bearer, env, client  # noqa: F401`).

- [ ] **Step 1: Write the matrix test**

`tests/test_v2_authorization_matrix.py`:

```python
"""Every /v2 route with a {league_id} is enumerated from the live router and driven through the spec's matrix:
{anonymous, viewer, admin, non-member} x {public, private, stale, visibility-expired, flipped-private, deleted}.
Routes are found by walking included routers (FastAPI 0.143 wraps them) and cross-checked against the OpenAPI paths. A new league route that forgets `league_tx` fails here: for an unknown league id it would answer something other
than 404 (a 401, 422 or 200)."""
import pytest

from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user
from test_leagues_api import bearer, client, env  # noqa: F401  (fixtures)

STATES = {
    "public":           dict(visibility="public"),
    "private":          dict(visibility="private", checked_at=None),
    "stale":            dict(visibility="private", status="stale", checked_at=None),
    "expired":          dict(visibility="public", checked_at="25 hours"),
    "flipped_private":  dict(visibility="private", epoch=1, checked_at="1 hour"),
    "deleted":          dict(visibility="public", deleted=True),
}
VISIBLE = {   # state -> actors who get past authorization
    "public": {"anon", "viewer", "admin", "stranger"}, "private": {"viewer", "admin"}, "stale": {"viewer", "admin"},
    "expired": {"viewer", "admin"}, "flipped_private": set(), "deleted": set(),
}
HEADERS = {"anon": {}, "viewer": bearer(U2), "admin": bearer(U1), "stranger": bearer(U3)}


def league_routes():
    from app.main import app
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v2/") and "{league_id}" in path:
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, path))
    assert len(out) >= 5, out          # Phase 1: league view, pin x2, perspective x2
    return out


def body_for(method, path):
    return {"team_external_id": "6"} if method == "PUT" and path.endswith("/perspective") else None


@pytest.fixture
def leagues(pg):
    with pg.connect() as con:
        for u in (U1, U2, U3):
            seed_user(con, u)
        ids = {name: seed_league(con, **kw) for name, kw in STATES.items()}
        for name in STATES:
            if name != "flipped_private":
                seed_membership(con, U1, ids[name], "admin")
                seed_membership(con, U2, ids[name], "viewer")
        con.commit()
    return ids


@pytest.mark.parametrize("method,path", league_routes())
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("actor", list(HEADERS))
def test_matrix(client, leagues, method, path, state, actor):
    url = path.replace("{league_id}", leagues[state])
    r = client.request(method, url, headers=HEADERS[actor], json=body_for(method, path))
    if actor not in VISIBLE[state]:
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (method, path, state, actor)
    elif method == "GET":
        assert r.status_code == 200, (method, path, state, actor)
        assert r.json()["role"] == {"viewer": "viewer", "admin": "admin"}.get(actor)
    elif actor == "anon":
        assert r.status_code == 401, (method, path, state, actor)
    else:
        assert r.status_code in (200, 204), (method, path, state, actor)


@pytest.mark.parametrize("method,path", league_routes())
@pytest.mark.parametrize("actor", list(HEADERS))
def test_unknown_league_is_404_on_every_route_before_anything_else(client, leagues, method, path, actor):
    url = path.replace("{league_id}", "lg_" + "q" * 24)
    r = client.request(method, url, headers=HEADERS[actor], json={"garbage": True})
    assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (method, path, actor)


def test_every_league_route_declares_the_authorizing_dependency():
    """Belt and braces for the behavioural checks above: the source of every league route depends on league_tx."""
    from app.api import leagues_v2
    from app.main import app
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v2/") and "{league_id}" in path:
            names = {d.call.__name__ for d in route.dependant.dependencies}
            assert "league_tx" in names, path
    assert leagues_v2.league_tx.__name__ == "league_tx"
```

- [ ] **Step 2: Rewrite `tests/test_multileague_isolation.py`**

Keep, unchanged in body, only the tests that do not touch the store, the HMAC assertion or the connect flow: `test_external_ids_are_ascii_digit_strings_for_digit_platforms`, `test_internal_ids_that_are_not_ours_are_the_same_denial` (rewrite its assertion to `check_league_id` raising `LeagueNotFound`), `test_cross_provider_and_cross_season_keys_are_never_equal_and_ids_stay_strings`, `test_team_scoped_ids_differ_across_leagues_providers_and_seasons`, `test_players_with_the_same_name_are_never_matched_by_name`, `test_sleeper_fixture_players_named_mike_williams_stay_unlinked_by_name`, `test_legacy_espn_name_matches_are_unresolved_identities`, `test_points_per_reception_is_none_when_the_league_does_not_say`, `test_espn_slot_23_is_flex_and_slot_7_is_superflex`, `test_slot_kind_follows_eligibility_not_the_label`, `test_every_provider_declares_a_state_for_every_capability`, `test_disabled_providers_raise_capability_unavailable_for_every_operation`, `test_unsupported_operations_on_an_enabled_provider_raise_instead_of_returning_empty_data`, `test_settings_defaults_with_all_new_environment_unset_are_off` (assert the new settings: `multileague_enabled is False`, `supabase_url is None`, `supabase_db_url is None`, `credential_public_keys == {}`), `test_espn_only_deployment_serves_existing_routes_and_404s_v2`, `test_schema_sql_has_no_tenancy_tables_and_the_schema_version_is_unchanged` (now asserts no `fi.` / control-plane table names appear in `app/db/schema.sql` and `SCHEMA_VERSION` is unchanged), `test_sec3_flag_off_is_404_for_every_method_and_path_shape`. Replace `test_the_analytics_database_never_receives_tenancy_tables_and_vice_versa` and `test_publish_never_references_the_tenancy_database` with one test `test_publishers_never_reference_the_control_plane` asserting that `app/db/publish.py`, `app/db/snapshot_publish.py` and `scripts/publish_snapshot.py` contain neither `psycopg` nor `control_plane`. Delete every other test and the `store`, `fakes`, `fake_client`, `real_client` fixtures. The `env` fixture becomes: set `MULTILEAGUE_ENABLED=1`, `SLEEPER_SOURCE=fixture`, delete `FANTASY_API_KEY`, `SUPABASE_URL`, `SUPABASE_DB_URL`. Update the module docstring's numbered list to the surviving items.

- [ ] **Step 3: Update `tests/test_no_credential_leaks.py`**

- Remove imports of `app.leagues.auth`, `app.leagues.store`, `OwnerEnvCredentials`, and the `leaky` fixture with the tests `test_a_failing_connect_leaks_no_credential_into_logs_responses_or_storage`, `test_a_user_who_is_not_connected_cannot_write_error_text_into_anyones_status`, `test_the_assertion_secret_api_key_and_headers_are_never_logged_or_stored`, `test_user_supplied_cookies_are_rejected_or_ignored_in_every_channel`, `test_the_owner_cookies_come_only_from_the_server_environment_and_only_for_the_owner`, `test_a_license_to_inspect_a_context_does_not_expose_credentials`.
- `sanitize_error` now imports from `app.leagues.redaction`.
- `ENV_ASSIGNMENT` gains `SUPABASE_DB_URL|CREDENTIALS_PRIVKEY_V[0-9]+` and drops `FANTASY_USER_ASSERTION_SECRET`.
- `ALLOWED_SYNTHETIC`: drop the three values that only the deleted tests used (`AEBsupersecret…`, `AEBkYWJj…`, `{B1A2C3D4-…}`); keep the rest.
- `test_credential_objects_never_reveal_their_values` keeps `Secret`/`EspnCookies` and adds `EncryptedCredential` (from `test_credential_crypto` shapes: build one via `PublicKeyring` with a generated key).
- Add:

```python
def test_a_bearer_token_is_never_logged_or_echoed(pg, caplog, monkeypatch):
    """A rejected Supabase token: 401 body is fixed text; the token appears in no log record."""
    import logging
    from fastapi.testclient import TestClient
    from test_leagues_api import URL, token
    import app.control_plane.jwt_auth as jwt_auth
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1"); monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_DB_URL", pg.dsn_for("fi_api"))
    monkeypatch.setattr(jwt_auth, "jwks_client", lambda url: (_ for _ in ()).throw(RuntimeError("jwks down")))
    from app.main import app
    bad = token("00000000-0000-4000-8000-000000000000", aud="anon")
    with caplog.at_level(logging.DEBUG):
        r = TestClient(app).get("/v2/providers", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid or expired session."}
    assert bad not in caplog.text and bad[-30:] not in caplog.text


def test_the_dsn_and_public_keys_are_absent_from_settings_repr(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fi_api:hunter2hunter2@db.example/postgres")
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V1", "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----")
    text = repr(get_settings())
    assert "hunter2" not in text and "BEGIN PUBLIC KEY" not in text
```
  (the first `test_no_credentials_are_committed_to_the_repository` scan must stay green: the fake DSN above lives in a `.py` file, which `ENV_ASSIGNMENT` skips, and its password is not a pattern match.)

- [ ] **Step 4: Run** → `uv run pytest tests/test_v2_authorization_matrix.py tests/test_multileague_isolation.py tests/test_no_credential_leaks.py tests/test_league_scope_audit.py -q` → PASS. The scope-audit ratchet is untouched by this phase (no `ORDER BY synced_at DESC` added or removed).

- [ ] **Step 5: Commit**

```bash
git add fantasy-intel/backend/tests
git commit -m "Phase 1: router-enumerated authorization matrix; isolation and leak suites on the control plane"
```

### Task 11: Frontend: remove the assertion signer; `/leagues` prototype without identity

**Files:**
- Delete: `fantasy-intel/frontend/lib/user-assertion.ts`, `lib/user-assertion.server.ts`, `lib/user-assertion.test.ts`
- Modify: `fantasy-intel/frontend/app/leagues/load.ts`, `app/leagues/actions.ts`, `lib/leagues.ts`, `lib/leagues.test.ts`

Sign-in (Supabase Auth in Next.js) arrives in Phase 2 with the league switcher; until then the server has no user, so the prototype page lists providers and shows its "sign-in is not available yet" state. Nothing user-visible changes with `MULTILEAGUE_UI_ENABLED` unset (the E2E `/leagues` smoke keeps passing).

- [ ] **Step 1: Update the unit test first**

In `lib/leagues.test.ts`: remove `NO_SIGNING_REASON` from any import (it is not imported today; verify with `grep -n NO_SIGNING lib/leagues.test.ts` → no output) and add:

```ts
test("SIGNED_OUT_REASON explains that sign-in is coming, without mentioning assertions", () => {
  assert.match(SIGNED_OUT_REASON, /sign-?in/i);
  assert.doesNotMatch(SIGNED_OUT_REASON, /assertion|secret/i);
});
```

- [ ] **Step 2: Run** → `npm test` → FAIL only if the text changes below are not yet applied (the regex passes on the current text too; keep the test as a guard).

- [ ] **Step 3: Apply the edits**

`lib/leagues.ts`: delete the `NO_SIGNING_REASON` line; change `SIGNED_OUT_REASON` to `"Sign-in is not available yet (it arrives with the league switcher), so leagues cannot be listed or connected here."`.

`app/leagues/load.ts`: drop the `user-assertion.server` import and `NO_SIGNING_REASON`; replace the body after `const empty = …` with `return { ...empty, signedOutReason: SIGNED_OUT_REASON };` and delete the now-unused `parseLeagues`/`parseTeams`/`LeagueView`/`TeamOption`/`LeagueWithTeams` fetch code (keep the exported types `LeagueWithTeams`, `LeaguesData`, and `serverNow()`, since `page.tsx` imports them).

`app/leagues/actions.ts`: drop the `user-assertion.server` import and `NO_SIGNING_REASON`; `guard()` becomes

```ts
async function guard(): Promise<Guard> {
  if (!multileagueUiEnabled(process.env.MULTILEAGUE_UI_ENABLED)) return fail(NOT_ENABLED_REASON);
  return fail(SIGNED_OUT_REASON);   // no sign-in yet: every mutation is refused on the server, never forwarded
}
```
and the `Guard` type becomes `{ ok: true } | { ok: false; state: ActionState }`; remove the `headers: g.headers` from the three `apiRequest` calls (they are unreachable until Phase 3 rebuilds connect, but must still typecheck).

```bash
cd fantasy-intel/frontend && git rm lib/user-assertion.ts lib/user-assertion.server.ts lib/user-assertion.test.ts
```

- [ ] **Step 4: Verify** → `npm run lint && npm run typecheck && npm test && npm run build` → all green.

- [ ] **Step 5: Commit**

```bash
git add -A fantasy-intel/frontend/lib fantasy-intel/frontend/app/leagues
git commit -m "Phase 1: frontend drops the HMAC user assertion; /leagues prototype waits for sign-in"
```

### Task 12: Docs, CI and README

**Files:**
- Create: `docs/multileague/ADR-0002-supabase-control-plane.md`
- Modify: `docs/multileague/threat-model.md` (addendum section), `docs/multileague/roadmap.md` (one-line pointer at the top)
- Create: `fantasy-intel/supabase/README.md`
- Modify: `fantasy-intel/README.md` (Hosting section: "Control plane (Supabase)" subsection)
- Modify: `docs/superpowers/plans/2026-10-19-multi-league-platform.md` (Phase 1 status line)

- [ ] **Step 1: ADR-0002**

`docs/multileague/ADR-0002-supabase-control-plane.md` records, with the spec as the source: Status (Accepted 2026-10-20, Phase 1); Context (free tiers; Phase 1a moved reads to R2; ADR-0001's DuckDB tenancy store and HMAC assertion were placeholders); Decisions: (1) Supabase Auth is the identity provider and FastAPI verifies its JWTs itself (JWKS, ES256/RS256, iss/aud/exp) — supersedes ADR-0001 §7; (2) the control plane is Postgres schema `fi` in project `fantasy-intel`, least-privilege roles `fi_api`/`fi_sync`, `SET LOCAL ROLE` + transaction-local claims, RLS as second line of defence, SECURITY DEFINER functions for cross-row mutations, no BYPASSRLS role, service-role key never deployed — supersedes ADR-0001 §2; (3) `authorize_league` before any league file; unknown == unauthorized == 404; public leagues need no sign-in; perspective is a preference — refines §1; (4) user-supplied ESPN cookies are accepted, opt-in, under envelope encryption with the private key only in GitHub Actions, used only by sync jobs for that league's read endpoint — supersedes ADR-0001 §8 and its "never collect ESPN cookies" alternative, with the reasons (ESPN offers no authorized third-party method; explicit consent text; URL allowlist; redaction; crypto-erase on deletion); (5) the per-instance response cache replaces `TenantScopedCache` (Phase 2) — supersedes §3; (6) flags unchanged (§9). Consequences: phases 2–7 per the master plan; the `/leagues` prototype's connect flow returns in Phase 3 on sync jobs. Alternatives considered: HMAC assertion minted by Next.js (kept the secret in two places, no real IdP); PostgREST with the anon key (would expose the control plane to any key holder; rejected by putting it in schema `fi`); Supabase service-role key in the API (bypasses RLS; rejected).

- [ ] **Step 2: Threat-model addendum**

Append a section `## Addendum (2026-10-20, Phase 1 of the multi-league platform)` to `docs/multileague/threat-model.md` with a table re-stating T2 (identity now Supabase JWT verified in FastAPI), T3 (API key still service-only), T7 (envelope crypto, API holds public key only, tests `test_credential_crypto.py`), T8 (cookies now collected opt-in; mitigations: consent text, encryption, read-only allowlisted use by sync jobs, never logged), T9 (control plane in Supabase, never in the published snapshot; `test_publishers_never_reference_the_control_plane`), T11 (public leagues need no sign-in; authorization before every lookup; `test_v2_authorization_matrix.py`), T13 (revoked membership: RLS re-evaluates per request), T14 (append-only `fi.audit_log`). Prepend to `roadmap.md`: `> Superseded on 2026-10-20 by [ADR-0002](ADR-0002-supabase-control-plane.md) and the [multi-league plan](../superpowers/plans/2026-10-19-multi-league-platform.md); kept for history.`

- [ ] **Step 3: `fantasy-intel/supabase/README.md`**

Contents: what lives in `migrations/` and `down/`; how migrations are applied (Supabase MCP `apply_migration` from a Claude session, or `supabase link --project-ref kdikrcmvfuzqsgahqaxf && supabase db push`); the one-time runbook (SQL editor, as `postgres`):

```sql
ALTER ROLE fi_api  WITH LOGIN PASSWORD '<generate 32+ random chars, never commit>';
ALTER ROLE fi_sync WITH LOGIN PASSWORD '<another one>';
```
then the DSN shape for Vercel `SUPABASE_DB_URL`: `postgresql://fi_api.kdikrcmvfuzqsgahqaxf:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require` (Supavisor transaction mode; IPv4; `prepare_threshold=None` is set in code); and the test harness note (`FI_TEST_PG_DSN`, local `initdb`).

- [ ] **Step 4: README hosting subsection**

After the existing step 4 in "Hosting" add **"Control plane (Supabase, Phase 1)"**: the env table for `fantasy-intel-api` (`MULTILEAGUE_ENABLED` unset for now, `SUPABASE_URL`, `SUPABASE_DB_URL`, `CREDENTIALS_PUBKEY_V1`), a sentence that with the flag unset none of them are read and Postgres is never opened, and a link to `fantasy-intel/supabase/README.md`. Remove the sentence about `FANTASY_USER_ASSERTION_SECRET` / `FANTASY_DEV_USER_ID` if the README mentions them (`grep -n ASSERTION fantasy-intel/README.md`).

- [ ] **Step 5: Master plan status**

In `docs/superpowers/plans/2026-10-19-multi-league-platform.md`, under Phase 1 add `**Status:** done 2026-10-20 (PR #<n>): …` after the PR merges (fill the number in Task 13).

- [ ] **Step 6: Commit**

```bash
git add docs/multileague fantasy-intel/supabase/README.md fantasy-intel/README.md
git commit -m "Phase 1: ADR-0002 (Supabase control plane), threat-model addendum, runbooks"
```

### Task 13: Apply to the Supabase project, prove acceptance, open the PR

- [ ] **Step 1: Full backend suite + E2E**

```bash
cd fantasy-intel/backend && uv run pytest -q -p no:cacheprovider
cd ../frontend && npm run lint && npm run typecheck && npm test && npm run build && npm run e2e
```
Expected: all green (baseline was 1549 passed, 1 skipped in ~9 min; the new control-plane tests add a few hundred parametrised cases).

- [ ] **Step 2: Apply both migrations to project `kdikrcmvfuzqsgahqaxf`**

Via the Supabase MCP `apply_migration` with names `control_plane` and `control_plane_functions` and the exact file contents. Then verify with `list_tables` (schema `fi`, 12 tables), `execute_sql`:

```sql
select rolname, rolbypassrls, rolcanlogin from pg_roles where rolname in ('fi_api','fi_sync');
select count(*) from fi.provider_slots;    -- 6
select has_table_privilege('fi_api', 'fi.league_credentials', 'SELECT');   -- false
select has_table_privilege('fi_sync', 'fi.league_memberships', 'SELECT');  -- false
```
and `get_advisors(security)`: no "RLS disabled" findings for `fi.*`; record any advisor notes in the PR.

- [ ] **Step 3: Acceptance checklist (paste the evidence into the PR body)**

| Criterion (master plan, Phase 1) | Evidence |
|---|---|
| Authorization matrix green for every `/v2` league route (router-enumerated) | `tests/test_v2_authorization_matrix.py` |
| RLS: anon only public; authenticated only own pins/selections/memberships; fi_api cannot select credentials; fi_sync cannot read memberships | `tests/test_control_plane_rls.py` |
| Claims isolation on one pooled connection | `tests/test_control_plane_pg.py` |
| Crypto: AAD swap fails; public-only cannot decrypt; rewrap leaves ciphertext/AAD untouched; redaction | `tests/test_credential_crypto.py`, `tests/test_no_credential_leaks.py` |
| Existing suite + E2E green; production unchanged with flags off | CI; `test_flag_off_never_opens_postgres`; no Vercel env added |
| No BYPASSRLS role created; service-role key nowhere | `test_no_role_we_created_bypasses_rls_or_is_superuser`; `grep -rn service_role` → only docs |

- [ ] **Step 4: Open the PR**

```bash
git push -u origin h/multileague-phase-1
gh pr create --title "Multi-league Phase 1: Supabase control plane, JWT verification, authorize_league, credential crypto" --body-file <(printf '%s\n' "<summary + acceptance table + rollback: flags off; down-migrations in fantasy-intel/supabase/down>" "" "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
```
Then: wait for Codex's review (or run `/code-review high` if Codex is unavailable), fix every finding, merge on green CI, fill the PR number into the master plan's Phase 1 status (Task 12 step 5) in a follow-up commit on main or in the same PR before merge.

- [ ] **Step 5: Hand-offs for Havyn (not blockers for the merge)**

1. Set `LOGIN PASSWORD` on `fi_api` and `fi_sync` in the Supabase SQL editor (runbook in `fantasy-intel/supabase/README.md`).
2. Add `SUPABASE_URL`, `SUPABASE_DB_URL` to the `fantasy-intel-api` Vercel project (needed before `MULTILEAGUE_ENABLED` flips in Phase 2).
3. Run `scripts/gen_credential_keypair.py`, store the private key as the GitHub Actions secret `CREDENTIALS_PRIVKEY_V1`, the public key as Vercel `CREDENTIALS_PUBKEY_V1` (needed in Phase 3).

---

## Self-review

**Spec coverage.** §1 Auth (JWKS, iss/aud/exp, no token = anonymous, API key stays service auth) → Task 5. §1 Authorization before any file, same query returns the pointer → Task 7. §2 four concepts (visibility, membership, credential ownership, perspective) → tables in Task 2, `Access` in Task 7, perspective/pins in Task 8. §2 invites (sha256 only, single use, 7 days, revocable, viewer-only, sign-in required, audited) → Task 3. §2 credential lifecycle statuses + one live credential per league + crypto-erase on account deletion (`ON DELETE CASCADE` from `app_users`) → Tasks 2–3; retention timers/notifications are Phase 3/6. §2 credential security (AES-GCM, AAD without key version, RSA-OAEP-3072, rewrap, mutable `wrap_key_version`, immutable `aad_format`) → Task 6 + column constraints in Task 2. §2 roles (`fi_api` NOINHERIT member of anon/authenticated, `SET LOCAL ROLE` + `set_config(..., true)`, no grants on credentials, SECURITY DEFINER with pinned search_path; `fi_sync` grants limited to the five sync tables, `sync_credential(job_id, lease_token)`; no BYPASSRLS; service-role key never deployed; claims isolation tested) → Tasks 2, 3, 4. §2 dynamic visibility 24 h rule fail-closed → Task 7 (probe trigger itself is Phase 3). §3.1 tables incl. `sync_fence`, `provider_slots` seeded ESPN 2 / Sleeper 4, `league_snapshots` + history, partial unique index for one running job per league → Task 2 (claim/publish SQL is Phase 3). §5 tests: authorization matrix, credentials, Postgres RLS/claims → Tasks 2, 4, 6, 10. Plan Phase 1 scope: ADR-0002, HMAC removal, `/v2` behind the flag → Tasks 9, 11, 12. Rollback: flags off + down-migrations → Tasks 2, 3, 12.

**Placeholders.** Task 12's ADR and README steps describe prose content by outline rather than full text; that is documentation, written from the spec sections named. Every code step carries its code. The `<n>` PR number is filled at merge time by design.

**Type consistency.** `Principal.from_claims` (Task 4) is what `current_principal` (Task 5) returns and what `request_tx` (Task 4) consumes. `authorize_league_in(con, principal, league_id)` (Task 7) is what `league_tx` (Task 9) and `list_leagues` call. `mutations.*` signatures (Task 8) match the router calls (Task 9). `EncryptedCredential` field order `(ciphertext, nonce, aad_format, wrapped_data_key, wrap_key_version)` matches `store_credential`'s parameter order into `fi.store_credential(p_credential_id, p_league_id, p_ciphertext, p_nonce, p_wrapped_data_key, p_wrap_key_version)`. `PgDatabase.FI_TABLES` lists exactly the 12 tables migration 0001 creates; `reset()` reseeds slots via `fi.seed_provider_slots()`, which the migration defines.

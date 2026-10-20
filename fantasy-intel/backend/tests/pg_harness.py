"""A throwaway Postgres for the control-plane tests.

Order of preference: FI_TEST_PG_DSN (CI: a postgres:17 service container, superuser DSN) -> a local cluster made
with `initdb` + `pg_ctl` in a temp dir (unix socket only, trust auth, torn down at session end) -> skip. In CI the
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
_PG_BIN_DIRS = ("/opt/homebrew/opt/postgresql@18/bin", "/opt/homebrew/opt/postgresql@17/bin",
                "/usr/lib/postgresql/17/bin", "/usr/lib/postgresql/16/bin")


def _find(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for d in _PG_BIN_DIRS:
        candidate = Path(d) / binary
        if candidate.exists():
            return str(candidate)
    return None


class PgCluster:
    def __init__(self):
        self.external_dsn = os.environ.get("FI_TEST_PG_DSN")
        self._dir: str | None = None
        self._pg_ctl: str | None = None
        self._data: str | None = None

    def start(self) -> None:
        if self.external_dsn:
            return
        initdb, pg_ctl = _find("initdb"), _find("pg_ctl")
        if not (initdb and pg_ctl):
            if os.environ.get("CI"):
                pytest.fail("CI must provide FI_TEST_PG_DSN (postgres service) for the control-plane tests")
            pytest.skip("no Postgres available: set FI_TEST_PG_DSN or install postgresql (initdb/pg_ctl)")
        # A short path: unix socket paths are limited to about 100 characters.
        self._dir = tempfile.mkdtemp(prefix="fipg", dir="/tmp" if os.path.isdir("/tmp") else None)
        data = os.path.join(self._dir, "data")
        # macOS: without a valid LC_ALL the postmaster "becomes multithreaded during startup" and refuses to run.
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        self._env = env
        subprocess.run([initdb, "-D", data, "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True, env=env)
        opts = (f"-c listen_addresses='' -c unix_socket_directories='{self._dir}' -c fsync=off "
                f"-c synchronous_commit=off -c full_page_writes=off")
        try:
            subprocess.run([pg_ctl, "-D", data, "-o", opts, "-l", os.path.join(self._dir, "log"), "-w", "start"],
                           check=True, capture_output=True, env=env)
        except subprocess.CalledProcessError as err:
            log = Path(self._dir, "log")
            raise RuntimeError(f"pg_ctl start failed: {err.stderr.decode(errors='replace')}\n"
                               f"{log.read_text() if log.exists() else ''}") from None
        self._pg_ctl, self._data = pg_ctl, data

    def stop(self) -> None:
        if self._dir and self._pg_ctl and self._data:
            subprocess.run([self._pg_ctl, "-D", self._data, "-m", "immediate", "-w", "stop"], capture_output=True,
                           env=getattr(self, "_env", None))
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
            con.execute("ALTER ROLE fi_api LOGIN")
            con.execute("ALTER ROLE fi_sync LOGIN")

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

"""A throwaway Postgres for the control-plane tests.

Order of preference: FI_TEST_PG_DSN (CI: a postgres:17 service container, superuser DSN) -> a local cluster made
with `initdb` + `pg_ctl` in a temp dir (unix socket only, trust auth, torn down at session end) -> skip. In CI the
DSN is mandatory: tests must never silently skip there.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import weakref
from pathlib import Path

import psycopg
import psycopg.errors
import psycopg.pq
import psycopg.sql
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "fantasy-intel" / "supabase" / "migrations"
SHIM = Path(__file__).with_name("pg_shim.sql")
# Test-only: used solely for the throwaway test database's service roles, never anywhere real.
TEST_ROLE_PASSWORD = "fi-test-role-only"
_PG_BIN_DIRS = ("/opt/homebrew/opt/postgresql@18/bin", "/opt/homebrew/opt/postgresql@17/bin",
                "/usr/lib/postgresql/17/bin", "/usr/lib/postgresql/16/bin")


def _is_exact_postgres_instance(pid: int, data_dir: str | Path) -> bool:
    """Check that PID is an active process owned by current user running postgres for exact data_dir.
    Protects against killing unrelated processes due to PID reuse."""
    import shlex
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,args="], capture_output=True, text=True, timeout=2)
        if p.returncode != 0 or not p.stdout.strip():
            return False
        parts = p.stdout.strip().split(None, 1)
        if len(parts) < 2:
            return False
        uid, cmd = int(parts[0]), parts[1]
        if uid != os.getuid():
            return False
        try:
            tokens = shlex.split(cmd)
        except Exception:
            tokens = cmd.split()
        if not tokens:
            return False
        # Actual binary must be postgres
        if Path(tokens[0]).name != "postgres":
            return False
        # Exact data directory must match -D argument
        target_data = Path(data_dir).resolve()
        has_matching_data_arg = False
        for i, tok in enumerate(tokens):
            if tok == "-D" and i + 1 < len(tokens):
                try:
                    if Path(tokens[i + 1]).resolve() == target_data:
                        has_matching_data_arg = True
                        break
                except Exception:
                    pass
            elif tok.startswith("-D") and len(tok) > 2:
                try:
                    if Path(tok[2:]).resolve() == target_data:
                        has_matching_data_arg = True
                        break
                except Exception:
                    pass
        return has_matching_data_arg
    except Exception:
        return False


def _verify_safe_test_dsn(dsn: str | None) -> None:
    """Refuse destructive test setup against an unverified external or production database."""
    if not dsn:
        return
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    host = info.get("host", "").strip()
    if os.environ.get("FI_ALLOW_REMOTE_TEST_PG") == "1":
        return
    safe_hosts = {"localhost", "127.0.0.1", "::1"}
    if host in safe_hosts or not host or host.startswith("/"):
        return
    raise ValueError(
        f"Refusing to run destructive test setup against unverified external DSN host: {host!r}. "
        "Set FI_ALLOW_REMOTE_TEST_PG=1 if this is an intentional remote test cluster."
    )


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
        _verify_safe_test_dsn(self.external_dsn)
        self._dir: str | None = None
        self._pg_ctl: str | None = None
        self._data: str | None = None
        self._pid: int | None = None

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
        pid_file = Path(data) / "postmaster.pid"
        if pid_file.exists():
            try:
                self._pid = int(pid_file.read_text().splitlines()[0].strip())
            except Exception:
                self._pid = None
        # Record cluster identity for safe orphan cleanup and active cluster detection
        owner_file = Path(self._dir) / "owner.json"
        owner_file.write_text(json.dumps({
            "runner_pid": os.getpid(),
            "created_at": time.time(),
            "cluster_dir": str(Path(self._dir).resolve()),
            "data_dir": str(Path(data).resolve()),
            "pg_ctl": pg_ctl,
            "harness": "fantasy-intel-pg-harness"
        }))
        atexit.register(self.stop)

    def stop(self) -> None:
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass
        if self._dir and self._pg_ctl and self._data:
            data_path = Path(self._data).resolve()
            try:
                subprocess.run([self._pg_ctl, "-D", str(data_path), "-m", "immediate", "-w", "stop"], capture_output=True,
                               timeout=10, env=getattr(self, "_env", None))
            except Exception:
                pass
            if self._pid is not None:
                # PID-reuse protection: only signal if process is still verified to be this exact postgres instance
                if _is_exact_postgres_instance(self._pid, data_path):
                    try:
                        os.kill(self._pid, signal.SIGTERM)
                        time.sleep(0.5)
                        if _is_exact_postgres_instance(self._pid, data_path):
                            os.kill(self._pid, signal.SIGKILL)
                    except OSError:
                        pass
                self._pid = None
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def superuser_dsn(self, db: str = "postgres") -> str:
        if self.external_dsn:
            return psycopg.conninfo.make_conninfo(self.external_dsn, dbname=db)
        return f"host={self._dir} user=postgres dbname={db}"

    def dsn(self, db: str, user: str) -> str:
        if user == "postgres":
            return self.superuser_dsn(db)
        # The service roles get a throwaway password (set in PgDatabase.create): the CI container authenticates
        # TCP connections by password, and the superuser DSN's password is not theirs.
        return psycopg.conninfo.make_conninfo(self.superuser_dsn(db), user=user, password=TEST_ROLE_PASSWORD)


_SAFE_DB_NAME_RE = re.compile(r"^fi_(?:test|e2e|golden)(?:_[a-z0-9_]{1,54})?$")


def _validate_test_db_name(name: str) -> None:
    """Reject database names that do not match repository test database naming conventions.

    Accepts only test database identifiers (fi_test, fi_test_*, fi_e2e, fi_e2e_*, fi_golden,
    fi_golden_*). Raises ValueError if the name is unsafe or unintended (e.g. 'postgres',
    'production', 'app_main'). This guards against dropping or modifying unintended databases
    via FI_TEST_DB_NAME or test harness calls.
    """
    if not _SAFE_DB_NAME_RE.match(name):
        raise ValueError(
            f"Unsafe test database name {name!r}. "
            "Must match test database conventions: fi_test, fi_test_*, fi_e2e, fi_e2e_*, fi_golden, fi_golden_* "
            "(e.g. fi_test_abc123_gw0)."
        )


class PgDatabase:
    """One database with the shim + migrations applied. Connections for any role of the test cluster."""
    MIGRATIONS = sorted(MIGRATIONS_DIR.glob("*.sql"))
    FI_TABLES = ("audit_log", "league_credentials", "league_invites", "team_selections", "league_pins",
                 "league_memberships", "league_snapshot_history", "league_snapshots", "sync_jobs", "provider_slots",
                 "leagues", "app_users", "global_snapshot")

    def __init__(self, cluster: PgCluster, name: str = "fi_test"):
        _validate_test_db_name(name)
        self.cluster, self.name = cluster, name
        self._tracked_connections: weakref.WeakSet[psycopg.Connection] = weakref.WeakSet()

    def create(self) -> None:
        self.terminate_other_backends()
        db_ident = psycopg.sql.Identifier(self.name)
        with psycopg.connect(self.cluster.superuser_dsn(), autocommit=True) as con:
            con.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(db_ident))
            con.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(db_ident))
        with psycopg.connect(self.cluster.superuser_dsn(self.name), autocommit=True) as con:
            con.execute(SHIM.read_text())
            for path in self.MIGRATIONS:
                con.execute(path.read_text())
            # The migration creates the service roles NOLOGIN (Havyn sets LOGIN + a password on Supabase). Here they
            # get a test-only password, which a throwaway cluster or CI container is the only place it exists.
            for role in ("fi_api", "fi_sync"):
                con.execute(psycopg.sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    psycopg.sql.Identifier(role), psycopg.sql.Literal(TEST_ROLE_PASSWORD)))

    def dsn_for(self, user: str = "postgres") -> str:
        return self.cluster.dsn(self.name, user)

    def connect(self, user: str = "postgres", lock_timeout_s: float = 10.0, statement_timeout_s: float = 30.0, **kw) -> psycopg.Connection:
        if "options" not in kw:
            kw["options"] = f"-c lock_timeout={int(lock_timeout_s * 1000)}ms -c statement_timeout={int(statement_timeout_s * 1000)}ms"
        con = psycopg.connect(self.dsn_for(user), prepare_threshold=None, **kw)
        self._tracked_connections.add(con)
        return con

    def close_open_connections(self) -> int:
        """Close all connections opened through this PgDatabase instance."""
        closed = 0
        for con in list(self._tracked_connections):
            try:
                if not con.closed:
                    try:
                        if con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                            con.rollback()
                    except Exception:
                        pass
                    con.close()
                    closed += 1
            except Exception:
                pass
        self._tracked_connections.clear()
        return closed

    def terminate_other_backends(self) -> int:
        """Terminate all other connections to self.name only (never touching other databases or unrelated processes)."""
        terminated = 0
        try:
            with psycopg.connect(self.cluster.superuser_dsn(), autocommit=True, connect_timeout=3) as su:
                rows = su.execute(
                    "SELECT pid, application_name, state, query FROM pg_stat_activity "
                    "WHERE datname = %s AND pid != pg_backend_pid()",
                    [self.name],
                ).fetchall()
                if rows:
                    su.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid != pg_backend_pid()",
                        [self.name],
                    )
                    terminated = len(rows)
        except Exception:
            pass
        return terminated

    def _diagnose_locks(self) -> str:
        try:
            with psycopg.connect(self.cluster.superuser_dsn(), autocommit=True, connect_timeout=3) as su:
                cur = su.execute(
                    "SELECT pid, client_addr, state, age(clock_timestamp(), query_start), query "
                    "FROM pg_stat_activity WHERE datname = %s",
                    [self.name],
                )
                rows = cur.fetchall()
                return f"Active backends on {self.name}: {rows}"
        except Exception as e:
            return f"Failed to retrieve lock diagnostics: {e}"

    def reset(self, timeout: float = 10.0) -> None:
        self.close_open_connections()
        self.terminate_other_backends()
        with self.connect(autocommit=True, lock_timeout_s=timeout, statement_timeout_s=timeout) as con:
            try:
                con.execute("TRUNCATE auth.users, " + ", ".join(f"fi.{t}" for t in self.FI_TABLES) + " RESTART IDENTITY CASCADE")
                con.execute("SELECT fi.seed_provider_slots()")
            except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as err:
                diagnostics = self._diagnose_locks()
                raise RuntimeError(f"Database reset timed out on {self.name} after {timeout}s. {diagnostics}") from err

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

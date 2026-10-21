#!/usr/bin/env python3
"""Repo-native verification workflow for Claude Code and developers on fantasy-intel.

Modes:
  quick:     Fast checks for current changes (<10s). Static safety checks + directly affected tests.
  affected:  Targeted tests conservatively inferred from changed files against a base ref.
  security:  Targeted multileague security, RLS, authorization, crypto, worker fencing, and static checks.
  full:      Comprehensive test suites across backend, frontend, golden responses, and build.
  doctor:    Diagnose local environment (Python, Node, uv, Postgres, ports, temp dirs) without side-effects.

Usage:
  python fantasy-intel/scripts/agent_check.py quick
  python fantasy-intel/scripts/agent_check.py affected [--base origin/main]
  python fantasy-intel/scripts/agent_check.py security
  python fantasy-intel/scripts/agent_check.py full
  python fantasy-intel/scripts/agent_check.py doctor
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "fantasy-intel" / "backend"
FRONTEND_DIR = REPO_ROOT / "fantasy-intel" / "frontend"
DIAGNOSTICS_DIR = REPO_ROOT / ".agent_diagnostics"

# Core security regression suites that must run on security-sensitive changes
SECURITY_SUITES = [
    "tests/test_pg_harness.py",
    "tests/test_control_plane_pg.py",
    "tests/test_control_plane_rls.py",
    "tests/test_control_plane_mutations.py",
    "tests/test_control_plane_functions.py",
    "tests/test_jwt_auth.py",
    "tests/test_authorize_league.py",
    "tests/test_v2_authorization_matrix.py",
    "tests/test_credential_crypto.py",
    "tests/test_sync_worker.py",
    "tests/test_sync_publish.py",
    "tests/test_sync_queue.py",
    "tests/test_sync_queue_schema.py",
    "tests/test_league_scope_audit.py",
    "tests/test_static_checks.py",
]

# Mapping rules: substring or regex pattern in changed file path -> list of backend test files
BACKEND_TEST_MAPPINGS: list[tuple[str | re.Pattern, list[str]]] = [
    # Migrations & DB schema
    ("supabase/migrations/", [
        "tests/test_pg_harness.py",
        "tests/test_control_plane_pg.py",
        "tests/test_control_plane_rls.py",
        "tests/test_control_plane_mutations.py",
        "tests/test_control_plane_functions.py",
        "tests/test_sync_queue_schema.py",
        "tests/test_sync_queue.py",
        "tests/test_credential_crypto.py",
        "tests/test_authorize_league.py",
        "tests/test_v2_authorization_matrix.py",
    ]),
    ("pg_harness.py", ["tests/test_pg_harness.py", "tests/test_control_plane_rls.py"]),
    ("pg_shim.sql", ["tests/test_pg_harness.py", "tests/test_control_plane_rls.py"]),

    # Authorization & JWT
    ("control_plane/jwt_auth.py", ["tests/test_jwt_auth.py", "tests/test_authorize_league.py"]),
    ("control_plane/authz.py", ["tests/test_authorize_league.py", "tests/test_v2_authorization_matrix.py", "tests/test_league_scope_audit.py"]),
    ("control_plane/credential_crypto.py", ["tests/test_credential_crypto.py", "tests/test_control_plane_functions.py"]),
    ("control_plane/mutations.py", ["tests/test_control_plane_mutations.py", "tests/test_control_plane_functions.py"]),
    ("control_plane/pg.py", ["tests/test_control_plane_pg.py", "tests/test_control_plane_functions.py"]),

    # Sync worker & queues
    ("sync/worker.py", ["tests/test_sync_worker.py", "tests/test_sync_failures.py", "tests/test_sync_publish.py"]),
    ("sync/queue.py", ["tests/test_sync_queue.py", "tests/test_sync_queue_schema.py"]),
    ("sync/build.py", ["tests/test_sync_build.py", "tests/test_sync_publish.py"]),
    ("sync/publish.py", ["tests/test_sync_publish.py", "tests/test_sync_worker.py"]),
    ("sync/espn_source.py", ["tests/test_sync_espn_source.py", "tests/test_sync_worker.py"]),
    ("sync/sleeper_source.py", ["tests/test_sync_sleeper.py", "tests/test_sync_worker.py"]),
    ("sync/maintenance.py", ["tests/test_sync_maintenance.py"]),

    # Storage & league files
    ("db/league_files.py", ["tests/test_league_files.py", "tests/test_league_conn.py"]),
    ("db/league_conn.py", ["tests/test_league_conn.py", "tests/test_league_files.py"]),
    ("db/league_split.py", ["tests/test_league_files.py", "tests/test_golden_legacy.py"]),
    ("db/snapshot.py", ["tests/test_snapshot.py", "tests/test_r2.py"]),
    ("db/r2.py", ["tests/test_r2.py", "tests/test_snapshot.py"]),
    ("db/connection.py", ["tests/test_db_and_sync.py", "tests/test_db_lock_wait.py"]),

    # API endpoints
    ("api/connect.py", ["tests/test_connect_api.py", "tests/test_connect_functions.py"]),
    ("api/leagues_v2.py", ["tests/test_authorize_league.py", "tests/test_v2_authorization_matrix.py", "tests/test_league_scope_audit.py"]),
    ("api/deps.py", ["tests/test_authorize_league.py", "tests/test_v2_authorization_matrix.py", "tests/test_api.py"]),
    ("api/league_scope.py", ["tests/test_league_scope_audit.py", "tests/test_league_scope_routes.py"]),
    ("api/routes.py", ["tests/test_api.py", "tests/test_league_scope_audit.py"]),
    ("api/trade", ["tests/test_trades.py", "tests/test_trade_v3.py", "tests/test_trade_discovery.py"]),
    ("api/start_sit", ["tests/test_start_sit.py", "tests/test_start_sit_evaluation.py"]),
    ("api/kicker", ["tests/test_kicker_projections.py", "tests/test_kicker_reprojection.py"]),
    ("api/rankings", ["tests/test_rankings.py"]),
    ("api/matchup", ["tests/test_matchup.py"]),
    ("api/schedule", ["tests/test_schedule_intelligence.py"]),
    ("api/weekly_recap", ["tests/test_weekly_recap.py"]),

    # Analytics engines
    ("analytics/trades", ["tests/test_trades.py", "tests/test_trade_v3.py", "tests/test_trade_discovery.py", "tests/test_trade_dual_value.py"]),
    ("analytics/start_sit", ["tests/test_start_sit.py", "tests/test_start_sit_evaluation.py"]),
    ("analytics/kicker", ["tests/test_kicker_projections.py", "tests/test_kicker_reprojection.py", "tests/test_synthetic_kicker.py"]),
    ("analytics/injury", ["tests/test_injury_model.py", "tests/test_injury_impact.py", "tests/test_injury_workload.py", "tests/test_injury_freshness.py"]),
    ("analytics/projections", ["tests/test_espn_projections.py", "tests/test_independent_projections.py", "tests/test_projection_performance.py"]),
    ("analytics/waiver", ["tests/test_waivers.py", "tests/test_waiver_data.py", "tests/test_waiver_decisions.py"]),
    ("analytics/rescore", ["tests/test_rescore.py", "tests/test_league_scoring.py"]),
    ("analytics/league_scoring", ["tests/test_league_scoring.py", "tests/test_rescore.py"]),
    ("analytics/pipeline", ["tests/test_refresh_all.py", "tests/test_golden_legacy.py"]),

    # Static checks tool
    ("scripts/static_checks.py", ["tests/test_static_checks.py"]),
]


@dataclass
class StepResult:
    name: str
    target: str
    status: str  # PASS, FAIL, SKIP, WARN
    duration: float
    summary: str
    log_file: str | None = None
    stdout: str = ""
    stderr: str = ""


class VerificationRunner:
    def __init__(self, mode: str, base_ref: str | None = None, verbose: bool = False, dry_run: bool = False):
        self.mode = mode
        self.base_ref = base_ref or self._detect_default_base()
        self.verbose = verbose
        self.dry_run = dry_run
        self.results: list[StepResult] = []
        self.run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    def _detect_default_base(self) -> str:
        # Check if origin/main exists
        res = subprocess.run(["git", "rev-parse", "--verify", "origin/main"], capture_output=True)
        if res.returncode == 0:
            return "origin/main"
        res = subprocess.run(["git", "rev-parse", "--verify", "main"], capture_output=True)
        if res.returncode == 0:
            return "main"
        return "HEAD~1"

    def get_changed_files(self) -> list[str]:
        files: set[str] = set()
        # Committed diff against base
        cmd_diff = ["git", "diff", "--name-only", f"{self.base_ref}...HEAD"]
        p_diff = subprocess.run(cmd_diff, capture_output=True, text=True)
        if p_diff.returncode == 0:
            for line in p_diff.stdout.splitlines():
                if line.strip():
                    files.add(line.strip())

        # Unstaged / staged working tree changes
        cmd_status = ["git", "status", "--porcelain"]
        p_status = subprocess.run(cmd_status, capture_output=True, text=True)
        if p_status.returncode == 0:
            for line in p_status.stdout.splitlines():
                if len(line) > 3:
                    f = line[3:].strip()
                    # In case of renames, take the target name
                    if " -> " in f:
                        f = f.split(" -> ")[1].strip()
                    files.add(f)

        return sorted(files)

    def classify_changes(self, changed_files: list[str]) -> dict[str, list[str]]:
        classes = {
            "backend": [],
            "frontend": [],
            "sql_migrations": [],
            "security_sensitive": [],
            "tests": [],
            "other": [],
        }
        for f in changed_files:
            f_norm = f.replace("\\", "/")
            if "supabase/migrations/" in f_norm or f_norm.endswith("pg_shim.sql"):
                classes["sql_migrations"].append(f)
                classes["security_sensitive"].append(f)
            elif "backend/tests/" in f_norm:
                classes["tests"].append(f)
            elif "backend/" in f_norm:
                classes["backend"].append(f)
                if any(k in f_norm for k in ("control_plane/", "sync/", "authz", "jwt_auth", "credential")):
                    classes["security_sensitive"].append(f)
            elif "frontend/" in f_norm:
                classes["frontend"].append(f)
            else:
                classes["other"].append(f)
        return classes

    def infer_affected_tests(self, changed_files: list[str]) -> tuple[list[str], list[str]]:
        """Returns (backend_tests, frontend_actions)."""
        backend_tests: set[str] = set()
        frontend_actions: set[str] = set()

        classes = self.classify_changes(changed_files)

        # Directly changed backend tests
        for f in classes["tests"]:
            norm = f.replace("\\", "/")
            if norm.endswith(".py"):
                idx = norm.find("tests/")
                if idx != -1:
                    test_file = norm[idx:]
                    file_name = Path(test_file).name
                    if file_name.startswith("test_") or file_name.endswith("_test.py"):
                        backend_tests.add(test_file)

        # Changed backend modules
        for f in changed_files:
            norm = f.replace("\\", "/")
            for pattern, test_list in BACKEND_TEST_MAPPINGS:
                matched = False
                if isinstance(pattern, str):
                    matched = pattern in norm
                elif isinstance(pattern, re.Pattern):
                    matched = bool(pattern.search(norm))
                if matched:
                    backend_tests.update(test_list)

        # Frontend changes
        if classes["frontend"]:
            frontend_actions.add("lint")
            frontend_actions.add("typecheck")
            frontend_actions.add("unit_tests")
            # If changed files touch build boundary (app/ routes, server actions, configs, package.json)
            build_triggers = ("app/", "next.config", "package.json", "tsconfig.json", "middleware.ts")
            if any(any(trig in f for trig in build_triggers) for f in classes["frontend"]):
                frontend_actions.add("build")

        # Fallback: if backend was changed but no specific test mapped, run core test set
        if classes["backend"] and not backend_tests:
            backend_tests.update(["tests/test_api.py", "tests/test_db_and_sync.py", "tests/test_authorize_league.py"])

        # Filter out tests that don't exist on disk
        valid_backend_tests = []
        for t in sorted(backend_tests):
            test_path = BACKEND_DIR / t
            if test_path.is_file():
                valid_backend_tests.append(t)

        return valid_backend_tests, sorted(frontend_actions)

    def run_command(self, name: str, cmd: list[str], cwd: Path, target: str = "") -> StepResult:
        if self.dry_run:
            res = StepResult(name, target, "SKIP", 0.0, f"Dry run: {' '.join(cmd)}")
            self.results.append(res)
            return res

        start_time = time.time()
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        duration = time.time() - start_time

        out = p.stdout
        err = p.stderr
        combined = f"--- STDOUT ---\n{out}\n--- STDERR ---\n{err}"

        # Parse output for quick summary
        summary = ""
        status = "PASS" if p.returncode == 0 else "FAIL"

        # Check for pytest summary (both standard and -q format)
        pytest_m = re.search(r"(=+ .*(?:passed|failed|skipped|error).* =+)", out)
        if pytest_m:
            summary = pytest_m.group(1).strip("=").strip()
        else:
            pytest_q = re.search(r"(\d+ (?:passed|failed|error)[^\n]*)", out)
            if pytest_q:
                summary = pytest_q.group(1).strip()

        # Check for npm test summary
        npm_pass = re.search(r"ℹ pass (\d+)", out)
        npm_fail = re.search(r"ℹ fail (\d+)", out)
        if npm_pass and npm_fail:
            summary = f"{npm_pass.group(1)} passed, {npm_fail.group(1)} failed"

        if not summary:
            summary = f"exit code {p.returncode}"

        # Write log file if failed or verbose
        log_file = None
        if status == "FAIL" or self.verbose:
            DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^\w-]", "_", name.lower())
            log_path = DIAGNOSTICS_DIR / f"{self.run_id}_{safe_name}.log"
            log_path.write_text(combined, encoding="utf-8")
            log_file = str(log_path)

        res = StepResult(name, target, status, duration, summary, log_file, out, err)
        self.results.append(res)
        return res

    def run_doctor(self, clean_orphans: bool = False) -> int:
        print("\n=== Running Environment Doctor ===")
        all_ok = True

        def check_item(label: str, ok: bool, details: str):
            nonlocal all_ok
            if not ok:
                all_ok = False
            status = "✓ OK" if ok else "✗ FAIL"
            print(f"  {status:<8} {label:<24} : {details}")

        # Python & uv
        uv_bin = shutil.which("uv")
        if uv_bin:
            p = subprocess.run(["uv", "--version"], capture_output=True, text=True)
            check_item("uv package manager", p.returncode == 0, p.stdout.strip())
            p_py = subprocess.run(["uv", "run", "python", "--version"], cwd=BACKEND_DIR, capture_output=True, text=True)
            py_ver = p_py.stdout.strip()
            check_item("Backend Python", "3.12" in py_ver, f"{py_ver} (via uv)")
        else:
            check_item("uv", False, "uv binary not found in PATH")
            check_item("Backend Python", False, "install uv or Python 3.12")

        # Node
        node_bin = shutil.which("node")
        if node_bin:
            p = subprocess.run(["node", "--version"], capture_output=True, text=True)
            check_item("Node.js Version", p.returncode == 0, p.stdout.strip())
        else:
            check_item("Node.js", False, "node binary not found in PATH")

        # npm
        npm_bin = shutil.which("npm")
        if npm_bin:
            p = subprocess.run(["npm", "--version"], capture_output=True, text=True)
            check_item("npm", p.returncode == 0, p.stdout.strip())
        else:
            check_item("npm", False, "npm binary not found in PATH")

        # Postgres
        initdb_bin = shutil.which("initdb") or "/opt/homebrew/bin/initdb"
        pg_ctl_bin = shutil.which("pg_ctl") or "/opt/homebrew/bin/pg_ctl"
        pg_avail = Path(initdb_bin).exists() and Path(pg_ctl_bin).exists()
        ext_dsn = os.environ.get("FI_TEST_PG_DSN")
        check_item("PostgreSQL Local", pg_avail or bool(ext_dsn),
                   ext_dsn if ext_dsn else f"initdb: {initdb_bin}")

        # DuckDB in /tmp via backend environment
        if uv_bin:
            duckdb_check = subprocess.run(
                ["uv", "run", "python", "-c",
                 "import duckdb, os, pathlib; p = pathlib.Path('/tmp') / f'doctor_{os.getpid()}.duckdb'; "
                 "con = duckdb.connect(str(p)); con.execute('CREATE TABLE t (x INT)'); con.close(); p.unlink()"],
                cwd=BACKEND_DIR, capture_output=True, text=True
            )
            check_item("DuckDB /tmp Access", duckdb_check.returncode == 0,
                       "can create and write DuckDB files in /tmp" if duckdb_check.returncode == 0 else duckdb_check.stderr.strip())
        else:
            check_item("DuckDB /tmp Access", False, "uv not available")

        # Ports check
        for port, service in [(8767, "E2E API"), (8768, "E2E Auth"), (3102, "E2E Web")]:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                in_use = s.connect_ex(("127.0.0.1", port)) == 0
                check_item(f"Port {port} ({service})", not in_use, "in use (stale server?)" if in_use else "available")

        # Temp directory check for orphaned fipg clusters
        orphans = list(Path("/tmp").glob("fipg*"))
        if clean_orphans and orphans:
            cleaned = 0
            for d in orphans:
                pid_file = d / "data" / "postmaster.pid"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text().splitlines()[0].strip())
                        import signal
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
            orphans = list(Path("/tmp").glob("fipg*"))
            print(f"  ↳ Cleaned up {cleaned} orphaned fipg test directory(ies)")

        check_item("Orphaned Pg Clusters", len(orphans) == 0,
                   f"{len(orphans)} fipg dirs found in /tmp (run 'python fantasy-intel/scripts/agent_check.py doctor --clean-orphans' to clean)" if orphans else "clean")

        print("===================================\n")
        return 0 if all_ok else 1

    def run_static_checks(self, files: Sequence[str] | None = None) -> StepResult:
        static_script = REPO_ROOT / "fantasy-intel" / "scripts" / "static_checks.py"
        cmd = [sys.executable, str(static_script)]
        if files:
            py_files = [f for f in files if f.endswith(".py")]
            if py_files:
                cmd.extend(["--files"] + py_files)
        return self.run_command("Static Safety", cmd, cwd=REPO_ROOT, target="AST rules")

    def run_backend_tests(self, test_files: list[str], title: str = "Backend Tests") -> StepResult | None:
        if not test_files:
            return None
        cmd = ["uv", "run", "pytest", "-q", "--tb=short"] + test_files
        target_summary = f"{len(test_files)} files: {', '.join(Path(t).name for t in test_files[:3])}"
        if len(test_files) > 3:
            target_summary += f" (+{len(test_files) - 3} more)"
        return self.run_command(title, cmd, cwd=BACKEND_DIR, target=target_summary)

    def run_frontend_actions(self, actions: list[str]) -> list[StepResult]:
        results = []
        if "lint" in actions:
            results.append(self.run_command("Frontend Lint", ["npm", "run", "lint"], cwd=FRONTEND_DIR, target="ESLint"))
        if "typecheck" in actions:
            results.append(self.run_command("Frontend Typecheck", ["npm", "run", "typecheck"], cwd=FRONTEND_DIR, target="TypeScript"))
        if "unit_tests" in actions:
            results.append(self.run_command("Frontend Tests", ["npm", "test"], cwd=FRONTEND_DIR, target="Node Test Runner"))
        if "build" in actions:
            results.append(self.run_command("Frontend Build", ["npm", "run", "build"], cwd=FRONTEND_DIR, target="Next.js Production Build"))
        return results

    def execute(self, clean_orphans: bool = False) -> int:
        if self.mode == "doctor":
            return self.run_doctor(clean_orphans=clean_orphans)

        print(f"\n[agent_check] Mode: {self.mode.upper()} | Base: {self.base_ref}")
        changed_files = self.get_changed_files()
        print(f"[agent_check] Detected {len(changed_files)} changed file(s)")

        if self.mode == "quick":
            # 1. Static checks on changed files
            self.run_static_checks(changed_files)
            # 2. Directly affected backend tests
            backend_tests, frontend_actions = self.infer_affected_tests(changed_files)
            if backend_tests:
                # In quick mode, pick up to 3 highest priority tests
                self.run_backend_tests(backend_tests[:3], title="Quick Backend")
            if "typecheck" in frontend_actions:
                self.run_command("Frontend Typecheck", ["npm", "run", "typecheck"], cwd=FRONTEND_DIR, target="TypeScript")
            if "unit_tests" in frontend_actions:
                self.run_command("Frontend Tests", ["npm", "test"], cwd=FRONTEND_DIR, target="Unit Tests")

        elif self.mode == "affected":
            self.run_static_checks(changed_files)
            backend_tests, frontend_actions = self.infer_affected_tests(changed_files)
            if backend_tests:
                self.run_backend_tests(backend_tests, title="Affected Backend")
            self.run_frontend_actions(frontend_actions)

        elif self.mode == "security":
            self.run_static_checks()
            # Run the complete security regression suite
            self.run_backend_tests(SECURITY_SUITES, title="Multi-League Security Suite")

        elif self.mode == "full":
            self.run_static_checks()
            # Run full backend suite
            self.run_command("Full Backend", ["uv", "run", "pytest", "-q"], cwd=BACKEND_DIR, target="All Shards / Suites")
            # Run all frontend gates
            self.run_frontend_actions(["lint", "typecheck", "unit_tests", "build"])
            # Run Golden check if duckdb available
            golden_test = BACKEND_DIR / "tests" / "test_golden_legacy.py"
            if golden_test.exists():
                self.run_backend_tests(["tests/test_golden_legacy.py"], title="Golden Suite")

        return self.print_summary()

    def print_summary(self) -> int:
        print("\n" + "=" * 80)
        print(f"{'VERIFICATION SUMMARY':^80}")
        print("=" * 80)
        print(f"{'Step':<24} {'Target':<24} {'Status':<8} {'Time':<8} {'Details'}")
        print("-" * 80)

        any_failure = False
        total_time = 0.0
        for r in self.results:
            total_time += r.duration
            if r.status == "FAIL":
                any_failure = True
            time_str = f"{r.duration:.1f}s"
            status_str = f"[{r.status}]"
            print(f"{r.name:<24} {r.target:<24} {status_str:<8} {time_str:<8} {r.summary}")
            if r.status == "FAIL" and r.log_file:
                print(f"  ↳ Diagnostic log saved: {r.log_file}")
                # Print last 3 error lines if available
                lines = [l for l in (r.stderr or r.stdout).splitlines() if "FAILED" in l or "Error:" in l or "error:" in l]
                for l in lines[:3]:
                    print(f"    • {l.strip()[:100]}")

        print("-" * 80)
        overall = "FAIL" if any_failure else "PASS"
        print(f"Overall Result: [{overall}] in {total_time:.1f}s")
        print("=" * 80)

        # Suggest next command
        if any_failure:
            print("\nSuggested next step: inspect diagnostic logs or run targeted test directly.")
        else:
            if self.mode == "quick":
                print("\nSuggested next step: python fantasy-intel/scripts/agent_check.py affected")
            elif self.mode == "affected":
                print("\nSuggested next step: python fantasy-intel/scripts/agent_check.py security")
            elif self.mode == "security":
                print("\nSuggested next step: ready for subagent code-review or final gate full run.")

        return 1 if any_failure else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repo-native verification runner for fantasy-intel")
    parser.add_argument("mode", choices=["quick", "affected", "security", "full", "doctor"],
                        default="affected", nargs="?", help="Verification mode")
    parser.add_argument("--base", default=None, help="Base Git ref to compare against (e.g. origin/main)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Store and print verbose logs")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without running commands")
    parser.add_argument("--clean-orphans", action="store_true", help="Clean orphaned test postgres clusters in /tmp (doctor mode)")

    args = parser.parse_args(argv)
    runner = VerificationRunner(mode=args.mode, base_ref=args.base, verbose=args.verbose, dry_run=args.dry_run)
    return runner.execute(clean_orphans=args.clean_orphans)


if __name__ == "__main__":
    sys.exit(main())

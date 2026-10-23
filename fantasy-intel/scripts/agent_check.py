#!/usr/bin/env python3
"""Repo-native verification workflow for Claude Code and developers on fantasy-intel.

Modes:
  quick:     Fast checks for current changes (<60s). Runs up to 3 prioritized test files and AST checks;
             files listed in QUICK_EXCLUDED_TESTS are deferred to `affected`.
  affected:  Targeted tests conservatively inferred from changed files against a base ref.
             Automatically triggers security regression suite if security boundaries are touched.
  security:  Targeted multileague security, RLS, authorization, crypto, worker fencing, and static checks.
  full:      Comprehensive test suites across backend, frontend, golden responses, build, and Playwright E2E.
             Reports UNVERIFIED if local environment cannot execute E2E, referencing CI on exact commit SHA.
  doctor:    Diagnose local environment (Python, Node, uv, Postgres, ports, temp dirs) without side-effects.
             Safely distinguishes active, orphaned, and unknown test Postgres clusters.

Usage:
  python fantasy-intel/scripts/agent_check.py quick
  python fantasy-intel/scripts/agent_check.py affected [--base origin/main]
  python fantasy-intel/scripts/agent_check.py security
  python fantasy-intel/scripts/agent_check.py full
  python fantasy-intel/scripts/agent_check.py doctor [--clean-orphans]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
    "tests/test_multileague_isolation.py",
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
    ("control_plane/authz.py", [
        "tests/test_authorize_league.py",
        "tests/test_v2_authorization_matrix.py",
        "tests/test_league_scope_audit.py",
    ]),
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
    ("api/leagues_v2.py", [
        "tests/test_leagues_api.py",
        "tests/test_authorize_league.py",
        "tests/test_v2_authorization_matrix.py",
        "tests/test_league_scope_audit.py",
        "tests/test_multileague_isolation.py",
    ]),
    ("api/deps.py", ["tests/test_authorize_league.py", "tests/test_v2_authorization_matrix.py", "tests/test_api.py"]),
    ("api/league_scope.py", ["tests/test_league_scope.py", "tests/test_league_scope_audit.py", "tests/test_league_scope_sleeper.py"]),
    ("api/routes.py", ["tests/test_api.py", "tests/test_league_scope_audit.py"]),
    ("api/trade", ["tests/test_trades.py", "tests/test_trade_v3.py", "tests/test_trade_discovery.py"]),
    ("api/start_sit", ["tests/test_start_sit.py", "tests/test_start_sit_evaluation.py", "tests/test_lineup_what_if.py"]),
    ("api/kicker", ["tests/test_kicker_projections.py", "tests/test_kicker_reprojection.py"]),
    ("api/rankings", ["tests/test_rankings.py"]),
    ("api/matchup", ["tests/test_matchup_api.py"]),
    ("api/schedule", ["tests/test_schedule_intelligence.py"]),
    ("api/weekly_recap", ["tests/test_weekly_recap.py"]),
    ("api/manager_scouting", ["tests/test_manager_scouting.py"]),
    ("api/player_compare", ["tests/test_player_compare.py"]),
    ("api/playoff_race", ["tests/test_playoff_race.py"]),
    ("api/projection_ranges", ["tests/test_projection_ranges.py"]),
    ("api/power_rankings", ["tests/test_power_rankings.py"]),
    ("api/league_rules", ["tests/test_league_rules.py", "tests/test_golden_legacy.py", "tests/test_provider_copy.py"]),
    ("api/luck_lab", ["tests/test_luck_lab.py"]),
    ("api/dst_projections", ["tests/test_dst_production.py", "tests/test_dst_shadow.py"]),

    # Analytics engines
    ("analytics/trades", ["tests/test_trades.py", "tests/test_trade_v3.py", "tests/test_trade_discovery.py", "tests/test_trade_dual_value.py"]),
    ("analytics/start_sit", ["tests/test_start_sit.py", "tests/test_start_sit_evaluation.py"]),
    ("analytics/lineup_what_if", ["tests/test_lineup_what_if.py", "tests/test_start_sit.py", "tests/test_sleeper_taxi.py",
                                  "tests/test_golden_legacy.py"]),
    ("analytics/kicker", ["tests/test_kicker_projections.py", "tests/test_kicker_reprojection.py", "tests/test_kdst_streaming.py"]),
    ("analytics/injury", ["tests/test_injury_model.py", "tests/test_injury_impact.py", "tests/test_injury_workload.py", "tests/test_injury_freshness.py"]),
    ("analytics/projections", ["tests/test_espn_projections.py", "tests/test_independent_projections.py", "tests/test_projection_performance.py"]),
    ("analytics/waiver", ["tests/test_waivers.py", "tests/test_waiver_data.py", "tests/test_waiver_decisions.py"]),
    ("analytics/rescore", ["tests/test_rescore.py", "tests/test_sleeper_scoring.py", "tests/test_forward_ours.py"]),
    ("analytics/league_scoring", ["tests/test_sleeper_scoring.py", "tests/test_rescore.py"]),
    ("analytics/league_rules", ["tests/test_league_rules.py", "tests/test_golden_legacy.py"]),
    ("analytics/pipeline", ["tests/test_refresh_all.py", "tests/test_golden_legacy.py"]),

    # Provider packages
    ("sleeper/", ["tests/test_sleeper_client.py", "tests/test_sleeper_normalize.py", "tests/test_sleeper_scoring.py", "tests/test_sleeper_write.py"]),
    ("espn/", ["tests/test_espn_benchmark.py", "tests/test_espn_projections.py", "tests/test_sync_espn_source.py"]),
    ("nfl/", ["tests/test_nfl_contract.py", "tests/test_nfl_ngs_passing.py", "tests/test_mapping.py"]),

    # Tooling scripts self-tests
    ("scripts/agent_check.py", ["tests/test_agent_check.py"]),
    ("scripts/static_checks.py", ["tests/test_static_checks.py"]),
]


# App-wide source audits: they read every file under backend/app/ (forbidden research imports, the documented readers
# of projection_source / league_scoring), so a NEW app module can break them without any mapping pointing at them.
# They are fast; `affected` adds them whenever an app/ .py or .sql source changes so CI is not the first place they run.
APP_WIDE_AUDITS: list[str] = [
    "tests/test_weekly_learning.py",
    "tests/test_dst_research.py",
    "tests/test_independent_projections.py",
]

# Test files too slow for `quick` (each measured alone on 2026-10-22; slower under load). `quick` runs at most three
# files under a 60 s budget, so these are left out of it and reported as deferred. Their coverage is unchanged:
# `affected`, `security` and CI still run them whenever their mappings select them.
QUICK_EXCLUDED_TESTS: dict[str, str] = {
    "tests/test_league_scope.py": "~66-85 s",
    "tests/test_v2_authorization_matrix.py": "~28 s",
}
QUICK_MAX_FILES = 3


def select_quick_tests(backend_tests: list[str]) -> tuple[list[str], list[str]]:
    """Return (files `quick` runs, heavy files it defers to `affected`), preserving priority order."""
    deferred = [t for t in backend_tests if t in QUICK_EXCLUDED_TESTS]
    selected = [t for t in backend_tests if t not in QUICK_EXCLUDED_TESTS][:QUICK_MAX_FILES]
    return selected, deferred


def validate_test_mappings() -> list[str]:
    """Audit all mapped test paths against disk. Returns list of stale test file errors."""
    stale = []
    for pattern, tests in BACKEND_TEST_MAPPINGS:
        for t in tests:
            if not (BACKEND_DIR / t).is_file():
                stale.append(f"Pattern '{pattern}': test file '{t}' does not exist on disk")
    for s in SECURITY_SUITES:
        if not (BACKEND_DIR / s).is_file():
            stale.append(f"Security suite: test file '{s}' does not exist on disk")
    for a in APP_WIDE_AUDITS:
        if not (BACKEND_DIR / a).is_file():
            stale.append(f"App-wide audit: test file '{a}' does not exist on disk")
    for q in QUICK_EXCLUDED_TESTS:
        if not (BACKEND_DIR / q).is_file():
            stale.append(f"Quick exclusion: test file '{q}' does not exist on disk")
    return stale


def _get_process_info(pid: int) -> tuple[int, str] | None:
    """Retrieve process UID and commandline arguments safely."""
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,args="], capture_output=True, text=True, timeout=2)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        parts = p.stdout.strip().split(None, 1)
        if len(parts) < 2:
            return None
        return int(parts[0]), parts[1]
    except Exception:
        return None


def _is_exact_postgres_instance(pid: int, data_dir: str | Path) -> bool:
    """Verify that PID is an active process owned by current user running postgres for exact data_dir.
    Protects against PID reuse."""
    info = _get_process_info(pid)
    if not info:
        return False
    uid, cmd = info
    if uid != os.getuid():
        return False
    try:
        tokens = shlex.split(cmd)
    except Exception:
        tokens = cmd.split()
    if not tokens:
        return False
    # Executable binary must be postgres
    if Path(tokens[0]).name != "postgres":
        return False
    # Exact data directory must match -D argument
    try:
        target_data = Path(data_dir).resolve()
    except Exception:
        return False
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


def classify_cluster(cluster_dir: Path) -> dict:
    """Classify a /tmp/fipg* directory as 'active', 'orphaned', or 'unknown'."""
    # 1. Path safety check: no symlinks, resolved inside temp directory
    if cluster_dir.is_symlink():
        return {"status": "unknown", "reason": "symlink detected (unsafe)", "dir": cluster_dir}
    try:
        resolved = cluster_dir.resolve()
        temp_dir = Path(tempfile.gettempdir()).resolve()
        resolved.relative_to(temp_dir)
    except Exception:
        return {"status": "unknown", "reason": "path traversal outside temp dir", "dir": cluster_dir}
    if not resolved.name.startswith("fipg"):
        return {"status": "unknown", "reason": "directory name does not match harness prefix", "dir": cluster_dir}
    try:
        if cluster_dir.stat().st_uid != os.getuid():
            return {"status": "unknown", "reason": "owned by different user", "dir": cluster_dir}
    except Exception as e:
        return {"status": "unknown", "reason": f"stat error: {e}", "dir": cluster_dir}

    # 2. Check nested paths for unsafe symlinks
    for root, dirs, files in os.walk(cluster_dir, followlinks=False):
        for d in dirs:
            if (Path(root) / d).is_symlink():
                return {"status": "unknown", "reason": "nested directory symlink detected (unsafe)", "dir": cluster_dir}
        for f in files:
            if (Path(root) / f).is_symlink():
                return {"status": "unknown", "reason": "nested file symlink detected (unsafe)", "dir": cluster_dir}

    data_dir = cluster_dir / "data"
    pid_file = data_dir / "postmaster.pid"
    owner_file = cluster_dir / "owner.json"

    # 3. Require verified harness ownership metadata (missing or malformed owner.json is UNKNOWN)
    if not owner_file.is_file():
        return {"status": "unknown", "reason": "missing owner.json (untrusted directory)", "dir": cluster_dir}

    try:
        owner_data = json.loads(owner_file.read_text(encoding="utf-8"))
        if not isinstance(owner_data, dict):
            return {"status": "unknown", "reason": "malformed owner.json", "dir": cluster_dir}
    except Exception as e:
        return {"status": "unknown", "reason": f"unreadable owner.json: {e}", "dir": cluster_dir}

    if owner_data.get("harness") != "fantasy-intel-pg-harness":
        return {"status": "unknown", "reason": "untrusted harness marker in owner.json", "dir": cluster_dir}

    # Validate recorded directories match this exact cluster directory and data directory
    recorded_data = owner_data.get("data_dir")
    if not recorded_data:
        return {"status": "unknown", "reason": "missing data_dir in owner.json", "dir": cluster_dir}
    try:
        if Path(recorded_data).resolve() != data_dir.resolve():
            return {"status": "unknown", "reason": "recorded data_dir mismatch in owner.json", "dir": cluster_dir}
    except Exception:
        return {"status": "unknown", "reason": "invalid data_dir in owner.json", "dir": cluster_dir}

    recorded_cluster = owner_data.get("cluster_dir")
    if recorded_cluster:
        try:
            if Path(recorded_cluster).resolve() != cluster_dir.resolve():
                return {"status": "unknown", "reason": "recorded cluster_dir mismatch in owner.json", "dir": cluster_dir}
        except Exception:
            return {"status": "unknown", "reason": "invalid cluster_dir in owner.json", "dir": cluster_dir}

    runner_pid = owner_data.get("runner_pid")
    if not isinstance(runner_pid, int) or runner_pid <= 0:
        return {"status": "unknown", "reason": "invalid runner_pid in owner.json", "dir": cluster_dir}

    # 4. Check runner liveness
    try:
        os.kill(runner_pid, 0)
        runner_alive = True
    except OSError:
        runner_alive = False

    if runner_alive:
        pm_pid = None
        if pid_file.is_file():
            try:
                pm_pid = int(pid_file.read_text().splitlines()[0].strip())
            except Exception:
                pass
        return {
            "status": "active",
            "reason": f"active test cluster for runner PID {runner_pid}",
            "dir": cluster_dir,
            "data": data_dir,
            "pid": pm_pid,
        }

    # Runner is confirmed dead. Now inspect postmaster.pid
    if not pid_file.is_file():
        # Harness-owned cluster with dead runner and no postmaster running
        return {
            "status": "orphaned",
            "reason": "verified harness cluster abandoned by dead runner",
            "dir": cluster_dir,
            "data": data_dir,
        }

    try:
        lines = pid_file.read_text().splitlines()
        pm_pid = int(lines[0].strip())
    except Exception:
        return {"status": "unknown", "reason": "corrupted postmaster.pid", "dir": cluster_dir}

    try:
        os.kill(pm_pid, 0)
        pm_alive = True
    except OSError:
        pm_alive = False

    if pm_alive:
        is_harness_pg = _is_exact_postgres_instance(pm_pid, data_dir)
        if is_harness_pg:
            return {
                "status": "orphaned",
                "reason": f"runner dead, postmaster {pm_pid} running orphaned",
                "dir": cluster_dir,
                "pid": pm_pid,
                "data": data_dir,
                "pg_ctl": owner_data.get("pg_ctl"),
            }
        else:
            # Running PID does not match exact postgres binary and data dir: PID reuse!
            return {"status": "unknown", "reason": f"PID {pm_pid} reused by unrelated process", "dir": cluster_dir}
    else:
        # Postmaster PID file exists but process is dead
        return {
            "status": "orphaned",
            "reason": "stale PID file from dead harness cluster",
            "dir": cluster_dir,
            "data": data_dir,
        }


def _redact_sensitive(text: str) -> str:
    """Comprehensive redaction of passwords, bearer tokens, cookies, SWIDs, and API keys."""
    if not text:
        return ""
    # PostgreSQL URIs with passwords
    text = re.sub(r"(postgres(?:ql)?://[^:]+:)[^@]+(@)", r"\1[REDACTED]\2", text)
    # Conninfo / parameter passwords
    text = re.sub(r"(password\s*[:=]\s*)[^\s;&\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    # ESPN cookies (espn_s2, ESPN_S2, espn-s2, etc.)
    text = re.sub(r"((?:espn_s2|espn-s2)\s*[:=]\s*)[^\s;&\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    # SWID in cookies or config (swid=, SWID=, swid:, SWID:)
    text = re.sub(r"(\bswid\s*[:=]\s*)[^\s;&\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    # Explicit SWID UUID pattern {xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}
    text = re.sub(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}", "{[SWID_REDACTED]}", text)
    # Bearer tokens
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9_.\-]+", r"\1[TOKEN_REDACTED]", text)
    # Generic secret/token/api_key
    text = re.sub(r"((?:token|secret|api_key|apikey)\s*[:=]\s*)[^\s;&\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    # Private keys
    text = re.sub(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----", "[PRIVATE_KEY_REDACTED]", text)
    return text


@dataclass
class StepResult:
    name: str
    target: str
    status: str  # PASS, FAIL, SKIP, WARN, UNVERIFIED
    duration: float
    summary: str
    log_file: str | None = None
    stdout: str = ""
    stderr: str = ""


class VerificationRunner:
    def __init__(self, mode: str, base_ref: str | None = None, verbose: bool = False, dry_run: bool = False):
        self.mode = mode
        self.explicit_base = base_ref is not None
        self.base_ref = base_ref or self._detect_default_base()
        self.verbose = verbose
        self.dry_run = dry_run
        self.results: list[StepResult] = []
        self.warnings: list[str] = []
        self.coverage_error = False
        self.coverage_incomplete = False  # True when diff is a partial view (HEAD~1 fallback)
        self.run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Automatically check for stale test mappings
        stale_mappings = validate_test_mappings()
        if stale_mappings:
            for sm in stale_mappings:
                print(f"[ERROR] Stale test mapping: {sm}", file=sys.stderr)
            raise RuntimeError(f"Configured test mappings contain {len(stale_mappings)} stale test file references.")

    def _detect_default_base(self) -> str:
        res = subprocess.run(["git", "rev-parse", "--verify", "origin/main"], capture_output=True)
        if res.returncode == 0:
            return "origin/main"
        res = subprocess.run(["git", "rev-parse", "--verify", "main"], capture_output=True)
        if res.returncode == 0:
            return "main"
        return "HEAD~1"

    def _parse_name_status(self, stdout: str, files: set[str], deleted_sources: set[str] | None = None) -> None:
        """Parse git --name-status output.

        Non-deleted paths are added to *files* for test running.
        Deleted source paths (not in tests/) are added to *deleted_sources* so that
        test-mapping can still fire for the removed module — but only the mapped test
        files (which still exist on disk) will be run, never the deleted path itself.
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            status = parts[0]
            if status.startswith("D"):
                # Deleted file: preserve source path for test-mapping inference only.
                if len(parts) >= 2 and deleted_sources is not None:
                    deleted_path = parts[1]
                    # Don't add deleted test files — we can never run them.
                    norm = deleted_path.replace("\\", "/")
                    if "backend/tests/" not in norm:
                        deleted_sources.add(deleted_path)
                continue
            if len(parts) >= 2:
                # If renamed (R100 old new), take the target file
                file_path = parts[-1]
                files.add(file_path)

    def get_changed_files(self) -> tuple[list[str], list[str]]:
        """Return (changed_files_for_running, warnings).

        changed_files_for_running includes existing paths plus deleted source paths
        (tagged internally via _deleted_sources so infer_affected_tests can map them
        to surviving test files without trying to run the deleted paths themselves).
        """
        files: set[str] = set()
        self._deleted_sources: set[str] = set()
        warnings: list[str] = []

        if self.explicit_base:
            p_check = subprocess.run(["git", "rev-parse", "--verify", self.base_ref], capture_output=True, text=True)
            if p_check.returncode != 0:
                err_msg = f"Explicit base ref '{self.base_ref}' cannot be resolved in Git: {p_check.stderr.strip()}"
                self.results.append(StepResult("Base Ref Validation", self.base_ref, "FAIL", 0.0, err_msg))
                self.coverage_error = True
                return [], [err_msg]

        # 1. Base diff
        cmd_diff = ["git", "diff", "--name-status", f"{self.base_ref}...HEAD"]
        p_diff = subprocess.run(cmd_diff, capture_output=True, text=True)
        if p_diff.returncode != 0:
            if self.explicit_base:
                err_msg = f"Failed to diff against explicit base '{self.base_ref}': {p_diff.stderr.strip()}"
                self.results.append(StepResult("Base Ref Validation", self.base_ref, "FAIL", 0.0, err_msg))
                self.coverage_error = True
                return [], [err_msg]
            warnings.append(f"Failed to diff against '{self.base_ref}': {p_diff.stderr.strip() or 'unknown git error'}. Falling back to HEAD~1 (partial view — run against full branch for accurate coverage).")
            p_fallback = subprocess.run(["git", "diff", "--name-status", "HEAD~1...HEAD"], capture_output=True, text=True)
            if p_fallback.returncode == 0:
                self._parse_name_status(p_fallback.stdout, files, self._deleted_sources)
                # HEAD~1 is a single-commit view, not the full branch diff; flag as incomplete
                self.coverage_incomplete = True
            else:
                warnings.append("Failed to diff against HEAD~1 (initial commit or shallow clone). Using working tree status.")
                self.coverage_incomplete = True
        else:
            self._parse_name_status(p_diff.stdout, files, self._deleted_sources)
            if not self.explicit_base and self.base_ref == "HEAD~1":
                self.coverage_incomplete = True
                warnings.append("Default base ref fell back to HEAD~1 (main branch not found). Partial view — run against full branch for accurate coverage.")

        # 2. Working tree status (staged, unstaged, untracked)
        cmd_status = ["git", "status", "--porcelain"]
        p_status = subprocess.run(cmd_status, capture_output=True, text=True)
        if p_status.returncode == 0:
            for line in p_status.stdout.splitlines():
                if len(line) > 3:
                    code = line[:2]
                    path_part = line[3:].strip()
                    if "D" in code:
                        # Preserve working-tree deleted sources for mapping
                        norm = path_part.replace("\\", "/")
                        if "backend/tests/" not in norm:
                            self._deleted_sources.add(path_part)
                        continue
                    if " -> " in path_part:
                        path_part = path_part.split(" -> ")[1].strip()
                    files.add(path_part)
        else:
            warnings.append("Failed to read git working tree status.")

        return sorted(files), warnings

    def classify_changes(self, changed_files: list[str]) -> dict[str, list[str]]:
        classes = {
            "backend": [],
            "frontend": [],
            "sql_migrations": [],
            "security_sensitive": [],
            "tests": [],
            "workflows": [],
            "config": [],
            "other": [],
        }
        for f in changed_files:
            f_norm = f.replace("\\", "/")
            if "supabase/migrations/" in f_norm or f_norm.endswith("pg_shim.sql"):
                classes["sql_migrations"].append(f)
                classes["security_sensitive"].append(f)
            elif ".github/workflows/" in f_norm:
                classes["workflows"].append(f)
            elif f_norm in (".gitignore", "CLAUDE.md", "README.md", "pyproject.toml", "package.json"):
                classes["config"].append(f)
            elif "backend/tests/" in f_norm:
                classes["tests"].append(f)
            elif "backend/" in f_norm or f_norm.startswith("fantasy-intel/scripts/"):
                classes["backend"].append(f)
                if any(k in f_norm for k in ("control_plane/", "sync/", "authz", "jwt_auth", "credential", "security")):
                    classes["security_sensitive"].append(f)
            elif "frontend/" in f_norm:
                classes["frontend"].append(f)
            else:
                classes["other"].append(f)
        return classes

    def infer_affected_tests(self, changed_files: list[str]) -> tuple[list[str], list[str], list[str]]:
        """Returns (prioritized_backend_tests, frontend_actions, unmapped_files).

        Deleted source files (tracked in self._deleted_sources after get_changed_files())
        are included in mapping lookups so that removing a module still triggers its
        associated tests. Deleted test files are never added to the runnable list.
        """
        backend_tests: list[str] = []
        frontend_actions: set[str] = set()
        unmapped_files: list[str] = []

        classes = self.classify_changes(changed_files)

        # 1. Directly modified test files (Highest priority)
        direct_tests = []
        for f in classes["tests"]:
            norm = f.replace("\\", "/")
            if norm.endswith(".py"):
                idx = norm.find("tests/")
                if idx != -1:
                    test_file = norm[idx:]
                    file_name = Path(test_file).name
                    if (file_name.startswith("test_") or file_name.endswith("_test.py")) and (BACKEND_DIR / test_file).is_file():
                        direct_tests.append(test_file)

        backend_tests.extend(direct_tests)

        # 2. Directly mapped tests for changed files, including deleted source files.
        # Deleted source paths participate in mapping (their associated tests still exist
        # and must run), but we never add the deleted path itself to the runnable list.
        deleted_sources: set[str] = getattr(self, "_deleted_sources", set())
        all_source_paths = list(changed_files) + [p for p in deleted_sources if p not in changed_files]
        for f in all_source_paths:
            norm = f.replace("\\", "/")
            matched = False
            for pattern, test_list in BACKEND_TEST_MAPPINGS:
                is_match = False
                if isinstance(pattern, str):
                    is_match = pattern in norm
                elif isinstance(pattern, re.Pattern):
                    is_match = bool(pattern.search(norm))
                if is_match:
                    matched = True
                    for t in test_list:
                        # Only add test files that actually exist on disk
                        if t not in backend_tests and (BACKEND_DIR / t).is_file():
                            backend_tests.append(t)
            if not matched and (f in classes["backend"] or f in classes["sql_migrations"]):
                unmapped_files.append(f)
            # Deleted source path with no mapping → include in unmapped for conservative fallback
            if not matched and f in deleted_sources:
                f_norm = f.replace("\\", "/")
                if "backend/" in f_norm and "backend/tests/" not in f_norm:
                    if f not in unmapped_files:
                        unmapped_files.append(f)

        # 2b. App-wide source audits whenever any backend app/ source changed (including new modules).
        if any("backend/app/" in p.replace("\\", "/") and p.endswith((".py", ".sql")) for p in all_source_paths):
            for t in APP_WIDE_AUDITS:
                if t not in backend_tests and (BACKEND_DIR / t).is_file():
                    backend_tests.append(t)

        # 3. Frontend actions
        if classes["frontend"]:
            frontend_actions.add("lint")
            frontend_actions.add("typecheck")
            frontend_actions.add("unit_tests")
            build_triggers = ("app/", "next.config", "package.json", "tsconfig.json", "middleware.ts")
            if any(any(trig in f for trig in build_triggers) for f in classes["frontend"]):
                frontend_actions.add("build")

        # 4. Conservative fallback: never silently drop unmapped changes
        if unmapped_files:
            fallback = [
                "tests/test_api.py",
                "tests/test_db_and_sync.py",
                "tests/test_authorize_league.py",
                "tests/test_v2_authorization_matrix.py",
                "tests/test_league_scope_audit.py",
            ]
            for fb in fallback:
                if fb not in backend_tests and (BACKEND_DIR / fb).is_file():
                    backend_tests.append(fb)

        return backend_tests, sorted(frontend_actions), unmapped_files

    def run_command(self, name: str, cmd: list[str], cwd: Path, target: str = "", timeout: float = 180.0) -> StepResult:
        safe_cmd = [_redact_sensitive(c) for c in cmd]
        target_display = _redact_sensitive(target)
        if self.dry_run:
            res = StepResult(name, target_display, "SKIP", 0.0, f"Dry run: {' '.join(safe_cmd)}")
            self.results.append(res)
            return res

        start_time = time.time()
        try:
            # Use start_new_session=True to cleanly terminate the process group on timeout
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                raw_out, raw_err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        raw_out, raw_err = proc.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(pgid, signal.SIGKILL)
                        raw_out, raw_err = proc.communicate()
                except Exception:
                    proc.kill()
                    raw_out, raw_err = proc.communicate()
                out = _redact_sensitive(raw_out or "")
                err = _redact_sensitive(raw_err or "")
                duration = time.time() - start_time
                summary = f"TIMEOUT (exceeded {timeout}s limit)"
                log_file = self._save_diagnostic(name, f"Command timed out after {timeout}s: {' '.join(safe_cmd)}\n--- STDOUT ---\n{out}\n--- STDERR ---\n{err}")
                res = StepResult(name, target_display, "FAIL", duration, summary, log_file, out, err)
                self.results.append(res)
                return res

            duration = time.time() - start_time
            out = _redact_sensitive(raw_out or "")
            err = _redact_sensitive(raw_err or "")
            status = "PASS" if proc.returncode == 0 else "FAIL"

            summary = ""
            static_m = re.search(r"\((\d+ errors?, \d+ warnings?)\)", out)
            if static_m:
                summary = static_m.group(1)
            else:
                pytest_m = re.search(r"(=+ .*(?:passed|failed|skipped|error).* =+)", out)
                if pytest_m:
                    summary = pytest_m.group(1).strip("=").strip()
                else:
                    pytest_q = re.search(r"(\d+ (?:passed|failed|error)[^\n]*)", out)
                    if pytest_q:
                        summary = pytest_q.group(1).strip()

            npm_pass = re.search(r"ℹ pass (\d+)", out)
            npm_fail = re.search(r"ℹ fail (\d+)", out)
            if npm_pass and npm_fail:
                summary = f"{npm_pass.group(1)} passed, {npm_fail.group(1)} failed"

            if not summary:
                summary = f"exit code {proc.returncode}"

            log_file = None
            if status == "FAIL" or self.verbose:
                combined = f"--- STDOUT ---\n{out}\n--- STDERR ---\n{err}"
                log_file = self._save_diagnostic(name, combined)

            # Cap in-memory representation to prevent excessive memory usage
            res = StepResult(name, target_display, status, duration, summary, log_file, out[:10000], err[:10000])
            self.results.append(res)
            return res

        except Exception as e:
            duration = time.time() - start_time
            res = StepResult(name, target, "FAIL", duration, f"Execution error: {e}")
            self.results.append(res)
            return res

    def _save_diagnostic(self, name: str, content: str) -> str:
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(DIAGNOSTICS_DIR, 0o700)
        except OSError:
            pass
        safe_name = re.sub(r"[^\w-]", "_", name.lower())
        log_path = DIAGNOSTICS_DIR / f"{self.run_id}_{safe_name}.log"
        redacted = _redact_sensitive(content)
        log_path.write_text(redacted, encoding="utf-8")
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        return str(log_path)

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
                   f"external DSN: {_redact_sensitive(ext_dsn)}" if ext_dsn else f"initdb: {initdb_bin}")

        # DuckDB in /tmp
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

        # Test cluster classification
        clusters = list(Path("/tmp").glob("fipg*"))
        classified = [classify_cluster(c) for c in clusters]
        active = [c for c in classified if c["status"] == "active"]
        orphaned = [c for c in classified if c["status"] == "orphaned"]
        unknown = [c for c in classified if c["status"] == "unknown"]

        if clean_orphans and orphaned:
            cleaned = 0
            for item in orphaned:
                cdir = item["dir"]
                data = item.get("data")
                pg_ctl = item.get("pg_ctl") or shutil.which("pg_ctl") or "/opt/homebrew/bin/pg_ctl"
                if data and Path(data).is_dir() and pg_ctl and Path(pg_ctl).is_file():
                    try:
                        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                                       capture_output=True, timeout=5)
                    except Exception:
                        pass
                pm_pid = item.get("pid")
                if pm_pid and data and _is_exact_postgres_instance(pm_pid, data):
                    try:
                        os.kill(pm_pid, signal.SIGTERM)
                        time.sleep(0.5)
                        if _is_exact_postgres_instance(pm_pid, data):
                            os.kill(pm_pid, signal.SIGKILL)
                    except OSError:
                        pass
                shutil.rmtree(cdir, ignore_errors=True)
                cleaned += 1
            print(f"  ↳ Cleaned up {cleaned} verified orphaned test directory(ies)")
            clusters = list(Path("/tmp").glob("fipg*"))
            classified = [classify_cluster(c) for c in clusters]
            active = [c for c in classified if c["status"] == "active"]
            orphaned = [c for c in classified if c["status"] == "orphaned"]
            unknown = [c for c in classified if c["status"] == "unknown"]

        status_msg = f"{len(active)} active, {len(orphaned)} orphaned, {len(unknown)} unknown"
        check_item("Test Pg Clusters", len(orphaned) == 0, status_msg)

        print("===================================\n")
        return 0 if all_ok else 1

    def run_static_checks(self, files: Sequence[str] | None = None) -> StepResult:
        static_script = REPO_ROOT / "fantasy-intel" / "scripts" / "static_checks.py"
        cmd = [sys.executable, str(static_script)]
        if files:
            py_files = [f for f in files if f.endswith(".py")]
            if py_files:
                cmd.extend(["--files"] + py_files)
        return self.run_command("Static Safety", cmd, cwd=REPO_ROOT, target="AST rules", timeout=30.0)

    def run_backend_tests(self, test_files: list[str], title: str = "Backend Tests", timeout: float = 300.0) -> StepResult | None:
        if not test_files:
            return None
        cmd = ["uv", "run", "pytest", "-q", "--tb=short"] + test_files
        target_summary = f"{len(test_files)} files: {', '.join(Path(t).name for t in test_files[:3])}"
        if len(test_files) > 3:
            target_summary += f" (+{len(test_files) - 3} more)"
        return self.run_command(title, cmd, cwd=BACKEND_DIR, target=target_summary, timeout=timeout)

    def run_frontend_actions(self, actions: list[str]) -> list[StepResult]:
        results = []
        if "lint" in actions:
            results.append(self.run_command("Frontend Lint", ["npm", "run", "lint"], cwd=FRONTEND_DIR, target="ESLint", timeout=120.0))
        if "typecheck" in actions:
            results.append(self.run_command("Frontend Typecheck", ["npm", "run", "typecheck"], cwd=FRONTEND_DIR, target="TypeScript", timeout=120.0))
        if "unit_tests" in actions:
            results.append(self.run_command("Frontend Tests", ["npm", "test"], cwd=FRONTEND_DIR, target="Node Test Runner", timeout=120.0))
        if "build" in actions:
            results.append(self.run_command("Frontend Build", ["npm", "run", "build"], cwd=FRONTEND_DIR, target="Next.js Production Build", timeout=300.0))
        return results

    def check_playwright_installed(self) -> bool:
        pw_bin = FRONTEND_DIR / "node_modules" / ".bin" / "playwright"
        return pw_bin.is_file()

    def execute(self, clean_orphans: bool = False) -> int:
        if self.mode == "doctor":
            return self.run_doctor(clean_orphans=clean_orphans)

        print(f"\n[agent_check] Mode: {self.mode.upper()} | Base: {self.base_ref}")
        changed_files, warnings = self.get_changed_files()
        for w in warnings:
            print(f"[WARN] {w}")

        if self.mode != "full" and self.coverage_error:
            self.results.append(StepResult(
                name="Coverage Status",
                target="Git Diff",
                status="UNVERIFIED",
                duration=0.0,
                summary="Accurate change coverage could not be determined. Base ref invalid or git failure.",
            ))
            return self.print_summary()

        print(f"\n[agent_check] Detected {len(changed_files)} changed file(s)")

        if self.mode != "full" and self.coverage_incomplete:
            self.results.append(StepResult(
                name="Coverage Status",
                target="Git Diff",
                status="UNVERIFIED",
                duration=0.0,
                summary=(
                    "Diff fell back to HEAD~1 (single commit, not full branch). "
                    "Incomplete comparison: full affected-test coverage cannot be guaranteed. "
                    "Run with an explicit --base for accurate coverage."
                ),
            ))

        classes = self.classify_changes(changed_files)

        # Flag CI / workflow modifications
        if classes["workflows"]:
            self.results.append(StepResult(
                name="Workflow Changes",
                target="CI / GitHub Actions",
                status="WARN",
                duration=0.0,
                summary=f"{len(classes['workflows'])} workflow file(s) modified. Local tests cannot verify GitHub Actions triggers; PR CI run is required.",
            ))

        backend_tests, frontend_actions, unmapped_files = self.infer_affected_tests(changed_files)
        if unmapped_files:
            self.results.append(StepResult(
                name="Coverage Warning",
                target="Unmapped Sources",
                status="WARN",
                duration=0.0,
                summary=f"{len(unmapped_files)} unmapped file(s): {', '.join(unmapped_files[:2])}. Conservative core tests executed.",
            ))

        if self.mode == "quick":
            self.run_static_checks(changed_files)
            if backend_tests:
                # Prioritize directly modified test files and top mapped tests, leaving heavy files to `affected`
                selected_quick, deferred = select_quick_tests(backend_tests)
                if selected_quick:
                    title = f"Quick Backend ({len(selected_quick)} of {len(backend_tests)} affected files)"
                    self.run_backend_tests(selected_quick, title=title, timeout=60.0)
                if deferred:
                    self.results.append(StepResult(
                        name="Quick Deferred",
                        target="Heavy Backend Tests",
                        status="SKIP",
                        duration=0.0,
                        summary=f"Too slow for quick, run by 'affected': {', '.join(deferred)}.",
                    ))
            if "typecheck" in frontend_actions:
                self.run_command("Frontend Typecheck", ["npm", "run", "typecheck"], cwd=FRONTEND_DIR, target="TypeScript", timeout=60.0)
            if "unit_tests" in frontend_actions:
                self.run_command("Frontend Tests", ["npm", "test"], cwd=FRONTEND_DIR, target="Unit Tests", timeout=60.0)

            if classes["security_sensitive"]:
                print(f"\n[NOTE] Security-sensitive files modified ({len(classes['security_sensitive'])}).")
                print("       'quick' mode runs only an inner-loop subset. Run 'security' mode before committing.")

        elif self.mode == "affected":
            self.run_static_checks(changed_files)
            if classes["security_sensitive"]:
                # Automatically require security regression suite when security boundaries are touched
                print(f"[agent_check] Security-sensitive changes detected: enforcing multi-league security suite.")
                for s in SECURITY_SUITES:
                    if s not in backend_tests and (BACKEND_DIR / s).is_file():
                        backend_tests.append(s)

            if backend_tests:
                self.run_backend_tests(backend_tests, title="Affected Backend", timeout=300.0)
            self.run_frontend_actions(frontend_actions)

        elif self.mode == "security":
            self.run_static_checks()
            self.run_backend_tests(SECURITY_SUITES, title="Multi-League Security Suite", timeout=300.0)

        elif self.mode == "full":
            self.run_static_checks()
            # Full backend suite (Note: already contains test_golden_legacy.py so we do not duplicate it)
            self.run_command("Full Backend", ["uv", "run", "pytest", "-q"], cwd=BACKEND_DIR, target="All Shards & Suites", timeout=900.0)
            # Frontend lint, typecheck, unit, build
            self.run_frontend_actions(["lint", "typecheck", "unit_tests", "build"])

            # E2E Gates: check local capability vs CI requirement
            if self.check_playwright_installed():
                self.run_command("E2E Route Smoke", ["npm", "run", "e2e"], cwd=FRONTEND_DIR, target="Playwright", timeout=300.0)
                self.run_command("E2E Sleeper Smoke", ["npm", "run", "e2e:sleeper"], cwd=FRONTEND_DIR, target="Playwright", timeout=300.0)
                self.run_command("E2E Multi-League Parity", ["npm", "run", "e2e:multileague"], cwd=FRONTEND_DIR, target="Playwright Matrix", timeout=600.0)
            else:
                pw_msg = "Playwright/node_modules not installed locally. Must verify against CI run for exact commit SHA."
                self.results.append(StepResult("E2E Route Smoke", "Playwright", "UNVERIFIED", 0.0, pw_msg))
                self.results.append(StepResult("E2E Sleeper Smoke", "Playwright", "UNVERIFIED", 0.0, pw_msg))
                self.results.append(StepResult("E2E Multi-League Parity", "Playwright Matrix", "UNVERIFIED", 0.0, pw_msg))

        return self.print_summary(classes)

    def print_summary(self, classes: dict | None = None) -> int:
        print("\n" + "=" * 80)
        print(f"{'VERIFICATION SUMMARY':^80}")
        print("=" * 80)
        print(f"{'Step':<24} {'Target':<24} {'Status':<12} {'Time':<8} {'Details'}")
        print("-" * 80)

        any_failure = False
        any_unverified = False
        total_time = 0.0
        for r in self.results:
            total_time += r.duration
            if r.status == "FAIL":
                any_failure = True
            elif r.status == "UNVERIFIED":
                any_unverified = True
            time_str = f"{r.duration:.1f}s"
            status_str = f"[{r.status}]"
            print(f"{r.name:<24} {r.target:<24} {status_str:<12} {time_str:<8} {r.summary}")
            if r.status == "FAIL" and r.log_file:
                print(f"  ↳ Diagnostic log saved: {r.log_file}")
                lines = [l for l in (r.stderr or r.stdout).splitlines() if "FAILED" in l or "Error:" in l or "error:" in l]
                for l in lines[:3]:
                    print(f"    • {_redact_sensitive(l.strip()[:100])}")

        print("-" * 80)
        all_skipped = len(self.results) > 0 and all(r.status == "SKIP" for r in self.results)
        if any_failure:
            overall = "FAIL"
            exit_code = 1
        elif any_unverified:
            overall = "UNVERIFIED"
            exit_code = 2
        elif self.dry_run or all_skipped:
            overall = "DRY_RUN"
            exit_code = 0
        else:
            overall = "PASS"
            exit_code = 0

        print(f"Overall Result: [{overall}] in {total_time:.1f}s")
        print("=" * 80)

        if any_failure:
            print("\nAction needed: inspect diagnostic logs or run targeted failed test directly.")
        elif any_unverified:
            print("\nAction needed: local environment or coverage could not execute/verify all required gates.")
        elif self.dry_run or all_skipped:
            print("\nNotice: dry-run mode completed. Checks were reported but not executed; no verification claimed.")
        else:
            if self.mode == "quick":
                print("\nSuggested next step: python fantasy-intel/scripts/agent_check.py affected")
            elif self.mode == "affected":
                if classes and classes.get("security_sensitive"):
                    print("\nSuggested next step: run `.claude/agents/multileague-security-reviewer.md` before external Codex review.")
                else:
                    print("\nSuggested next step: ready for review or final gate full run.")
            elif self.mode == "security":
                print("\nSuggested next step: run `.claude/agents/multileague-security-reviewer.md` before external Codex review.")

        return exit_code


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

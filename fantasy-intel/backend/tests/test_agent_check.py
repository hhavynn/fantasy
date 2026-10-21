"""Comprehensive tests for the verification runner (fantasy-intel/scripts/agent_check.py)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_check import (
    BACKEND_DIR,
    BACKEND_TEST_MAPPINGS,
    REPO_ROOT,
    SECURITY_SUITES,
    StepResult,
    VerificationRunner,
    _is_exact_postgres_instance,
    _redact_sensitive,
    classify_cluster,
    validate_test_mappings,
)


def test_classify_changes():
    runner = VerificationRunner(mode="affected")
    files = [
        "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql",
        "fantasy-intel/backend/app/control_plane/jwt_auth.py",
        "fantasy-intel/backend/app/analytics/trades.py",
        "fantasy-intel/frontend/components/league_header.tsx",
        "fantasy-intel/backend/tests/test_trades.py",
        "CLAUDE.md",
        ".github/workflows/ci.yml",
        "some_doc.txt",
    ]
    classes = runner.classify_changes(files)
    assert "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql" in classes["sql_migrations"]
    assert "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql" in classes["security_sensitive"]
    assert "fantasy-intel/backend/app/control_plane/jwt_auth.py" in classes["security_sensitive"]
    assert "fantasy-intel/backend/app/analytics/trades.py" in classes["backend"]
    assert "fantasy-intel/frontend/components/league_header.tsx" in classes["frontend"]
    assert "fantasy-intel/backend/tests/test_trades.py" in classes["tests"]
    assert "CLAUDE.md" in classes["config"]
    assert ".github/workflows/ci.yml" in classes["workflows"]
    assert "some_doc.txt" in classes["other"]


def test_infer_affected_tests_for_sql_migration():
    runner = VerificationRunner(mode="affected")
    tests, actions, unmapped = runner.infer_affected_tests(
        ["fantasy-intel/supabase/migrations/20261020120000_control_plane.sql"]
    )
    assert "tests/test_control_plane_rls.py" in tests
    assert "tests/test_pg_harness.py" in tests
    assert "tests/test_control_plane_functions.py" in tests
    assert "tests/test_sync_queue.py" in tests
    assert len(unmapped) == 0


def test_infer_affected_tests_for_jwt_auth():
    runner = VerificationRunner(mode="affected")
    tests, actions, unmapped = runner.infer_affected_tests(
        ["fantasy-intel/backend/app/control_plane/jwt_auth.py"]
    )
    assert "tests/test_jwt_auth.py" in tests
    assert "tests/test_authorize_league.py" in tests


def test_infer_affected_tests_for_sync_worker():
    runner = VerificationRunner(mode="affected")
    tests, actions, unmapped = runner.infer_affected_tests(
        ["fantasy-intel/backend/app/sync/worker.py"]
    )
    assert "tests/test_sync_worker.py" in tests
    assert "tests/test_sync_publish.py" in tests


def test_infer_affected_tests_for_frontend():
    runner = VerificationRunner(mode="affected")
    # Component edit: lint, typecheck, unit_tests (no build required)
    _, actions, _ = runner.infer_affected_tests(["fantasy-intel/frontend/components/card.tsx"])
    assert "lint" in actions
    assert "typecheck" in actions
    assert "unit_tests" in actions
    assert "build" not in actions

    # App route / server action edit: build required
    _, actions_build, _ = runner.infer_affected_tests(["fantasy-intel/frontend/app/layout.tsx"])
    assert "build" in actions_build


def test_infer_affected_tests_conservative_fallback():
    runner = VerificationRunner(mode="affected")
    # An unknown backend module should fall back to core conservative suites and record unmapped
    tests, _, unmapped = runner.infer_affected_tests(["fantasy-intel/backend/app/unknown_new_module.py"])
    assert "fantasy-intel/backend/app/unknown_new_module.py" in unmapped
    assert "tests/test_api.py" in tests
    assert "tests/test_authorize_league.py" in tests


def test_infer_affected_tests_prioritizes_direct_test_edits():
    runner = VerificationRunner(mode="quick")
    changed = [
        "fantasy-intel/backend/app/analytics/trades.py",
        "fantasy-intel/backend/tests/test_trades.py",
    ]
    tests, _, _ = runner.infer_affected_tests(changed)
    # Directly modified test must be prioritized first
    assert tests[0] == "tests/test_trades.py"


def test_infer_affected_tests_handles_deleted_or_missing_files():
    runner = VerificationRunner(mode="affected")
    # File that no longer exists on disk (e.g. deleted in git diff)
    changed = ["fantasy-intel/backend/app/deleted_old_feature.py"]
    tests, _, unmapped = runner.infer_affected_tests(changed)
    assert "fantasy-intel/backend/app/deleted_old_feature.py" in unmapped
    assert len(tests) > 0  # Conservative fallback still triggers


def test_validate_test_mappings_clean():
    # All mapped backend tests must exist on disk right now
    stale = validate_test_mappings()
    assert stale == [], f"Found stale test mappings in agent_check.py: {stale}"


def test_validate_test_mappings_detects_nonexistent_test(monkeypatch):
    import agent_check

    fake_mappings = list(agent_check.BACKEND_TEST_MAPPINGS) + [
        ("fake/path.py", ["tests/test_nonexistent_fake_path_12345.py"])
    ]
    monkeypatch.setattr(agent_check, "BACKEND_TEST_MAPPINGS", fake_mappings)
    stale = agent_check.validate_test_mappings()
    assert any("test_nonexistent_fake_path_12345.py" in s for s in stale)


def test_security_suites_all_exist():
    backend_dir = Path(__file__).resolve().parents[1]
    for s in SECURITY_SUITES:
        assert (backend_dir / s).is_file(), f"Security suite {s} does not exist on disk"


def test_is_exact_postgres_instance_rejects_unrelated_pids():
    # Invalid PID
    assert not _is_exact_postgres_instance(99999999, "/tmp/fipg_dummy")

    # Current process PID (python, not postgres)
    assert not _is_exact_postgres_instance(os.getpid(), "/tmp/fipg_dummy")


def test_classify_cluster_active_runner(tmp_path):
    cluster_dir = tmp_path / "fipg_active_test"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    # Write owner metadata with CURRENT runner PID (which is alive)
    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps({"runner_pid": os.getpid()}))

    result = classify_cluster(cluster_dir)
    assert result["status"] == "active"
    assert "is alive" in result["reason"] or "active" in result["reason"]


def test_classify_cluster_orphaned_dead_runner(tmp_path):
    cluster_dir = tmp_path / "fipg_orphan_test"
    cluster_dir.mkdir()

    # Write owner metadata with dead PID
    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps({"runner_pid": 99999999}))

    result = classify_cluster(cluster_dir)
    assert result["status"] == "orphaned"


def test_classify_cluster_rejects_symlink(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlink_dir = tmp_path / "fipg_symlink"
    symlink_dir.symlink_to(real_dir)

    result = classify_cluster(symlink_dir)
    assert result["status"] == "unknown"
    assert "symlink detected" in result["reason"]


def test_classify_cluster_rejects_pid_reuse(tmp_path):
    cluster_dir = tmp_path / "fipg_pid_reuse"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    # Write postmaster.pid with current Python process PID (which is alive, but NOT postgres!)
    pid_file = data_dir / "postmaster.pid"
    pid_file.write_text(f"{os.getpid()}\n/tmp/something\n")

    result = classify_cluster(cluster_dir)
    assert result["status"] == "unknown"
    assert "reused" in result["reason"].lower()


def test_clean_orphans_leaves_unknown_and_active_untouched(tmp_path, monkeypatch):
    import agent_check

    # Create dummy clusters in tmp_path
    active_dir = tmp_path / "fipg_active"
    active_dir.mkdir()
    (active_dir / "owner.json").write_text(json.dumps({"runner_pid": os.getpid()}))

    orphan_dir = tmp_path / "fipg_orphan"
    orphan_dir.mkdir()
    (orphan_dir / "owner.json").write_text(json.dumps({"runner_pid": 99999999}))

    symlink_target = tmp_path / "target"
    symlink_target.mkdir()
    unknown_symlink = tmp_path / "fipg_unknown"
    unknown_symlink.symlink_to(symlink_target)

    # Monkeypatch Path("/tmp").glob to return our test clusters
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [active_dir, orphan_dir, unknown_symlink] if pattern == "fipg*" else [])

    runner = VerificationRunner(mode="doctor")
    # Run doctor with clean_orphans
    runner.run_doctor(clean_orphans=True)

    # Assert orphan was cleaned, but active and unknown remain untouched
    assert not orphan_dir.exists(), "Orphaned cluster should have been cleaned"
    assert active_dir.exists(), "Active cluster must remain untouched"
    assert unknown_symlink.exists(), "Unknown / symlinked cluster must remain untouched"


def test_bounded_execution_timeout():
    runner = VerificationRunner(mode="quick")
    # Run a synthetic hanging command with a 0.5s timeout
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    res = runner.run_command("Hang Test", cmd, cwd=REPO_ROOT, target="timeout test", timeout=0.5)

    assert res.status == "FAIL"
    assert "TIMEOUT" in res.summary
    assert res.duration >= 0.4 and res.duration < 3.0


def test_diagnostic_file_permissions_and_redaction():
    runner = VerificationRunner(mode="quick")
    # Run command that fails and outputs sensitive data
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('Connect error: postgresql://postgres:supersecret@127.0.0.1:5432/db\\nBearer secret_jwt_token_123\\n'); sys.exit(1)",
    ]
    res = runner.run_command("Redaction Test", cmd, cwd=REPO_ROOT, target="redaction", timeout=5.0)
    assert res.status == "FAIL"
    assert res.log_file is not None

    log_path = Path(res.log_file)
    assert log_path.exists()

    # Verify log permissions (0o600) and directory (0o700)
    log_mode = stat.S_IMODE(log_path.stat().st_mode)
    dir_mode = stat.S_IMODE(log_path.parent.stat().st_mode)
    assert log_mode == 0o600, f"Expected 0o600, got {oct(log_mode)}"
    assert dir_mode == 0o700, f"Expected 0o700, got {oct(dir_mode)}"

    # Verify secrets are redacted in log file
    log_content = log_path.read_text()
    assert "supersecret" not in log_content
    assert "[REDACTED]" in log_content
    assert "secret_jwt_token_123" not in log_content
    assert "[TOKEN_REDACTED]" in log_content


def test_full_mode_e2e_unverified_when_playwright_missing(monkeypatch):
    runner = VerificationRunner(mode="full")
    # Force check_playwright_installed to False
    monkeypatch.setattr(runner, "check_playwright_installed", lambda: False)
    # Stub run_command to succeed for prior gates
    monkeypatch.setattr(
        runner,
        "run_command",
        lambda name, cmd, cwd=None, target="", timeout=300.0: StepResult(name, target, "PASS", 0.1, "Passed"),
    )
    monkeypatch.setattr(runner, "run_frontend_actions", lambda actions: [])

    code = runner.execute()
    # Overall code must be 2 (UNVERIFIED)
    assert code == 2

    # Verify E2E results are UNVERIFIED
    e2e_results = [r for r in runner.results if "E2E" in r.name]
    assert len(e2e_results) == 3
    for r in e2e_results:
        assert r.status == "UNVERIFIED"
        assert "exact commit SHA" in r.summary


def test_authorization_doc_contract_enforces_adr0002():
    """Verify that documentation enforces private league 404 indistinguishability per ADR-0002 §3,
    and does not claim anonymous requests to private leagues return 401."""
    reviewer_doc = (REPO_ROOT / ".claude/agents/multileague-security-reviewer.md").read_text()
    arch_doc = (REPO_ROOT / ".claude/skills/fantasy-verify/references/architecture-map.md").read_text()
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()

    for doc, name in [(reviewer_doc, "reviewer"), (arch_doc, "arch"), (claude_md, "CLAUDE.md")]:
        # Must mention 404 and indistinguishability
        assert "404" in doc, f"{name} missing 404 contract"
        assert "indistinguish" in doc.lower(), f"{name} must enforce 404 indistinguishability"
        # Must not claim that anonymous private league requests return 401
        assert "anonymous private-league requests must return 401" not in doc.lower()
        assert "anonymous caller gets 401" not in doc.lower()

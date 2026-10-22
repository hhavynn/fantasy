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


def _make_valid_owner_json(cluster_dir: Path, runner_pid: int = 12345) -> dict:
    data_dir = cluster_dir / "data"
    return {
        "runner_pid": runner_pid,
        "created_at": time.time(),
        "cluster_dir": str(cluster_dir.resolve()),
        "data_dir": str(data_dir.resolve()),
        "pg_ctl": "/opt/homebrew/bin/pg_ctl",
        "harness": "fantasy-intel-pg-harness",
    }


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



def test_infer_affected_tests_adds_app_wide_audits_for_a_new_app_module():
    """A brand-new analytics module has no mapping, but the app-wide source audits read every app/ file, so `affected`
    must run them; frontend-only and test-only changes do not pay for them."""
    from agent_check import APP_WIDE_AUDITS

    runner = VerificationRunner(mode="affected")
    tests, _, unmapped = runner.infer_affected_tests(["fantasy-intel/backend/app/analytics/brand_new_feature.py"])
    assert all(t in tests for t in APP_WIDE_AUDITS)
    assert "fantasy-intel/backend/app/analytics/brand_new_feature.py" in unmapped   # conservative fallback kept
    tests, _, _ = runner.infer_affected_tests(["fantasy-intel/backend/app/db/schema.sql"])   # the audits read .sql too
    assert all(t in tests for t in APP_WIDE_AUDITS)
    tests, _, _ = runner.infer_affected_tests(["fantasy-intel/frontend/app/page.tsx"])
    assert not any(t in tests for t in APP_WIDE_AUDITS)


def test_validate_test_mappings_reports_a_missing_app_wide_audit(monkeypatch):
    import agent_check
    monkeypatch.setattr(agent_check, "APP_WIDE_AUDITS", ["tests/test_no_such_audit.py"])
    assert any("App-wide audit" in e for e in agent_check.validate_test_mappings())

def test_infer_affected_tests_for_sync_worker():
    runner = VerificationRunner(mode="affected")
    tests, actions, unmapped = runner.infer_affected_tests(
        ["fantasy-intel/backend/app/sync/worker.py"]
    )
    assert "tests/test_sync_worker.py" in tests
    assert "tests/test_sync_publish.py" in tests


def test_infer_affected_tests_for_frontend():
    runner = VerificationRunner(mode="affected")
    _, actions, _ = runner.infer_affected_tests(["fantasy-intel/frontend/components/card.tsx"])
    assert "lint" in actions
    assert "typecheck" in actions
    assert "unit_tests" in actions
    assert "build" not in actions

    _, actions_build, _ = runner.infer_affected_tests(["fantasy-intel/frontend/app/layout.tsx"])
    assert "build" in actions_build


def test_infer_affected_tests_conservative_fallback():
    runner = VerificationRunner(mode="affected")
    tests, _, unmapped = runner.infer_affected_tests(["fantasy-intel/backend/app/unknown_new_module.py"])
    assert "fantasy-intel/backend/app/unknown_new_module.py" in unmapped
    assert "tests/test_api.py" in tests
    assert "tests/test_authorize_league.py" in tests
    assert "tests/test_league_scope_audit.py" in tests
    assert "tests/test_v2_authorization_matrix.py" in tests


def test_infer_affected_tests_prioritizes_direct_test_edits():
    runner = VerificationRunner(mode="quick")
    changed = [
        "fantasy-intel/backend/app/analytics/trades.py",
        "fantasy-intel/backend/tests/test_trades.py",
    ]
    tests, _, _ = runner.infer_affected_tests(changed)
    assert tests[0] == "tests/test_trades.py"


def test_infer_affected_tests_handles_deleted_or_missing_files():
    runner = VerificationRunner(mode="affected")
    changed = ["fantasy-intel/backend/app/deleted_old_feature.py"]
    tests, _, unmapped = runner.infer_affected_tests(changed)
    assert "fantasy-intel/backend/app/deleted_old_feature.py" in unmapped
    assert len(tests) > 0


def test_deleted_source_still_maps_to_tests():
    """A deleted backend source file must still trigger its mapped tests
    even though it is not present on disk (it was removed, not renamed)."""
    runner = VerificationRunner(mode="affected")
    # Inject a deleted source path as if _parse_name_status collected it
    runner._deleted_sources = {"fantasy-intel/backend/app/control_plane/jwt_auth.py"}
    # No living changed files
    tests, _, _ = runner.infer_affected_tests([])
    # jwt_auth is mapped to test_jwt_auth.py and test_authorize_league.py
    assert "tests/test_jwt_auth.py" in tests
    assert "tests/test_authorize_league.py" in tests


def test_deleted_test_file_never_added_to_runnable_list():
    """A deleted test file must never appear in the runnable test list, even if
    it was in the deleted_sources set (e.g. parsed from a git D-status line)."""
    runner = VerificationRunner(mode="affected")
    runner._deleted_sources = {"fantasy-intel/backend/tests/test_jwt_auth.py"}
    tests, _, _ = runner.infer_affected_tests([])
    # The deleted test itself must not be in the runnable list
    assert "tests/test_jwt_auth.py" not in tests


def test_head_tilde1_fallback_marks_coverage_incomplete(monkeypatch):
    """When the primary diff against the base ref fails and HEAD~1 succeeds,
    coverage_incomplete must be set and emit UNVERIFIED in affected mode to prevent
    falsely reporting full affected-test coverage."""
    runner = VerificationRunner(mode="affected")
    runner.base_ref = "origin/main"
    runner.explicit_base = False

    call_count = [0]
    original_run = __import__("subprocess").run

    def fake_run(cmd, **kwargs):
        call_count[0] += 1
        # First call (base diff): fail
        if "--name-status" in cmd and "origin/main...HEAD" in " ".join(cmd):
            class R:
                returncode = 1
                stdout = ""
                stderr = "not a git repository"
            return R()
        # Second call (HEAD~1 fallback): succeed with one changed file
        if "--name-status" in cmd and "HEAD~1...HEAD" in " ".join(cmd):
            class R:
                returncode = 0
                stdout = "M\tfantasy-intel/backend/app/analytics/trades.py\n"
                stderr = ""
            return R()
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("agent_check.subprocess.run", fake_run)
    changed_files, warnings = runner.get_changed_files()
    assert runner.coverage_incomplete is True
    assert any("partial" in w.lower() or "head~1" in w.lower() for w in warnings)

    # In affected mode, execute() must emit UNVERIFIED and return exit code 2
    monkeypatch.setattr(
        runner,
        "run_command",
        lambda name, cmd, cwd=None, target="", timeout=300.0: StepResult(name, target, "PASS", 0.1, "Passed"),
    )
    monkeypatch.setattr(runner, "run_backend_tests", lambda tests, title="", timeout=300.0: None)
    monkeypatch.setattr(runner, "run_frontend_actions", lambda actions: [])
    code = runner.execute()
    assert code == 2  # UNVERIFIED, never PASS
    cov_results = [r for r in runner.results if "Coverage Status" in r.name]
    assert len(cov_results) == 1
    assert cov_results[0].status == "UNVERIFIED"
    assert "Incomplete comparison" in cov_results[0].summary


def test_validate_test_mappings_clean():
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
    assert not _is_exact_postgres_instance(99999999, "/tmp/fipg_dummy")
    assert not _is_exact_postgres_instance(os.getpid(), "/tmp/fipg_dummy")


def test_classify_cluster_active_runner(tmp_path):
    cluster_dir = tmp_path / "fipg_active_test"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps(_make_valid_owner_json(cluster_dir, runner_pid=os.getpid())))

    result = classify_cluster(cluster_dir)
    assert result["status"] == "active"
    assert "is alive" in result["reason"] or "active" in result["reason"]


def test_classify_cluster_orphaned_dead_runner(tmp_path):
    cluster_dir = tmp_path / "fipg_orphan_test"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps(_make_valid_owner_json(cluster_dir, runner_pid=99999999)))

    result = classify_cluster(cluster_dir)
    assert result["status"] == "orphaned"


def test_classify_cluster_rejects_missing_owner_json(tmp_path):
    # An unrelated same-user /tmp/fipg* directory with no owner.json must be classified as unknown
    cluster_dir = tmp_path / "fipg_unrelated_same_user"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    result = classify_cluster(cluster_dir)
    assert result["status"] == "unknown"
    assert "missing owner.json" in result["reason"]


def test_classify_cluster_rejects_malformed_or_untrusted_owner_json(tmp_path):
    cluster_dir = tmp_path / "fipg_malformed"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    # Wrong harness
    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps({"harness": "other-tool", "runner_pid": 123}))
    assert classify_cluster(cluster_dir)["status"] == "unknown"

    # Directory mismatch
    owner_file.write_text(json.dumps({
        "harness": "fantasy-intel-pg-harness",
        "runner_pid": 99999999,
        "data_dir": "/tmp/some_other_data",
        "cluster_dir": str(cluster_dir.resolve()),
    }))
    assert classify_cluster(cluster_dir)["status"] == "unknown"


def test_classify_cluster_rejects_symlink(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlink_dir = tmp_path / "fipg_symlink"
    symlink_dir.symlink_to(real_dir)

    result = classify_cluster(symlink_dir)
    assert result["status"] == "unknown"
    assert "symlink detected" in result["reason"]


def test_classify_cluster_rejects_nested_symlink(tmp_path):
    cluster_dir = tmp_path / "fipg_nested_symlink"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()
    (cluster_dir / "owner.json").write_text(json.dumps(_make_valid_owner_json(cluster_dir, runner_pid=os.getpid())))

    # Inject a nested symlink inside data
    fake_target = tmp_path / "secret"
    fake_target.mkdir()
    (data_dir / "evil_symlink").symlink_to(fake_target)

    result = classify_cluster(cluster_dir)
    assert result["status"] == "unknown"
    assert "nested" in result["reason"].lower()


def test_classify_cluster_rejects_pid_reuse(tmp_path):
    cluster_dir = tmp_path / "fipg_pid_reuse"
    cluster_dir.mkdir()
    data_dir = cluster_dir / "data"
    data_dir.mkdir()

    # Valid owner.json with dead runner
    owner_file = cluster_dir / "owner.json"
    owner_file.write_text(json.dumps(_make_valid_owner_json(cluster_dir, runner_pid=99999999)))

    # Postmaster.pid with current Python process PID (which is alive, but NOT postgres!)
    pid_file = data_dir / "postmaster.pid"
    pid_file.write_text(f"{os.getpid()}\n/tmp/something\n")

    result = classify_cluster(cluster_dir)
    assert result["status"] == "unknown"
    assert "reused" in result["reason"].lower()


def test_clean_orphans_leaves_unknown_and_active_untouched(tmp_path, monkeypatch):
    active_dir = tmp_path / "fipg_active"
    active_dir.mkdir()
    (active_dir / "data").mkdir()
    (active_dir / "owner.json").write_text(json.dumps(_make_valid_owner_json(active_dir, runner_pid=os.getpid())))

    orphan_dir = tmp_path / "fipg_orphan"
    orphan_dir.mkdir()
    (orphan_dir / "data").mkdir()
    (orphan_dir / "owner.json").write_text(json.dumps(_make_valid_owner_json(orphan_dir, runner_pid=99999999)))

    unrelated_dir = tmp_path / "fipg_unrelated_no_owner"
    unrelated_dir.mkdir()

    symlink_target = tmp_path / "target"
    symlink_target.mkdir()
    unknown_symlink = tmp_path / "fipg_unknown"
    unknown_symlink.symlink_to(symlink_target)

    monkeypatch.setattr(Path, "glob", lambda self, pattern: [active_dir, orphan_dir, unrelated_dir, unknown_symlink] if pattern == "fipg*" else [])

    runner = VerificationRunner(mode="doctor")
    runner.run_doctor(clean_orphans=True)

    assert not orphan_dir.exists(), "Verified orphaned cluster should have been cleaned"
    assert active_dir.exists(), "Active cluster must remain untouched"
    assert unrelated_dir.exists(), "Unrelated same-user directory without owner.json must remain untouched"
    assert unknown_symlink.exists(), "Symlinked cluster must remain untouched"


def test_bounded_execution_timeout():
    runner = VerificationRunner(mode="quick")
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    res = runner.run_command("Hang Test", cmd, cwd=REPO_ROOT, target="timeout test", timeout=0.5)

    assert res.status == "FAIL"
    assert "TIMEOUT" in res.summary
    assert res.duration >= 0.4 and res.duration < 3.0


def test_synthetic_secrets_comprehensive_redaction():
    """Synthetic secrets (ESPN_S2, swid, postgres credentials, tokens, private keys)
    must be completely redacted and never appear in stdout, stderr, summaries, or logs."""
    fake_swid = "{" + "12345678-ABCD-1234-ABCD-1234567890AB" + "}"
    fake_bearer = "Bearer " + "my_secret_jwt_token_here"
    fake_privkey = "-----" + "BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0\n-----" + "END RSA PRIVATE KEY-----"
    test_str = (
        "Error in connection postgresql://postgres:SecretPass123@127.0.0.1:5432/testdb\n"
        f"Cookie: espn_s2=AEA1234567890abcdef; ESPN_S2=UPPERCASE_COOKIE_123;\n"
        f"User SWID={fake_swid} and swid: 99999999-1111-2222-3333-444444444444\n"
        f"Authorization: {fake_bearer}\n"
        "password=ConninfoSecretPassword456\n"
        f"{fake_privkey}"
    )
    redacted = _redact_sensitive(test_str)
    assert "SecretPass123" not in redacted
    assert "AEA1234567890abcdef" not in redacted
    assert "UPPERCASE_COOKIE_123" not in redacted
    assert "12345678-ABCD-1234-ABCD-1234567890AB" not in redacted
    assert "99999999-1111-2222-3333-444444444444" not in redacted
    assert "my_secret_jwt_token_here" not in redacted
    assert "ConninfoSecretPassword456" not in redacted
    assert "MIIEowIBAAKCAQEA0" not in redacted
    assert "[REDACTED]" in redacted
    assert "[TOKEN_REDACTED]" in redacted
    assert "[PRIVATE_KEY_REDACTED]" in redacted


def test_diagnostic_file_permissions_and_redaction():
    runner = VerificationRunner(mode="quick")
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('Connect error: postgresql://postgres:supersecret@127.0.0.1:5432/db\\n' + 'Bearer ' + 'secret_jwt_token_123\\n'); sys.exit(1)",
    ]
    res = runner.run_command("Redaction Test", cmd, cwd=REPO_ROOT, target="redaction", timeout=5.0)
    assert res.status == "FAIL"
    assert res.log_file is not None

    log_path = Path(res.log_file)
    assert log_path.exists()

    log_mode = stat.S_IMODE(log_path.stat().st_mode)
    dir_mode = stat.S_IMODE(log_path.parent.stat().st_mode)
    assert log_mode == 0o600, f"Expected 0o600, got {oct(log_mode)}"
    assert dir_mode == 0o700, f"Expected 0o700, got {oct(dir_mode)}"

    log_content = log_path.read_text()
    assert "supersecret" not in log_content
    assert "[REDACTED]" in log_content
    assert "secret_jwt_token_123" not in log_content
    assert "[TOKEN_REDACTED]" in log_content


def test_invalid_explicit_base_fails_and_does_not_silently_fallback():
    runner = VerificationRunner(mode="affected", base_ref="invalid-nonexistent-ref-xyz-123")
    code = runner.execute()
    # Must return UNVERIFIED (2) or FAIL (1), never PASS (0)
    assert code != 0
    assert any("Base Ref Validation" in r.name for r in runner.results)
    base_res = next(r for r in runner.results if "Base Ref Validation" in r.name)
    assert base_res.status == "FAIL"


def test_dry_run_never_reports_pass(monkeypatch):
    runner = VerificationRunner(mode="quick", dry_run=True)
    monkeypatch.setattr(runner, "get_changed_files", lambda: (["fantasy-intel/backend/tests/test_trades.py"], []))
    code = runner.execute()
    # Dry run reports DRY_RUN, not PASS
    assert not any(r.status == "PASS" for r in runner.results)
    # Runner results are SKIP
    for r in runner.results:
        assert r.status == "SKIP"


def test_full_mode_e2e_unverified_when_playwright_missing(monkeypatch):
    runner = VerificationRunner(mode="full")
    monkeypatch.setattr(runner, "get_changed_files", lambda: ([], []))
    monkeypatch.setattr(runner, "check_playwright_installed", lambda: False)
    monkeypatch.setattr(
        runner,
        "run_command",
        lambda name, cmd, cwd=None, target="", timeout=300.0: StepResult(name, target, "PASS", 0.1, "Passed"),
    )
    monkeypatch.setattr(runner, "run_frontend_actions", lambda actions: [])

    code = runner.execute()
    assert code == 2

    e2e_results = [r for r in runner.results if "E2E" in r.name]
    assert len(e2e_results) == 3
    for r in e2e_results:
        assert r.status == "UNVERIFIED"
        assert "exact commit SHA" in r.summary


def test_authorization_doc_contract_enforces_adr0002():
    reviewer_doc = (REPO_ROOT / "docs/agent-security-review.md").read_text()
    arch_doc = (REPO_ROOT / ".claude/skills/fantasy-verify/references/architecture-map.md").read_text()
    workflow_doc = (REPO_ROOT / "docs/agent-workflow.md").read_text()

    for doc, name in [(reviewer_doc, "reviewer"), (arch_doc, "arch"), (workflow_doc, "shared workflow")]:
        assert "404" in doc, f"{name} missing 404 contract"
        assert "indistinguish" in doc.lower(), f"{name} must enforce 404 indistinguishability"
        assert "anonymous private-league requests must return 401" not in doc.lower()
        assert "anonymous caller gets 401" not in doc.lower()

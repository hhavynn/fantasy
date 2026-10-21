"""Unit tests for the verification runner (fantasy-intel/scripts/agent_check.py)."""
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_check import VerificationRunner, BACKEND_TEST_MAPPINGS, SECURITY_SUITES


def test_classify_changes():
    runner = VerificationRunner(mode="affected")
    files = [
        "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql",
        "fantasy-intel/backend/app/control_plane/jwt_auth.py",
        "fantasy-intel/backend/app/analytics/trades.py",
        "fantasy-intel/frontend/components/league_header.tsx",
        "fantasy-intel/backend/tests/test_trades.py",
        "README.md",
    ]
    classes = runner.classify_changes(files)
    assert "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql" in classes["sql_migrations"]
    assert "fantasy-intel/supabase/migrations/20261020120000_control_plane.sql" in classes["security_sensitive"]
    assert "fantasy-intel/backend/app/control_plane/jwt_auth.py" in classes["security_sensitive"]
    assert "fantasy-intel/backend/app/analytics/trades.py" in classes["backend"]
    assert "fantasy-intel/frontend/components/league_header.tsx" in classes["frontend"]
    assert "fantasy-intel/backend/tests/test_trades.py" in classes["tests"]
    assert "README.md" in classes["other"]


def test_infer_affected_tests_for_sql_migration():
    runner = VerificationRunner(mode="affected")
    tests, _ = runner.infer_affected_tests(["fantasy-intel/supabase/migrations/20261020120000_control_plane.sql"])
    assert "tests/test_control_plane_rls.py" in tests
    assert "tests/test_pg_harness.py" in tests
    assert "tests/test_control_plane_functions.py" in tests
    assert "tests/test_sync_queue.py" in tests


def test_infer_affected_tests_for_jwt_auth():
    runner = VerificationRunner(mode="affected")
    tests, _ = runner.infer_affected_tests(["fantasy-intel/backend/app/control_plane/jwt_auth.py"])
    assert "tests/test_jwt_auth.py" in tests
    assert "tests/test_authorize_league.py" in tests


def test_infer_affected_tests_for_sync_worker():
    runner = VerificationRunner(mode="affected")
    tests, _ = runner.infer_affected_tests(["fantasy-intel/backend/app/sync/worker.py"])
    assert "tests/test_sync_worker.py" in tests
    assert "tests/test_sync_publish.py" in tests


def test_infer_affected_tests_for_frontend():
    runner = VerificationRunner(mode="affected")
    # Component edit: lint, typecheck, unit_tests (no build required)
    _, actions = runner.infer_affected_tests(["fantasy-intel/frontend/components/card.tsx"])
    assert "lint" in actions
    assert "typecheck" in actions
    assert "unit_tests" in actions
    assert "build" not in actions

    # App route / server action edit: build required
    _, actions_build = runner.infer_affected_tests(["fantasy-intel/frontend/app/layout.tsx"])
    assert "build" in actions_build


def test_infer_affected_tests_conservative_fallback():
    runner = VerificationRunner(mode="affected")
    # An unknown backend module should trigger core fallback tests
    tests, _ = runner.infer_affected_tests(["fantasy-intel/backend/app/unknown_new_module.py"])
    assert "tests/test_api.py" in tests or "tests/test_authorize_league.py" in tests


def test_security_suites_all_exist():
    backend_dir = Path(__file__).resolve().parents[1]
    for s in SECURITY_SUITES:
        assert (backend_dir / s).is_file(), f"Security suite {s} does not exist on disk"


def test_doctor_passes_on_healthy_env():
    runner = VerificationRunner(mode="doctor")
    code = runner.run_doctor()
    assert code == 0

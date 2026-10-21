"""Unit tests for AST-based static safety checks (fantasy-intel/scripts/static_checks.py)."""
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from static_checks import (
    check_source,
    check_all,
    AsyncRouteSyncDbVisitor,
    SecretLoggingVisitor,
    PrivilegedConfigVisitor,
)


def test_async_route_flags_blocking_db_call():
    bad_code = """
async def get_league_data(league_id: str):
    con = open_db("fantasy.duckdb")
    return con.execute("SELECT 1").fetchall()
"""
    findings = check_source(bad_code, "fantasy-intel/backend/app/api/test_route.py")
    assert any(f.rule_id == "ASYNC001" for f in findings)


def test_async_route_flags_sync_dependency():
    bad_code = """
async def get_league_data(ltx=Depends(league_tx)):
    return {"ok": True}
"""
    findings = check_source(bad_code, "fantasy-intel/backend/app/api/test_route.py")
    assert any(f.rule_id == "ASYNC002" for f in findings)


def test_async_route_flags_con_execute():
    bad_code = """
async def get_league_data(con):
    return con.execute("SELECT 1").fetchall()
"""
    findings = check_source(bad_code, "fantasy-intel/backend/app/api/test_route.py")
    assert any(f.rule_id == "ASYNC001" for f in findings)


def test_sync_def_route_permits_blocking_db_call():
    good_code = """
def get_league_data(league_id: str, ltx=Depends(league_tx)):
    con = open_db("fantasy.duckdb")
    return con.execute("SELECT 1").fetchall()
"""
    findings = check_source(good_code, "fantasy-intel/backend/app/api/test_route.py")
    assert not any(f.rule_id in ("ASYNC001", "ASYNC002") for f in findings)


def test_secret_logging_detected():
    bad_log = """
def process_sync(espn_s2, swid):
    logger.info("Starting sync with swid=%s and s2=%s", swid, espn_s2)
"""
    findings = check_source(bad_log, "fantasy-intel/backend/app/sync/worker.py")
    assert any(f.rule_id == "SEC002" for f in findings)


def test_redacted_secret_logging_allowed():
    good_log = """
def process_sync(swid):
    logger.info("Starting sync for user %s", hash_swid(swid))
"""
    findings = check_source(good_log, "fantasy-intel/backend/app/sync/worker.py")
    assert not any(f.rule_id == "SEC002" for f in findings)


def test_privileged_config_detected_in_api():
    bad_cfg = """
import os
SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
"""
    findings = check_source(bad_cfg, "fantasy-intel/backend/app/api/deps.py")
    assert any(f.rule_id == "CFG001" for f in findings)


def test_v2_league_route_auth_missing_detected():
    bad_route = """
@router.get("/v2/leagues/{league_id}/analytics")
def get_analytics(league_id: str):
    return {"unauthorized": True}
"""
    findings = check_source(bad_route, "fantasy-intel/backend/app/api/analytics_v2.py")
    assert any(f.rule_id == "AUTH001" for f in findings)


def test_v2_league_route_auth_present_passes():
    good_route = """
@router.get("/v2/leagues/{league_id}/analytics")
def get_analytics(ltx=Depends(league_tx)):
    return {"authorized": True}
"""
    findings = check_source(good_route, "fantasy-intel/backend/app/api/analytics_v2.py")
    assert not any(f.rule_id == "AUTH001" for f in findings)


def test_current_codebase_clean():
    findings = check_all()
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], f"Static check errors found: {errors}"

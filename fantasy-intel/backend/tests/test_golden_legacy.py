"""Every legacy GET endpoint answers exactly what tests/golden/e2e/ recorded for the E2E fixture league (volatile
stamps masked). A failure means an endpoint's output changed: if intended, re-record with
`uv run python ../scripts/record_golden.py` and review the diff; otherwise it is a regression."""
import difflib

import pytest
from fastapi.testclient import TestClient

from golden.golden import E2E_DIR, E2E_SEASON, build_e2e_db, cases, read_golden, response_text


@pytest.fixture(scope="module")
def e2e_db(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("golden") / "e2e.duckdb")


@pytest.fixture
def legacy_client(e2e_db, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(e2e_db))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _diff(expected: str, actual: str) -> str:
    return "\n".join(list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), "golden", "actual",
                                               lineterm="", n=2))[:60])


def test_every_legacy_get_route_has_a_golden(legacy_client, e2e_db):
    import duckdb
    from app.main import app
    con = duckdb.connect(str(e2e_db), read_only=True)
    try:
        names = {c.name for c in cases(app, con, legacy_client)}
    finally:
        con.close()
    recorded = {p.stem for p in E2E_DIR.glob("*.json")}
    assert names == recorded, f"missing: {sorted(names - recorded)}; stale: {sorted(recorded - names)}"


def test_legacy_responses_match_the_goldens(legacy_client):
    from golden.golden import Case
    failures = []
    for golden in sorted(E2E_DIR.glob("*.json")):
        path, expected = read_golden(E2E_DIR, golden.stem)
        actual = response_text(legacy_client, Case(golden.stem, path))
        if actual != expected:
            failures.append(f"{golden.stem} ({path}):\n{_diff(expected, actual)}")
    assert not failures, "\n\n".join(failures)

"""Hosted mode (Vercel + MotherDuck): API key gate, data version, publish guards, and the API
running without the sync-only packages that are left out of the hosted bundle."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analytics.pipeline import run_analytics
from app.db.connection import connect, motherduck_dsn, open_db
from app.db.publish import check_database_name, publish
from app.espn.sync import run_espn_sync

from espn_fixture import MY_SWID, SEASON, FakeEspnClient

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def api_db(tmp_path):
    db_path = tmp_path / "api.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    run_analytics(con, SEASON)
    con.close()
    return db_path


def test_motherduck_dsn_scopes_to_one_database():
    assert motherduck_dsn("fantasy_intel") == "md:fantasy_intel?attach_mode=single&saas_mode=true"


@pytest.mark.parametrize("name", ["fantasy_intel", "_x1"])
def test_database_name_accepts_identifiers(name):
    assert check_database_name(name) == name


@pytest.mark.parametrize("name", ["", "1db", "fantasy-intel", "x; DROP DATABASE y", "md:x"])
def test_database_name_rejects_anything_else(name):
    with pytest.raises(ValueError):
        check_database_name(name)


def test_publish_refuses_missing_or_unsynced_local_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish(tmp_path / "nope.duckdb", "fantasy_intel")
    empty = tmp_path / "empty.duckdb"
    connect(empty).close()
    with pytest.raises(ValueError, match="no fantasy-intel schema"):
        publish(empty, "fantasy_intel")


def test_api_key_gate(api_db, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(api_db))
    monkeypatch.setenv("FANTASY_API_KEY", "s3cret")
    from app.main import app
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/overview", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_no_api_key_means_open_locally(api_db, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(api_db))
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    from app.main import app
    assert TestClient(app).get("/api/overview").status_code == 200


def test_local_data_version_follows_file_writes(api_db, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(api_db))
    from app.api.deps import data_version, db
    with db() as con:
        before = data_version(con)
    con = connect(api_db)
    con.execute("INSERT INTO schema_meta VALUES ('touch', '1')")
    con.close()
    with db() as con:
        assert data_version(con) != before


# Every GET route, with path parameters filled in, in a fresh interpreter where the sync-only
# packages cannot be imported. A route that reaches polars/nflreadpy/espn_api fails with a 500.
_PROBE = r"""
import json, sys
for name in ("polars", "nflreadpy", "espn_api"):
    sys.modules[name] = None
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app, raise_server_exceptions=False)
statuses = {}
player = client.get("/api/overview").json()["roster"][0]["espn_player_id"]
for route_path, ops in app.openapi()["paths"].items():
    if "get" in ops:
        path = route_path.replace("{espn_player_id}", str(player)).replace("{team_id}", "2")
        statuses[route_path] = client.get(path).status_code
print(json.dumps(statuses))
"""


def test_api_runs_without_sync_only_packages(api_db):
    env = {"FANTASY_DB_PATH": str(api_db), "PATH": "/usr/bin:/bin", "TZ": "UTC"}
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    statuses = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(statuses) > 20
    assert statuses["/api/overview"] == 200
    assert not [p for p, s in statuses.items() if s == 500], statuses


def test_hosted_mode_reads_motherduck_through_one_shared_connection(api_db, monkeypatch):
    """MotherDuck stood in for by the same file: every request is a cursor on one connection, and
    the data version is the publish stamp rather than a file mtime."""
    import duckdb
    from app.api import deps

    con = connect(api_db)
    con.execute("INSERT INTO schema_meta VALUES ('published_at', '2026-10-15T08:00:00')")
    con.close()
    opened = []

    def fake_connect(database):
        opened.append(database)
        return duckdb.connect(str(api_db), read_only=True)

    monkeypatch.setattr(deps, "connect_motherduck", fake_connect)
    monkeypatch.setattr(deps, "_MD_CONS", {})
    monkeypatch.setenv("FANTASY_DB_PATH", "/nonexistent/fantasy.duckdb")
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "fantasy_intel")
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/managers").status_code == 200
    assert opened == ["fantasy_intel"]
    with deps.db() as cur:
        assert deps.data_version(cur) == "md:fantasy_intel:2026-10-15T08:00:00"
    deps._MD_CONS["fantasy_intel"].close()


def test_hosted_mode_without_token_fails_fast(monkeypatch):
    from app.api import deps
    monkeypatch.setattr(deps, "_MD_CONS", {})
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "fantasy_intel")
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.delenv("motherduck_token", raising=False)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    from app.main import app
    r = TestClient(app).get("/api/overview")
    assert r.status_code == 503 and "MOTHERDUCK_TOKEN is not set" in r.json()["detail"]

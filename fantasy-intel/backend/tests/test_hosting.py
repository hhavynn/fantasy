"""Hosted mode (Vercel + MotherDuck): API key gate, data version, publish guards, and the API
running without the sync-only packages that are left out of the hosted bundle."""

import json
import os
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


def test_hosted_mode_without_token_fails_fast(monkeypatch, caplog):
    from app.api import deps
    monkeypatch.setattr(deps, "_MD_CONS", {})
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "fantasy_intel")
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.delenv("motherduck_token", raising=False)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    from app.main import app
    with caplog.at_level("WARNING", logger="app.api.deps"):
        r = TestClient(app).get("/api/overview")
    # Visitors get a fixed 503 that names no setting or database; the operator reason goes to the server log only.
    assert r.status_code == 503 and r.json()["detail"] == "Cannot reach the hosted database."
    assert "MOTHERDUCK" not in r.text and "fantasy_intel" not in r.text
    assert "MOTHERDUCK_TOKEN is not set" in caplog.text


def test_a_hosted_stale_schema_names_no_command_and_logs_the_fix(api_db, monkeypatch, caplog):
    """STATUS 1s: a hosted visitor can see a 503 detail printed inline by a page, so the stale-schema answer names no
    sync command there (the operator's fix goes to the log); it keeps "Database schema", which the frontend
    classifies by. A local run still tells the developer the command."""
    import duckdb
    from app.api import deps
    from app.db.connection import SCHEMA_VERSION

    con = connect(api_db)
    con.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
    con.close()
    monkeypatch.setattr(deps, "connect_motherduck", lambda database: duckdb.connect(str(api_db), read_only=True))
    monkeypatch.setattr(deps, "_MD_CONS", {})
    monkeypatch.setenv("FANTASY_DB_PATH", "/nonexistent/fantasy.duckdb")
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "fantasy_intel")
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    from app.main import app
    with caplog.at_level("ERROR", logger="app.api.deps"):
        r = TestClient(app).get("/api/overview")
    detail = r.json()["detail"]
    assert r.status_code == 503 and detail.startswith("Database schema v1 is older than v")
    assert "uv run" not in detail and "sync_" not in detail and "scripts" not in detail
    assert "sync_nfl.py" in caplog.text
    deps._MD_CONS["fantasy_intel"].close()

    monkeypatch.delenv("MOTHERDUCK_DATABASE")
    monkeypatch.setenv("FANTASY_DB_PATH", str(api_db))
    local = TestClient(app).get("/api/overview").json()["detail"]
    assert f"older than v{SCHEMA_VERSION}" in local and "sync_nfl.py" in local


def test_motherduck_connect_supplies_home_when_empty(monkeypatch, tmp_path):
    """Vercel functions run with HOME empty; the motherduck extension refuses to load without one."""
    import tempfile

    import app.db.connection as connection
    seen = {}
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "t")
    monkeypatch.setenv("HOME", "")
    monkeypatch.setattr(connection.duckdb, "connect", lambda dsn, **kw: seen.update(dsn=dsn, home=os.environ["HOME"]))
    connection.connect_motherduck("fantasy_intel")
    assert seen == {"dsn": "md:fantasy_intel?attach_mode=single&saas_mode=true", "home": tempfile.gettempdir()}
    monkeypatch.setenv("HOME", str(tmp_path))
    connection.connect_motherduck("fantasy_intel")
    assert seen["home"] == str(tmp_path)


def test_replace_tables_updates_in_place(api_db, tmp_path):
    """Publishing rewrites the target's tables without dropping the target database, so a reader
    attached before the publish sees the new data on its next query."""
    import duckdb
    from app.db.publish import replace_tables

    target = tmp_path / "hosted.duckdb"
    reader_path = str(target)
    con = duckdb.connect()
    con.execute(f"ATTACH '{api_db}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{reader_path}' AS hosted")
    con.execute("CREATE TABLE hosted.main.gone (x INT)")          # left over from an older publish
    con.execute("CREATE TABLE hosted.main.schema_meta (key VARCHAR, value VARCHAR)")
    n = replace_tables(con, "src", "hosted", "2026-10-15T09:00:00")
    src_tables = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name='src'").fetchall()}
    hosted_tables = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name='hosted'").fetchall()}
    assert n == len(src_tables) and hosted_tables == src_tables
    for t in ("leagues", "sync_runs", "player_week_usage"):
        assert con.execute(f"SELECT count(*) FROM hosted.{t}").fetchone() == \
            con.execute(f"SELECT count(*) FROM src.{t}").fetchone()
    stamp = "SELECT value FROM hosted.schema_meta WHERE key='published_at'"
    assert con.execute(stamp).fetchone() == ("2026-10-15T09:00:00",)
    replace_tables(con, "src", "hosted", "2026-10-15T10:00:00")      # same attachment, second publish
    assert con.execute(stamp).fetchall() == [("2026-10-15T10:00:00",)]
    con.close()


def test_replace_tables_rolls_back_on_failure(api_db, tmp_path, monkeypatch):
    import duckdb
    from app.db import publish as pub

    con = duckdb.connect()
    con.execute(f"ATTACH '{api_db}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{tmp_path / 'hosted.duckdb'}' AS hosted")
    pub.replace_tables(con, "src", "hosted", "first")
    monkeypatch.setattr(pub, "_ident", lambda name: '"no such table"' if name == "leagues" else '"' + name + '"')
    with pytest.raises(duckdb.Error):
        pub.replace_tables(con, "src", "hosted", "second")
    assert con.execute("SELECT value FROM hosted.schema_meta WHERE key='published_at'").fetchone() == ("first",)
    con.close()


def test_hosted_request_reads_one_snapshot(api_db, monkeypatch):
    """A publish that commits mid-request must not mix versions within the request."""
    import duckdb
    from app.api import deps

    con = connect(api_db)
    con.execute("INSERT INTO schema_meta VALUES ('published_at', 'v1')")
    con.close()
    shared = duckdb.connect(str(api_db))          # stands in for the process-wide MotherDuck connection
    monkeypatch.setattr(deps, "connect_motherduck", lambda database: shared)
    monkeypatch.setattr(deps, "_MD_CONS", {})
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "fantasy_intel")
    with deps.db() as cur:
        assert deps.data_version(cur) == "md:fantasy_intel:v1"
        publisher = shared.cursor()               # a publish committing while the request runs
        publisher.execute("UPDATE schema_meta SET value = 'v2' WHERE key = 'published_at'")
        publisher.close()
        assert deps.data_version(cur) == "md:fantasy_intel:v1"
    with deps.db() as cur:
        assert deps.data_version(cur) == "md:fantasy_intel:v2"
    shared.close()



def test_the_legacy_app_does_not_load_the_control_plane_libraries():
    """app.main imports the /v2 router unconditionally; its Postgres, JWT and crypto libraries load only when a /v2
    request needs them, so legacy cold starts do not pay for them."""
    probe = ("import sys, app.main; "
             "print(','.join(sorted(m for m in ('psycopg', 'psycopg_pool', 'jwt', 'cryptography') if m in sys.modules)))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"loaded at import: {out.stdout.strip()}"


def test_hosted_deployments_serve_no_interactive_docs_or_openapi_schema():
    """FastAPI's /docs, /redoc and /openapi.json show every route's docstring, and those name settings and operator
    steps (SLEEPER_SOURCE, SIGNUPS_ENABLED, sync scripts). A hosted deployment serves none of them; a local run keeps
    them for the developer."""
    from app.main import docs_urls
    off = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    local = SimpleNamespace(api_key=None, motherduck_database=None, snapshot_enabled=False)
    assert docs_urls(local, env={}) == {}
    for hosted in (SimpleNamespace(api_key="k", motherduck_database=None, snapshot_enabled=False),
                   SimpleNamespace(api_key=None, motherduck_database="fantasy_intel", snapshot_enabled=False),
                   SimpleNamespace(api_key=None, motherduck_database=None, snapshot_enabled=True)):
        assert docs_urls(hosted, env={}) == off
    assert docs_urls(local, env={"VERCEL": "1"}) == off


def test_the_app_wires_the_docs_switch_at_import():
    """In a fresh process (the shared test app may have been imported under a hosted environment): a local run serves
    the docs and the schema, an API-keyed (hosted) one serves neither."""
    probe = ("from fastapi.testclient import TestClient; from app.main import app; "
             "c = TestClient(app, headers={'x-api-key': 'test-key'}); "     # past the API-key check: is the route there?
             "print(c.get('/openapi.json').status_code, c.get('/docs').status_code)")
    base = {"PATH": "/usr/bin:/bin", "TZ": "UTC"}
    for env, expected in ((base, "200 200"), ({**base, "FANTASY_API_KEY": "test-key"}, "404 404")):
        out = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == expected, (env.keys(), out.stdout)

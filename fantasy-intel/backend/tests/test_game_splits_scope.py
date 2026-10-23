"""Game splits under /v2/leagues/{lg}/api/research/game-splits (issue #210): the research router's owner-only mount.

Authorization runs before validation and before any file is read: an anonymous caller and a league admin who is not a
platform admin get the same 404 as an unknown league even for an invalid query; a platform admin gets the 422 for
the invalid query and the answer for a valid one, which is private, no-store and identical to the legacy route."""
import pytest
from fastapi.testclient import TestClient

from cp_seed import U1, U2, seed_membership, seed_user
from golden.golden import build_e2e_db
from league_fixtures import publish_local
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

VALID = "/api/research/game-splits?player=00-SPLWR01&teammate=00-SPLWR02&season_from=2025"
INVALID = "/api/research/game-splits?player=00-SPLWR01&teammate=00-SPLWR02&week_to=19"


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("splits_scope") / "e2e.duckdb")


@pytest.fixture
def served(env, pg, tmp_path, monkeypatch):   # noqa: F811
    from golden.golden import E2E_SEASON
    from app.api import deps, league_scope
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    monkeypatch.setattr(league_scope, "_CACHE", {})
    monkeypatch.setattr(league_scope, "_RESPONSES", {})
    monkeypatch.setattr(deps, "_LAYERED", {})
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return tmp_path / "objects"


@pytest.fixture
def client(served):
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_game_splits_need_a_platform_admin_before_validation(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_membership(con, U1, pub.league_id, "admin")
        seed_user(con, U2, admin=True)
        con.commit()
    for path in (VALID, INVALID, "/api/research/game-splits/players"):
        url = f"/v2/leagues/{pub.league_id}{path}"
        assert client.get(url).status_code == 404, path                          # anonymous
        assert client.get(url, headers=bearer(U1)).status_code == 404, path      # a league admin is not enough
    assert client.get(f"/v2/leagues/lg_{'z' * 24}{INVALID}", headers=bearer(U2)).status_code == 404
    assert client.get(f"/v2/leagues/{pub.league_id}{INVALID}", headers=bearer(U2)).status_code == 422
    ok = client.get(f"/v2/leagues/{pub.league_id}{VALID}", headers=bearer(U2))
    assert ok.status_code == 200, ok.text[:300]
    assert ok.headers["cache-control"] == "private, no-store"
    body = ok.json()
    assert (body["counts"]["with"], body["counts"]["without"], body["counts"]["excluded"]) == (5, 4, 2)
    # every input is in the query: another window is another cache entry, never the first answer
    other = client.get(f"/v2/leagues/{pub.league_id}{VALID}&as_of=2025-10-01", headers=bearer(U2))
    assert other.status_code == 200 and other.json()["status"] == "low_n"
    again = client.get(f"/v2/leagues/{pub.league_id}{VALID}", headers=bearer(U2))
    assert again.json() == body and again.headers.get("x-fi-cache") == "hit"

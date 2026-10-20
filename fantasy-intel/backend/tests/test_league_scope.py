"""Every existing page served for one league from its files: /v2/leagues/{lg}/api/... (Phase 2).

Golden parity (the E2E fixture league answers exactly the legacy goldens), authorization before any file is touched,
two leagues with colliding team and player ids never leak into each other, anonymous perspective from the URL."""
import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from cp_seed import U1, U2, seed_membership, seed_user
from golden.golden import E2E_DIR, E2E_SEASON, Case, build_e2e_db, read_golden, response_text
from league_fixtures import publish_local
from test_leagues_api import bearer, env  # noqa: F401  (fixture)


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("scope") / "e2e.duckdb")


MARK = "Zqleak"     # appears nowhere in the fixture league; B's team names carry it


@pytest.fixture(scope="module")
def e2e_b(tmp_path_factory, e2e):
    """League B: the same ids as A (colliding team and player ids), different names and scoring results."""
    b = tmp_path_factory.mktemp("scope_b") / "b.duckdb"
    shutil.copy(e2e, b)
    con = duckdb.connect(str(b))
    con.execute(f"UPDATE fantasy_teams SET name = '{MARK} ' || name")
    con.execute(f"UPDATE leagues SET name = 'League {MARK}'")
    con.execute("UPDATE espn_player_weekly_points SET actual_points = actual_points * 2")
    con.close()
    return b


def my_team(db) -> int:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("SELECT team_id FROM fantasy_teams WHERE is_mine").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def served(env, pg, tmp_path, monkeypatch):   # noqa: F811
    """Fresh per-process caches pointed at a local object directory."""
    from app.api import deps, league_scope
    objects = tmp_path / "objects"
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(objects))
    monkeypatch.setenv("ESPN_SEASON", E2E_SEASON)
    monkeypatch.setattr(league_scope, "_CACHE", {})
    monkeypatch.setattr(league_scope, "_RESPONSES", {})
    monkeypatch.setattr(deps, "_LAYERED", {})
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return objects


@pytest.fixture
def client(served):
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_fixture_league_matches_every_golden_through_v2(pg, served, client, e2e, tmp_path):
    """A signed-in member whose saved team is the legacy owner's team sees exactly the legacy answers."""
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_membership(con, U1, pub.league_id, "admin")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                    [U1, pub.league_id, str(my_team(e2e))])
        con.commit()
    import difflib
    failures = []
    for golden in sorted(E2E_DIR.glob("*.json")):
        path, expected = read_golden(E2E_DIR, golden.stem)
        r = client.get(f"/v2/leagues/{pub.league_id}{path}", headers=bearer(U1))
        actual = response_text(_Fixed(r), Case(golden.stem, path))
        if actual != expected:
            diff = "\n".join(list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), lineterm="", n=1))[:30])
            failures.append(f"{golden.stem}: {diff}")
    assert not failures, "\n\n".join(failures[:5]) + f"\n... {len(failures)} failing"


class _Fixed:
    """Adapts one already-made response to response_text()'s client.get()."""
    def __init__(self, response):
        self.response = response

    def get(self, _path):
        return self.response


def test_responses_are_private_and_name_the_versions_read(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w")
    r = client.get(f"/v2/leagues/{pub.league_id}/api/overview")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert (r.headers["x-fi-data-version"], r.headers["x-fi-global-version"]) == (pub.data_version, pub.global_version)


def test_an_anonymous_visitor_reads_a_public_league_from_any_perspective(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    for team in (1, 2, 3, 4):
        r = client.get(f"/v2/leagues/{pub.league_id}/api/overview?team_id={team}")
        assert r.status_code == 200, r.text
        assert r.json()["perspective"]["team_id"] == team
    # without a perspective the view has no "my team"; with ?team_id= that team is the visitor's default everywhere
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview").json()["perspective"] is None
    teams = client.get(f"/v2/leagues/{pub.league_id}/api/teams?team_id=3").json()["teams"]
    assert [t["team_id"] for t in teams if t["is_owner_default"]] == [3]


def test_private_leagues_and_unknown_ids_are_404_before_any_file_is_read(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    for lg in (pub.league_id, "lg_" + "z" * 24):
        r = client.get(f"/v2/leagues/{lg}/api/overview")
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."})
    assert not list((tmp_path / "tmp" / "fantasy-leagues").glob("*")), "nothing may be downloaded for a 404"


def test_a_league_without_a_published_snapshot_is_first_sync_not_someone_elses_data(pg, served, client):
    from cp_seed import seed_job, seed_league
    with pg.connect() as con:
        lid = seed_league(con, visibility="public")
        seed_job(con, lid, status="running")
        con.commit()
    r = client.get(f"/v2/leagues/{lid}/api/overview")
    assert r.status_code == 409
    assert r.json() == {"detail": {"detail": "First sync in progress.", "sync_state": "first_sync_running"}}


def test_two_leagues_with_colliding_ids_never_leak(pg, served, client, e2e, e2e_b, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                      stamp="2026-10-20T13:00:00", external_id="1999")   # another ESPN league; same team/player ids
    with pg.connect() as con:
        seed_user(con, U1)                     # a real account, just not a member of B
        seed_user(con, U2)
        seed_membership(con, U2, b.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U2, b.league_id])
        con.commit()
    paths = sorted({read_golden(E2E_DIR, g.stem)[0] for g in E2E_DIR.glob("*.json")})
    alone = {p: client.get(f"/v2/leagues/{a.league_id}{p}").text for p in paths}
    for p in paths:                                   # interleave: B, then A, for every route
        rb = client.get(f"/v2/leagues/{b.league_id}{p}", headers=bearer(U2))
        assert rb.status_code == 200, (p, rb.text[:200])
        ra = client.get(f"/v2/leagues/{a.league_id}{p}")
        assert response_text(_Fixed(ra), Case("a", p)) == response_text(_Fixed(_Text(alone[p], ra.status_code)), Case("a", p)), p
        assert MARK not in ra.text, p
        assert MARK in rb.text or rb.json() == ra.json() or p.startswith("/api/research"), p
    # B is private: an anonymous caller, or a user who is not B's member, gets the same 404 as an unknown league
    assert client.get(f"/v2/leagues/{b.league_id}/api/overview").status_code == 404
    assert client.get(f"/v2/leagues/{b.league_id}/api/overview", headers=bearer(U1)).status_code == 404


class _Text:
    def __init__(self, text, status):
        self.text, self.status_code = text, status

    def json(self):
        import json
        return json.loads(self.text)


# ---- per-instance response cache (Task 6) -------------------------------------------------------------------------

def test_responses_are_cached_per_league_version_and_perspective(pg, served, client, e2e, e2e_b, tmp_path):
    from app.api import league_scope
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="public", name="B", external_id="1999",
                      stamp="2026-10-20T13:00:00")
    cache = league_scope.response_cache()
    first = client.get(f"/v2/leagues/{a.league_id}/api/overview?team_id=2")
    other = client.get(f"/v2/leagues/{b.league_id}/api/overview?team_id=2")      # same route and params, league B
    again = client.get(f"/v2/leagues/{a.league_id}/api/overview?team_id=2")
    persp = client.get(f"/v2/leagues/{a.league_id}/api/overview?team_id=3")
    assert first.status_code == other.status_code == again.status_code == persp.status_code == 200
    assert again.content == first.content and MARK in other.text and MARK not in again.text
    assert persp.json()["perspective"]["team_id"] == 3
    assert again.headers["x-fi-cache"] == "hit" and first.headers["x-fi-cache"] == "miss"
    assert other.headers["x-fi-cache"] == "miss" and persp.headers["x-fi-cache"] == "miss"
    assert again.headers["cache-control"] == "private, no-store"
    assert cache.hits == 1


def test_authorization_runs_before_the_cache(pg, served, client, e2e, tmp_path):
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview").status_code == 200
    with pg.connect() as con:                       # the league turns private: cached public answers are unreachable
        con.execute("UPDATE fi.leagues SET visibility = 'private', visibility_epoch = visibility_epoch + 1 WHERE id = %s",
                    [pub.league_id])
        con.commit()
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview").status_code == 404


def test_only_successful_gets_are_cached_and_the_cache_is_bounded(pg, served, client, e2e, tmp_path, monkeypatch):
    from app.api import league_scope
    monkeypatch.setattr(league_scope, "_RESPONSES", {})
    monkeypatch.setenv("LEAGUE_RESPONSE_CACHE_BYTES", "200000")
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    for path in ("/api/compare?a=1&b=2", "/api/overview", "/api/standings", "/api/waivers", "/api/managers"):
        client.get(f"/v2/leagues/{pub.league_id}{path}")
    cache = league_scope.response_cache()
    assert cache.bytes_used <= 200_000
    assert all(status == 200 for status in cache.statuses())

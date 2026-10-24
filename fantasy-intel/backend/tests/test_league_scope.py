"""Every existing page served for one league from its files: /v2/leagues/{lg}/api/... (Phase 2).

Golden parity (the E2E fixture league answers exactly the legacy goldens), authorization before any file is touched,
two leagues with colliding team and player ids never leak into each other, anonymous perspective from the URL."""
import json
import shutil

import duckdb
import pytest
from fastapi.testclient import TestClient

from cp_seed import U1, U2, U3, seed_membership, seed_user
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
        seed_user(con, U1, admin=True)      # the owner-only research pages need a platform admin
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


def test_signal_provenance_names_the_versions_read_and_no_league_id(pg, served, client, e2e, tmp_path):
    """signal-provenance.v1 in a league-scoped request: league data carries the league file version, nflverse data the
    global file version (the same values as the headers); the opaque league id never appears in the block."""
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    for path in ("/api/overview", "/api/start-sit", "/api/waivers", "/api/trades/options"):
        r = client.get(f"/v2/leagues/{pub.league_id}{path}")
        assert r.status_code == 200, (path, r.text[:200])
        prov = r.json()["provenance"]
        versions = {s["signal"]: s["snapshot_version"] for s in prov["signals"]}
        assert versions["league"] == r.headers["x-fi-data-version"], path
        if "injuries" in versions:
            assert versions["injuries"] == r.headers["x-fi-global-version"], path
        assert pub.league_id not in str(prov), path


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
    assert r.json() == {"detail": "First sync in progress."}
    assert r.headers["x-fi-sync-state"] == "first_sync_running"


def test_two_leagues_with_colliding_ids_never_leak(pg, served, client, e2e, e2e_b, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                      stamp="2026-10-20T13:00:00", external_id="1999")   # another ESPN league; same team/player ids
    with pg.connect() as con:
        seed_user(con, U1)                     # a real account, just not a member of B
        seed_user(con, U2, admin=True)         # a platform admin: the owner-only research routes are covered too
        seed_membership(con, U2, b.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U2, b.league_id])
        con.commit()
    paths = sorted({read_golden(E2E_DIR, g.stem)[0] for g in E2E_DIR.glob("*.json")})
    alone = {p: client.get(f"/v2/leagues/{a.league_id}{p}", headers=bearer(U2)).text for p in paths}
    for p in paths:                                   # interleave: B, then A, for every route
        rb = client.get(f"/v2/leagues/{b.league_id}{p}", headers=bearer(U2))
        assert rb.status_code == 200, (p, rb.text[:200])
        ra = client.get(f"/v2/leagues/{a.league_id}{p}", headers=bearer(U2))
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


# ---- the response cache's time bound (STATUS 1k) ----------------------------------------------------------------------

class _Clock:
    """An aware-UTC clock the test moves by hand."""
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _utc(text: str):
    from datetime import datetime
    return datetime.fromisoformat(text)


# The E2E fixture's first week not kicked off (espn_fixture CURRENT = 3) is scheduled on 2099-10-03 at 13:00 US Eastern
# (tests/synthetic_nfl.py FUTURE_DAY), 17:00 UTC; week 4 on 2099-10-04.
WEEK3_KICKOFF, WEEK4_KICKOFF = _utc("2099-10-03T17:00:00+00:00"), _utc("2099-10-04T17:00:00+00:00")


def test_a_cached_entry_expires_at_its_bound_and_never_outlives_the_cap():
    from datetime import timedelta
    from app.api.league_scope import RESPONSE_TTL, ResponseCache
    clock = _Clock(_utc("2026-10-23T12:00:00+00:00"))
    cache = ResponseCache(1 << 20, clock=clock)
    t0 = clock.now
    cache.put("kick", 200, b"k", "application/json", t0 + timedelta(minutes=5))
    cache.put("far", 200, b"f", "application/json", t0 + timedelta(days=30))       # capped at RESPONSE_TTL
    cache.put("none", 200, b"n", "application/json")                               # no bound given: still capped
    cache.put("past", 200, b"p", "application/json", t0)                           # its kickoff already came: not stored
    assert cache.get("past") is None and len(cache) == 3
    clock.now = t0 + timedelta(minutes=5) - timedelta(seconds=1)
    assert cache.get("kick") == (200, b"k", "application/json")
    clock.now = t0 + timedelta(minutes=5)
    assert cache.get("kick") is None and len(cache) == 2                          # dropped, not just hidden
    clock.now = t0 + RESPONSE_TTL - timedelta(seconds=1)
    assert cache.get("far") is not None and cache.get("none") is not None
    clock.now = t0 + RESPONSE_TTL
    assert cache.get("far") is None and cache.get("none") is None
    assert cache.expired == 3 and len(cache) == 0


def test_expired_entries_never_hold_room():
    """Keys that are never asked for again (another week's query, a perspective nobody opens) are dropped once their
    bound passes, the next time anything is stored; the byte bound still applies on top."""
    from datetime import timedelta
    from app.api.league_scope import ResponseCache
    clock = _Clock(_utc("2026-10-23T12:00:00+00:00"))
    cache = ResponseCache(1 << 20, clock=clock)
    for i in range(50):
        cache.put(f"old{i}", 200, b"x" * 100, "application/json", clock.now + timedelta(minutes=1))
    assert len(cache) == 50 and cache.bytes_used == 5000
    clock.now += timedelta(minutes=1)
    cache.put("new", 200, b"y", "application/json", clock.now + timedelta(minutes=1))
    assert len(cache) == 1 and cache.bytes_used == 1 and cache.expired == 50


def test_next_kickoff_reads_the_global_schedule(tmp_path, monkeypatch):
    """The first scheduled kickoff strictly after now, from the global file only; a game without a usable time is
    skipped (never guessed), a late game past midnight UTC stays in view, and no schedule at all means no bound."""
    from app.api import deps, league_scope
    from app.api.deps import LeagueScope
    monkeypatch.setattr(deps, "_LAYERED", {})
    lg, gl, bare, timeless = (tmp_path / f"{n}.duckdb" for n in ("lg", "gl", "bare", "timeless"))
    con = duckdb.connect(str(lg))
    con.execute("CREATE TABLE fantasy_teams (team_id INT, name VARCHAR, is_mine BOOLEAN)")
    # a league table of the same name must never be read: the schedule is global
    con.execute("CREATE TABLE nfl_schedules (gameday VARCHAR, gametime VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES ('2099-10-03', '09:00')")
    con.close()
    con = duckdb.connect(str(gl))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, gametime VARCHAR)")
    con.executemany("INSERT INTO nfl_schedules VALUES (?, ?, ?, ?, ?)", [
        [2099, 3, "REG", "2099-10-03", "13:00"], [2099, 3, "REG", "2099-10-03", None],
        [2099, 3, "REG", "2099-10-03", "20:15"],          # 00:15 UTC on 2099-10-04
        [2099, 4, "REG", "2099-10-10", "13:00"], [2026, 1, "REG", "2026-09-10", "20:20"],
        [2026, 5, "REG", "2099-10-02", "13:00"]])     # selected by date, whatever its season label says
    con.close()
    duckdb.connect(str(bare)).close()
    con = duckdb.connect(str(timeless))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (2099, 3, 'REG', '2099-10-03')")
    con.close()

    def scope(global_path):
        return LeagueScope("lg_t", lg, global_path, "d", "g", 0, None)

    late = _utc("2099-10-04T00:15:00+00:00")
    assert league_scope.next_kickoff(scope(gl), _utc("2099-10-03T16:00:00+00:00")) == WEEK3_KICKOFF
    assert league_scope.next_kickoff(scope(gl), _utc("2099-10-02T12:00:00+00:00")) == _utc("2099-10-02T17:00:00+00:00")
    assert league_scope.next_kickoff(scope(gl), WEEK3_KICKOFF) == late                # at kickoff: the next one
    assert league_scope.next_kickoff(scope(gl), _utc("2099-10-04T00:10:00+00:00")) == late
    assert league_scope.next_kickoff(scope(gl), late) == _utc("2099-10-10T17:00:00+00:00")
    assert league_scope.next_kickoff(scope(gl), _utc("2100-01-01T00:00:00+00:00")) is None
    assert league_scope.next_kickoff(scope(bare), WEEK3_KICKOFF) is None
    assert league_scope.next_kickoff(scope(timeless), _utc("2099-10-01T00:00:00+00:00")) is None   # no gametime column


@pytest.mark.parametrize("path", ["/api/trades/values", "/api/trades/options"])
def test_a_time_dependent_answer_is_recomputed_once_its_week_kicks_off(pg, served, client, e2e, e2e_b, tmp_path, path,
                                                                      monkeypatch):
    """The trade window starts at the first week not kicked off (app/analytics/trade_data.evaluation_start_week). A
    cached answer is served until the next kickoff, then computed again with the window moved on; another league's and
    another perspective's entries stay their own. The handler reads the same moved clock as the cache."""
    from datetime import timedelta
    from app.analytics import trade_data
    from app.api import league_scope
    from app.api.league_scope import RESPONSE_TTL
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="public", name="B", external_id="1999",
                      stamp="2026-10-20T13:00:00")
    clock = _Clock(WEEK3_KICKOFF - timedelta(minutes=10))
    cache = league_scope.response_cache()
    cache.clock = clock
    real_now = trade_data._aware_utc
    monkeypatch.setattr(trade_data, "_aware_utc", lambda now: real_now(clock() if now is None else now))
    url_a, url_b = f"/v2/leagues/{a.league_id}{path}?team_id=2", f"/v2/leagues/{b.league_id}{path}?team_id=2"
    first = client.get(url_a)
    assert first.status_code == 200 and first.headers["x-fi-cache"] == "miss", first.text[:200]
    assert {e[3] for e in cache._entries.values()} == {WEEK3_KICKOFF}             # bound to the kickoff, not the cap
    other_league = client.get(url_b)
    other_team = client.get(f"/v2/leagues/{a.league_id}{path}?team_id=3")
    assert other_league.headers["x-fi-cache"] == "miss" and other_team.headers["x-fi-cache"] == "miss"
    assert MARK in other_league.text or other_league.json() == first.json()
    assert MARK not in first.text
    clock.now = WEEK3_KICKOFF - timedelta(seconds=1)
    before = client.get(url_a)
    assert before.headers["x-fi-cache"] == "hit" and before.content == first.content
    assert client.get(url_b).headers["x-fi-cache"] == "hit"
    clock.now = WEEK3_KICKOFF + timedelta(seconds=1)                           # week 3 has kicked off
    after = client.get(url_a)
    assert after.status_code == 200 and after.headers["x-fi-cache"] == "miss"
    assert MARK not in after.text
    if path == "/api/trades/options":           # the window moved on: week 3 has kicked off, so it starts at week 4
        assert first.json()["evaluation"]["start_week"] == 3 and after.json()["evaluation"]["start_week"] == 4
    else:                                       # every value is taken over the shorter window
        assert first.json()["available"] and after.json()["players"] != first.json()["players"]
    assert len(cache) == 1                                                      # the three expired entries are gone
    assert cache._entries[next(iter(cache._entries))][3] == clock.now + RESPONSE_TTL   # week 4 is a day away: the cap
    assert client.get(url_a).headers["x-fi-cache"] == "hit"
    b_after = client.get(url_b)
    assert b_after.headers["x-fi-cache"] == "miss" and MARK not in client.get(url_a).text


def test_a_time_independent_answer_still_hits_until_the_cap(pg, served, client, e2e, tmp_path):
    """No kickoff ahead within the cap (the fixture's next one is in 2099): the answer is served for RESPONSE_TTL."""
    from datetime import timedelta
    from app.api import league_scope
    from app.api.league_scope import RESPONSE_TTL
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    clock = _Clock(_utc("2026-10-23T12:00:00+00:00"))
    league_scope.response_cache().clock = clock
    url = f"/v2/leagues/{pub.league_id}/api/overview"
    t0 = clock.now
    first = client.get(url)
    assert first.status_code == 200 and first.headers["x-fi-cache"] == "miss"
    clock.now = t0 + RESPONSE_TTL - timedelta(seconds=1)
    assert client.get(url).headers["x-fi-cache"] == "hit"
    clock.now = t0 + RESPONSE_TTL
    assert client.get(url).headers["x-fi-cache"] == "miss"
    assert client.get(url).headers["x-fi-cache"] == "hit"


OWNER_ONLY = ("/api/projections/performance", "/api/projections/replay", "/api/projections/independent/promotion")
# The research tools are league-member tools (#289 research-tools rulings): authorize_league alone.
RESEARCH = ("/api/research/opportunity?position=WR", "/api/research/game-splits/players",
            "/api/research/game-splits?player=00-SPLWR01&teammate=00-SPLWR02&season_from=2023",
            "/api/research/players/00-SYNRB01", "/api/pace-lab")


def test_projection_accuracy_and_the_promotion_tracker_stay_platform_admin_only(pg, served, client, e2e, e2e_b,
                                                                                tmp_path):
    """Spec §6 (and the #289 audit): projection performance, its replay and Ours promotion stay platform-admin only on
    every league: anonymous viewers, members and league admins get 404, before any parameter is validated. A platform
    admin reads them on a public league and on a private league he is a member of (no admin bypass of membership)."""
    pub = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    priv = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                         stamp="2026-10-20T13:00:00", external_id="1999")
    with pg.connect() as con:
        seed_user(con, U1)                                  # a plain member of both
        seed_user(con, U3)                                  # a league admin of both
        seed_user(con, U2, admin=True)                      # platform admin, a member of B
        for lg in (pub.league_id, priv.league_id):
            seed_membership(con, U1, lg, "viewer")
            seed_membership(con, U3, lg, "admin")
        seed_membership(con, U2, priv.league_id, "viewer")
        con.commit()
    for lg in (pub.league_id, priv.league_id):
        for path in OWNER_ONLY:
            url = f"/v2/leagues/{lg}{path}"
            for headers in ({}, bearer(U1), bearer(U3)):
                assert client.get(url, headers=headers).status_code == 404, (lg, path)
                assert client.get(url + "?season=abc", headers=headers).status_code == 404, (lg, path)   # before 422
            assert client.get(url, headers=bearer(U2)).status_code == 200, (lg, path)
            if path != "/api/projections/independent/promotion":                   # it takes no parameter
                assert client.get(url + "?season=abc", headers=bearer(U2)).status_code == 422, (lg, path)
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview").status_code == 200


def test_research_tools_follow_the_league_visibility_rules(pg, served, client, e2e, e2e_b, tmp_path):
    """Anonymous viewers of a public league, its members and platform admins get the research tools. On a private
    league everyone who is not a member gets the 404 an unknown league gets, before any parameter is validated:
    anonymous, a signed-in outsider, a member of another private league who swaps in this one's id, and a platform
    admin who is not a member (membership is never bypassed). A platform admin who is a member gets the answer."""
    pub = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    priv = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B",
                         stamp="2026-10-20T13:00:00", external_id="1999")
    other = publish_local(pg, e2e, served, tmp_path / "c", visibility="private", name="C",
                          stamp="2026-10-20T14:00:00", external_id="1998")
    with pg.connect() as con:
        seed_user(con, U1)                                  # member of B only
        seed_membership(con, U1, priv.league_id, "viewer")
        seed_user(con, U2, admin=True)                      # platform admin, member of C only
        seed_membership(con, U2, other.league_id, "viewer")
        seed_user(con, U3)                                  # member of C only
        seed_membership(con, U3, other.league_id, "viewer")
        con.commit()
    for path in RESEARCH:
        public = f"/v2/leagues/{pub.league_id}{path}"
        for headers in ({}, bearer(U1), bearer(U2)):                              # anonymous, a member elsewhere, admin
            assert client.get(public, headers=headers).status_code == 200, path
        private = f"/v2/leagues/{priv.league_id}{path}"
        unknown = client.get(f"/v2/leagues/lg_{'z' * 24}{path}", headers=bearer(U3))
        assert unknown.status_code == 404, path
        for headers in ({}, bearer(U3), bearer(U2)):        # anonymous; C's member swaps in B; an admin who is no member
            r = client.get(private, headers=headers)
            assert r.status_code == 404 and r.text == unknown.text, path         # indistinguishable from unknown
        assert client.get(private, headers=bearer(U1)).status_code == 200, path  # B's member
        assert client.get(f"/v2/leagues/{other.league_id}{path}", headers=bearer(U2)).status_code == 200, path
    # before validation: an outsider's invalid query is the unknown-league 404, a member's is the 422
    bad = "/api/research/opportunity"                                             # position is required
    unknown = client.get(f"/v2/leagues/lg_{'z' * 24}{bad}", headers=bearer(U3))
    r = client.get(f"/v2/leagues/{priv.league_id}{bad}", headers=bearer(U3))
    assert r.status_code == 404 and r.text == unknown.text
    assert client.get(f"/v2/leagues/{priv.league_id}{bad}", headers=bearer(U1)).status_code == 422


def test_the_saved_team_stays_my_team_while_another_team_is_analysed(pg, served, client, e2e, tmp_path):
    """Legacy parity: is_mine is who the viewer is (their saved team); ?team_id= picks the team a route analyses."""
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_membership(con, U1, pub.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U1, pub.league_id])
        con.commit()
    teams = client.get(f"/v2/leagues/{pub.league_id}/api/teams?team_id=3", headers=bearer(U1)).json()["teams"]
    assert [t["team_id"] for t in teams if t["is_owner_default"]] == [2]
    assert client.get(f"/v2/leagues/{pub.league_id}/api/overview?team_id=3",
                      headers=bearer(U1)).json()["perspective"]["team_id"] == 3


def test_a_sleeper_league_beside_an_espn_league_keeps_its_own_words_and_data(pg, served, client, e2e, tmp_path,
                                                                             monkeypatch):
    """Phase 4b: an ESPN league and a Sleeper league (the synthetic one, colliding team ids) served interleaved.
    The ESPN league answers exactly as alone; the Sleeper league's answers never name ESPN or show its data."""
    import importlib.util
    import re
    import sys
    from pathlib import Path

    from app.api import provider_copy

    monkeypatch.setenv("SLEEPER_SOURCE", "live")
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("build_e2e_sleeper", scripts / "build_e2e_sleeper.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sleeper_db = tmp_path / "sleeper.duckdb"
        mod.build(sleeper_db)
    finally:
        sys.path.remove(str(scripts))
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    s = publish_local(pg, sleeper_db, served, tmp_path / "s", visibility="public", name="S", provider="sleeper",
                      stamp="2026-10-20T13:00:00")
    paths = sorted({read_golden(E2E_DIR, g.stem)[0] for g in E2E_DIR.glob("*.json")})
    alone = {p: client.get(f"/v2/leagues/{a.league_id}{p}").text for p in paths}
    for p in paths:
        rs = client.get(f"/v2/leagues/{s.league_id}{p}?team_id=1" if "?" not in p else f"/v2/leagues/{s.league_id}{p}")
        ra = client.get(f"/v2/leagues/{a.league_id}{p}")
        assert response_text(_Fixed(ra), Case("a", p)) == response_text(_Fixed(_Text(alone[p], ra.status_code)),
                                                                        Case("a", p)), p
        if rs.status_code == 200 and provider_copy.applies(p.split("?")[0]):
            assert re.findall(r'"([^"]*ESPN[^"]*)"', rs.text) == [], p
            assert "\"Fixture League\"" not in rs.text, p    # the ESPN league's name never reaches the Sleeper league


# ---- issue #201: concurrency, single flight, degraded reads, Server-Timing ------------------------------------------

CONCURRENT_PATHS = ("/api/overview", "/api/teams", "/api/managers", "/api/waivers?view=BEST&limit=3", "/api/matchup",
                    "/api/status")


def test_concurrent_cold_loads_of_two_leagues_never_bleed(pg, served, e2e, e2e_b, tmp_path):
    """Both leagues cold (empty file and response caches), every route of both requested at once from many threads:
    each answer is its own league's."""
    from concurrent.futures import ThreadPoolExecutor
    from app.main import app
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="public", name="B", external_id="1999",
                      stamp="2026-10-20T13:00:00")
    jobs = [(lg, p, team) for team in (1, 2) for p in CONCURRENT_PATHS for lg in (a.league_id, b.league_id)] * 2

    def get(job):
        lg, path, team = job
        sep = "&" if "?" in path else "?"
        r = TestClient(app).get(f"/v2/leagues/{lg}{path}" + (f"{sep}team_id={team}" if team else ""))
        return lg, path, r.status_code, r.text, r.headers.get("x-fi-data-version")

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(get, jobs))
    for lg, path, status, text, version in results:
        assert status == 200, (path, text[:200])
        if lg == a.league_id:
            assert MARK not in text and version == a.data_version, path
        else:
            assert version == b.data_version, path
            if path in ("/api/overview", "/api/teams", "/api/managers"):
                assert MARK in text, path


def test_identical_concurrent_requests_run_the_handler_once(pg, served, e2e, tmp_path, monkeypatch):
    import asyncio
    import time as _time
    import httpx
    from app.api import league_scope, routes
    from app.main import app
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    real, calls = routes._league, []

    def slow_league(con):
        calls.append(1)
        _time.sleep(0.3)
        return real(con)

    monkeypatch.setattr(routes, "_league", slow_league)
    url = f"/v2/leagues/{pub.league_id}/api/overview?team_id=2"

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.get(f"/v2/leagues/{pub.league_id}/api/status")          # files warm: the burst races the handler
            return await asyncio.gather(*(c.get(url) for _ in range(6)))

    responses = asyncio.run(burst())
    assert [r.status_code for r in responses] == [200] * 6
    assert len({r.content for r in responses}) == 1
    assert sorted(r.headers["x-fi-cache"] for r in responses) == ["hit"] * 5 + ["miss"]
    assert len(calls) == 1
    assert league_scope.response_cache().shared >= 1
    assert all(r.headers["cache-control"] == "private, no-store" for r in responses)
    # another perspective is another key: never shared
    other = asyncio.run(_one(app, f"/v2/leagues/{pub.league_id}/api/overview?team_id=3"))
    assert other.headers["x-fi-cache"] == "miss" and other.json()["perspective"]["team_id"] == 3


async def _one(app, url):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(url)


def test_a_new_version_that_cannot_be_fetched_serves_the_previous_files_marked(pg, served, client, e2e, tmp_path,
                                                                                 monkeypatch):
    import psycopg
    from app.db import league_files
    from app.db.league_files import league_key
    from app.db.league_publish import DirWriter, publish_league
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    first = client.get(f"/v2/leagues/{pub.league_id}/api/overview")
    assert first.status_code == 200 and "x-fi-degraded" not in first.headers
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        res = publish_league(e2e, DirWriter(served), sync, pub.league_id, stamp="2026-10-21T12:00:00")
    assert res.applied and res.data_version != pub.data_version
    obj = served / league_key(pub.league_id, res.data_version)
    held = obj.read_bytes()
    obj.unlink()                                               # R2 cannot deliver the new file
    stale = client.get(f"/v2/leagues/{pub.league_id}/api/overview")
    assert stale.status_code == 200
    assert stale.headers["x-fi-degraded"] == "stale-files"
    assert stale.headers["x-fi-data-version"] == pub.data_version
    assert stale.json() == first.json()
    obj.write_bytes(held)                                      # R2 is back; the failure back-off has passed
    monkeypatch.setattr(league_files, "FAILED_FETCH_RETRY_S", 0.0)
    fresh = None
    for _ in range(50):
        fresh = client.get(f"/v2/leagues/{pub.league_id}/api/overview")
        if "x-fi-degraded" not in fresh.headers:
            break
        import time as _time
        _time.sleep(0.05)
    assert fresh.status_code == 200 and "x-fi-degraded" not in fresh.headers
    assert fresh.headers["x-fi-data-version"] == res.data_version


def test_server_timing_names_phases_without_the_league(pg, served, client, e2e, tmp_path, monkeypatch):
    from app.monitor import timing as perf
    lines = []

    class Collect:
        def info(self, line):
            lines.append(line)

    monkeypatch.setattr(perf, "_logger", lambda: Collect())
    monkeypatch.setenv("FI_PERF_SAMPLE", "1")
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="public")
    cold = client.get(f"/v2/leagues/{pub.league_id}/api/overview?team_id=2")
    assert lines and lines[-1].startswith("perf GET /v2/leagues/{league_id}/api/overview status=200 ")
    assert pub.league_id not in lines[-1] and "team_id" not in lines[-1]
    warm = client.get(f"/v2/leagues/{pub.league_id}/api/teams")
    hit = client.get(f"/v2/leagues/{pub.league_id}/api/overview?team_id=2")
    st = {k: r.headers["server-timing"] for k, r in (("cold", cold), ("warm", warm), ("hit", hit))}
    assert 'files;dur=' in st["cold"] and 'desc="cold"' in st["cold"] and st["cold"].startswith("auth;dur=")
    assert 'desc="warm"' in st["warm"]
    assert 'cache;dur=0.0;desc="hit"' in st["hit"]
    for header in st.values():
        assert pub.league_id not in header and "app;dur=" in header
    assert "server-timing" in client.get("/health").headers


def test_response_key_cannot_be_forged_with_encoded_separators():
    """An encoded `=`/`&` inside one query name must not produce the same response-cache key as the real pairs."""
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.api.league_scope import _response_key

    def key(qs: str) -> str | None:
        req = Request({"type": "http", "method": "GET", "path": "/v2/leagues/lg_x/api/start-sit/what-if",
                       "query_string": qs.encode(), "headers": []})
        return _response_key(SimpleNamespace(cache_key="lg_x|1|1|0|-"), req)

    assert key("week=7&whatif=w7t3~FLEX_0.8") != key("week%3D7%26whatif=w7t3~FLEX_0.8")
    assert key("a=1&b=2") != key("a=1%26b%3D2")
    assert key("b=2&a=1") == key("a=1&b=2")                     # order-insensitive, as before
    # a repeated name keeps its values' order: the routes read the last value or the sequence
    assert key("a=2&a=1") != key("a=1&a=2")
    assert key("override=12:home&override=12:away") != key("override=12:away&override=12:home")
    assert key("b=1&a=2&a=1") == key("a=2&b=1&a=1")             # other names may still move around


def test_targeted_trade_discovery_resolves_players_in_the_scoped_league_only(pg, served, client, e2e, tmp_path):
    """Issue #182: a target player id is looked up on the scoped league's rosters only. League C is A with Echo RB
    (201) unrostered and marked team names: the same id is a target in A, a 422 in C, and neither league's names or
    players reach the other's answers (the view and need caches are per league)."""
    c_db = tmp_path / "c.duckdb"
    shutil.copy(e2e, c_db)
    con = duckdb.connect(str(c_db))
    con.execute("DELETE FROM lineup_snapshots WHERE espn_player_id = 201")
    con.execute(f"UPDATE fantasy_teams SET name = '{MARK} ' || name")
    con.close()
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    c = publish_local(pg, c_db, served, tmp_path / "c", visibility="public", name="C", stamp="2026-10-20T13:00:00",
                      external_id="1998")
    q = "/api/trades/discover/summary?mode=targeted&target_player=201&team_id=2"     # an anonymous visitor names a team
    first = client.get(f"/v2/leagues/{a.league_id}{q}")
    assert first.status_code == 200, first.text[:300]
    assert first.json()["target"]["player"]["team_id"] == 1 and MARK not in first.text
    other = client.get(f"/v2/leagues/{c.league_id}{q}")
    assert other.status_code == 422 and "not on another team's roster" in other.json()["detail"]
    again = client.get(f"/v2/leagues/{a.league_id}{q}")
    assert again.status_code == 200 and again.json() == first.json()
    ta = client.get(f"/v2/leagues/{a.league_id}/api/trades/discover/targets?team_id=2").json()
    tc = client.get(f"/v2/leagues/{c.league_id}/api/trades/discover/targets?team_id=2").json()
    assert 201 in {p["espn_player_id"] for p in ta["players"]} and MARK not in json.dumps(ta)
    assert 201 not in {p["espn_player_id"] for p in tc["players"]}
    assert tc["players"] and all(p["team_name"].startswith(MARK) for p in tc["players"])


def test_trade_evidence_is_served_only_for_the_league_in_the_path(pg, served, client, e2e, e2e_b, tmp_path,
                                                                   monkeypatch):
    """The row evidence behind the discovery page's "Why this trade" panel (and the #182 targeted views): a member of
    league A can never read league B's evidence. The league comes only from the /v2 path, authorized per request
    (league_scope -> authorize_league_in): B is the same 404 as an unknown league for a non-member and for an anonymous
    caller, a forged league header or a league id in the query never moves A's request to B, and the single-league
    /api path (which ignores any league) is closed once the classic site is retired."""
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B", stamp="2026-10-20T13:00:00",
                      external_id="1999")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, a.league_id, "viewer")
        seed_membership(con, U2, b.league_id, "viewer")
        for user, lg in ((U1, a.league_id), (U2, b.league_id)):
            con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                        [user, lg])
        con.commit()
    q = "mode=targeted&target_position=RB"
    summary = client.get(f"/v2/leagues/{b.league_id}/api/trades/discover/summary?{q}", headers=bearer(U2))
    assert summary.status_code == 200, summary.text[:200]
    key = next(x["key"] for items in summary.json()["sections"].values() for x in items)
    ev = f"/api/trades/discover/evidence?{q}&key={key}"
    own = client.get(f"/v2/leagues/{b.league_id}{ev}", headers=bearer(U2))
    assert own.status_code == 200 and MARK in own.text                     # B's member reads B's evidence

    not_found = (404, {"detail": "League not found."})
    unknown = client.get(f"/v2/leagues/lg_{'z' * 24}{ev}", headers=bearer(U1))
    assert (unknown.status_code, unknown.json()) == not_found
    for path in (ev, f"/api/trades/discover/summary?{q}", f"/api/trades/discover/context?{q}",
                 "/api/trades/discover/targets"):
        for headers in (bearer(U1), {}):                                    # A's member, then anonymous
            r = client.get(f"/v2/leagues/{b.league_id}{path}", headers=headers)
            assert (r.status_code, r.json()) == not_found, (path, headers)
    # Counteroffers (#214) are a POST on the same router: the same per-league authorization, before the body is
    # validated (a JSON body is decoded first, so malformed JSON is the same 422 for every league id).
    rosters = {t["team_id"]: [p["espn_player_id"] for p in t["roster"]]
               for t in client.get(f"/v2/leagues/{b.league_id}/api/trades/options", headers=bearer(U2)).json()["teams"]}
    ta, tb = sorted(rosters)[:2]
    offer = {"team_a_id": ta, "team_b_id": tb, "team_a_sends": rosters[ta][:1], "team_b_sends": rosters[tb][:1]}
    for headers in (bearer(U1), {}):
        r = client.post(f"/v2/leagues/{b.league_id}/api/trades/counteroffers", json=offer, headers=headers)
        assert (r.status_code, r.json()) == not_found, headers
    mine = client.post(f"/v2/leagues/{b.league_id}/api/trades/counteroffers", json=offer, headers=bearer(U2))
    assert mine.status_code == 200 and mine.json()["contract"] == "trade-counteroffers/v1", mine.text[:200]

    # A forged league header and a league id in the query are ignored: A's request answers from A's files only.
    forged = client.get(f"/v2/leagues/{a.league_id}{ev}&league_id={b.league_id}",
                        headers={**bearer(U1), "x-fi-league": b.league_id})
    plain = client.get(f"/v2/leagues/{a.league_id}{ev}", headers=bearer(U1))
    assert forged.status_code == plain.status_code == 200, forged.text[:200]
    assert MARK not in forged.text and forged.json() == plain.json()

    # The single-league path never selects a tenant; retired, it answers 404 whatever league header is sent.
    monkeypatch.setenv("CLASSIC_SITE_ENABLED", "0")
    for path in (ev, "/api/trades/discover/targets"):
        r = client.get(path, headers={"x-fi-league": b.league_id})
        assert r.status_code == 404 and MARK not in r.text, path
    assert client.get(f"/v2/leagues/{b.league_id}{ev}", headers=bearer(U2)).status_code == 200


# ---- projection-source comparison (#189): Ours runs, ranges and forecast history stay in their league ---------------

def _add_ours_ranges_and_history(db) -> None:
    """League B's own data for /api/rankings/compare: a sealed Ours run for the current week (ranks roughly reversed
    against ESPN, so there are large gaps; its stored drivers carry MARK), a pinned point-distribution table under the
    league's scoring, and a second, earlier ESPN projection snapshot (so there is forecast history)."""
    from datetime import datetime

    from app.analytics import independent_projection_store as ips
    from app.analytics import projection_ranges as pr
    from app.analytics import projection_source as ps
    from app.db.connection import connect
    from test_projection_ranges import _dist_artifact, _pin

    con = connect(db)
    try:
        season, week = con.execute("SELECT season, current_week FROM leagues").fetchone()
        fp, why = ps._scoring(con, season)
        assert fp is not None, why
        players = con.execute("""SELECT fp.espn_player_id, fp.position, p.projected_points FROM fantasy_players fp
                                 JOIN espn_player_weekly_points p ON p.espn_player_id = fp.espn_player_id
                                 WHERE p.season = ? AND p.scoring_period = ? AND p.projected_points IS NOT NULL
                                   AND fp.position IN ('QB', 'RB', 'WR', 'TE')""", [season, week]).fetchall()
        created = datetime(2026, 10, 19, 12, 0)
        rows = []
        for pid, pos, espn in players:
            pts = round(max(0.5, 30.0 - float(espn)), 2)
            rows.append({"season": season, "week": week, "espn_player_id": pid, "gsis_id": None, "position": pos,
                         "nfl_team": None, "opponent": None, "is_home": None, "projected_points": pts,
                         "interval_low": None, "interval_high": None, "model": "tenant-test",
                         "coverage_status": "projected", "games_used": 4, "latest_usage_season": season,
                         "latest_usage_week": week - 1, "missing_inputs": [], "flags": [],
                         "explanation": [{"feature": "leakcheck", "value": 1.0, "training_mean": 0.0,
                                          "contribution": 2.5, "text": f"{MARK} input"}],
                         "p_play": 1.0, "expected_points": pts, "participation": None})
        ips.write_run(con, {"season": season, "week": week, "forecast_horizon": 0, "projected_from_week": week,
                            "data_through_season": season, "data_through_week": week - 1, "created_at": created,
                            "artifact_id": "tenant-test", "artifact_created_at": created.isoformat(),
                            "run_id": f"{MARK.lower()}-run", "model_version": ps.MODEL_VERSION,
                            "scoring_fingerprint": fp, "model_status": {}, "evaluation": {},
                            "participation_version": None, "participation_status": None,
                            "participation_evaluation": None, "nfl_synced_at": created}, rows)
        art = _dist_artifact(scoring_fingerprint=fp)
        pr.store(con, art, _pin(art))
        con.execute("""INSERT INTO espn_projection_history
                       SELECT DATE '2026-10-13', season, espn_player_id, scoring_period, projected_points * 0.6,
                              TIMESTAMP '2026-10-13 12:00:00'
                       FROM espn_projection_history WHERE snapshot_date = (SELECT max(snapshot_date) FROM espn_projection_history)""")
        con.execute("CHECKPOINT")
    finally:
        con.close()


@pytest.fixture(scope="module")
def e2e_ours_b(tmp_path_factory, e2e):
    b = tmp_path_factory.mktemp("scope_ours_b") / "b.duckdb"
    shutil.copy(e2e, b)
    _add_ours_ranges_and_history(b)
    return b


def test_source_comparison_never_reads_another_leagues_runs_ranges_or_history(pg, served, client, e2e, e2e_ours_b,
                                                                               tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    b = publish_local(pg, e2e_ours_b, served, tmp_path / "b", visibility="public", name="B", external_id="1999",
                      stamp="2026-10-20T13:00:00")      # the same team and player ids as A
    path = "/api/rankings/compare?show=large&scope=overall"
    first_a = client.get(f"/v2/leagues/{a.league_id}{path}")
    rb = client.get(f"/v2/leagues/{b.league_id}{path}")
    ra = client.get(f"/v2/leagues/{a.league_id}{path}")
    assert first_a.status_code == rb.status_code == ra.status_code == 200

    # B: its own Ours run, ranges and forecast history are all served
    body_b = rb.json()
    explained = [p for p in body_b["players"] if p["explanation"]]
    assert body_b["sources"][1]["available"] and explained
    assert explained[0]["explanation"]["model"]["inputs"][0]["text"] == f"{MARK} input"
    assert any(p["range"]["available"] for p in body_b["players"])
    assert explained[0]["explanation"]["history"]["provider"]["available"]

    # A, before and after B was served: no Ours, no explanation, no range, no history, nothing of B's
    for r in (first_a, ra):
        body = r.json()
        assert body["sources"][1]["available"] is False and body["players"] == [] and body["counts"]["large"] == 0
        assert MARK not in r.text and MARK.lower() not in r.text
    every = client.get(f"/v2/leagues/{a.league_id}/api/rankings/compare?scope=overall").json()
    assert every["players"] and every["ranges"]["available"] is False
    assert all(p["explanation"] is None and not p["range"]["available"] for p in every["players"])

    # cache entries never cross: A's repeat is a hit identical to A's own first answer, B's answer was its own miss
    assert first_a.headers["x-fi-cache"] == "miss" and rb.headers["x-fi-cache"] == "miss"
    assert ra.headers["x-fi-cache"] == "hit" and ra.content == first_a.content
    again_b = client.get(f"/v2/leagues/{b.league_id}{path}")
    assert again_b.headers["x-fi-cache"] == "hit" and again_b.content == rb.content


def test_roster_utility_is_served_only_for_the_league_in_the_path(pg, served, client, e2e, e2e_b, tmp_path):
    """Roster utility (#216, GET /api/my-team/value/marginal on the team value router): league B's member reads B's
    answer for B's own roster; a member of league A and an anonymous caller get the same 404 as an unknown league."""
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B", stamp="2026-10-20T13:00:00",
                      external_id="1999")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, a.league_id, "viewer")
        seed_membership(con, U2, b.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U2, b.league_id])
        con.commit()
    value = client.get(f"/v2/leagues/{b.league_id}/api/my-team/value", headers=bearer(U2))
    assert value.status_code == 200, value.text[:200]
    pid = value.json()["players"][0]["espn_player_id"]
    path = f"/api/my-team/value/marginal?ids={pid}"
    own = client.get(f"/v2/leagues/{b.league_id}{path}", headers=bearer(U2))
    assert own.status_code == 200 and own.json()["contract"] == "roster-utility/v1", own.text[:200]
    assert own.json()["team"]["team_id"] == value.json()["team"]["team_id"]      # the same team as the page
    for headers in (bearer(U1), {}):                                        # A's member, then anonymous
        r = client.get(f"/v2/leagues/{b.league_id}{path}", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), headers


def test_the_stash_plan_is_served_only_for_the_league_in_the_path(pg, served, client, e2e, e2e_b, tmp_path):
    """Stash planner (#218, GET /api/my-team/stash on the team value router): league B's member reads B's plan; a
    member of league A and an anonymous caller get the same 404 as an unknown league."""
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="A")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="B", stamp="2026-10-20T13:00:00",
                      external_id="1999")
    with pg.connect() as con:
        seed_user(con, U1)
        seed_user(con, U2)
        seed_membership(con, U1, a.league_id, "viewer")
        seed_membership(con, U2, b.league_id, "viewer")
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                    [U2, b.league_id])
        con.commit()
    own = client.get(f"/v2/leagues/{b.league_id}/api/my-team/stash", headers=bearer(U2))
    assert own.status_code == 200 and own.json()["contract"] == "stash-plan/v1", own.text[:200]
    for headers in (bearer(U1), {}):                                        # A's member, then anonymous
        r = client.get(f"/v2/leagues/{b.league_id}/api/my-team/stash", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), headers

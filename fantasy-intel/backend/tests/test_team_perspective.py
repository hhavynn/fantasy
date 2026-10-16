"""League-wide team perspective: every team-scoped view can be computed for any team of the league, not only the
owner's (is_mine), with the owner default unchanged and nothing computed for one team served for another."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analytics.pipeline import run_analytics
from app.analytics.waivers import build_waivers
from app.db.connection import connect, fetch_dicts, open_db
from app.espn.sync import run_espn_sync

from espn_fixture import MY_SWID, ROSTERS, SEASON, FakeEspnClient

OWNER = 2
TEAMS = sorted(ROSTERS)


def _sync(db_path):
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    run_analytics(con, SEASON)
    return con


@pytest.fixture
def league(tmp_path, monkeypatch):
    db_path = tmp_path / "perspective.duckdb"
    _sync(db_path).close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    return SimpleNamespace(api=TestClient(app), path=db_path)


def _roster_ids(con, team_id):
    return {r["espn_player_id"] for r in fetch_dicts(con, """SELECT espn_player_id FROM lineup_snapshots l
        WHERE team_id = ? AND scoring_period = (SELECT current_week FROM leagues)""", [team_id])}


def test_teams_lists_every_team_with_the_owner_default(league):
    body = league.api.get("/api/teams").json()
    assert [t["team_id"] for t in body["teams"]] == TEAMS
    assert body["default_team_id"] == OWNER
    assert [t["team_id"] for t in body["teams"] if t["is_owner_default"]] == [OWNER]
    assert all(t["roster_synced_at"] for t in body["teams"])
    assert {t["team_id"]: t["roster_players"] for t in body["teams"]} == {t: len(r) for t, r in ROSTERS.items()}


def test_overview_default_is_unchanged_and_every_team_gets_its_own_roster_and_needs(league):
    default = league.api.get("/api/overview").json()
    assert default["my_team"]["team_id"] == OWNER and default["team"]["team_id"] == OWNER
    assert default["perspective"]["source"] == "owner_default" and len(default["roster"]) == len(ROSTERS[OWNER])
    con = connect(league.path, read_only=True)
    needs_by_team = {}
    for tid in TEAMS:
        ov = league.api.get("/api/overview", params={"team_id": tid}).json()
        assert ov["team"]["team_id"] == tid and ov["perspective"]["source"] == "query"
        assert ov["my_team"]["team_id"] == OWNER                       # the saved owner flag never follows the viewer
        assert {p["espn_player_id"] for p in ov["roster"]} == _roster_ids(con, tid)
        assert {n["team_id"] for n in ov["needs"]} == {tid}
        needs_by_team[tid] = {n["position"]: n["need_score"] for n in ov["needs"]}
    con.close()
    # Different managers, different positional needs: team 4 has no RB/WR at all, the owner has depth everywhere.
    assert needs_by_team[4]["RB"] > needs_by_team[OWNER]["RB"]
    assert needs_by_team[4]["WR"] > needs_by_team[OWNER]["WR"]
    assert needs_by_team[4] != needs_by_team[3] != needs_by_team[OWNER]


def test_waivers_are_evaluated_from_the_selected_teams_roster(league):
    con = connect(league.path, read_only=True)
    fits = {}
    for tid in TEAMS:
        body = league.api.get("/api/waivers", params={"team_id": tid, "limit": 360}).json()
        assert body["perspective"]["team_id"] == tid and body["players"]
        assert {p["team_id"] for p in body["players"]} == {tid}
        roster = _roster_ids(con, tid)
        drops = {p["suggested_drop_id"] for p in body["players"] if p["suggested_drop_id"] is not None}
        assert drops <= roster                                          # a drop always comes off this team's roster
        assert all(p["drop_on_roster"] is (p["suggested_drop_id"] is not None) or p["drop_on_roster"] is None
                   for p in body["players"])
        fits[tid] = {p["espn_player_id"]: p["roster_fit_score"] for p in body["players"]}
    con.close()
    assert fits[4] != fits[OWNER]                                       # recalculated, not renamed


def test_owner_default_waivers_match_the_owners_team_rows(league):
    default = league.api.get("/api/waivers", params={"limit": 360}).json()
    owner = league.api.get("/api/waivers", params={"limit": 360, "team_id": OWNER}).json()
    key = lambda b: [(p["espn_player_id"], p["waiver_score"], p["recommendation"], p["suggested_drop_id"])
                     for p in b["players"]]
    assert key(default) == key(owner) and default["total"] == owner["total"]
    assert default["perspective"]["source"] == "owner_default"


def test_team_value_waiver_checks_use_each_teams_own_recommendations(league):
    con = connect(league.path, read_only=True)
    for tid in TEAMS:
        body = league.api.get("/api/my-team/value", params={"team_id": tid, "trades": False}).json()
        assert body["team"]["team_id"] == tid
        roster = _roster_ids(con, tid)
        drops = {w["drop_id"] for w in body["upgrades"]["waiver_checks"] if w["drop_id"] is not None}
        assert drops <= roster
    con.close()


@pytest.mark.parametrize("path", ["/api/overview", "/api/waivers", "/api/start-sit", "/api/matchup",
                                  "/api/my-team/value", "/api/trades/discover", "/api/playoff-race",
                                  "/api/schedule-intelligence", "/api/luck-lab"])
def test_unknown_team_fails_safely(league, path):
    res = league.api.get(path, params={"team_id": 99})
    assert res.status_code == 404
    assert "99" in res.json()["detail"]


def test_every_team_scoped_endpoint_answers_for_every_team(league):
    for tid in TEAMS:
        for path in ("/api/start-sit", "/api/matchup", "/api/schedule-intelligence", "/api/playoff-race"):
            res = league.api.get(path, params={"team_id": tid})
            assert res.status_code == 200, (path, tid, res.text)
        assert league.api.get("/api/start-sit", params={"team_id": tid}).json()["team"]["id"] == tid
        assert league.api.get("/api/matchup", params={"team_id": tid}).json()["me"]["team_id"] == tid


def test_trade_discovery_works_from_both_sides_and_never_shares_a_cached_search(league):
    params = {"mode": "all", "shapes": "1-for-1", "limit": 50}
    seen = {}
    for tid in TEAMS + [OWNER]:                   # the owner again last: served from cache, still its own anchor
        body = league.api.get("/api/trades/discover", params={**params, "team_id": tid}).json()
        assert body["anchor"]["team_id"] == tid and body["query"]["team_id"] == tid
        assert all(r["anchor_team_id"] == tid and r["opponent_team_id"] != tid for r in body["results"])
        rows = [(r["opponent_team_id"], tuple(r["sends"]), tuple(r["receives"])) for r in body["results"]]
        if tid in seen:
            assert rows == seen[tid]
        seen[tid] = rows
        if not body["results"]:
            continue
        r = body["results"][0]
        # The same trade from the partner's side: the manual analyzer with the teams swapped agrees on both gains.
        swapped = league.api.post("/api/trades/evaluate", json={
            "team_a_id": r["opponent_team_id"], "team_b_id": tid,
            "team_a_sends": r["receives"], "team_b_sends": r["sends"]}).json()
        assert swapped["team_a"]["deltas"]["ros_delta"] == r["theirs"]["ros_delta"]
        assert swapped["team_b"]["deltas"]["ros_delta"] == r["mine"]["ros_delta"]
    assert len({tuple(v) for v in seen.values()}) > 1


def test_legacy_database_without_per_team_rows_keeps_the_owner_default_and_refuses_other_teams(league):
    con = connect(league.path)
    con.execute("DROP TABLE team_waiver_recommendations")
    con.close()
    assert league.api.get("/api/waivers").status_code == 200
    assert league.api.get("/api/waivers", params={"team_id": OWNER}).status_code == 200
    res = league.api.get("/api/waivers", params={"team_id": 1})
    assert res.status_code == 503 and "other teams" in res.json()["detail"]
    tv = league.api.get("/api/my-team/value", params={"team_id": 1, "trades": False}).json()
    assert tv["upgrades"]["waiver_checks"] == []                        # the owner's rows are never shown for team 1


def test_empty_roster_team_still_gets_a_safe_view(league):
    con = connect(league.path)
    con.execute("DELETE FROM lineup_snapshots WHERE team_id = 4")
    build_waivers(con)
    con.close()
    ov = league.api.get("/api/overview", params={"team_id": 4}).json()
    assert ov["team"]["team_id"] == 4 and ov["roster"] == [] and ov["injuries"] == []
    assert ov["perspective"]["roster_synced_at"] is None
    w = league.api.get("/api/waivers", params={"team_id": 4, "limit": 360}).json()
    assert w["players"] and all(p["suggested_drop_id"] is None for p in w["players"])
    assert any("Roster snapshot is missing" in risk for p in w["players"]
               for risk in p["components"]["decision"].get("risks", []))
    assert league.api.get("/api/start-sit", params={"team_id": 4}).status_code == 200


def test_roster_changed_after_the_analysis_is_flagged(league):
    con = connect(league.path)
    moved = sorted(_roster_ids(con, 1))[0]
    row = fetch_dicts(con, "SELECT espn_player_id FROM team_waiver_recommendations WHERE team_id = 1 LIMIT 1")[0]
    con.execute("UPDATE team_waiver_recommendations SET suggested_drop_id = ? WHERE team_id = 1 AND espn_player_id = ?",
                [moved, row["espn_player_id"]])
    con.close()
    fresh = league.api.get("/api/waivers", params={"team_id": 1, "limit": 360}).json()
    assert fresh["roster_newer_than_analysis"] is False
    assert next(p for p in fresh["players"] if p["suggested_drop_id"] == moved)["drop_on_roster"] is True
    # A later ESPN roster sync (no analytics rerun) moved that player off team 1.
    con = connect(league.path)
    con.execute("UPDATE lineup_snapshots SET team_id = 3 WHERE team_id = 1 AND espn_player_id = ?", [moved])
    con.execute("UPDATE lineup_snapshots SET synced_at = now()::TIMESTAMP + INTERVAL 1 DAY")
    con.close()
    w = league.api.get("/api/waivers", params={"team_id": 1, "limit": 360}).json()
    assert w["roster_newer_than_analysis"] is True
    assert next(p for p in w["players"] if p["suggested_drop_id"] == moved)["drop_on_roster"] is False
    ov = league.api.get("/api/overview", params={"team_id": 1}).json()
    assert moved not in {p["espn_player_id"] for p in ov["roster"]}    # the roster itself is read live

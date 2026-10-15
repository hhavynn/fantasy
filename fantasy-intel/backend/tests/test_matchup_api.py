"""Weekly matchup API: default team/week, explicit weeks, status rule, byes and unscheduled playoff weeks,
incomplete projections keeping NULL, past weeks without stored projections, set-lineup snapshots and
read-only behaviour. Built on the ESPN fixture league (4 teams, current week 3, my team is team 2) with a
rewritten round-robin schedule; every number is an invented test input."""

import json

import pytest
from fastapi.testclient import TestClient

from app.analytics.matchup import matchup_status
from app.db.connection import connect, fetch_dicts

import synthetic_race as sr
from espn_fixture import SEASON
from test_trade_data import synced

MY_ID, CURRENT_WEEK = 2, 3


def set_schedule(con, matchup_periods: dict | None, length: int | None = 1):
    """Store ESPN's scheduleSettings (matchup period -> scoring weeks) in the league settings."""
    sched = {} if matchup_periods is None else {"matchupPeriods": matchup_periods}
    if length is not None:
        sched["matchupPeriodLength"] = length
    con.execute("UPDATE leagues SET settings_raw = ?", [json.dumps({"scheduleSettings": sched})])


@pytest.fixture
def league(tmp_path):
    """(db_path, writable connection): periods 1-2 final, 3 current and unplayed, 4-14 ahead."""
    db_path = tmp_path / "matchup.duckdb"
    con = synced(db_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    pairs = sr.round_robin([1, 2, 3, 4], 14)
    con.execute("DELETE FROM matchups")
    mid = 0
    for p in range(1, 15):
        for h, a in pairs[p]:
            mid += 1
            if p <= 2:
                con.execute("INSERT INTO matchups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NONE')",
                            [lid, SEASON, mid, p, h, a, 110.0 + h, 90.0 + a, "HOME"])
            else:
                con.execute("INSERT INTO matchups VALUES (?, ?, ?, ?, ?, ?, 0.0, 0.0, 'UNDECIDED', 'NONE')",
                            [lid, SEASON, mid, p, h, a])
    set_schedule(con, {str(w): [w] for w in range(1, 18)})
    # the fixture only snapshots period 1: reuse it for the final week 2 and the current week 3
    con.execute("DELETE FROM lineup_snapshots WHERE scoring_period IN (2, 3)")
    for period, with_points in ((2, True), (3, False)):
        con.execute("""INSERT INTO lineup_snapshots
                       SELECT league_id, season, ?, team_id, espn_player_id, lineup_slot_id, lineup_slot, is_starter,
                              acquisition_type, acquisition_date, injury_status, ?, projected_points, synced_at
                       FROM lineup_snapshots WHERE scoring_period = 1""", [period, 12.5 if with_points else 0.0])
    return db_path, con, lid


@pytest.fixture
def serve(monkeypatch):
    """Close the writable connection and serve the database through the API."""
    def _serve(league):
        db_path, con, _ = league
        con.close()
        monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
        from app.main import app
        return TestClient(app)
    return _serve


@pytest.fixture
def api(league, serve):
    return serve(league)


def opponent_of(week, team=MY_ID):
    for h, a in sr.round_robin([1, 2, 3, 4], 14)[week]:
        if team in (h, a):
            return a if h == team else h


def test_default_is_my_team_and_the_current_week(api):
    body = api.get("/api/matchup").json()
    assert body["week"] == CURRENT_WEEK and body["league"]["current_week"] == CURRENT_WEEK
    assert body["me"]["team_id"] == MY_ID and body["opponent"]["team_id"] == opponent_of(CURRENT_WEEK)
    assert body["weeks_available"] == list(range(1, 15)) and body["phase"] == "regular"
    assert body["status"] == "upcoming" and not body["is_bye_week"] and body["matchup_id"]
    me = body["me"]
    assert me["name"] == "Team 2" and me["record"] == {"wins": 2, "losses": 1, "ties": 0}
    assert me["standing"] == 2 and body["opponent"]["standing"] in (1, 3, 4)
    assert me["projected_points"] > 0
    assert me["projection_complete"] and me["projected_points"] == me["known_points"]
    assert me["actual_points"] is None and body["opponent"]["actual_points"] is None
    assert body["notes"] == []


def test_projected_lineup_is_the_stored_optimal_lineup_with_player_context(api, league):
    _, _, lid = league
    me = api.get("/api/matchup").json()["me"]
    assert me["lineup"], "stored assignment is returned"
    for slot in me["lineup"]:
        assert set(slot) == {"slot", "player_id", "name", "position", "pro_team", "points", "is_flex",
                             "injury_status", "on_bye"}
    filled = [s for s in me["lineup"] if s["player_id"] is not None]
    assert filled and all(s["name"] and s["points"] is not None for s in filled)
    assert round(sum(s["points"] for s in filled), 1) == me["projected_points"]
    assert not {s["player_id"] for s in me["lineup"]} & {b["player_id"] for b in me["bench"]}


def test_the_set_lineup_comes_from_snapshots_and_has_no_points_before_the_games(api):
    me = api.get("/api/matchup").json()["me"]
    assert me["actual_lineup"] and all(p["points"] is None for p in me["actual_lineup"])
    assert all(p["projected_points"] is not None for p in me["actual_lineup"])
    assert {p["slot"] for p in me["actual_bench"]} <= {"BE", "IR"}
    assert api.get("/api/matchup", params={"week": 5}).json()["me"]["actual_lineup"] is None   # nothing synced ahead


def test_a_final_past_week_has_scores_a_set_lineup_and_no_stored_projection(api):
    body = api.get("/api/matchup", params={"week": 2}).json()
    assert body["status"] == "final" and body["phase"] == "regular"
    opp = body["opponent"]
    assert opp["team_id"] == opponent_of(2)
    home, away = next((h, a) for h, a in sr.round_robin([1, 2, 3, 4], 14)[2] if MY_ID in (h, a))
    expected = {home: 110.0 + home, away: 90.0 + away}
    assert (body["me"]["actual_points"], opp["actual_points"]) == (expected[MY_ID], expected[opp["team_id"]])
    for side in (body["me"], opp):
        assert side["projected_points"] is None and side["known_points"] is None
        assert not side["projection_complete"] and side["lineup"] == [] and side["bench"] == []
        assert side["actual_lineup"] and all(p["points"] == 12.5 for p in side["actual_lineup"])
    assert body["notes"] == ["No stored projection for either team in week 2 "
                             "(projections are kept only for weeks that have not been played)."]


def test_a_future_week_projects_both_teams_and_has_no_actual_points(api):
    body = api.get("/api/matchup", params={"week": 7}).json()
    assert body["status"] == "upcoming" and body["me"]["actual_points"] is None
    assert body["me"]["projected_points"] > 0 and body["opponent"]["projected_points"] > 0
    assert body["me"]["lineup"] and body["opponent"]["lineup"] and body["notes"] == []


def test_in_progress_week_shows_live_points_and_a_caveat(league, serve):
    _, con, _ = league
    home = fetch_dicts(con, "SELECT home_team_id h FROM matchups WHERE matchup_period = 3 AND "
                            "(home_team_id = ? OR away_team_id = ?)", [MY_ID, MY_ID])[0]["h"]
    con.execute("UPDATE matchups SET home_points = 41.5, away_points = 12.0 WHERE matchup_period = 3")
    con.execute("UPDATE lineup_snapshots SET points = 3.25 WHERE scoring_period = 3")
    body = serve(league).get("/api/matchup").json()
    assert body["status"] == "in_progress"
    mine, theirs = (41.5, 12.0) if home == MY_ID else (12.0, 41.5)
    assert (body["me"]["actual_points"], body["opponent"]["actual_points"]) == (mine, theirs)
    assert all(p["points"] == 3.25 for p in body["me"]["actual_lineup"])
    assert any("last ESPN sync" in n for n in body["notes"])


def test_status_rule():
    undecided = {"winner": "UNDECIDED", "home_points": 0.0, "away_points": 0.0}
    assert matchup_status(None, 3, 3) == "unknown"
    assert matchup_status({**undecided, "winner": "TIE"}, 2, 3) == "final"
    assert matchup_status({**undecided, "home_points": 1.0}, 3, 3) == "in_progress"
    assert matchup_status(undecided, 3, 3) == "upcoming" and matchup_status(undecided, 9, 3) == "upcoming"
    assert matchup_status(undecided, 2, 3) == "unknown"            # played week with no result and no scores
    assert matchup_status({**undecided, "home_points": None, "away_points": None}, 4, 3) == "upcoming"


def test_a_bye_week_has_no_opponent_and_says_so(league, serve):
    _, con, _ = league
    con.execute("DELETE FROM matchups WHERE matchup_period = 6 AND (home_team_id = ? OR away_team_id = ?)",
                [MY_ID, MY_ID])
    body = serve(league).get("/api/matchup", params={"week": 6}).json()
    assert body["is_bye_week"] and body["opponent"] is None and body["matchup_id"] is None
    assert body["status"] == "unknown" and 6 not in body["weeks_available"]
    assert body["me"]["lineup"] and body["me"]["projected_points"] > 0     # your own lineup is still shown
    assert any("bye in week 6" in n for n in body["notes"])


def test_a_playoff_week_without_pairings_is_not_a_bye(api):
    body = api.get("/api/matchup", params={"week": 15}).json()
    assert body["phase"] == "playoff" and body["opponent"] is None and not body["is_bye_week"]
    assert body["status"] == "unknown" and 15 not in body["weeks_available"]
    assert body["me"]["projected_points"] > 0
    assert any("No opponent is scheduled for week 15" in n for n in body["notes"])


def test_single_week_periods_report_their_period_and_no_span(api):
    body = api.get("/api/matchup", params={"week": 4}).json()
    assert body["matchup_period"] == 4 and body["period_weeks"] is None


def test_a_two_week_playoff_period_resolves_from_either_scoring_week(league, serve):
    _, con, lid = league
    # playoff matchup period 15 covers scoring weeks 15 and 16; period 16 does not exist
    set_schedule(con, {**{str(w): [w] for w in range(1, 15)}, "15": [15, 16]})
    mid = fetch_dicts(con, "SELECT max(matchup_id) m FROM matchups")[0]["m"] + 1
    con.execute("INSERT INTO matchups VALUES (?, ?, ?, 15, ?, ?, 0.0, 0.0, 'UNDECIDED', 'WINNERS_BRACKET')",
                [lid, SEASON, mid, MY_ID, 4])
    con.execute("INSERT INTO matchups VALUES (?, ?, ?, 15, 1, 3, 0.0, 0.0, 'UNDECIDED', 'WINNERS_BRACKET')",
                [lid, SEASON, mid + 1])
    api = serve(league)
    w15 = api.get("/api/matchup", params={"week": 15}).json()
    w16 = api.get("/api/matchup", params={"week": 16}).json()
    for body, week in ((w15, 15), (w16, 16)):
        assert body["week"] == week and body["matchup_period"] == 15 and body["period_weeks"] == [15, 16]
        assert body["opponent"]["team_id"] == 4 and body["matchup_id"] == mid
        assert body["phase"] == "playoff" and not body["is_bye_week"] and body["status"] == "upcoming"
        assert any("whole matchup period (weeks 15–16)" in n for n in body["notes"])
    assert {15, 16} <= set(w15["weeks_available"]) and 17 not in w15["weeks_available"]
    # projections stay keyed by the scoring week
    assert w15["me"]["projected_points"] != w16["me"]["projected_points"]
    # week 17 has no pairing: no opponent, not a bye
    w17 = api.get("/api/matchup", params={"week": 17}).json()
    assert w17["opponent"] is None and w17["matchup_period"] is None and not w17["is_bye_week"]


def test_a_period_in_progress_scores_the_whole_period_but_not_the_unplayed_week(league, serve):
    _, con, lid = league
    set_schedule(con, {**{str(w): [w] for w in range(1, 15)}, "15": [15, 16]})
    con.execute("INSERT INTO matchups VALUES (?, ?, 900, 15, ?, 4, 61.5, 40.0, 'UNDECIDED', 'WINNERS_BRACKET')",
                [lid, SEASON, MY_ID])
    con.execute("UPDATE leagues SET current_week = 16")
    con.execute("DELETE FROM lineup_snapshots WHERE scoring_period = 16")
    con.execute("""INSERT INTO lineup_snapshots
                   SELECT league_id, season, 16, team_id, espn_player_id, lineup_slot_id, lineup_slot, is_starter,
                          acquisition_type, acquisition_date, injury_status, 9.0, projected_points, synced_at
                   FROM lineup_snapshots WHERE scoring_period = 1""")
    body = serve(league).get("/api/matchup", params={"week": 16}).json()
    assert body["status"] == "in_progress"
    assert (body["me"]["actual_points"], body["opponent"]["actual_points"]) == (61.5, 40.0)


def test_missing_schedule_settings_fall_back_to_week_equals_period_with_a_note(league, serve):
    _, con, _ = league
    con.execute("UPDATE leagues SET settings_raw = NULL")
    body = serve(league).get("/api/matchup", params={"week": 4}).json()
    assert body["matchup_period"] == 4 and body["opponent"]["team_id"] == opponent_of(4)
    assert any("matchup period schedule is not in the synced settings" in n for n in body["notes"])


def test_incomplete_projection_keeps_projected_null_and_adds_a_note(league, serve):
    _, con, _ = league
    opp = opponent_of(4)
    victim = fetch_dicts(con, "SELECT assignment FROM team_week_lineup_projections WHERE team_id = ? AND week = 4",
                         [opp])[0]["assignment"]
    pid = json.loads(victim)[0]["player_id"]
    con.execute("""UPDATE team_week_lineup_projections SET projection_complete = false, projected_lineup_points = NULL,
                   missing_projection_ids = ? WHERE team_id = ? AND week = 4""", [json.dumps([pid]), opp])
    body = serve(league).get("/api/matchup", params={"week": 4}).json()
    side = body["opponent"]
    assert side["projected_points"] is None and side["known_points"] is not None
    assert side["projection_complete"] is False
    assert body["me"]["projected_points"] is not None and body["me"]["projection_complete"]
    note = next(n for n in body["notes"] if "projection incomplete" in n)
    assert side["name"] in note and "not a full-lineup projection" in note


def test_explicit_team_and_errors(api):
    other = api.get("/api/matchup", params={"team_id": 3, "week": 4}).json()
    assert other["me"]["team_id"] == 3 and other["opponent"]["team_id"] == opponent_of(4, 3)
    assert api.get("/api/matchup", params={"team_id": 99}).status_code == 404
    for bad in (0, -1, 18, 99):
        assert api.get("/api/matchup", params={"week": bad}).status_code == 422
    assert api.get("/api/matchup", params={"week": "abc"}).status_code == 422
    assert api.get("/api/matchup", params={"week": 17}).status_code == 200


def test_no_league_is_a_503(tmp_path, monkeypatch):
    from app.db.connection import init_db
    db_path = tmp_path / "empty.duckdb"
    con = connect(db_path)
    init_db(con)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    assert TestClient(app).get("/api/matchup").status_code == 503


def test_the_endpoint_is_read_only(league, serve):
    db_path, con, _ = league
    tables = ("matchups", "fantasy_teams", "team_week_lineup_projections", "lineup_snapshots", "fantasy_players")
    before = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables}
    api = serve(league)
    mtime = db_path.stat().st_mtime_ns
    for week in (2, 3, 7, 15):
        assert api.get("/api/matchup", params={"week": week}).status_code == 200
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == before
    con.close()

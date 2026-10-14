"""V2 lineup/trade engine against a database: evaluation start week, league state + replacement pool,
derived team lineup tables, the v6 migration, score isolation and the read-only API."""

import copy
import json
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.analytics import pipeline
from app.analytics.pipeline import run_analytics
from app.analytics.team_lineups import replacement_market
from app.analytics.trade_data import build_team_lineup_projections, evaluation_start_week, load_league_state
from app.analytics.trades import evaluate_trade
from app.db.connection import SCHEMA_VERSION, connect, fetch_dicts, init_db, open_db, table_exists, upsert_rows
from app.espn.sync import run_espn_sync

from espn_fixture import CURRENT, FREE_AGENTS, MY_SWID, ROSTERS, SEASON, FakeEspnClient, projection_payload
from test_espn_projections import _analytics_snapshot
from test_external_usage import SEASON as NFL_SEASON, _snapshot, nfl  # noqa: F401  (nfl is a fixture)

FINAL = 17
ROSTERED = {pid for entries in ROSTERS.values() for pid, _ in entries}


def schedule(con, week_scores: dict[int, list[tuple]]):
    """week -> [(home_score, away_score)] REG games."""
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR,
                   home_team VARCHAR, away_team VARCHAR, home_score INT, away_score INT)""")
    for week, games in week_scores.items():
        for i, (h, a) in enumerate(games):
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, ?)",
                        [SEASON, week, f"H{i}", f"A{i}", h, a])


def synced(db_path, client=None, analytics=True):
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=client or FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    if analytics:
        run_analytics(con, SEASON)
    return con


# ----------------------------------------------------------------- evaluation start week --

def test_start_week_advances_only_when_every_current_week_game_is_final(con):
    schedule(con, {6: [(24, 17), (10, 13), (30, 27)]})
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 7 and s["current_week_completed"] is True
    assert "all 3 REG games in week 6" in s["reason"]


def test_partial_week_is_not_skipped(con):
    schedule(con, {6: [(24, 17), (None, None), (None, None)]})   # Thursday played, Sunday/Monday not
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 6 and s["current_week_completed"] is False
    assert "1 of 3" in s["reason"]


def test_unknown_completion_defaults_to_current_week_with_reason(con):
    s = evaluation_start_week(con, SEASON, 6, 17)               # no schedule table at all
    assert s["evaluation_start_week"] == 6 and s["current_week_completed"] is None and "unavailable" in s["reason"]
    schedule(con, {5: [(1, 2)]})                                 # table exists, week 6 absent
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 6 and "no REG games" in s["reason"]


def test_start_week_without_score_columns_is_unknown(con):
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (?, 6, 'REG', 'KC', 'BUF')", [SEASON])
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 6 and "no score columns" in s["reason"]


def test_start_week_clamped_to_final_scoring_period(con):
    schedule(con, {17: [(20, 10)]})
    s = evaluation_start_week(con, SEASON, 17, 17)
    assert s["evaluation_start_week"] == 17 and "clamped" in s["reason"]


# ---------------------------------------------------------------- league state + market --

def test_league_state_uses_current_rosters_and_only_fa_waiver_players(tmp_path):
    con = synced(tmp_path / "s.duckdb", analytics=False)
    st = load_league_state(con)
    con.close()
    assert st.start_week == CURRENT and "unavailable" in st.start_week_reason["reason"]
    assert st.final_week == FINAL and st.last_regular == 14
    assert {t: {p.espn_player_id for p in ps} for t, ps in st.rosters.items()} == \
        {t: {pid for pid, _ in e} for t, e in ROSTERS.items()}
    assert {p.espn_player_id for p in st.free_agents} == set(FREE_AGENTS)   # FREEAGENT + WAIVERS only
    assert not {p.espn_player_id for p in st.free_agents} & ROSTERED
    assert st.active_capacity == 12                              # 9 starters + 3 bench; IR separate
    assert next(p for p in st.rosters[2] if p.espn_player_id == 207).on_ir
    m = replacement_market(st.free_agents, st.slots, st.window(), st.projections)
    w = m["weeks"][0]["positions"]
    assert w["RB"]["best_available"]["espn_player_id"] == 209 and w["WR"]["best_available"]["espn_player_id"] == 309
    assert w["QB"]["available_count"] == 1 and w["TE"]["available_count"] == 1 and w["FLEX"]["available_count"] == 3


def test_unprojected_free_agent_counted_but_not_on_projected_frontier(tmp_path):
    weeks = range(CURRENT, FINAL + 1)
    con = synced(tmp_path / "m.duckdb", FakeEspnClient(projections={w: projection_payload(w, missing=(309,))
                                                                     for w in weeks}), analytics=False)
    st = load_league_state(con)
    con.close()
    wr = replacement_market(st.free_agents, st.slots, st.window(), st.projections)["weeks"][0]["positions"]["WR"]
    assert wr["available_count"] == 1 and wr["projected_count"] == 0 and wr["best_available"] is None


# --------------------------------------------------------------------- derived tables --

def test_team_lineup_tables_built_by_analytics(tmp_path):
    con = synced(tmp_path / "t.duckdb")
    weeks = fetch_dicts(con, "SELECT * FROM team_week_lineup_projections ORDER BY team_id, week")
    teams = fetch_dicts(con, "SELECT * FROM team_ros_lineup_projections ORDER BY team_id")
    assert len(teams) == 4 and len(weeks) == 4 * (FINAL - CURRENT + 1)
    t2 = next(t for t in teams if t["team_id"] == 2)
    assert t2["evaluation_start_week"] == CURRENT and t2["final_week"] == FINAL
    assert t2["ros_weeks_expected"] == t2["ros_weeks_complete"] == FINAL - CURRENT + 1
    assert t2["ros_projected_starting_points"] == t2["ros_known_starting_points"]
    assert abs(t2["regular_season_projected_starting_points"] + t2["playoff_projected_starting_points"]
               - t2["ros_projected_starting_points"]) < 0.02
    w = next(w for w in weeks if w["team_id"] == 2 and w["week"] == CURRENT)
    assignment = json.loads(w["assignment"])
    assert len(assignment) == 9 and all("name" in a for a in assignment)
    assert w["projection_complete"] and w["phase"] == "regular"
    con.close()


def test_missing_rostered_projection_makes_team_rows_incomplete(tmp_path):
    client = FakeEspnClient(projections={8: projection_payload(8, missing=(204,))})   # team 2 RB, NFL team PHI
    con = synced(tmp_path / "i.duckdb", client)
    t2 = fetch_dicts(con, "SELECT * FROM team_ros_lineup_projections WHERE team_id = 2")[0]
    assert t2["ros_projected_starting_points"] is None and t2["ros_known_starting_points"] is not None
    assert json.loads(t2["missing_weeks"]) == [8] and json.loads(t2["missing_players"]) == [204]
    assert t2["playoff_projected_starting_points"] is not None   # weeks 15-17 are complete
    w8 = fetch_dicts(con, "SELECT * FROM team_week_lineup_projections WHERE team_id = 2 AND week = 8")[0]
    assert w8["projected_lineup_points"] is None and json.loads(w8["missing_projection_ids"]) == [204]
    con.close()


def test_v5_database_migrates_to_v6_preserving_raw_data(tmp_path):
    con = connect(tmp_path / "v5.duckdb")
    init_db(con)
    con.execute("DROP TABLE team_week_lineup_projections")
    con.execute("DROP TABLE team_ros_lineup_projections")
    con.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
    con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, 1, 7, NULL, 6.0, NULL)")
    con.execute("INSERT INTO espn_projection_history VALUES ('2026-10-01', 2026, 1, 7, 6.0, NULL)")
    con.execute("INSERT INTO player_ros_projections (season, espn_player_id) VALUES (2026, 1)")
    con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id) VALUES (1, 2026, 6, 1, 1)")
    con.execute("INSERT INTO external_player_map (provider, source_player_key, gsis_id) VALUES ('p', 'k', 'G1')")
    con.execute("INSERT INTO player_scores (season, gsis_id) VALUES (2026, 'G1')")
    init_db(con)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "6" == str(SCHEMA_VERSION)
    assert table_exists(con, "team_week_lineup_projections") and table_exists(con, "team_ros_lineup_projections")
    for t in ("espn_player_weekly_points", "espn_projection_history", "player_ros_projections", "lineup_snapshots",
              "external_player_map", "player_scores"):
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 1, t
    con.close()


# --------------------------------------------------------------------- score isolation --

RAW = {"lineup_snapshots": "team_id, espn_player_id, scoring_period", "fantasy_players": "espn_player_id",
       "leagues": "league_id", "espn_player_weekly_points": "espn_player_id, scoring_period",
       "espn_projection_history": "espn_player_id, scoring_period", "player_ros_projections": "espn_player_id",
       "fantasy_teams": "team_id", "transactions": "transaction_id"}


def _raw_snapshot(con):
    return {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY {o}") for t, o in RAW.items()}


def test_lineup_and_trade_engine_never_change_existing_analytics_or_espn_state(tmp_path, monkeypatch):
    db_path = tmp_path / "iso.duckdb"
    synced(db_path).close()
    before, raw_before = _analytics_snapshot(db_path), None
    con = connect(db_path)
    raw_before = _raw_snapshot(con)
    build_team_lineup_projections(con, SEASON)
    st = load_league_state(con)
    st_copy = copy.deepcopy(st.rosters)
    evaluate_trade(st, 2, 1, [304], [301, 302])
    evaluate_trade(st, 1, 2, [201], [205])
    assert st.rosters == st_copy
    assert _raw_snapshot(con) == raw_before
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    assert api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                  "team_b_sends": [301, 302]}).status_code == 200
    assert _analytics_snapshot(db_path) == before
    con = connect(db_path)
    assert _raw_snapshot(con) == raw_before
    con.close()


def test_existing_analytics_identical_with_and_without_the_lineup_step(tmp_path, monkeypatch):
    synced(tmp_path / "a.duckdb").close()
    monkeypatch.setattr(pipeline, "build_team_lineup_projections", lambda con, season: {"skipped": "test"})
    synced(tmp_path / "b.duckdb").close()
    a, b = _analytics_snapshot(tmp_path / "a.duckdb"), _analytics_snapshot(tmp_path / "b.duckdb")
    assert a["waiver_recommendations"] and a == b
    con = connect(tmp_path / "b.duckdb")
    assert con.execute("SELECT count(*) FROM team_ros_lineup_projections").fetchone()[0] == 0
    con.close()


def test_scores_signals_sustainability_and_context_unchanged_by_lineup_engine(nfl):
    con = nfl
    before = _snapshot(con)
    assert before[1] and any(s["market_signal"] for s in before[1].values())
    upsert_rows(con, "leagues", [{"league_id": 1, "season": NFL_SEASON, "current_week": 4, "final_scoring_period": 6,
                                  "reg_season_matchups": 5, "roster_slots": {"WR": 1, "RB": 1, "BE": 1},
                                  "settings_raw": {}, "synced_at": datetime(2026, 1, 1)}], ["league_id", "season"])
    upsert_rows(con, "fantasy_teams", [{"league_id": 1, "season": NFL_SEASON, "team_id": t, "name": f"T{t}"}
                                       for t in (1, 2)], ["league_id", "season", "team_id"])
    for pid, team, pos in ((301, 1, "WR"), (201, 1, "RB"), (302, 2, "WR"), (202, 2, "RB")):
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, availability_status) "
                    "VALUES (?, ?, ?, 'KC', 'ONTEAM')", [pid, f"P{pid}", pos])
        con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, "
                    "lineup_slot_id) VALUES (1, ?, 4, ?, ?, 20)", [NFL_SEASON, team, pid])
        for w in (4, 5, 6):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, NULL)",
                        [NFL_SEASON, pid, w, 10.0 + pid % 7])
    assert build_team_lineup_projections(con, NFL_SEASON)["teams"] == 2
    res = evaluate_trade(load_league_state(con, NFL_SEASON), 1, 2, [301], [302])
    assert res["result"]["label"] != "INCOMPLETE_DATA"
    assert _snapshot(con) == before   # usage, scores, market signal, production_sustainability, context


def test_existing_scoring_modules_never_import_the_trade_engine():
    from pathlib import Path

    import app.analytics.manager_loader as ml
    import app.analytics.manager_metrics as mm
    import app.analytics.player_scores as ps
    import app.analytics.waivers as wv
    for mod in (ps, wv, mm, ml):
        src = Path(mod.__file__).read_text()
        for name in ("team_lineups", "from .trades", "trade_data", "team_week_lineup_projections",
                     "team_ros_lineup_projections"):
            assert name not in src, (mod.__name__, name)


# ---------------------------------------------------------------------------------- API --

def test_trade_api_endpoints(tmp_path, monkeypatch):
    db_path = tmp_path / "api.duckdb"
    synced(db_path).close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)

    opts = api.get("/api/trades/options").json()
    assert next(t for t in opts["teams"] if t["team_id"] == 2)["is_mine"] is True
    assert {p["espn_player_id"] for p in next(t for t in opts["teams"] if t["team_id"] == 1)["roster"]} == \
        {pid for pid, _ in ROSTERS[1]}
    assert opts["evaluation"]["start_week"] == CURRENT and opts["active_capacity"] == 12

    res = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                 "team_b_sends": [301, 302], "start_week": None})
    assert res.status_code == 200
    body = res.json()
    assert body["trade"]["shape"] == "1-for-2" and body["projection_source"] == "ESPN"
    assert body["team_a"]["team_id"] == 2 and body["team_b"]["team_id"] == 1
    assert body["team_b"]["roster"]["open_active_roster_spots"] >= 1
    assert body["result"]["label"] in ("MUTUAL_PROJECTED_GAIN", "TEAM_A_GAIN_TEAM_B_LOSS", "TEAM_A_LOSS_TEAM_B_GAIN",
                                       "BOTH_LOSE", "TEAM_A_GAIN_TEAM_B_UNCHANGED", "TEAM_A_UNCHANGED_TEAM_B_GAIN",
                                       "TEAM_A_LOSS_TEAM_B_UNCHANGED", "TEAM_A_UNCHANGED_TEAM_B_LOSS",
                                       "NO_PROJECTED_CHANGE")
    assert len(body["team_a"]["weekly"]) == FINAL - CURRENT + 1
    assert body["team_a"]["lineups"]["before"][0]["assignment"][0]["name"]
    assert {p["espn_player_id"] for p in body["players"]} == {304, 301, 302}
    assert all(p["intelligence"]["full_name"] for p in body["players"])
    assert set(body["manager_context"]) == {"2", "1"}
    assert any("undroppable" in x for x in body["limitations"])
    assert "301" in body["names"]

    bad = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [301],
                                                 "team_b_sends": [304]})
    assert bad.status_code == 422 and "not on team 2" in bad.json()["detail"]
    early = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                   "team_b_sends": [301], "start_week": 1})
    assert early.status_code == 422
    late = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                  "team_b_sends": [301], "start_week": 16})
    assert late.json()["evaluation"]["weeks"] == [16, 17]

    lp = api.get("/api/lineup-projections").json()
    assert len(lp["teams"]) == 4 and lp["evaluation"]["start_week"] == CURRENT
    assert [g["group"] for g in lp["replacement_market"]["groups"]] == ["QB", "RB", "WR", "TE", "K", "D/ST", "FLEX"]

    team = api.get("/api/teams/2/lineup-projection").json()
    assert len(team["weeks"]) == FINAL - CURRENT + 1
    assert {m["espn_player_id"] for m in team["marginal_values"]} == {pid for pid, _ in ROSTERS[2]}
    assert api.get("/api/teams/99/lineup-projection").status_code == 404

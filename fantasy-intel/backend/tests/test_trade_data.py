"""V2 lineup/trade engine against a database: evaluation start week, league state + replacement pool,
derived team lineup tables, the v6 migration, score isolation and the read-only API."""

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analytics import pipeline
from app.analytics.pipeline import run_analytics
from app.analytics.team_lineups import replacement_market
from app.analytics.trade_data import (build_team_lineup_projections, evaluation_start_week, kickoff_utc,
                                      load_league_state)
from app.analytics.team_lineups import IR_HOLD_NOTE
from app.analytics.trades import NoRemainingWeeksError, TradeError, evaluate_trade
from app.db.connection import (SCHEMA_VERSION, connect, fetch_dicts, init_db, open_db, table_columns, table_exists,
                               upsert_rows)
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
# Week 6 of the synthetic schedule: Thu 2026-10-15 20:15 ET, Sun 10-18 13:00 and 16:25 ET, Mon 10-19 20:15 ET
# (EDT = UTC-4, so Thursday kickoff is 2026-10-16 00:15 UTC). Week 7 starts Thu 2026-10-22.

WEEK6 = [("2026-10-15", "20:15"), ("2026-10-18", "13:00"), ("2026-10-18", "16:25"), ("2026-10-19", "20:15")]
WEEK7 = [("2026-10-22", "20:15"), ("2026-10-25", "13:00")]
THU6_KICKOFF = datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def kickoffs(con, weeks: dict[int, list[tuple]]):
    """week -> [(gameday, gametime[, home_score, away_score])] REG games, nflverse column names/types."""
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR,
                   home_score INT, away_score INT)""")
    for week, games in weeks.items():
        for i, g in enumerate(games):
            day, tm, h, a = (tuple(g) + (None, None))[:4]
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, ?, ?, ?, ?)",
                        [SEASON, week, f"{SEASON}_{week:02d}_{i}", day, tm, f"H{i}", f"A{i}", h, a])


def test_start_week_advances_only_when_every_current_week_game_is_final(con):
    schedule(con, {6: [(24, 17), (10, 13), (30, 27)]})
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 7 and s["current_week_completed"] is True
    assert s["current_week_status"] == "completed" and "all 3 REG games of week 6 are final" in s["weeks_checked"][0]["detail"]


def test_thursday_final_without_kickoff_columns_still_skips_the_started_week(con):
    # Thursday played, Sunday/Monday not: some week-6 lineups are locked, so a new trade starts week 7.
    # (Before this fix the whole week 6 was evaluated.) Score-only schedule: the final score is the evidence.
    schedule(con, {6: [(24, 17), (None, None), (None, None)]})
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["current_week_status"] == "in_progress" and s["current_week_kicked_off"] is True
    assert s["current_week_completed"] is False and s["evaluation_start_week"] == 7


def test_unknown_completion_defaults_to_current_week_with_reason(con):
    s = evaluation_start_week(con, SEASON, 6, 17)               # no schedule table at all
    assert s["evaluation_start_week"] == 6 and s["current_week_completed"] is None and "unavailable" in s["reason"]
    assert s["uncertain"] is True and "could not be established" in s["warning"]
    schedule(con, {5: [(1, 2)]})                                 # table exists, week 6 absent
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 6 and "no REG games" in s["reason"] and s["uncertain"] is True


def test_start_week_without_score_or_kickoff_columns_is_uncertain(con):
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (?, 6, 'REG', 'KC', 'BUF')", [SEASON])
    s = evaluation_start_week(con, SEASON, 6, 17)
    assert s["evaluation_start_week"] == 6 and s["uncertain"] is True and "no gameday/gametime" in s["reason"]


def test_current_week_before_first_kickoff_is_included(con):
    kickoffs(con, {6: WEEK6, 7: WEEK7})
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 15, 23, 0))     # Thu 19:00 ET
    assert s["evaluation_start_week"] == 6 and s["current_week_status"] == "not_started"
    assert s["current_week_kicked_off"] is False and s["uncertain"] is False and s["warning"] is None
    assert s["weeks_checked"][0]["first_kickoff"] == THU6_KICKOFF.isoformat()


def test_kickoff_comparison_is_timezone_aware(con):
    kickoffs(con, {6: WEEK6, 7: WEEK7})
    # 20:15 ET is 00:15 UTC the next day; a naive ET-vs-UTC comparison would be four hours off.
    before = evaluation_start_week(con, SEASON, 6, 17, now=THU6_KICKOFF - timedelta(minutes=1))
    at = evaluation_start_week(con, SEASON, 6, 17, now=THU6_KICKOFF)
    pacific = evaluation_start_week(con, SEASON, 6, 17,
                                    now=datetime(2026, 10, 15, 17, 16, tzinfo=timezone(timedelta(hours=-7))))
    assert before["evaluation_start_week"] == 6 and at["evaluation_start_week"] == 7
    assert pacific["evaluation_start_week"] == 7                 # 17:16 PDT = 00:16 UTC, after kickoff
    with pytest.raises(ValueError):
        evaluation_start_week(con, SEASON, 6, 17, now=datetime(2026, 10, 15, 23, 0))   # naive: refused


def test_kickoff_utc_parses_nflverse_and_date_values():
    assert kickoff_utc("2026-10-15", "20:15") == THU6_KICKOFF
    assert kickoff_utc(date(2026, 12, 13), "13:00") == utc(2026, 12, 13, 18, 0)        # EST in December
    assert kickoff_utc("2026-10-15", None) is None and kickoff_utc(None, "13:00") is None
    assert kickoff_utc("", "") is None and kickoff_utc("not-a-date", "13:00") is None
    assert kickoff_utc("2026-10-15", "TBD") is None


def test_one_game_kicked_off_without_any_final_score_skips_to_next_week(con):
    kickoffs(con, {6: WEEK6, 7: WEEK7})                          # no scores anywhere
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 16, 1, 30))      # Thursday game live
    assert s["evaluation_start_week"] == 7 and s["current_week_status"] == "in_progress"
    assert s["current_week_kicked_off"] is True and s["current_week_completed"] is False
    assert s["current_week_games_final"] == 0 and s["uncertain"] is False
    assert s["weeks_checked"][0]["games_kicked_off"] == 1


def test_thursday_completed_sunday_not_played(con):
    kickoffs(con, {6: [(*WEEK6[0], 24, 17), *WEEK6[1:]], 7: WEEK7})
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 17, 16, 0))      # Saturday
    assert s["evaluation_start_week"] == 7 and s["current_week_status"] == "in_progress"
    assert s["weeks_checked"][0]["games_final"] == 1 and s["weeks_checked"][1]["status"] == "not_started"
    assert "week 6 already kicked off" in s["reason"]


def test_all_current_week_games_completed(con):
    kickoffs(con, {6: [(d, t, 20, 13) for d, t in WEEK6], 7: WEEK7})
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 20, 15, 0))      # Tuesday
    assert s["evaluation_start_week"] == 7 and s["current_week_status"] == "completed"
    assert s["current_week_completed"] is True and s["remaining_weeks"] is True


def test_missing_score_never_means_unplayed_and_zero_zero_is_not_final(con):
    kickoffs(con, {6: [(*WEEK6[0], 0, 0), *WEEK6[1:]], 7: WEEK7})
    pre = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 15, 12, 0))
    assert pre["evaluation_start_week"] == 6 and pre["weeks_checked"][0]["games_final"] == 0   # 0-0 ignored
    late = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 20, 15, 0))
    assert late["evaluation_start_week"] == 7               # kickoffs passed although no score was recorded
    assert late["current_week_status"] == "in_progress"      # never "completed" without final scores


def test_missing_or_ambiguous_kickoff_data_is_reported_as_uncertain(con):
    kickoffs(con, {6: [(WEEK6[0][0], None), *WEEK6[1:]], 7: WEEK7})   # Thursday's time missing
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 17, 12, 0))
    assert s["evaluation_start_week"] == 6 and s["current_week_status"] == "unknown"
    assert s["uncertain"] is True and s["current_week_kicked_off"] is None
    assert "1 of 4 REG games of week 6 have no usable kickoff time" in s["reason"]
    assert "may already have started" in s["warning"] and "start week 7" in s["warning"]
    # A known kickoff in the past settles it even when another game's time is missing.
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 18, 18, 0))
    assert s["evaluation_start_week"] == 7 and s["uncertain"] is False


def test_stale_espn_current_week_skips_every_started_week(con):
    kickoffs(con, {6: [(d, t, 20, 13) for d, t in WEEK6], 7: WEEK7, 8: [("2026-10-29", "20:15")]})
    s = evaluation_start_week(con, SEASON, 6, 17, now=utc(2026, 10, 23, 12, 0))      # week 7 Thursday played
    assert s["evaluation_start_week"] == 8 and "weeks 6-7 already kicked off" in s["reason"]


def test_final_fantasy_week_completed_has_no_remaining_weeks(con):
    kickoffs(con, {17: [("2026-12-31", "20:15", 20, 10), ("2027-01-03", "13:00", 27, 24)]})
    s = evaluation_start_week(con, SEASON, 17, 17, now=utc(2027, 1, 5, 12, 0))
    assert s["evaluation_start_week"] is None and s["remaining_weeks"] is False
    assert s["current_week_status"] == "completed" and "No remaining fantasy scoring periods" in s["warning"]
    past = evaluation_start_week(con, SEASON, 18, 17)            # ESPN moved past the final period
    assert past["evaluation_start_week"] is None and past["current_week_status"] == "after_final"


def test_final_fantasy_week_before_first_kickoff_is_still_actionable(con):
    kickoffs(con, {17: [("2026-12-31", "20:15"), ("2027-01-03", "13:00")]})
    s = evaluation_start_week(con, SEASON, 17, 17, now=utc(2026, 12, 31, 12, 0))
    assert s["evaluation_start_week"] == 17 and s["remaining_weeks"] is True and s["uncertain"] is False
    mid = evaluation_start_week(con, SEASON, 17, 17, now=utc(2027, 1, 2, 12, 0))     # Thursday played
    assert mid["evaluation_start_week"] is None and mid["remaining_weeks"] is False


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
    # ESPN's acquisition type is kept per free agent (trade vs. free agency separates waiver claims).
    assert set(st.free_agent_status) == set(FREE_AGENTS) and set(st.free_agent_status.values()) <= {"FREEAGENT", "WAIVERS"}
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
    con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points, synced_at) VALUES (2026, 1, 7, NULL, 6.0, NULL)")
    con.execute("INSERT INTO espn_projection_history VALUES ('2026-10-01', 2026, 1, 7, 6.0, NULL)")
    con.execute("INSERT INTO player_ros_projections (season, espn_player_id) VALUES (2026, 1)")
    con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id) VALUES (1, 2026, 6, 1, 1)")
    con.execute("INSERT INTO external_player_map (provider, source_player_key, gsis_id) VALUES ('p', 'k', 'G1')")
    con.execute("INSERT INTO player_scores (season, gsis_id) VALUES (2026, 'G1')")
    init_db(con)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
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
            con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points, synced_at) VALUES (?, ?, ?, NULL, ?, NULL)",
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
    assert body["kdst_streaming"]["enabled"] is True                 # K/D/ST valued against streaming by default
    raw = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                 "team_b_sends": [301, 302], "kdst_streaming": False}).json()
    assert raw["kdst_streaming"] == {"enabled": False, "positions": []}
    disc = api.get("/api/trades/discover?kdst_streaming=false").json()
    assert disc["kdst_streaming"]["enabled"] is False and disc["query"]["kdst_streaming"] is False
    assert "streaming baseline is off" in disc["kdst_policy"]
    assert api.get("/api/trades/discover").json()["kdst_streaming"]["enabled"] is True

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


# ------------------------------------------------- evaluation window end to end (DB + API) --
# Fixture week CURRENT=3 of 2026: Thu 09-24 20:15 ET, Sun 09-27 13:00 ET; week 4 starts Thu 10-01.

WEEK3 = [("2026-09-24", "20:15"), ("2026-09-27", "13:00")]
WEEK4 = [("2026-10-01", "20:15"), ("2026-10-04", "13:00")]
DERIVED = {"team_week_lineup_projections": "team_id, week", "team_ros_lineup_projections": "team_id"}


def _final_week_state(con, scores):
    """Move the synced league to its final scoring period (17) with week-17 rosters, and give week 17
    the schedule rows `scores` (None = not yet played)."""
    con.execute("UPDATE leagues SET current_week = ?", [FINAL])
    con.execute(f"""INSERT INTO lineup_snapshots SELECT * REPLACE ({FINAL} AS scoring_period)
                    FROM lineup_snapshots WHERE scoring_period = {CURRENT}""")
    kickoffs(con, {FINAL: [(d, t, *(sc or (None, None))) for (d, t), sc in
                           zip([("2026-12-31", "20:15"), ("2027-01-03", "13:00")], scores)]})


def test_completed_final_week_returns_no_remaining_weeks_without_touching_stored_data(tmp_path, monkeypatch):
    db_path = tmp_path / "end.duckdb"
    con = synced(db_path)
    _final_week_state(con, [(20, 10), (27, 24)])
    derived = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY {o}") for t, o in DERIVED.items()}
    raw = _raw_snapshot(con)
    st = load_league_state(con)
    assert st.start_week is None and st.window().weeks == () and st.start_week_reason["remaining_weeks"] is False
    with pytest.raises(NoRemainingWeeksError, match="No remaining fantasy scoring periods"):
        evaluate_trade(st, 2, 1, [304], [301])
    with pytest.raises(NoRemainingWeeksError):
        evaluate_trade(st, 2, 1, [304], [301], start_week=FINAL)     # no override can revive a played week
    assert build_team_lineup_projections(con, SEASON)["skipped"] == "no remaining fantasy scoring periods"
    assert _raw_snapshot(con) == raw                                  # stored projections/scores untouched
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY {o}") for t, o in DERIVED.items()} == derived
    run_analytics(con, SEASON)                                        # the full pipeline copes too
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY {o}") for t, o in DERIVED.items()} == derived
    after = _raw_snapshot(con)                                        # (player_ros_projections is the existing
    assert {t: v for t, v in after.items() if t != "player_ros_projections"} == \
        {t: v for t, v in raw.items() if t != "player_ros_projections"}   # ESPN ROS step, rebuilt as of week 17)
    con.close()

    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    opts = api.get("/api/trades/options").json()
    assert opts["evaluation"]["remaining_weeks"] is False and opts["evaluation"]["start_week"] is None
    assert opts["evaluation"]["weeks"] == [] and "No remaining" in opts["evaluation"]["warning"]
    res = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                 "team_b_sends": [301]})
    assert res.status_code == 409 and "No remaining fantasy scoring periods" in res.json()["detail"]
    team = api.get("/api/teams/2/lineup-projection").json()
    assert team["summary"] is None and team["weeks"] == [] and team["marginal_values"] == []
    lp = api.get("/api/lineup-projections").json()
    assert lp["replacement_market"] is None and lp["teams_note"] and len(lp["teams"]) == 4


def test_final_week_before_first_kickoff_is_evaluated(tmp_path):
    con = synced(tmp_path / "pre.duckdb", analytics=False)
    _final_week_state(con, [None, None])
    st = load_league_state(con, now=utc(2026, 12, 31, 20, 0))       # Thursday 15:00 ET
    res = evaluate_trade(st, 2, 1, [304], [301])
    assert st.start_week == FINAL and res["evaluation"]["weeks"] == [FINAL]
    assert res["evaluation"]["uncertain"] is False and res["evaluation"]["remaining_weeks"] is True
    assert load_league_state(con, now=utc(2027, 1, 1, 6, 0)).start_week is None   # Thursday kicked off
    con.close()


def test_started_current_week_moves_the_db_window_and_api_rejects_retroactive_override(tmp_path):
    con = synced(tmp_path / "ko.duckdb", analytics=False)
    kickoffs(con, {CURRENT: WEEK3, CURRENT + 1: WEEK4})
    st = load_league_state(con, now=utc(2026, 9, 25, 12, 0))         # Thursday night game played
    assert st.start_week == CURRENT + 1 and st.first_projection_week == CURRENT
    res = evaluate_trade(st, 2, 1, [304], [301])
    assert res["evaluation"]["weeks"][0] == CURRENT + 1 and res["evaluation"]["start_week_overridden"] is False
    with pytest.raises(TradeError, match="before the earliest actionable week"):
        evaluate_trade(st, 2, 1, [304], [301], start_week=CURRENT)
    pre = load_league_state(con, now=utc(2026, 9, 24, 12, 0))
    assert pre.start_week == CURRENT and evaluate_trade(pre, 2, 1, [304], [301])["evaluation"]["weeks"][0] == CURRENT
    con.close()


def test_ir_sensitivity_stays_separate_after_window_moves(tmp_path):
    con = synced(tmp_path / "ir.duckdb", analytics=False)
    kickoffs(con, {CURRENT: WEEK3, CURRENT + 1: WEEK4})
    st = load_league_state(con, now=utc(2026, 9, 28, 12, 0))         # week 3 under way
    assert next(p for p in st.rosters[2] if p.espn_player_id == 207).on_ir
    res = evaluate_trade(st, 2, 1, [304], [301])
    weeks = list(range(CURRENT + 1, FINAL + 1))
    sens = {x["espn_player_id"]: x for x in res["team_a"]["ir_activation_sensitivity"]["players"]}
    assert set(sens) == {207} and list(sens[207]["before_trade"]["weekly_projection"]) == weeks
    assert sens[207]["before_trade"]["assumption"] == IR_HOLD_NOTE == res["ir_assumption"]
    assert res["team_a"]["ir_activation_sensitivity"]["assumption"] == IR_HOLD_NOTE and IR_HOLD_NOTE in res["assumptions"]
    for key in ("before", "after"):
        assert all(207 not in {a["player_id"] for a in w["assignment"]} for w in res["team_a"]["lineups"][key])
    # Canonical deltas are exactly those of a league where 207 has no projection at all.
    blind = evaluate_trade(replace(st, projections={k: v for k, v in st.projections.items() if k != 207}),
                           2, 1, [304], [301])
    for side in ("team_a", "team_b"):
        assert res[side]["deltas"] == blind[side]["deltas"]
    con.close()


def test_eastern_dst_fallback_matches_the_tz_database(monkeypatch):
    import zoneinfo
    cases = [("2026-09-10", "20:20"), ("2026-11-01", "13:00"), ("2026-10-31", "23:30"), ("2026-12-27", "16:25"),
             ("2027-01-03", "13:00"), ("2026-03-08", "03:00")]
    real = [kickoff_utc(d, t) for d, t in cases]

    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    assert [kickoff_utc(d, t) for d, t in cases] == real


def test_v6_database_rebuilds_independent_projection_tables_with_participation_columns(tmp_path):
    con = connect(tmp_path / "v6.duckdb")
    init_db(con)
    # A v6 database built before the participation columns existed.
    con.execute("DROP TABLE independent_player_projections")
    con.execute("CREATE TABLE independent_player_projections (season INTEGER, week INTEGER, espn_player_id BIGINT)")
    con.execute("ALTER TABLE independent_projection_runs DROP COLUMN participation_version")
    con.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
    con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points, synced_at) VALUES (2026, 1, 7, NULL, 6.0, NULL)")
    init_db(con)
    assert {"p_play", "expected_points", "participation"} <= set(table_columns(con, "independent_player_projections"))
    assert "participation_version" in table_columns(con, "independent_projection_runs")
    assert con.execute("SELECT count(*) FROM espn_player_weekly_points").fetchone()[0] == 1
    con.close()


def test_a_league_with_more_flex_slots_than_supported_gets_a_clean_422(tmp_path, monkeypatch):
    """31 flex slots (a provider can store any count): the exact lineup solver refuses them, and trades evaluate
    answers a fixed 422 that names no internals instead of a 500."""
    from dataclasses import replace as dc_replace

    from app.analytics.lineup import MAX_FLEX_SLOTS, UNSUPPORTED_LINEUP, lineup_slots
    from app.api import routes

    db_path = tmp_path / "flex31.duckdb"
    synced(db_path).close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    real_state = routes._state
    monkeypatch.setattr(routes, "_state", lambda con: dc_replace(
        real_state(con), slots=lineup_slots({"QB": 1, "FLEX": MAX_FLEX_SLOTS + 1})))
    from app.main import app
    res = TestClient(app).post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                             "team_b_sends": [301, 302]})
    assert res.status_code == 422
    assert res.json() == {"detail": UNSUPPORTED_LINEUP}

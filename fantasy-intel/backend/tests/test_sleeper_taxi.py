"""Sleeper taxi-squad players (#228): owned and visible, never an active starter, outside the active roster.

The synthetic Sleeper fixture league (app/leagues/providers/fixture_data/sleeper) has one taxi slot; roster 1 holds
"Taxi Rookie" (Sleeper 12500, a WR) on it, listed in `taxi` and again in `players` as Sleeper does. These tests give
him by far the best projection on his team and check that no optimizer starts him, that trade/waiver capacity
ignores the taxi slot, and that a taxi player traded or promoted becomes an ordinary active player.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone

import duckdb
import pytest

from app.analytics.availability import availability
from app.analytics.reserve_slots import active_capacity, is_reserved, is_taxi
from app.analytics.start_sit import load_start_sit_evaluation
from app.analytics.team_lineups import Window, project_team, replacement_context, roster_marginal_value, starters
from app.analytics.trade_data import load_league_state
from app.analytics.trades import evaluate_trade, roster_issues
from app.analytics.waiver_roster import plan_roster_move, roster_constraints
from app.analytics.waivers import suggest_drop, team_state
from app.db.connection import fetch_dicts, init_db
from app.sleeper.write import write_sleeper

from test_sleeper_write import FIX, payloads

TAXI = -10012500           # Sleeper 12500 has no ESPN id in the fixture map: synthetic id
IR_PLAYER = -10005850      # Sleeper 5850, in roster 1's reserve
WEEKS = range(6, 17)       # current week 6 through the final scoring period 16
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _db(p=None):
    con = duckdb.connect(":memory:")
    init_db(con)
    con.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    write_sleeper(con, p or payloads())
    return con


def _project(con, taxi_points=40.0, others=5.0):
    """Provider-style weekly projections for every rostered player: the taxi player far above everyone."""
    ids = [r["espn_player_id"] for r in fetch_dicts(con, "SELECT espn_player_id FROM fantasy_players "
                                                         "WHERE availability_status = 'ONTEAM'")]
    con.execute("DELETE FROM espn_player_weekly_points WHERE scoring_period >= 6")
    con.executemany("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, "
                    "projected_points) VALUES (2026, ?, ?, NULL, ?)",
                    [[pid, w, taxi_points if pid == TAXI else others] for pid in ids for w in WEEKS])


@pytest.fixture
def con():
    c = _db()
    _project(c)
    yield c
    c.close()


# ------------------------------------------------------------------ the league file --

def test_a_taxi_player_is_written_once_with_the_bench_id_and_the_taxi_label_and_stays_owned():
    con = _db()
    rows = fetch_dicts(con, "SELECT * FROM lineup_snapshots WHERE scoring_period = 6 AND espn_player_id = ?", [TAXI])
    assert len(rows) == 1, "Sleeper lists a taxi player in `taxi` and in `players`: one row, the taxi one"
    r = rows[0]
    assert (r["team_id"], r["lineup_slot_id"], r["lineup_slot"], r["is_starter"]) == (1, 20, "TAXI", False)
    fp = fetch_dicts(con, "SELECT * FROM fantasy_players WHERE espn_player_id = ?", [TAXI])[0]
    assert (fp["availability_status"], fp["on_team_id"]) == ("ONTEAM", 1), "taxi is owned, never a free agent"
    assert fetch_dicts(con, "SELECT roster_slots FROM leagues")[0]["roster_slots"].count('"TAXI": 1') == 1


def test_reserve_slot_rules():
    assert is_taxi({"lineup_slot": "TAXI", "lineup_slot_id": 20}) and is_taxi({"on_taxi": True})
    assert not is_taxi({"lineup_slot": "BE", "lineup_slot_id": 20}) and not is_taxi({"lineup_slot_id": 20})
    assert is_reserved({"lineup_slot_id": 21}) and is_reserved({"lineup_slot": "TAXI"})
    assert not is_reserved({"lineup_slot": "BE", "lineup_slot_id": 20})
    # ESPN slot counts: unchanged (everything but IR); Sleeper's taxi slots are not active capacity either
    espn = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 7, "IR": 1}
    assert active_capacity(espn) == sum(v for k, v in espn.items() if k != "IR") == 16
    assert active_capacity({**espn, "TAXI": 3}) == 16
    assert availability("ACTIVE", 20) == {"status": "AVAILABLE", "weight": 1.0, "sources": ["ESPN ACTIVE"]}
    assert availability("ACTIVE", 20, taxi=True)["weight"] == 0.0


# ------------------------------------------------------------- trade / lineup engine --

def test_the_trade_state_keeps_the_taxi_player_owned_but_outside_the_active_roster(con):
    st = load_league_state(con, source="espn")
    me = {p.espn_player_id: p for p in st.rosters[1]}
    assert me[TAXI].on_taxi and not me[TAXI].on_ir and me[TAXI].reserved
    assert me[IR_PLAYER].on_ir and not me[IR_PLAYER].on_taxi
    assert TAXI not in {p.espn_player_id for p in st.free_agents}
    # 10 starting slots + 3 bench; the IR and the taxi slot hold players outside the active roster
    assert st.active_capacity == 13
    issues = roster_issues(st, st.rosters[1], st.rosters[1])
    assert issues["active_before_drops"] == len(st.rosters[1]) - 2 and issues["over_capacity_by"] == 0


def test_a_high_projection_taxi_player_never_enters_the_optimal_lineup(con):
    st = load_league_state(con, source="espn")
    window = st.window()
    roster = st.rosters[1]
    team = project_team(roster, st.slots, window, st.projections)
    for w in team["weeks"]:
        assert TAXI not in starters(w)
        assert w["taxi"] == [{"player_id": TAXI, "position": "WR", "points": 40.0}]
        assert TAXI not in w["missing_projection_ids"]
    mv = roster_marginal_value(roster, TAXI, st.slots, window, st.projections, team)
    assert mv["on_taxi"] is True and mv["roster_marginal_lineup_points"] == 0.0 and mv["weeks_started"] == []
    assert replacement_context(roster, TAXI, st.free_agents, st.slots, window, st.projections, team)["replacement"] is None
    # the fixture is meaningful: the same player off the taxi squad starts every week
    promoted = [replace(p, on_taxi=False) if p.espn_player_id == TAXI else p for p in roster]
    assert all(TAXI in starters(w) for w in project_team(promoted, st.slots, window, st.projections)["weeks"])
    # teams without a taxi player keep their exact week shape (an ESPN league never has the key)
    assert all("taxi" not in w for w in project_team(st.rosters[2], st.slots, window, st.projections)["weeks"])


def test_trading_a_taxi_player_moves_him_into_an_active_spot_on_the_new_team(con):
    st = load_league_state(con, source="espn")
    theirs = sorted(p.espn_player_id for p in st.rosters[2])[0]
    res = evaluate_trade(st, 1, 2, [TAXI], [theirs])
    a, b = res["team_a"]["roster"], res["team_b"]["roster"]
    # sending an inactive taxi player for an active one: the sender's active count grows, the receiver's is unchanged
    assert (a["active_before"], a["active_after"]) == (6, 7)
    assert b["active_before"] == b["active_after"]
    assert a["active_capacity"] == b["active_capacity"] == 13
    assert res["team_b"]["deltas"]["ros_delta"] > 0, "on his new team he is active and starts"
    sent = next(p for p in res["players"] if p["espn_player_id"] == TAXI)
    assert sent["on_taxi"] is True and sent["marginal_value_on_current_team"]["roster_marginal_lineup_points"] == 0.0


# --------------------------------------------------------------------- start / sit --

def test_start_sit_never_recommends_a_taxi_player(con):
    out = load_start_sit_evaluation(con, team_id=1, source="espn", now=NOW)
    assert TAXI not in {r["player_id"] for r in out["recommended_lineup"]}
    assert TAXI not in {r["player_id"] for r in out["unconstrained_lineup"]}
    assert any(r["slot"] == "FLEX" and r["player_id"] is None for r in out["recommended_lineup"]), \
        "an empty FLEX stays empty rather than starting the taxi WR"
    bench = {r["player_id"]: r for r in out["current_bench"]}
    assert bench[TAXI]["on_taxi"] is True and bench[TAXI]["on_ir"] is False
    assert "on_taxi" not in bench[IR_PLAYER]


# ------------------------------------------------------------------------- waivers --

def _waiver_state(con):
    lg = fetch_dicts(con, "SELECT roster_slots, settings_raw FROM leagues")[0]
    rows = fetch_dicts(con, """SELECT l.espn_player_id, l.lineup_slot_id, l.lineup_slot, fp.position, fp.full_name,
                                      fp.injury_status FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
                               WHERE l.scoring_period = 6 AND l.team_id = 1 ORDER BY l.espn_player_id""")
    slots = json.loads(lg["roster_slots"])
    st = team_state(rows, {}, slots)
    return st, lg


def test_waivers_keep_taxi_out_of_the_lineup_capacity_and_the_drop_list(con):
    st, lg = _waiver_state(con)
    taxi = next(p for p in st["players"] if p["espn_player_id"] == TAXI)
    assert taxi["on_taxi"] is True and taxi["available"] is False and taxi["availability"]["status"] == "TAXI"
    assert TAXI not in {a["player_id"] for a in st["lineup"]}
    assert all("on_taxi" not in p for p in st["players"] if p["espn_player_id"] != TAXI)
    c = roster_constraints(lg)
    assert c["capacity"] == 13, "a TAXI slot count no longer makes Sleeper roster capacity unknown"
    drop = suggest_drop(st)
    assert drop is None or drop["espn_player_id"] not in (TAXI, IR_PLAYER)
    cand = {"espn_player_id": 1, "position": "WR", "full_name": "FA", "raw_value": 1.0, "value": 1.0,
            "available": True, "lineup_slot_id": None}
    # pin what this test is not about: Sleeper's raw settings carry no ESPN rosterSettings, and the IR occupant is
    # injured (an IR player with a healthy status blocks the planner, a separate rule)
    c = {**c, "settings_complete": True}
    for p in st["players"]:
        if p["espn_player_id"] == IR_PLAYER:
            p["injury_status"] = "OUT"
    move = plan_roster_move(st, cand, c)
    assert move["active_count"] == len(st["players"]) - 2 and move["status"] == "ADD_WITHOUT_DROP"
    # a full active roster: dropping the taxi (or IR) player can never make room
    full = plan_roster_move(st, cand, {**c, "capacity": move["active_count"]})
    excluded = {e["espn_player_id"]: e["reason"] for e in full["excluded"]}
    assert excluded[TAXI] == "Taxi-squad removal does not free an active spot"
    assert excluded[IR_PLAYER] == "IR removal does not free an active spot"
    assert TAXI not in {d["espn_player_id"] for d in full["alternatives"]}


# ------------------------------------------------------------ activations and old files --

def test_a_promoted_taxi_player_is_an_ordinary_bench_player_on_the_next_sync():
    p = payloads()
    r1 = next(r for r in p.rosters if r["roster_id"] == 1)
    r1["taxi"] = []                     # Sleeper activation: off the taxi squad, still in `players`
    con = _db(p)
    _project(con)
    row = fetch_dicts(con, "SELECT lineup_slot, lineup_slot_id FROM lineup_snapshots WHERE scoring_period = 6 "
                           "AND espn_player_id = ?", [TAXI])[0]
    assert (row["lineup_slot"], row["lineup_slot_id"]) == ("BE", 20)
    st = load_league_state(con, source="espn")
    assert not next(x for x in st.rosters[1] if x.espn_player_id == TAXI).on_taxi
    assert all(TAXI in starters(w) for w in project_team(st.rosters[1], st.slots, st.window(), st.projections)["weeks"])


def test_a_snapshot_without_the_taxi_label_reads_as_before(con):
    """A row whose label does not say TAXI (historic played weeks, roster_history) cannot be told from bench: it is
    read exactly as before this rule. The taxi slot count still is not active capacity."""
    con.execute("UPDATE lineup_snapshots SET lineup_slot = 'BE' WHERE espn_player_id = ?", [TAXI])
    st = load_league_state(con, source="espn")
    me = next(x for x in st.rosters[1] if x.espn_player_id == TAXI)
    assert not me.on_taxi and not me.reserved
    assert st.active_capacity == 13
    window = Window(st.window().weeks, st.last_regular)
    assert all(TAXI in starters(w) for w in project_team(st.rosters[1], st.slots, window, st.projections)["weeks"])


def test_team_value_explains_a_benched_taxi_player(con):
    from app.analytics.team_value import bench_reason
    st = load_league_state(con, source="espn")
    me = next(x for x in st.rosters[1] if x.espn_player_id == TAXI)
    week = project_team(st.rosters[1], st.slots, st.window(), st.projections)["weeks"][0]
    assert bench_reason(me, week, st.projections, None, {})["code"] == "taxi"


def test_the_overview_roster_reports_a_taxi_player_as_unavailable(tmp_path, monkeypatch):
    """/api/overview's roster availability: a taxi player is TAXI (weight 0), never AVAILABLE; others unchanged."""
    from fastapi.testclient import TestClient
    path = tmp_path / "sleeper.duckdb"
    c = duckdb.connect(str(path))
    init_db(c)
    c.execute(f"CREATE TABLE nfl_ff_playerids AS SELECT * FROM read_csv_auto('{FIX / 'ff_playerids.csv'}')")
    write_sleeper(c, payloads())
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    res = TestClient(app).get("/api/overview", params={"team_id": 1})
    assert res.status_code == 200, res.text
    roster = {p["espn_player_id"]: p for p in res.json()["roster"]}
    assert roster[TAXI]["lineup_slot"] == "TAXI"
    assert roster[TAXI]["availability"] == {"status": "TAXI", "weight": 0.0, "sources": ["Sleeper taxi squad"]}
    assert roster[IR_PLAYER]["availability"]["status"] == "IR"
    assert all(p["availability"]["status"] != "TAXI" for pid, p in roster.items() if pid != TAXI)

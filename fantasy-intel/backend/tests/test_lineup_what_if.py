"""What-if lineups on Start/Sit (issue #171, app/analytics/lineup_what_if.py, GET /api/start-sit/what-if).

A synthetic league in week 7: KC-BUF has kicked off (QB 1, RB 2 and 3, WR 5 and 6 are locked starters, RB 15 a locked
bench player), DAL-NYG has not. RB 13 is on fantasy IR with the best projection on the roster, WR 14 has no projection,
and WR 50 belongs to the other team.

    slot    recommended  proj        bench (unlocked unless noted)
    QB_0    1  (KC)      20          4  RB  DAL   9
    RB_0    2  (KC)      15          8  WR  NYG  10
    RB_1    3  (BUF)     12          10 TE  NYG   6
    WR_0    5  (KC)      14          14 WR  NYG  no projection
    WR_1    6  (BUF)     13          15 RB  BUF   4  (locked)
    TE_0    9  (DAL)      8          13 RB  DAL  30  (IR)
    FLEX_0  7  (DAL)     11
    K_0     11 (NYG)      7
    D/ST_0  12 (NYG)      5          recommended total 105
"""
from datetime import datetime, timezone

import duckdb
import pytest

from app.analytics.lineup_what_if import (
    DUPLICATE, INELIGIBLE, INELIGIBLE_SWAP, LOCKED_PLAYER, LOCKED_SLOT, MAX_MOVES, MAX_PARAM_LEN, NOT_ON_ROSTER,
    RESERVED, WRONG_SCOPE, ScenarioSyntaxError, evaluate_what_if, parse_scenario,
)
from app.analytics.start_sit import load_start_sit_evaluation
from app.db.connection import init_db
from test_league_scope import e2e, served  # noqa: F401  (fixtures)
from test_leagues_api import env  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)      # KC-BUF kicked off at 17:00 UTC, DAL-NYG is Monday
PLAYERS = {  # pid: (name, position, team, week-7 projection, lineup slot label, slot id)
    1: ("Quinn Arrow", "QB", "KC", 20.0, "QB", 0),
    2: ("Ray Back", "RB", "KC", 15.0, "RB", 2),
    3: ("Rob Burner", "RB", "BUF", 12.0, "RB", 2),
    5: ("Wes Wide", "WR", "KC", 14.0, "WR", 4),
    6: ("Will Wing", "WR", "BUF", 13.0, "WR", 4),
    9: ("Ted End", "TE", "DAL", 8.0, "TE", 6),
    7: ("Walt Slot", "WR", "DAL", 11.0, "FLEX", 23),
    11: ("Kip Kicker", "K", "NYG", 7.0, "K", 17),
    12: ("Giants D/ST", "D/ST", "NYG", 5.0, "D/ST", 16),
    4: ("Reggie Bench", "RB", "DAL", 9.0, "BE", 20),
    8: ("Wally Bench", "WR", "NYG", 10.0, "BE", 20),
    10: ("Tom Bench", "TE", "NYG", 6.0, "BE", 20),
    14: ("Will Unknown", "WR", "NYG", None, "BE", 20),
    15: ("Rex Locked", "RB", "BUF", 4.0, "BE", 20),
    13: ("Ira Hurt", "RB", "DAL", 30.0, "IR", 21),
}
OTHER_TEAM = {50: ("Zed Rival", "WR", "NYG", 25.0, "WR", 4)}


def _seed(con, *, gameday_kc="2026-10-18", gameday_dal="2026-10-19"):
    init_db(con)
    con.execute("""INSERT INTO leagues (league_id, season, name, size, scoring_type, current_week, final_scoring_period,
                   roster_slots, synced_at) VALUES (100, 2026, 'Test League', 10, 'PPR', 7, 14,
                   '{"QB":1,"RB":2,"WR":2,"TE":1,"FLEX":1,"K":1,"D/ST":1,"BE":6,"IR":1}', '2026-10-14 12:00:00')""")
    con.execute("""INSERT INTO fantasy_teams (league_id, season, team_id, name, abbrev, is_mine)
                   VALUES (100, 2026, 1, 'My Champions', 'MY', true), (100, 2026, 2, 'Rival Squad', 'RIV', false)""")
    for team, roster in ((1, PLAYERS), (2, OTHER_TEAM)):
        for pid, (name, pos, pro, proj, label, slot_id) in roster.items():
            con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status) "
                        "VALUES (?, ?, ?, ?, 'ACTIVE')", [pid, name, pos, pro])
            con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, "
                        "projected_points, synced_at) VALUES (2026, ?, 7, ?, '2026-10-14 12:00:00')", [pid, proj])
            con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, "
                        "lineup_slot_id, lineup_slot, is_starter, synced_at) VALUES (100, 2026, 7, ?, ?, ?, ?, ?, "
                        "'2026-10-14 12:00:00')", [team, pid, slot_id, label, slot_id not in (20, 21)])
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR,
                   game_id VARCHAR, gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR,
                   home_score INTEGER, away_score INTEGER)""")
    con.execute(f"INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', 'g1', '{gameday_kc}', '13:00', 'KC', 'BUF', NULL, NULL)")
    con.execute(f"INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', 'g2', '{gameday_dal}', '20:15', 'NYG', 'DAL', NULL, NULL)")
    for i in range(12):
        con.execute(f"INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', 'o{i}', '{gameday_dal}', '13:00', 'H{i}', 'A{i}', NULL, NULL)")


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    _seed(c)
    yield c
    c.close()


def what_if(con, raw=None, **kw):
    return evaluate_what_if(con, scenario=parse_scenario(raw), now=NOW, **kw)


def lineup(out, which="scenario_player_id"):
    return {s["slot_id"]: s[which] for s in out["slots"]}


# ---- the scenario parameter ---------------------------------------------------------------------------------------

def test_scenario_parameter_is_strict_and_bounded():
    assert parse_scenario(None) is None and parse_scenario("") is None
    s = parse_scenario("w7t1~FLEX_0.8,D/ST_0.-16002,RB/WR_1.42")
    assert (s.week, s.team, s.moves) == (7, 1, (("FLEX_0", 8), ("D/ST_0", -16002), ("RB/WR_1", 42)))
    assert parse_scenario("w7t1").moves == ()
    too_many = "w7t1~" + ",".join(["FLEX_0.8"] * (MAX_MOVES + 1))
    bad = ["7t1", "w7t1~", "w7t1~FLEX_0", "w7t1~FLEX.8", "w7t1~BE_0.8", "w7t1~IR_0.8", "w7t1~TAXI_0.8",
           "w7t1~flex_0.8", "w7t1~FLEX_0.8,", "w7t1~FLEX_0.8;DROP", "w7t1~FLEX_0.1e3", "w7t1 ~FLEX_0.8",
           "w7t1~FLEX_0.8\n", "w123t1", "w7t1~FLEX_0.1234567890123", too_many, "w7t1~" + "x" * MAX_PARAM_LEN]
    for raw in bad:
        with pytest.raises(ScenarioSyntaxError):
            parse_scenario(raw)


# ---- the baseline is the engine's recommendation, unchanged -------------------------------------------------------

def test_no_scenario_is_the_engine_recommendation_exactly_and_the_engine_payload_is_unchanged(con):
    plain = load_start_sit_evaluation(con, now=NOW)
    cap: dict = {}
    assert load_start_sit_evaluation(con, now=NOW, capture=cap) == plain, "the capture hook never changes the payload"
    out = what_if(con)
    rec = {r["slot_id"]: r["player_id"] for r in plain["recommended_lineup"]}
    assert lineup(out) == lineup(out, "recommended_player_id") == rec
    assert rec["FLEX_0"] == 7 and rec["TE_0"] == 9
    t = out["totals"]
    assert t["recommended"]["total"] == plain["summary"]["recommended_projected_total"] == 105.0
    assert t["recommended"]["known_total"] == plain["summary"]["known_recommended_total"]
    assert (t["scenario"]["total"], t["delta"], t["known_delta"]) == (105.0, 0.0, 0.0)
    assert out["changed_slot_ids"] == [] and out["errors"] == []
    assert out["scenario"] == {"requested": False, "applied": False, "whatif": None, "moves": [], "move_limit": MAX_MOVES}
    assert out["key"] == "w7t1" and out["contract"] == "start-sit-what-if/v1"


# ---- moves ---------------------------------------------------------------------------------------------------------

def test_a_bench_swap_changes_the_points_and_names_the_affected_slot(con):
    out = what_if(con, "w7t1~FLEX_0.8")
    assert out["scenario"]["applied"] is True and out["errors"] == []
    assert lineup(out)["FLEX_0"] == 8 and out["changed_slot_ids"] == ["FLEX_0"]
    assert (out["totals"]["scenario"]["total"], out["totals"]["delta"]) == (104.0, -1.0)
    move = out["scenario"]["moves"][0]
    assert (move["slot_id"], move["player_id"], move["displaced_player_id"], move["from_slot_id"]) == ("FLEX_0", 8, 7, None)
    walt = next(p for p in out["players"] if p["player_id"] == 7)
    assert (walt["recommended_slot_id"], walt["scenario_slot_id"]) == ("FLEX_0", None)


def test_two_starters_swap_slots_when_both_are_eligible(con):
    out = what_if(con, "w7t1~FLEX_0.10,TE_0.10")       # TE 10 in at FLEX, then into TE: TE 9 moves to FLEX
    assert lineup(out)["TE_0"] == 10 and lineup(out)["FLEX_0"] == 9
    assert out["totals"]["delta"] == -5.0 and set(out["changed_slot_ids"]) == {"TE_0", "FLEX_0"}
    assert out["scenario"]["moves"][1]["displaced_to_slot_id"] == "FLEX_0"


def test_every_offered_option_is_exactly_what_applying_it_gives(con):
    out = what_if(con, "w7t1~FLEX_0.8")
    offered = [(s["slot_id"], o) for s in out["slots"] for o in s["options"]]
    assert offered
    for slot_id, o in offered:
        after = what_if(con, o["whatif"])
        assert after["scenario"]["applied"], (slot_id, o)
        assert lineup(after)[slot_id] == o["player_id"]
        assert (after["totals"]["scenario"]["total"], after["totals"]["delta"]) == (o["total"], o["delta"])


def test_reset_and_undo(con):
    out = what_if(con, "w7t1~FLEX_0.10,FLEX_0.9")      # the second move needs the first (WR 7 cannot play TE)
    first, second = out["scenario"]["moves"]
    assert first["removable"] is False and first["whatif_without"] is None
    assert "would send Walt Slot (WR) to TE" in first["blocked_by"]
    assert second["removable"] is True and second["whatif_without"] == "w7t1~FLEX_0.10"
    assert lineup(what_if(con, second["whatif_without"]))["FLEX_0"] == 10
    single = what_if(con, "w7t1~FLEX_0.8")["scenario"]["moves"][0]
    assert single["removable"] and single["whatif_without"] is None              # undoing the only move is a reset
    assert lineup(what_if(con, None)) == lineup(what_if(con, "w7t1"))            # an empty scenario is the baseline


# ---- what is prevented, and why --------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,code,needle", [
    ("w7t1~RB_0.4", LOCKED_SLOT, "Ray Back's game has kicked off"),
    ("w7t1~FLEX_0.15", LOCKED_PLAYER, "stays on the bench"),
    ("w7t1~FLEX_0.2", LOCKED_PLAYER, "stays in RB 1"),
    ("w7t1~FLEX_0.13", RESERVED, "fantasy IR"),
    ("w7t1~K_0.8", INELIGIBLE, "it takes K"),
    ("w7t1~FLEX_0.9", INELIGIBLE_SWAP, "Walt Slot (WR) to TE"),
    ("w7t1~FLEX_0.8,FLEX_0.8", DUPLICATE, "already in FLEX"),
    ("w7t1~FLEX_0.50", NOT_ON_ROSTER, "Player 50 is not on this team"),
    ("w7t1~OP_0.8", "UNKNOWN_SLOT", "no OP_0"),
    ("w8t1~FLEX_0.8", WRONG_SCOPE, "week 8"),
    ("w7t2~FLEX_0.8", WRONG_SCOPE, "team 2"),
])
def test_rule_breaking_scenarios_are_not_applied_and_say_why(con, raw, code, needle):
    base = what_if(con)
    out = what_if(con, raw)
    assert out["scenario"]["requested"] and not out["scenario"]["applied"]
    assert [e["code"] for e in out["errors"]] == [code]
    assert needle in out["errors"][0]["message"]
    assert lineup(out) == lineup(base) and out["totals"] == base["totals"] and out["scenario"]["whatif"] is None
    assert "Zed Rival" not in str(out), "another team's player is never described"


def test_one_bad_move_rejects_the_whole_scenario(con):
    out = what_if(con, "w7t1~FLEX_0.8,K_0.8")
    assert not out["scenario"]["applied"] and [e["code"] for e in out["errors"]] == [INELIGIBLE]
    assert lineup(out)["FLEX_0"] == 7


def test_options_never_offer_locked_reserved_or_ineligible_players(con):
    out = what_if(con)
    slots = {s["slot_id"]: s for s in out["slots"]}
    for sid in ("QB_0", "RB_0", "RB_1", "WR_0", "WR_1"):
        assert slots[sid]["locked"] and slots[sid]["options"] == [] and "kicked off" in slots[sid]["lock_reason"]
    flex = slots["FLEX_0"]
    assert flex["eligible_positions"] == ["RB", "WR", "TE"]
    offered = {o["player_id"] for o in flex["options"]}
    assert offered == {4, 8, 10, 14}             # 9 (TE) would push WR 7 into TE: not offered
    blocked = {b["player_id"]: b["code"] for b in flex["blocked"]}
    assert blocked[13] == RESERVED and blocked[15] == LOCKED_PLAYER and blocked[2] == LOCKED_PLAYER
    assert blocked[9] == INELIGIBLE_SWAP and 11 not in blocked          # position mismatches are stated per slot
    players = {p["player_id"]: p for p in out["players"]}
    assert players[13]["roster_spot"] == "ir" and players[13]["movable"] is False
    assert players[8]["movable"] is True and players[8]["restriction"] is None


def test_a_player_without_a_projection_is_unknown_never_zero(con):
    out = what_if(con, "w7t1~FLEX_0.14")
    t = out["totals"]
    assert t["scenario"]["total"] is None and t["scenario"]["complete"] is False
    assert t["scenario"]["missing_projection_ids"] == [14]
    assert t["scenario"]["known_total"] == 94.0 and t["known_delta"] == -11.0
    assert t["delta"] is None and t["complete"] is False
    opt = next(o for s in what_if(con)["slots"] if s["slot_id"] == "FLEX_0" for o in s["options"] if o["player_id"] == 14)
    assert opt["total"] is None and opt["delta"] is None and opt["known_delta"] == -11.0


def test_the_moves_cap_stops_offering_more_moves(con):
    raw = "w7t1~" + ",".join(["FLEX_0.8", "FLEX_0.7"] * (MAX_MOVES // 2))
    out = what_if(con, raw)
    assert out["scenario"]["applied"] and len(out["scenario"]["moves"]) == MAX_MOVES
    assert all(o["whatif"] is None for s in out["slots"] for o in s["options"])


def test_a_future_preview_has_no_locks_and_still_never_starts_ir(con):
    con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, projected_points) "
                "SELECT season, espn_player_id, 8, projected_points FROM espn_player_weekly_points WHERE scoring_period = 7")
    out = what_if(con, "w8t1~RB_0.4", week=8)
    assert out["is_future_preview"] and out["scenario"]["applied"]
    assert lineup(what_if(con, "w8t1~RB_0.13", week=8))["RB_0"] != 13


def test_lineup_lock_rule_is_reported_from_league_rules(con):
    lock = what_if(con)["lineup_lock"]
    assert lock["engine_status"] == "unverified" and lock["rule"]["value"] is None
    con.execute("""UPDATE leagues SET settings_raw = '{"rosterSettings": {"lineupLocktimeType": "INDIVIDUAL_GAME"}}'""")
    lock = what_if(con)["lineup_lock"]
    assert lock["engine_status"] == "handled" and lock["rule"]["value"] == "individual_game"


# ---- taxi squad (#228) ---------------------------------------------------------------------------------------------

def test_a_taxi_player_is_never_startable():
    from test_sleeper_taxi import NOW as SLEEPER_NOW, TAXI, _db, _project
    c = _db()
    _project(c)
    try:
        out = evaluate_what_if(c, team_id=1, source="espn", now=SLEEPER_NOW)
        assert all(o["player_id"] != TAXI for s in out["slots"] for o in s["options"])
        taxi = next(p for p in out["players"] if p["player_id"] == TAXI)
        assert taxi["roster_spot"] == "taxi" and taxi["movable"] is False and taxi["restriction"] == RESERVED
        flex = next(s for s in out["slots"] if s["slot"] == "FLEX")
        bad = evaluate_what_if(c, team_id=1, source="espn", now=SLEEPER_NOW,
                               scenario=parse_scenario(f"{out['key']}~{flex['slot_id']}.{TAXI}"))
        assert [e["code"] for e in bad["errors"]] == [RESERVED] and "taxi squad" in bad["errors"][0]["message"]
    finally:
        c.close()


# ---- the API -------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    db_file = tmp_path / "fantasy.duckdb"
    c = duckdb.connect(str(db_file))
    _seed(c, gameday_kc="2099-10-18", gameday_dal="2099-10-19")     # nothing has kicked off at test time
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_file))
    return TestClient(app, raise_server_exceptions=False)


def test_api_what_if(client):
    r = client.get("/api/start-sit/what-if")
    assert r.status_code == 200
    body = r.json()
    assert body["contract"] == "start-sit-what-if/v1" and body["key"] == "w7t1"
    assert {s["signal"] for s in body["provenance"]["signals"]} >= {"league", "projections"}
    r = client.get("/api/start-sit/what-if", params={"whatif": "w7t1~FLEX_0.8"})
    assert r.status_code == 200 and r.json()["totals"]["delta"] == -1.0
    assert r.json()["scenario"]["whatif"] == "w7t1~FLEX_0.8"
    rec = client.get("/api/start-sit").json()["recommended_lineup"]
    assert {x["slot_id"]: x["player_id"] for x in rec}["FLEX_0"] == 7, "the recommendation itself never changes"


def test_api_rejects_malformed_and_oversized_scenarios(client):
    for raw in ("nonsense", "w7t1~FLEX_0.8;x", "w7t1~" + "FLEX_0.8," * 60):
        r = client.get("/api/start-sit/what-if", params={"whatif": raw})
        assert r.status_code == 422, raw
    assert client.get("/api/start-sit/what-if?team_id=99").status_code == 404
    assert client.get("/api/start-sit/what-if?week=99").status_code == 422


def test_the_what_if_route_is_read_only_and_league_scoped_under_v2():
    from app.api.league_scope import league_scope
    from app.api.routing import iter_routes
    from app.main import app
    routes = [r for r in iter_routes(app) if r.path.endswith("/api/start-sit/what-if")]
    assert {(r.path, tuple(sorted(r.methods))) for r in routes} == {
        ("/api/start-sit/what-if", ("GET",)), ("/v2/leagues/{league_id}/api/start-sit/what-if", ("GET",))}
    v2 = next(r for r in routes if r.path.startswith("/v2/"))
    assert league_scope in v2.dependency_calls()


def test_the_what_if_module_never_reaches_a_provider():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "analytics" / "lineup_what_if.py").read_text()
    for needle in ("espn.client", "sleeper.client", "requests", "httpx", "urllib", "INSERT", "UPDATE", "DELETE"):
        assert needle not in src, needle


# ---- /v2: authorized per league, 404 indistinguishable ------------------------------------------------------------

def test_v2_what_if_is_authorized_before_the_scenario_is_read(pg, served, e2e, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from cp_seed import U1, seed_user
    from league_fixtures import publish_local
    from test_leagues_api import bearer
    client = TestClient(app, raise_server_exceptions=False)
    private = publish_local(pg, e2e, served, tmp_path / "p", visibility="private", name="P")
    public = publish_local(pg, e2e, served, tmp_path / "q", visibility="public", name="Q", external_id="1998",
                           stamp="2026-10-20T13:00:00")
    with pg.connect() as c:
        seed_user(c, U1)                       # signed in, but a member of neither league
        c.commit()
    path = "/api/start-sit/what-if"
    answers = []
    for lg in (private.league_id, "lg_" + "z" * 24):
        for params in ({}, {"whatif": "nonsense"}, {"whatif": "w3t2~FLEX_0.305"}, {"whatif": "x" * 2000}):
            for headers in ({}, bearer(U1)):
                r = client.get(f"/v2/leagues/{lg}{path}", params=params, headers=headers)
                answers.append((r.status_code, r.json()))
    assert set(map(str, answers)) == {str((404, {"detail": "League not found."}))}
    r = client.get(f"/v2/leagues/{public.league_id}{path}")
    assert r.status_code == 200, r.text[:200]
    key = r.json()["key"]
    opt = next(o for s in r.json()["slots"] for o in s["options"])
    r2 = client.get(f"/v2/leagues/{public.league_id}{path}", params={"whatif": opt["whatif"]})
    assert r2.status_code == 200 and r2.json()["scenario"]["applied"] and r2.json()["key"] == key
    assert r2.headers["cache-control"] == "private, no-store"
    assert client.get(f"/v2/leagues/{public.league_id}{path}", params={"whatif": "nonsense"}).status_code == 422

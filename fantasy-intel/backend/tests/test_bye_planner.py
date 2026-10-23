"""Bye and lineup-coverage planner (issue #177): exact bye collisions, verified NFL absences and unknown evidence are
kept apart; multi-player byes and uncovered starting slots are detected deterministically; unknown evidence never
yields a definitive covered or short week. Synthetic inputs only (tests/synthetic_nfl.py, tests/espn_fixture.py)."""

import shutil

import pytest
from fastapi.testclient import TestClient

from app.analytics.bye_planner import (AVAILABLE, BYE, OUT, UNSURE, build_bye_planner, plan_week, player_week,
                                       starting_slots, week_coverage)
from app.analytics.lineup import lineup_slots
from app.analytics.player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, UNKNOWN
from app.analytics.team_lineups import RosterPlayer
from app.db.connection import connect

from test_schedule_intelligence import NOW, WEEK, _bye_pair, setup
from test_trade_data import _raw_snapshot

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1})
BUF_STARTERS = {102, 402, -16002, 502}         # team 2: Bravo QB, Victor TE, Bills D/ST, Zulu K (all BUF)


@pytest.fixture(scope="module")
def base_db(tmp_path_factory):
    """One synced league (BUF and its week-10 opponent rest in week 10), copied per test: syncing is the slow part."""
    path, con = setup(tmp_path_factory.mktemp("bye"))
    con.close()
    return path


@pytest.fixture
def league(base_db, tmp_path):
    path = tmp_path / "bye.duckdb"
    shutil.copy(base_db, path)
    con = connect(path)
    yield path, con
    con.close()


def P(pid, pos, team="KC", **kw):
    return RosterPlayer(pid, pos, team, f"P{pid}", **kw)


def states(players, over=None, state=AVAILABLE):
    over = over or {}
    return {p.espn_player_id: {"state": over.get(p.espn_player_id, state), "code": "x"} for p in players}


def week_of(res, w):
    return next(x for x in res["weeks"] if x["week"] == w)


def slot(week, label):
    return next(s for s in week["slots"] if s["slot"] == label)


# ------------------------------------------------------------------ pure coverage --

def test_flex_coverage_is_an_exact_matching():
    slots = ["RB", "RB", "WR", "WR", "FLEX"]
    two_rb_three_wr = [P(1, "RB"), P(2, "RB"), P(3, "WR"), P(4, "WR"), P(5, "WR")]
    wk = plan_week(9, slots, [], two_rb_three_wr, states(two_rb_three_wr), set())
    assert wk["status"] == "covered" and wk["covered"] == 5 and wk["uncovered"] == 0
    # A TE fills FLEX; a third RB with only one WR leaves exactly one WR slot uncovered.
    te = two_rb_three_wr[:4] + [P(6, "TE")]
    assert plan_week(9, slots, [], te, states(te), set())["status"] == "covered"
    one_wr = [P(1, "RB"), P(2, "RB"), P(7, "RB"), P(3, "WR")]
    wk = plan_week(9, slots, [], one_wr, states(one_wr), set())
    assert wk["status"] == "short" and wk["short_slots"] == ["WR"] and slot(wk, "WR")["uncovered"] == 1
    assert slot(wk, "FLEX")["covered"] == 1                     # the third RB takes the FLEX
    # Non-nested flex slots: RB/WR + WR/TE with one RB and one TE are both filled.
    mixed = [P(1, "RB"), P(6, "TE")]
    rows = week_coverage(["RB/WR", "WR/TE"], mixed, states(mixed))
    assert [r["fill"] for r in rows] == ["covered", "covered"]


def test_matching_fills_the_most_slots_and_then_the_most_certain_players():
    # RB + FLEX. Certain WR and unknown RB: both fill (RB slot unknown, FLEX covered by the WR), never 1 covered + 1 empty.
    players = [P(1, "RB"), P(2, "WR")]
    wk = plan_week(9, ["RB", "FLEX"], [], players, states(players, {1: UNSURE}), set())
    assert (wk["covered"], wk["unknown"], wk["uncovered"]) == (1, 1, 0) and wk["status"] == "unconfirmed"
    # A certain RB beats an unknown RB for the RB slot; the unknown one then only backs the FLEX.
    players = [P(1, "RB"), P(3, "RB")]
    wk = plan_week(9, ["RB", "FLEX"], [], players, states(players, {1: UNSURE}), set())
    assert slot(wk, "RB")["covered"] == 1 and slot(wk, "FLEX")["unknown"] == 1


def test_unknown_never_produces_covered_or_short_on_its_own():
    players = [P(1, "QB"), P(2, "QB")]
    # The only QB who can certainly play is enough: an unknown backup changes nothing.
    assert plan_week(9, ["QB"], [], players, states(players, {2: UNSURE}), set())["status"] == "covered"
    # The starter is unknown and the backup is on bye: neither covered nor short.
    wk = plan_week(9, ["QB"], [], players, states(players, {1: UNSURE, 2: BYE}), set())
    assert wk["status"] == "unconfirmed" and wk["unconfirmed_slots"] == ["QB"] and wk["uncovered"] == 0
    # Short only when even the unknowns could not fill the slot: no K on the roster at all.
    wk = plan_week(9, ["QB", "K"], [], players, states(players, {1: UNSURE, 2: BYE}), set())
    assert wk["status"] == "short" and wk["short_slots"] == ["K"] and wk["unknown"] == 1
    # Every player unknown (no schedule for the week): unconfirmed, never covered.
    wk = plan_week(9, ["QB"], [], players, states(players, state=UNSURE), set())
    assert wk["status"] == "unconfirmed"


def test_unresolved_or_unsynced_slots_never_read_as_covered():
    slots, unresolved, known = starting_slots({"QB": 1, "LB": 1, "BE": 6, "IR": 1, "TAXI": 2})
    assert slots == ["QB"] and [u["slot"] for u in unresolved] == ["LB"] and known
    players = [P(1, "QB")]
    assert plan_week(9, slots, unresolved, players, states(players), set())["status"] == "unconfirmed"
    slots, unresolved, known = starting_slots(None)
    assert slots == [] and unresolved == [] and known is False
    assert plan_week(9, slots, unresolved, players, states(players), set(), slots_known=known)["status"] == "unconfirmed"


def test_bye_cluster_counts_active_players_and_names_current_starters():
    players = [P(1, "QB", "BUF"), P(2, "WR", "BUF"), P(3, "WR"), P(4, "RB")]
    st = states(players, {1: BYE, 2: BYE})
    wk = plan_week(9, ["QB", "WR"], [], players, st, starters={1, 3})
    assert wk["byes"] == [1, 2] and wk["bye_cluster"] and wk["starters_on_bye"] == [1] and wk["conflict"]
    assert wk["status"] == "short" and wk["short_slots"] == ["QB"]
    # Bench alternatives: benched players who can play that week and fill a starting slot (RB fits none here).
    assert wk["bench_alternatives"] == []
    wk = plan_week(9, ["QB", "WR", "RB"], [], players, st, starters={1, 3})
    assert wk["bench_alternatives"] == [{"espn_player_id": 4, "slots": ["RB"]}]


# ------------------------------------------------------------------ player-weeks --

SCHED = {"covered": {9, 10}, "games": {("KC", 9): {"opponent": "BUF", "home": True, "kickoff": None},
                                      ("BUF", 9): {"opponent": "KC", "home": False, "kickoff": None},
                                      ("KC", 10): {"opponent": "DEN", "home": True, "kickoff": None}},
         "complete_teams": {"KC", "BUF"}, "teams": {"KC", "BUF", "DEN"}}


def test_player_week_sources_are_kept_apart():
    bye = player_week(P(1, "QB", "BUF"), None, 10, SCHED)
    assert (bye["state"], bye["code"], bye["source"]) == (BYE, "bye", "schedule")
    assert player_week(P(1, "QB", "KC"), None, 9, SCHED)["state"] == AVAILABLE
    out = player_week(P(1, "QB", "KC", unavailable_weeks=frozenset({9})), {"status": INELIGIBLE,
                      "earliest_eligible_week": 10, "eligibility_basis": "Reserve/Injured since week 5"}, 9, SCHED)
    assert (out["state"], out["code"], out["source"]) == (OUT, "nfl_ineligible", "availability")
    after = player_week(P(1, "QB", "KC", unavailable_weeks=frozenset({9})), {"status": INELIGIBLE,
                        "earliest_eligible_week": 10}, 10, SCHED)
    assert (after["state"], after["code"]) == (UNSURE, "return_unconfirmed")
    unknown = {
        "no_team": player_week(P(1, "QB", None), None, 9, SCHED),
        "schedule_missing": player_week(P(1, "QB"), None, 11, SCHED),
        "team_schedule_incomplete": player_week(P(1, "QB", "DEN"), None, 9, SCHED),
        "availability_unknown": player_week(P(1, "QB"), {"status": UNKNOWN, "warnings": ["?"]}, 9, SCHED),
        "injury_designation": player_week(P(1, "QB", injury_status="OUT"), None, 9, SCHED),
    }
    unknown["return_unconfirmed"] = player_week(P(1, "QB"), {"status": ELIGIBLE_UNCONFIRMED,
                                                             "earliest_eligible_week": 9}, 9, SCHED)
    for code, row in unknown.items():
        assert (row["state"], row["code"], row["source"]) == (UNSURE, code, "unknown"), code
    # QUESTIONABLE is a game-day call, not a future absence.
    assert player_week(P(1, "QB", injury_status="QUESTIONABLE"), None, 9, SCHED)["state"] == AVAILABLE


# ------------------------------------------------------------------ the league file --

def test_multi_player_bye_and_uncovered_slots_detected_deterministically(league):
    _, con = league                                   # BUF and its week-10 opponent rest in week 10
    before = _raw_snapshot(con)
    res = build_bye_planner(con, now=NOW)
    assert res["contract"] == "bye-planner.v1" and res["team"]["team_id"] == 2
    assert res["window"]["weeks"] == list(range(WEEK + 1, 18)) and res["window"]["playoff_weeks"] == [15, 16, 17]
    w10 = week_of(res, 10)
    assert w10["status"] == "short" and set(w10["short_slots"]) == {"QB", "TE", "K", "D/ST"}
    assert set(w10["byes"]) == BUF_STARTERS and w10["bye_cluster"] and set(w10["starters_on_bye"]) == BUF_STARTERS
    assert {"required": 1, "uncovered": 1, "bye": 1, "available": 0}.items() <= slot(w10, "QB").items()
    assert slot(w10, "FLEX")["covered"] == 1                                         # RB/WR depth still fills FLEX
    assert {b["espn_player_id"] for b in w10["bench_alternatives"]} == {206, 307}   # benched SEA RB, TB WR
    assert all(week_of(res, w)["status"] == "covered" for w in res["window"]["weeks"] if w != 10)
    assert res["summary"]["short"] == [10] and res["summary"]["bye_cluster_weeks"] == [10]
    assert res["summary"]["first_conflict_week"] == 10 and res["summary"]["most_byes"] == {"week": 10, "count": 4}
    qb = next(p for p in res["players"] if p["espn_player_id"] == 102)
    assert qb["bye_week"] == 10 and qb["weeks"]["10"]["source"] == "schedule"
    # IR never counts: 207 is listed but excluded from coverage and byes.
    ir = next(p for p in res["players"] if p["espn_player_id"] == 207)
    assert ir["slot"] == "ir" and ir["counts"] is False and ir["weeks"] == {}
    assert build_bye_planner(con, now=NOW) == res                                    # deterministic
    assert _raw_snapshot(con) == before                                              # read-only


def test_missing_schedule_week_is_unknown_not_safe(league):
    _, con = league
    con.execute("DELETE FROM nfl_schedules WHERE week = 12")
    res = build_bye_planner(con, now=NOW)
    w12 = week_of(res, 12)
    assert w12["status"] == "unconfirmed" and w12["covered"] == 0 and w12["byes"] == []
    assert {u["code"] for u in w12["uncertain"]} == {"schedule_missing"}
    assert res["schedule"]["weeks_missing"] == [12]


def test_incomplete_team_schedule_is_not_a_confirmed_bye(league):
    _, con = league
    con.execute("DELETE FROM nfl_schedules WHERE week = 11 AND 'BUF' IN (home_team, away_team)")
    res = build_bye_planner(con, now=NOW)
    w10 = week_of(res, 10)
    assert w10["byes"] == [] and w10["status"] == "unconfirmed"                       # no longer a known bye
    assert {u["code"] for u in w10["uncertain"]} == {"team_schedule_incomplete"}
    assert "BUF" in res["schedule"]["incomplete_teams"]
    assert next(p for p in res["players"] if p["espn_player_id"] == 102)["bye_week"] is None


def test_teamless_player_is_unknown_never_short_or_covered(league):
    _, con = league
    con.execute("UPDATE fantasy_players SET pro_team = NULL WHERE espn_player_id = 102")    # the only QB
    res = build_bye_planner(con, now=NOW)
    others = [w for w in res["window"]["weeks"] if w != 10]
    assert res["summary"]["unconfirmed"] == others and res["summary"]["short"] == [10]
    assert all(week_of(res, w)["uncertain"] == [{"espn_player_id": 102, "code": "no_team"}] for w in res["window"]["weeks"])
    w10 = week_of(res, 10)                    # TE, K and D/ST are on a confirmed bye; the QB slot is only unknown
    assert set(w10["short_slots"]) == {"TE", "K", "D/ST"} and w10["unconfirmed_slots"] == ["QB"]


def test_no_remaining_weeks_plans_nothing(tmp_path):
    _, con = setup(tmp_path, final_weeks=set(range(1, 19)), partial={})
    con.execute("UPDATE leagues SET current_week = 17")
    con.execute("INSERT INTO lineup_snapshots SELECT * REPLACE (17 AS scoring_period) FROM lineup_snapshots "
                "WHERE scoring_period = 8")
    res = build_bye_planner(con, now=NOW)
    assert res["window"]["remaining_weeks"] is False and res["weeks"] == [] and res["summary"]["weeks"] == 0
    con.close()


def test_api_route_provenance_and_league_scope_mount(league, monkeypatch):
    db_path, con = league
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    r = api.get("/api/schedule-intelligence/bye-planner?team_id=1")
    assert r.status_code == 200 and r.json()["team"]["team_id"] == 1
    prov = r.json()["provenance"]
    assert prov["contract"] == "signal-provenance.v1"
    assert [s["signal"] for s in prov["signals"]] == ["league", "schedule", "injuries"]
    sched = prov["signals"][1]
    assert sched["source"] == "nflverse" and sched["coverage"]["available"] == sched["coverage"]["total"]
    assert api.get("/api/schedule-intelligence/bye-planner?team_id=77").status_code == 404
    # Mounted again under the league prefix behind league_scope (authorization before any read).
    from app.api.league_scope import league_scope
    from app.api.routing import iter_routes
    v2 = [x for x in iter_routes(app) if x.path == "/v2/leagues/{league_id}/api/schedule-intelligence/bye-planner"]
    assert len(v2) == 1 and league_scope in v2[0].dependency_calls()

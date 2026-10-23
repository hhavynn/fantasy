"""Bye and lineup-coverage planner (issue #177): exact bye collisions, verified NFL absences and unknown evidence are
kept apart; multi-player byes and uncovered starting slots are detected deterministically; unknown evidence never
yields a definitive covered or short week. Synthetic inputs only (tests/synthetic_nfl.py, tests/espn_fixture.py)."""

import json
import random
import shutil
import time

import pytest
from espn_fixture import SEASON
from fastapi.testclient import TestClient
from test_schedule_intelligence import NOW, WEEK, setup
from test_trade_data import _raw_snapshot

from app.analytics.bye_planner import (
    AVAILABLE,
    BYE,
    MAX_STARTING_SLOTS,
    OUT,
    UNSURE,
    build_bye_planner,
    plan_week,
    player_week,
    starting_slots,
    week_coverage,
)
from app.analytics.lineup import FIXED_SLOTS, assign_lineup, lineup_slots
from app.analytics.player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, UNKNOWN
from app.analytics.team_lineups import RosterPlayer
from app.db.connection import connect

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


def test_reserve_list_provenance_reads_only_the_planned_season(league):
    """Weekly rosters stored only for another season verify nothing for this one: unavailable with a reason, never
    claimed (missing is not zero). Coverage counts the lineup players this season's rosters list."""
    from app.api.schedule_intelligence import bye_planner_provenance
    from app.api.signal_provenance import status
    _, con = league

    def reserve_signal():
        res = build_bye_planner(con, team_id=2, now=NOW)
        sig = next(s for s in bye_planner_provenance(con, res)["signals"] if s["signal"] == "injuries")
        return res, sig

    res, sig = reserve_signal()                                        # no weekly-rosters table at all
    counted = sorted(p["espn_player_id"] for p in res["players"] if p["counts"])
    assert res["nfl_rosters"] == {"available": False, "season": SEASON, "latest_week": None, "players_found": 0,
                                  "players_counted": len(counted)}
    assert sig["reason_unavailable"] and status(sig) == "unavailable"

    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, team VARCHAR, "
                "espn_id VARCHAR, status VARCHAR)")
    con.executemany("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', 'BUF', ?, 'ACT')",
                    [[SEASON - 1, w, str(pid)] for w in range(1, 18) for pid in counted])
    res, sig = reserve_signal()                                        # the table exists, but only for last season
    assert res["nfl_rosters"]["available"] is False and res["nfl_rosters"]["players_found"] == 0
    assert sig["reason_unavailable"] == (f"No {SEASON} NFL weekly rosters are stored, so no reserve-list absence can "
                                         "be verified.")
    assert sig["coverage"]["available"] == 0 and status(sig) == "unavailable"

    con.executemany("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', 'BUF', ?, 'ACT')",
                    [[SEASON, w, str(pid)] for w in range(1, WEEK) for pid in counted[1:]])
    res, sig = reserve_signal()                                        # this season, all but one lineup player
    assert res["nfl_rosters"] == {"available": True, "season": SEASON, "latest_week": WEEK - 1,
                                  "players_found": len(counted) - 1, "players_counted": len(counted)}
    assert sig["coverage"]["available"] == len(counted) - 1 and sig["coverage"]["total"] == len(counted)
    assert sig["reason_unavailable"].startswith(f"1 of {len(counted)} lineup players")

    con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', 'BUF', ?, 'ACT')", [SEASON, WEEK - 1, str(counted[0])])
    _, sig = reserve_signal()
    assert sig["reason_unavailable"] is None and sig["coverage"]["available"] == sig["coverage"]["total"]


# ------------------------------------------------------------------ cost and equivalence --

def _assign_lineup_coverage(slots, players, states):
    """The planner's first solver (PR #244 head d746861): lineup.assign_lineup with certain players weighted
    1 + 1/(slots + 1) and unknown ones 1. Exponential when values tie, so only used here on small cases."""
    bonus = 1.0 / (len(slots) + 1)
    pool = [{"espn_player_id": p.espn_player_id, "position": p.position,
             "value": 1.0 + (bonus if states[p.espn_player_id]["state"] == AVAILABLE else 0.0)}
            for p in players if states[p.espn_player_id]["state"] in (AVAILABLE, UNSURE)]
    return [{"slot": a["slot"], "fill": "uncovered" if a["player_id"] is None
             else "covered" if states[a["player_id"]]["state"] == AVAILABLE else "unknown"}
            for a in assign_lineup(pool, slots)]


def _by_label(rows):
    out = {}
    for r in rows:
        out.setdefault(r["slot"], {"covered": 0, "unknown": 0, "uncovered": 0})[r["fill"]] += 1
    return out


def _assert_equivalent(slots, players, states, where):
    old, new = _by_label(_assign_lineup_coverage(slots, players, states)), _by_label(week_coverage(slots, players, states))
    total = lambda d: {k: sum(v[k] for v in d.values()) for k in ("covered", "unknown", "uncovered")}
    assert total(new) == total(old), where                         # the same maxima: slots filled, certain fills
    fixed = lambda d: {k: v for k, v in d.items() if k in FIXED_SLOTS}
    # Fixed slots read the same; which of several flex labels holds a tied player may legitimately differ (both are
    # maximum matchings), so flex labels are compared through the totals above.
    assert fixed(new) == fixed(old), where


def test_matching_gives_the_same_counts_as_the_previous_solver_on_random_rosters():
    positions = ["QB", "RB", "WR", "TE", "K", "D/ST"]
    tried = 0
    for seed in range(600):
        rnd = random.Random(seed)
        slots = lineup_slots({k: rnd.randint(0, 2) for k in ("QB", "RB", "WR", "TE", "K", "D/ST", "FLEX", "RB/WR",
                                                             "WR/TE", "OP")})
        if not slots or len(slots) > 9:
            continue
        players = [P(i, rnd.choice(positions)) for i in range(1, rnd.randint(1, 10))]
        st = states(players, {p.espn_player_id: rnd.choice([AVAILABLE, AVAILABLE, UNSURE, BYE, OUT]) for p in players})
        _assert_equivalent(slots, players, st, seed)
        tried += 1
    assert tried > 200


def test_matching_gives_the_same_counts_as_the_previous_solver_on_the_fixture_league(league):
    _, con = league
    con.execute("DELETE FROM nfl_schedules WHERE week = 12")              # adds an all-unknown week to the BUF bye
    res = build_bye_planner(con, now=NOW)
    slots = [s["slot"] for s in res["slots"]["starting"] for _ in range(s["count"])]
    active = [p for p in res["players"] if p["counts"]]
    players = [P(p["espn_player_id"], p["position"], p["pro_team"]) for p in active]
    for w in res["window"]["weeks"]:
        st = {p["espn_player_id"]: p["weeks"][str(w)] for p in active}
        _assert_equivalent(slots, players, st, w)
        assert {r["slot"]: (r["covered"], r["unknown"], r["uncovered"]) for r in week_of(res, w)["slots"]} == \
            {k: (v["covered"], v["unknown"], v["uncovered"]) for k, v in _by_label(week_coverage(slots, players, st)).items()}


def test_wide_flex_league_plans_every_week_in_about_a_second(league):
    """Review finding on PR #244: tied values made the flex search exponential (FLEX 8 + OP 2: ~39 s a week)."""
    _, con = league
    con.execute("UPDATE leagues SET roster_slots = ?", [json.dumps({"QB": 1, "FLEX": 8, "OP": 2, "BE": 16})])
    have = con.execute("SELECT count(*) FROM lineup_snapshots WHERE team_id = 2 AND scoring_period = ?", [WEEK]).fetchone()[0]
    for i in range(27 - have):
        pid = 9000 + i
        con.execute("INSERT INTO fantasy_players SELECT * REPLACE (? AS espn_player_id, ? AS position, 'KC' AS pro_team, "
                    "? AS full_name) FROM fantasy_players WHERE espn_player_id = 205", [pid, ("RB", "WR", "TE")[i % 3], f"D{i}"])
        con.execute("INSERT INTO lineup_snapshots SELECT * REPLACE (? AS espn_player_id) FROM lineup_snapshots "
                    "WHERE team_id = 2 AND scoring_period = ? AND espn_player_id = 206", [pid, WEEK])
    t0 = time.perf_counter()
    res = build_bye_planner(con, now=NOW)
    elapsed = time.perf_counter() - t0
    assert len(res["players"]) == 27 and len([p for p in res["players"] if p["counts"]]) == 24   # less IR, K, D/ST
    assert res["summary"]["short"] == [10]                                     # the only QB is on bye in week 10
    assert all(week_of(res, w)["status"] == "covered" for w in res["window"]["weeks"] if w != 10)
    assert elapsed < 1.5, f"planner took {elapsed:.2f}s"


def test_every_week_is_solved_in_polynomial_time_without_the_week_cache():
    slots = lineup_slots({"QB": 1, "FLEX": 8, "OP": 2})
    players = [P(i, ("QB", "RB", "WR", "TE")[i % 4]) for i in range(1, 28)]
    t0 = time.perf_counter()
    for week in range(17):                         # a different unknown player each week: no two weeks alike
        rows = week_coverage(slots, players, states(players, {week + 1: UNSURE}))
        assert sum(r["fill"] == "covered" for r in rows) == len(slots)
    assert time.perf_counter() - t0 < 1.0


def test_starting_slots_are_capped_before_they_are_expanded():
    slots, unresolved, known = starting_slots({"QB": 1, "RB": 2, "FLEX": 10**9, "OP": 5})
    assert len(slots) == MAX_STARTING_SLOTS and slots[:3] == ["QB", "RB", "RB"] and known
    assert {u["slot"]: u["count"] for u in unresolved} == {"FLEX": 10**9 - (MAX_STARTING_SLOTS - 3), "OP": 5}
    players = [P(1, "QB")]
    assert plan_week(9, slots, unresolved, players, states(players), set())["status"] == "short"

"""Multi-week K and D/ST streaming planner (issue #181): one eligible player per slot per week, never a rostered or
ineligible player, never a bye-week add or a hold through an unknown week, a polynomial solver (also on tied values and
large pools) and a data horizon that stops instead of inventing far-future numbers. Synthetic inputs only
(tests/synthetic_nfl.py, tests/espn_fixture.py)."""

import itertools
import random
import shutil
import time

import pytest
from fastapi.testclient import TestClient
from test_schedule_intelligence import NOW, setup

from app.analytics.kdst_streaming import (
    BYE,
    EMPTY,
    MAX_POOL,
    PLAYS,
    UNKNOWN,
    Cell,
    _conflicts,
    build_kdst_streaming,
    plan_position,
    solve,
)
from app.analytics.team_lineups import RosterPlayer
from app.db.connection import connect

ALPHA, BRAVO, CHARLIE, DELTA, ECHO = 9001, 9002, 9003, 9004, 9005
# K free agents (weeks 9-11 are planned; BUF and its week-10 opponent rest in week 10, see test_schedule_intelligence).
FREE_KICKERS = {
    ALPHA: ("Alpha K", "KC", "FREEAGENT", "ACTIVE", {9: 8.5, 10: 8.5, 11: 8.5, 12: 8.5}),
    BRAVO: ("Bravo K", "BUF", "WAIVERS", "ACTIVE", {9: 12.0, 10: 9.0, 11: 12.0}),      # week 10 is his bye
    CHARLIE: ("Charlie K", "DET", "FREEAGENT", "ACTIVE", {9: 9.0, 10: 10.0}),          # no week-11 projection
    DELTA: ("Delta K", "PHI", "FREEAGENT", "ACTIVE", {9: 7.0, 10: 7.0, 11: 7.0}),
    ECHO: ("Echo K", "SEA", "FREEAGENT", "OUT", {9: 20.0, 10: 20.0, 11: 20.0}),        # open designation
}
MY_K, OTHER_K = 502, 501                       # team 2's (mine, BUF) and team 1's kickers


@pytest.fixture(scope="module")
def base_db(tmp_path_factory):
    path, con = setup(tmp_path_factory.mktemp("kdst"))
    for pid, (name, team, status, injury, proj) in FREE_KICKERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status, "
                    "availability_status) VALUES (?, ?, 'K', ?, ?, ?)", [pid, name, team, injury, status])
        for w, v in proj.items():
            con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, "
                        "projected_points) VALUES (2026, ?, ?, ?)", [pid, w, v])
    # Rostered kickers whose stale status says FREEAGENT: still never candidates.
    con.execute("UPDATE fantasy_players SET availability_status = 'FREEAGENT' WHERE espn_player_id IN (?, ?)",
                [MY_K, OTHER_K])
    con.execute("UPDATE espn_player_weekly_points SET projected_points = 5.0 WHERE espn_player_id = ? "
                "AND scoring_period BETWEEN 9 AND 12", [MY_K])
    con.close()
    return path


@pytest.fixture
def league(base_db, tmp_path):
    path = tmp_path / "kdst.duckdb"
    shutil.copy(base_db, path)
    con = connect(path)
    yield path, con
    con.close()


def pos(res, p):
    return next(x for x in res["positions"] if x["position"] == p)


def plays(*vals):
    return [Cell(PLAYS, float(v)) if v is not None else Cell(UNKNOWN, None) for v in vals]


# ------------------------------------------------------------------------------------------------ league --

def test_plan_uses_free_agents_only_and_never_an_impossible_week(league):
    _, con = league
    res = build_kdst_streaming(con, now=NOW)
    k = pos(res, "K")
    assert res["window"]["weeks"] == [9, 10, 11] and k["planned_weeks"] == [9, 10, 11]
    ids = {a["espn_player_id"] for w in k["weeks"] for a in ([w["start"]] if w["start"] else []) + w["alternatives"]}
    assert ids <= {ALPHA, BRAVO, CHARLIE, DELTA}                    # rostered (501, 502) and Echo (out) never appear
    assert {MY_K, OTHER_K}.isdisjoint(ids) and ECHO not in ids
    assert [e["espn_player_id"] for e in k["excluded"]] == [ECHO]
    starts = [(w["week"], w["action"], w["start"]["espn_player_id"]) for w in k["weeks"]]
    # Bravo is best in weeks 9 and 11 but on bye in 10; Charlie has no week-11 projection, so he is not held there.
    assert starts == [(9, "add", BRAVO), (10, "add", CHARLIE), (11, "add", BRAVO)]
    for w in k["weeks"]:
        assert w["start"]["state"] == PLAYS and w["start"]["points"] is not None
        assert w["start"]["espn_player_id"] not in {a["espn_player_id"] for a in w["alternatives"]}
    assert k["plan"]["moves"] == 3 and k["plan"]["projected_points"] == 34.0
    assert k["plan"]["hold_points"] == 10.0 and k["plan"]["gain_vs_hold"] == 24.0     # 5 + bye 0 + 5
    w10 = k["weeks"][1]
    assert w10["hold"]["state"] == BYE and w10["hold"]["points"] == 0.0
    assert BRAVO not in {a["espn_player_id"] for a in w10["alternatives"]}             # a bye week is never addable
    # Eligibility: this week's waiver player needs a claim; later adds and re-adds are hypothetical.
    assert [w["start"]["eligibility"] for w in k["weeks"]] == ["waiver_claim", "hypothetical", "hypothetical"]
    tw = k["this_week"]
    assert tw["best"]["espn_player_id"] == BRAVO and tw["best_confirmed"]["espn_player_id"] == CHARLIE
    assert tw["best_confirmed"]["eligibility"] == "free_agent"
    # The streaming baseline is the #3 projected free agent.
    assert k["weeks"][0]["baseline"]["points"] == 8.5 and k["weeks"][0]["baseline"]["rank"] == 3


def test_missing_projection_is_unknown_never_zero(league):
    _, con = league
    k = pos(build_kdst_streaming(con, now=NOW), "K")
    w11 = k["weeks"][2]
    assert CHARLIE not in {a["espn_player_id"] for a in w11["alternatives"]}
    assert all(a["points"] is not None and a["points"] > 0 for w in k["weeks"] for a in w["alternatives"])


def test_max_moves_and_transaction_cost_change_the_plan(league):
    _, con = league
    one = pos(build_kdst_streaming(con, max_moves=1, now=NOW), "K")
    assert [(w["action"], w["start"]["espn_player_id"]) for w in one["weeks"]] == \
        [("add", ALPHA), ("keep", ALPHA), ("keep", ALPHA)]
    assert one["plan"]["moves"] == 1 and one["plan"]["projected_points"] == 25.5
    assert [w["start"]["eligibility"] for w in one["weeks"]] == ["free_agent", "held", "held"]
    # 5 points an add/drop: Charlie held into week 10, then Bravo (31 - 10 = 21 beats Alpha's 25.5 - 5 and 34 - 15).
    costly = pos(build_kdst_streaming(con, move_cost=5, now=NOW), "K")
    assert [(w["action"], w["start"]["espn_player_id"]) for w in costly["weeks"]] == \
        [("add", CHARLIE), ("keep", CHARLIE), ("add", BRAVO)]
    assert costly["plan"]["moves"] == 2 and costly["plan"]["net_points"] == 21.0 and costly["plan"]["cost_total"] == 10.0
    dearest = pos(build_kdst_streaming(con, move_cost=10, now=NOW), "K")
    assert dearest["plan"]["moves"] == 1 and dearest["plan"]["net_points"] == 15.5      # Alpha, held
    none = pos(build_kdst_streaming(con, max_moves=0, now=NOW), "K")
    assert [w["action"] for w in none["weeks"]] == ["hold", "hold", "hold"] and none["plan"]["moves"] == 0
    assert none["weeks"][1]["gap"]["code"] == "empty_slot"                 # holding through the bye leaves it empty


def test_thin_week_ends_the_plan_without_numbers(league):
    _, con = league
    k = pos(build_kdst_streaming(con, weeks=4, now=NOW), "K")
    assert k["planned_weeks"] == [9, 10, 11] and k["capped_weeks"] == [12]
    w12 = k["weeks"][3]
    assert w12["planned"] is False and w12["start"] is None and w12["alternatives"] == [] and w12.get("points") is None
    # Beyond the horizon nothing carries a player or a number: not the hold, not the baseline.
    assert w12["hold"]["player"] is None and w12["hold"]["points"] is None and w12["baseline"] is None
    assert w12["reason"] == "Only 1 free agent is projected for week 12 (at least 3 needed)."
    assert [w["outlook"] for w in k["weeks"]] == [False, False, True, True]
    # D/ST: no free agent at all, so no week is planned and nothing is invented.
    d = pos(build_kdst_streaming(con, now=NOW), "D/ST")
    assert d["status"] == "no_weeks" and d["plan"] is None and d["planned_weeks"] == []
    assert d["weeks"][0]["reason"] == "No free agent is projected for week 9 (at least 3 needed)."


def test_sleeper_status_is_unconfirmed(league):
    _, con = league
    con.execute("UPDATE leagues SET settings_raw = json_object('provider', 'sleeper')")
    k = pos(build_kdst_streaming(con, now=NOW), "K")
    assert {w["start"]["eligibility"] for w in k["weeks"] if w["start"]} == {"unconfirmed"}
    assert k["this_week"]["best_confirmed"] is None


def test_api_route_validation_provenance_and_league_scope_mount(league, monkeypatch):
    db_path, con = league
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    r = api.get("/api/schedule-intelligence/kdst-streaming?team_id=2&weeks=2&max_moves=2&move_cost=1.5")
    assert r.status_code == 200
    body = r.json()
    assert body["contract"] == "kdst-streaming.v1" and body["query"] == {"team_id": 2, "weeks": 2, "max_moves": 2,
                                                                         "move_cost": 1.5}
    assert [s["signal"] for s in body["provenance"]["signals"]] == ["league", "projections", "schedule"]
    for bad in ("weeks=1", "weeks=5", "max_moves=-1", "max_moves=5", "move_cost=-1", "move_cost=11"):
        assert api.get(f"/api/schedule-intelligence/kdst-streaming?{bad}").status_code == 422, bad
    # Only the parameter checks answer 422; any other ValueError is an internal error, and its text is not echoed.
    from app.api import schedule_intelligence as si

    def boom(*a, **k):
        raise ValueError("internal detail")
    monkeypatch.setattr(si, "build_kdst_streaming", boom)
    r500 = TestClient(app, raise_server_exceptions=False).get("/api/schedule-intelligence/kdst-streaming")
    assert r500.status_code == 500 and "internal detail" not in r500.text
    monkeypatch.undo()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    assert api.get("/api/schedule-intelligence/kdst-streaming?team_id=77").status_code == 404
    from app.api.league_scope import league_scope
    from app.api.routing import iter_routes
    v2 = [x for x in iter_routes(app) if x.path == "/v2/leagues/{league_id}/api/schedule-intelligence/kdst-streaming"]
    assert len(v2) == 1 and league_scope in v2[0].dependency_calls()


# ------------------------------------------------------------------------------------------------ solver --

def _brute(hold, cand, max_moves, cost):
    """Every sequence, the same rules as solve(): (best score, fewest moves among the best)."""
    weeks = len(hold)
    cap = weeks if max_moves is None else min(max_moves, weeks)
    best = None
    for seq in itertools.product(range(len(cand) + 1), repeat=weeks):
        prev, moves, pts, ok = 0, 0, 0.0, True
        for t, s in enumerate(seq):
            c = hold[t] if s == 0 else cand[s - 1][t]
            if s == 0 and prev != 0 or not c.known or (s != prev and not c.addable):
                ok = False
                break
            moves += s != prev
            pts += c.points
            prev = s
        if ok and moves <= cap:
            key = (round(pts - cost * moves, 9), -moves)
            best = key if best is None or key > best else best
    return best


def test_solver_matches_exhaustive_search_on_random_cases():
    for seed in range(400):
        rnd = random.Random(seed)
        weeks, n = rnd.randint(1, 4), rnd.randint(0, 4)

        def cell(rnd=rnd):
            x = rnd.random()
            return Cell(UNKNOWN, None) if x < 0.15 else Cell(BYE, 0.0) if x < 0.3 else \
                Cell(PLAYS, float(rnd.choice([0, 3, 5, 5, 7, 9])))
        hold = [Cell(EMPTY, 0.0) if rnd.random() < 0.2 else cell() for _ in range(weeks)]
        cand = [[cell() for _ in range(weeks)] for _ in range(n)]
        max_moves = rnd.choice([None, 0, 1, 2])
        cost = rnd.choice([0.0, 0.0, 1.5])
        plan, want = solve(hold, cand, max_moves, cost), _brute(hold, cand, max_moves, cost)
        if want is None:
            assert plan is None, seed
            continue
        assert plan is not None and (round(plan.score, 9), -plan.moves) == want, seed
        # The returned sequence obeys the rules and its numbers add up.
        prev, moves, pts = 0, 0, 0.0
        for t, s in enumerate(plan.holders):
            c = hold[t] if s == 0 else cand[s - 1][t]
            assert c.known and (s == prev or (s != 0 and c.addable)), seed
            moves, pts, prev = moves + (s != prev), pts + c.points, s
        assert moves == plan.moves and abs(pts - plan.points) < 1e-9, seed


def test_ties_are_deterministic_prefer_fewer_moves_and_one_player_per_week():
    hold = plays(5, 5, 5)
    cand = [plays(5, 5, 5), plays(5, 5, 5)]
    assert solve(hold, cand).holders == (0, 0, 0)                     # equal points: no add/drop
    tied = [plays(6, 6, 6) for _ in range(5)]
    p = solve(plays(1, 1, 1), tied)
    assert p.holders == (1, 1, 1) and p.moves == 1                    # lowest index, held, never a churn


def test_solver_is_polynomial_on_a_large_tied_pool():
    weeks, n = 4, 5000
    hold = plays(*([4.0] * weeks))
    cand = [plays(*([6.0] * weeks)) for _ in range(n)]                # every candidate tied every week
    t0 = time.perf_counter()
    plan = solve(hold, cand, None, 0.0)
    assert time.perf_counter() - t0 < 2.0
    assert plan.moves == 1 and plan.points == 24.0


def test_planner_bounds_the_pool_and_stays_fast():
    weeks = [9, 10, 11, 12]
    teams = [f"T{i}" for i in range(32)]
    sched = {"available": True, "covered": set(weeks), "complete_teams": set(teams),
             "games": {(t, w): {"opponent": teams[(i + w) % 32], "home": True, "kickoff": None}
                       for i, t in enumerate(teams) for w in weeks}}
    fas = [RosterPlayer(10_000 + i, "K", teams[i % 32], f"K{i}") for i in range(3000)]
    proj = {p.espn_player_id: {w: float((p.espn_player_id * 7 + w) % 11) for w in weeks} for p in fas}
    t0 = time.perf_counter()
    out = plan_position("K", weeks, free_agents=fas, roster=[], statuses={}, proj=proj, sched=sched, provider="espn",
                        models={}, max_moves=None, cost=0.5, phase_of=lambda w: "regular", slot_count=1)
    assert time.perf_counter() - t0 < 5.0
    assert out["pool"]["candidates"] == MAX_POOL and out["plan"] is not None
    used = [w["start"]["espn_player_id"] for w in out["weeks"]]
    assert len(used) == 4 and all(w["start"]["points"] == 10.0 for w in out["weeks"])


def test_conflicts_name_your_own_players_on_the_other_side():
    roster = [RosterPlayer(1, "QB", "KC", "Q"), RosterPlayer(2, "D/ST", "KC", "KC D/ST"),
              RosterPlayer(3, "WR", "KC", "W", on_ir=True)]
    assert [c["espn_player_id"] for c in _conflicts("D/ST", "KC", roster)] == [1]     # IR players never start
    assert [c["espn_player_id"] for c in _conflicts("K", "KC", roster)] == [2]
    assert _conflicts("K", None, roster) == []


def _synthetic(weeks, teams, loaded=None):
    """A complete schedule for `teams` (every team plays every week), or only the `loaded` teams' games."""
    loaded = teams if loaded is None else loaded
    games = {(t, w): {"opponent": "OPP", "home": True, "kickoff": None} for t in loaded for w in weeks}
    return {"available": True, "covered": set(weeks), "complete_teams": set(loaded), "teams": set(loaded),
            "games": games}


def _plan(roster, fas, proj, sched, weeks, **kw):
    return plan_position("K", weeks, free_agents=fas, roster=roster, statuses={}, proj=proj, sched=sched,
                         provider="espn", models={}, max_moves=kw.get("max_moves"), cost=kw.get("cost", 0.0),
                         phase_of=lambda w: "regular", slot_count=1)


@pytest.mark.parametrize("backup", [
    RosterPlayer(2, "K", "T1", "Backup K", injury_status="OUT"),        # open designation
    RosterPlayer(2, "K", "T1", "Backup K"),                             # no projection
    RosterPlayer(2, "K", None, "Backup K"),                             # no NFL team
])
def test_an_unknown_backup_never_forces_an_add(backup):
    """Review #250: one unknown incumbent used to make the whole hold unknown, forcing a worse add."""
    weeks = [9, 10, 11]
    sched = _synthetic(weeks, ["T0", "T1", "T2", "T3", "T4", "T5"])
    roster = [RosterPlayer(1, "K", "T0", "Starter K"), backup]
    fas = [RosterPlayer(100 + i, "K", f"T{2 + i}", f"FA{i}") for i in range(4)]
    proj = {1: {w: 9.0 for w in weeks}, **{p.espn_player_id: {w: 5.0 for w in weeks} for p in fas}}
    if backup.injury_status == "OUT":
        proj[2] = {w: 0.0 for w in weeks}
    out = _plan(roster, fas, proj, sched, weeks)
    assert [w["action"] for w in out["weeks"]] == ["hold", "hold", "hold"]
    assert out["plan"]["hold_points"] == 27.0 and out["plan"]["moves"] == 0
    assert all(w["hold"]["player"]["espn_player_id"] == 1 for w in out["weeks"])
    assert "Backup K" in out["weeks"][0]["hold"]["detail"]                       # named, not silently dropped
    frozen = _plan(roster, fas, proj, sched, weeks, max_moves=0)
    assert frozen["status"] == "planned" and frozen["plan"]["hold_points"] == 27.0


def test_hold_is_unknown_only_when_no_incumbent_is_known():
    weeks = [9, 10]
    sched = _synthetic(weeks, ["T0", "T1", "T2", "T3"])
    roster = [RosterPlayer(1, "K", "T0", "Hurt K", injury_status="OUT")]
    fas = [RosterPlayer(100 + i, "K", f"T{1 + i}", f"FA{i}") for i in range(3)]
    proj = {p.espn_player_id: {w: 5.0 for w in weeks} for p in fas}
    out = _plan(roster, fas, proj, sched, weeks, max_moves=0)
    assert out["status"] == "no_plan" and out["plan"] is None


def test_a_partly_loaded_schedule_week_is_not_planned():
    """Codex review #250: three projected kickers on the loaded teams must not plan a week whose other candidates'
    games and byes are unknown."""
    weeks = [9, 10]
    loaded = ["T0", "T1", "T2"]
    sched = _synthetic(weeks, loaded)
    fas = [RosterPlayer(100 + i, "K", f"T{i}", f"FA{i}") for i in range(8)]       # T3-T7: not in the schedule
    proj = {p.espn_player_id: {w: 6.0 for w in weeks} for p in fas}
    out = _plan([], fas, proj, sched, weeks)
    assert out["planned_weeks"] == [] and out["plan"] is None
    w9 = out["weeks"][0]
    assert w9["planned"] is False and w9["reason"].startswith("The NFL schedule for week 9 is incomplete (T3, T4")
    assert w9["candidates_with_game"] == 8 and w9["projected_candidates"] == 3     # unknowns stay in the denominator


def test_a_two_kicker_league_streams_the_slot_its_weaker_kicker_holds():
    """Issue #253 G9: with two K slots the plan streams the slot the weaker incumbent holds (the stronger one keeps the
    other), so a free agent better than the backup but worse than the starter is added, which a one-slot plan never
    does; the position reports slot_coverage partial. Roster fewer kickers than slots: the streamed slot is empty."""
    weeks = [9, 10]
    sched = _synthetic(weeks, ["T0", "T1", "T2", "T3", "T4", "T5"])
    roster = [RosterPlayer(1, "K", "T0", "Starter K"), RosterPlayer(2, "K", "T1", "Backup K")]
    fas = [RosterPlayer(100 + i, "K", f"T{2 + i}", f"FA{i}") for i in range(4)]
    proj = {1: {w: 9.0 for w in weeks}, 2: {w: 3.0 for w in weeks},
            **{p.espn_player_id: {w: 5.0 for w in weeks} for p in fas}}
    one = _plan(roster, fas, proj, sched, weeks)
    assert [w["action"] for w in one["weeks"]] == ["hold", "hold"] and "slot_coverage" not in one
    two = plan_position("K", weeks, free_agents=fas, roster=roster, statuses={}, proj=proj, sched=sched,
                        provider="espn", models={}, max_moves=None, cost=0.0, phase_of=lambda w: "regular",
                        slot_count=2)
    assert [w["hold"]["player"]["espn_player_id"] for w in two["weeks"]] == [2, 2]       # the backup's slot
    assert [w["action"] for w in two["weeks"]] == ["add", "keep"]
    assert two["plan"]["gain_vs_hold"] == 4.0                                               # 2 x (5 - 3)
    cov = two["slot_coverage"]
    assert (cov["status"], cov["code"], cov["slots"], cov["planned_slots"]) == ("partial", "streaming_slots", 2, 1)
    assert all(any("Streams 1 of your 2 K slots" in r for r in w["reasons"]) for w in two["weeks"])
    alone = plan_position("K", weeks, free_agents=fas, roster=roster[:1], statuses={}, proj=proj, sched=sched,
                          provider="espn", models={}, max_moves=0, cost=0.0, phase_of=lambda w: "regular",
                          slot_count=2)
    assert [w["hold"]["state"] for w in alone["weeks"]] == [EMPTY, EMPTY] and alone["plan"]["hold_points"] == 0.0

"""Stash planner (#218, app/analytics/stash_planner.py) on synthetic rosters: a stash adds starting points only in later
weeks (the #216 evaluator's exact lineup difference), current-week pickups are kept apart, the drop candidate is the
cheapest active player, availability / position limits / IR are respected, missing projections abstain, and the
contingency list is if-then evidence from the opportunity tree with no probability."""
import pytest

import compat_fixtures as cf
from app.analytics import stash_planner as sp
from app.analytics.lineup import lineup_slots
from app.analytics.marginal_utility import scenario
from app.analytics.team_lineups import RosterPlayer, Window

STD = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WIN = Window((1, 2, 3, 4, 5, 6, 7), 5)


def P(pid, pos, **kw):
    return RosterPlayer(pid, pos, pro_team="KC", name=f"P{pid}", **kw)


def roster():
    return [P(1, "QB"), P(2, "RB"), P(3, "RB"), P(4, "RB"), P(5, "WR"), P(6, "WR"), P(7, "WR"), P(8, "TE")]


def weekly(v, **over):
    return {w: over.get(f"w{w}", v) for w in WIN.weeks}


def proj(extra=None):
    base = {1: weekly(20.0), 2: weekly(15.0), 3: weekly(12.0, w3=0.0), 4: weekly(9.0), 5: weekly(14.0),
            6: weekly(11.0), 7: weekly(3.0), 8: weekly(8.0)}
    base.update(extra or {})
    return base


FAS = [P(31, "RB"), P(32, "WR"), P(33, "TE"), P(34, "RB"), P(35, "RB"), P(36, "K")]
STATUS = {31: "FREEAGENT", 32: "WAIVERS", 33: "FREEAGENT", 34: "FREEAGENT", 36: "FREEAGENT"}     # 35: unknown


def plan(players=None, projections=None, **kw):
    pr = projections or proj({31: weekly(5.0, w3=11.0), 32: weekly(18.0), 33: weekly(2.0), 34: weekly(10.0, w4=None),
                                35: weekly(30.0), 36: weekly(9.0)})
    pr = {k: {w: v for w, v in d.items() if v is not None} for k, d in pr.items()}
    args = dict(free_agents=FAS, free_agent_status=STATUS, active_capacity=len(players or roster()),
                horizon=4, projection_source="ESPN")
    args.update(kw)
    return sp.plan_stashes(players or roster(), STD, WIN, pr, **args), pr


def test_a_stash_adds_only_later_and_a_current_week_add_is_kept_apart():
    res, pr = plan()
    assert res["status"] == "evaluated" and res["now_week"] == 1 and res["later_weeks"] == [2, 3, 4, 5]
    ids = [x["espn_player_id"] for x in res["stashes"]]
    assert 31 in ids and 32 not in ids                                    # RB 31 covers RB 3's week-3 bye
    assert [x["espn_player_id"] for x in res["pick_up_now"]] == [32]      # WR 32 starts this week already
    s31 = next(x for x in res["stashes"] if x["espn_player_id"] == 31)
    assert s31["this_week_delta"] <= 0 and s31["later_delta"] > 0 and s31["first_start_week"] == 3
    # RB 3's bye pushes WR 7 (3 points) into FLEX in week 3; he is the drop, so RB 31 (11) starts there: +8
    assert s31["why"].startswith("week 3: starts in place of P7 (the drop) (+8.0)")
    drop = res["drop"]
    hw = Window((1, 2, 3, 4, 5), 5)
    direct = scenario(roster(), STD, hw, pr, remove=[drop["espn_player_id"]], add=[FAS[0]])
    assert s31["horizon_delta"] == direct["ros_delta"]                    # the #216 evaluator, exactly
    # the cheapest drop: WR 7 starts only in RB 3's bye week (3 points), every other removal costs more
    assert drop["espn_player_id"] == 7 and drop["horizon_cost"] == -3.0


def test_availability_projections_and_positions_are_respected():
    res, _ = plan()
    c = res["counts"]
    assert c["considered"] == 4                       # 35 (unknown availability) and 36 (a kicker) are never planned
    assert c["excluded_unprojected"] == 1             # 34 has no week-4 projection: unknown, not 0
    assert all(x["espn_player_id"] not in (34, 35, 36) for x in res["stashes"] + res["pick_up_now"])


def test_an_open_roster_spot_means_no_drop():
    res, _ = plan(active_capacity=len(roster()) + 1)
    assert res["drop"] is None and res["drop_basis"] == "open_spot"


def test_ir_players_are_never_the_drop():
    players = roster() + [P(20, "RB", on_ir=True)]
    pr = proj({20: weekly(0.0), 31: weekly(5.0, w3=11.0), 32: weekly(18.0), 33: weekly(2.0), 34: weekly(10.0),
                 36: weekly(9.0)})
    res, _ = plan(players, pr, active_capacity=len(roster()))
    assert res["drop"]["espn_player_id"] != 20


def test_a_full_position_limit_excludes_additions_at_that_position():
    res, _ = plan(position_limits={"RB": 3})          # three RBs held; the drop (a WR) frees no RB spot
    assert all(x["position"] != "RB" for x in res["stashes"] + res["pick_up_now"])
    assert res["counts"]["excluded_position_limit"] >= 1


def test_a_missing_starter_projection_leaves_no_drop_to_choose():
    pr = proj({2: {w: 15.0 for w in WIN.weeks if w != 2}, 31: weekly(5.0)})
    res, _ = plan(projections=pr)
    assert res["status"] == "drop_unknown" and res["stashes"] == [] and "unknown" in res["reason"]


def test_horizons_and_short_seasons():
    with pytest.raises(sp.StashPlanError):
        plan(horizon=3)
    res = sp.plan_stashes(roster(), STD, Window((7,), 5), proj(), free_agents=[], free_agent_status={},
                          active_capacity=8, projection_source="ESPN")
    assert res["status"] == "no_later_weeks"
    six, _ = plan(horizon=6)
    assert six["weeks"] == [1, 2, 3, 4, 5, 6, 7]


def test_negative_projections_follow_the_evaluator():
    pr = proj({7: weekly(-2.0), 31: weekly(-1.0, w3=11.0), 32: weekly(18.0), 33: weekly(2.0), 34: weekly(10.0),
                 36: weekly(9.0)})
    res, pr = plan(projections=pr)
    hw = Window((1, 2, 3, 4, 5), 5)
    for x in res["stashes"] + res["pick_up_now"]:
        fa = next(p for p in FAS if p.espn_player_id == x["espn_player_id"])
        direct = scenario(roster(), STD, hw, pr, remove=[res["drop"]["espn_player_id"]], add=[fa])
        assert x["horizon_delta"] == direct["ros_delta"]


def test_a_three_flex_superflex_league_is_exact_and_deterministic():
    slots = lineup_slots(cf.FX17)
    positions = ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 8 + ["TE"] * 3
    players = [P(100 + i, pos) for i, pos in enumerate(positions)]
    pr = {p.espn_player_id: weekly(float(5 + (p.espn_player_id * 7) % 13)) for p in players}
    pr[105] = weekly(14.0, w2=0.0, w3=0.0)                               # a two-week bye stack for one RB
    fas = [P(300 + i, pos) for i, pos in enumerate(["RB", "WR", "QB", "TE"])]
    for i, f in enumerate(fas):
        pr[f.espn_player_id] = weekly(4.0 + i, w2=12.0, w3=12.0)
    status = {f.espn_player_id: "FREEAGENT" for f in fas}
    a = sp.plan_stashes(players, slots, WIN, pr, free_agents=fas, free_agent_status=status,
                        active_capacity=len(players), projection_source="ESPN")
    b = sp.plan_stashes(players, slots, WIN, pr, free_agents=fas, free_agent_status=status,
                        active_capacity=len(players), projection_source="ESPN")
    assert a == b and a["status"] == "evaluated"
    hw = Window((1, 2, 3, 4, 5), 5)
    for x in a["stashes"]:
        fa = next(f for f in fas if f.espn_player_id == x["espn_player_id"])
        assert x["horizon_delta"] == scenario(players, slots, hw, pr, remove=[a["drop"]["espn_player_id"]],
                                              add=[fa])["ros_delta"]


def test_dominated_free_agents_are_pruned_and_counted():
    res, _ = plan(projections=proj({31: weekly(5.0, w3=11.0), 32: weekly(18.0), 33: weekly(2.0), 34: weekly(4.0, w3=10.0),
                                      36: weekly(9.0)}))
    assert res["counts"]["dominated"] >= 1                       # RB 34 is below RB 31 in every week
    assert all(x["espn_player_id"] != 34 for x in res["stashes"])


def test_contingencies_are_if_then_evidence_for_claimable_players_only():
    effect = lambda pid, claim, basis: {"espn_player_id": pid, "full_name": f"N{pid}", "position": "RB",    # noqa: E731
                                        "ownership": {"claim": claim}, "basis": basis,
                                        "observed": {"with": 4.0, "without": 11.0}, "reasons": ["depth chart"]}
    tree = {"teams": [{"team": "KC", "scenarios": [
        {"kind": "teammate_unavailable", "subject": {"full_name": "Star RB", "position": "RB"},
         "certainty": "if_he_sits", "unit_label": "touches", "sample": {"team_games_without": 3}, "outcome": "supported",
         "effects": [effect(41, "free_agent", "observed"), effect(42, "opponent", "observed"),
                     effect(43, "waivers", "hypothesis")]},
        {"kind": "teammate_returning", "subject": {"full_name": "Back RB", "position": "RB"}, "outcome": "abstain",
         "effects": []}]}]}
    c = sp.contingencies(tree)
    assert [x["espn_player_id"] for x in c["items"]] == [41, 43]           # observed first; the opponent's never
    assert c["items"][0]["if"]["starter"] == "Star RB" and c["scenarios_abstained"] == 1
    text = repr(c).lower()
    assert "probab" not in text.replace("not a prediction", "") and "if-then" in c["note"].lower()


# ------------------------------------------------------------------------------ API --

def test_the_stash_route_is_read_only_and_labelled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db.connection import connect
    from test_trade_data import _raw_snapshot, synced
    db_path = tmp_path / "stash.duckdb"
    con = synced(db_path)
    raw = _raw_snapshot(con)
    con.close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    res = api.get("/api/my-team/stash")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["contract"] == "stash-plan/v1" and body["team"]["is_mine"] and body["horizon"] == 4
    assert body["projection_source"] and body["policy"] and any("probability" in n for n in body["notes"])
    assert "available" in body["contingencies"] and body["provenance"]
    assert body["status"] in ("evaluated", "no_later_weeks", "drop_unknown")
    other = next(t for t in api.get("/api/my-team/value").json()["teams"] if not t["is_mine"])
    two = api.get("/api/my-team/stash", params={"team_id": other["team_id"], "horizon": 2}).json()
    assert two["team"]["team_id"] == other["team_id"] and two["horizon"] == 2
    assert api.get("/api/my-team/stash", params={"horizon": 3}).status_code == 422
    assert api.get("/api/my-team/stash", params={"team_id": 999}).status_code == 404
    assert db_path.stat().st_mtime_ns == mtime                     # nothing written
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    con.close()


def test_an_internal_tree_error_is_not_served_as_an_unavailable_reason(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import team_value
    from test_trade_data import synced
    db_path = tmp_path / "stash.duckdb"
    synced(db_path).close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))

    def broken(con, team_id=None):
        raise KeyError("secret_col")
    monkeypatch.setattr(team_value, "build_opportunity_tree", broken)
    from app.main import app
    res = TestClient(app, raise_server_exceptions=False).get("/api/my-team/stash")
    assert res.status_code == 500 and "secret_col" not in res.text

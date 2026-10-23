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
    assert s31["why"].startswith("week 3: starts in place of P7 (the drop) (+8)")
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
    assert a["stashes"] and all(x["first_start_week"] in (2, 3) for x in a["stashes"])    # they cover the bye stack
    hw = Window((1, 2, 3, 4, 5), 5)
    for x in a["stashes"]:
        fa = next(f for f in fas if f.espn_player_id == x["espn_player_id"])
        assert x["horizon_delta"] == scenario(players, slots, hw, pr, remove=[a["drop"]["espn_player_id"]],
                                              add=[fa])["ros_delta"]


def run(players, pr, fas, status=None, **kw):
    args = dict(free_agents=fas, free_agent_status=status or {f.espn_player_id: "FREEAGENT" for f in fas},
                active_capacity=len([p for p in players if not p.reserved]), horizon=4, projection_source="ESPN")
    args.update(kw)
    return sp.plan_stashes(players, STD, WIN, pr, **args)


def test_a_net_loss_over_the_horizon_is_never_listed_and_stashes_rank_by_net_gain():
    # WR 6 is on bye in week 1, so WR 7 (9 then nothing) starts only then: he is the cheapest drop at -9
    pr = proj({6: weekly(11.0, w1=0.0), 7: weekly(0.0, w1=9.0), 32: weekly(0.0, w5=10.0),
               33: weekly(0.0, w1=9.0, w5=10.0), 34: weekly(9.0, w3=11.0)})
    fas = [P(32, "WR"), P(33, "WR"), P(34, "RB")]
    res = run(roster(), pr, fas)
    assert res["drop"]["espn_player_id"] == 7 and res["drop"]["horizon_cost"] == -9.0
    listed = {x["espn_player_id"]: x for x in res["stashes"] + res["pick_up_now"]}
    assert 32 not in listed                         # -9 this week, +1 in week 5: a net loss, counted as no gain
    # RB 34 costs the drop's week (-9, stated) and more than makes it up in RB 3's bye (+11): net +2, ranked first
    assert [(x["espn_player_id"], x["this_week_delta"], x["horizon_delta"]) for x in res["stashes"]] == \
        [(34, -9.0, 2.0), (33, 0.0, 1.0)]
    assert res["stashes"][0]["why"].startswith("this week: -9, P7 (the drop) leaves the lineup; week 3: starts")
    assert res["counts"]["no_gain"] >= 1


def test_an_unverified_roster_count_never_assumes_an_open_spot():
    players = roster()
    res, _ = plan(players, active_capacity=len(players) + 1)
    assert res["drop_basis"] == "open_spot"
    res, _ = plan(players, active_capacity=len(players) + 1, roster_count_unverified="2 players are not synced.",
                  undroppable_list_in_use=True)
    assert res["drop"]["espn_player_id"] == 7 and res["drop_basis"] == "roster_count_unverified"
    assert res["drop_notes"][0].startswith("2 players are not synced.") and "undroppable" in res["drop_notes"][1]


def test_free_agents_who_can_never_start_do_not_crowd_out_a_real_stash():
    # 45 free-agent QBs at 15 a week never beat the rostered QB (20) in a one-QB league: no lineup can gain from them
    qbs = [P(500 + i, "QB") for i in range(45)]
    pr = proj({31: weekly(5.0, w3=11.0), **{q.espn_player_id: weekly(15.0 - i / 100) for i, q in enumerate(qbs)}})
    res = run(roster(), pr, [*qbs, P(31, "RB")])
    assert [x["espn_player_id"] for x in res["stashes"]] == [31]
    assert res["counts"]["no_possible_gain"] == 45 and res["counts"]["evaluated"] == 1


def test_the_evaluation_cap_is_per_position_and_counted():
    rbs = [P(600 + i, "RB") for i in range(sp.MAX_PER_POSITION + 3)]
    pr = proj({p.espn_player_id: weekly(1.0, w3=11.0 + i / 10) for i, p in enumerate(rbs)} | {32: weekly(0.0, w4=16.0)})
    res = run(roster(), pr, [*rbs, P(32, "WR")])
    assert res["counts"]["not_evaluated_cap"] == 3 and res["counts"]["evaluated"] == sp.MAX_PER_POSITION + 1
    assert res["counts"]["stashes"] == sp.MAX_PER_POSITION + 1          # the WR is evaluated past the RB cap


def test_an_nfl_ineligible_free_agent_counts_as_zero_and_hides_nobody():
    pr = proj({31: weekly(5.0, w3=11.0), 40: weekly(6.0, w3=12.0)})
    res = run(roster(), pr, [P(31, "RB"), P(40, "RB", unavailable_weeks=frozenset(WIN.weeks))])
    assert [x["espn_player_id"] for x in res["stashes"]] == [31]


def test_a_current_week_add_does_not_hide_a_stash_at_his_position():
    pr = proj({31: weekly(2.0, w3=11.0), 32: weekly(9.5, w3=12.0)})
    res = run(roster(), pr, [P(31, "RB"), P(32, "RB")])
    assert [x["espn_player_id"] for x in res["pick_up_now"]] == [32]
    assert [x["espn_player_id"] for x in res["stashes"]] == [31]


@pytest.mark.parametrize("league", ["std", "fx17", "op"])
def test_the_gain_bound_never_skips_a_real_gain(league):
    """The planner's rule, checked against the exact evaluator on random rosters: short ones (empty slots), negative
    projections, NFL-ineligible weeks and a random drop (or none)."""
    import random
    rng = random.Random(218)
    slots = {"std": STD, "fx17": lineup_slots(cf.FX17), "op": lineup_slots({"QB": 1, "FLEX": 1, "OP": 1})}[league]
    shape = {"std": ["QB", "RB", "RB", "RB", "WR", "WR", "WR", "TE"], "op": ["QB", "RB", "WR", "TE"],
             "fx17": ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 7 + ["TE"] * 3}[league]
    hw = Window((1, 2, 3, 4, 5), 5)
    weeks = list(hw.weeks)
    for trial in range(120):
        players = [P(100 + i, pos) for i, pos in enumerate(shape) if rng.random() > 0.2]
        fas = [P(300 + i, rng.choice(sp.STASH_POSITIONS),
                 unavailable_weeks=frozenset(w for w in WIN.weeks if rng.random() < 0.1)) for i in range(8)]
        lo = -3 if rng.random() < 0.4 else 0
        pr = {p.espn_player_id: {w: round(rng.uniform(lo, 25), 1) * (rng.random() > 0.15) for w in WIN.weeks}
              for p in players + fas}
        base = sp.project_team(players, slots, hw, pr)
        drop = rng.choice([None, *players]) if players else None
        rest = players if drop is None else [p for p in players if p is not drop]
        removal = 0.0 if drop is None else scenario(players, slots, hw, pr, remove=[drop.espn_player_id],
                                                    base=base)["_ros_exact"]
        gains = removal > sp.EPS
        after = sp.project_team(rest, slots, hw, pr)
        floors = sp._floors(after, weeks)
        for fa in fas:
            if gains:
                gain = scenario(rest, slots, hw, pr, add=[fa], base=after)["_ros_exact"]
            else:
                gain = scenario(players, slots, hw, pr, remove=[drop.espn_player_id] if drop else (), add=[fa],
                                base=base)["_ros_exact"]
            bound = sp._bound(fa, floors, weeks, pr) + (0.0 if gains else removal)
            assert gain <= bound + 1e-9, (league, trial, fa)


def test_the_flex_solver_leaving_slots_empty_is_never_bounded():
    # a flex total at or below -1 leaves every flex slot empty: the roster's negative weeks have no bound, so TE 31 is
    # solved (he gains more than his own projection in every week) rather than skipped
    slots = lineup_slots({"QB": 1, "FLEX": 1, "OP": 1})
    w = Window((1, 2, 3), 3)
    players = [P(1, "RB"), P(2, "WR")]
    pr = {1: {1: 1.0, 2: 1.0, 3: 1.0}, 2: {1: -2.0, 2: -2.0, 3: -2.0}, 31: {1: 0.0, 2: 0.0, 3: 3.0}}
    floors = sp._floors(sp.project_team(players, slots, w, pr), list(w.weeks))
    assert all(f is not None and all(v == float("-inf") for v in f.values()) for f in floors.values())
    res = sp.plan_stashes(players, slots, w, pr, free_agents=[P(31, "TE")], free_agent_status={31: "FREEAGENT"},
                          active_capacity=3, horizon=2, projection_source="ESPN")
    direct = scenario(players, slots, w, pr, add=[P(31, "TE")])["_ros_exact"]
    assert res["counts"]["no_possible_gain"] == 0
    assert [(x["espn_player_id"], x["horizon_delta"]) for x in res["pick_up_now"]] == [(31, round(direct, 2))]


def test_a_negative_player_out_of_reach_keeps_the_bound():
    # a negative bench D/ST can fill no RB-reachable slot, so 55 RBs who never start are still skipped exactly and the
    # one real stash (RB 3's bye week) is found
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1})
    players = [P(0, "WR")] + roster() + [P(9, "D/ST"), P(10, "D/ST")]    # WR 0 (bench, 0.5): the drop, not the D/ST
    rbs = [P(700 + i, "RB") for i in range(55)]
    pr = proj({0: weekly(0.5), 9: weekly(6.0), 10: weekly(-1.0), 31: weekly(0.0, w3=4.0),
               **{p.espn_player_id: weekly(2.9) for p in rbs}})
    res = sp.plan_stashes(players, slots, WIN, pr, free_agents=[*rbs, P(31, "RB")],
                          free_agent_status={p.espn_player_id: "FREEAGENT" for p in [*rbs, P(31, "RB")]},
                          active_capacity=len(players), projection_source="ESPN")
    assert res["drop"]["espn_player_id"] == 0
    after = sp.project_team([p for p in players if p.espn_player_id != 0], slots, Window((1, 2, 3, 4, 5), 5), pr)
    assert all(f["RB"] > float("-inf") for f in sp._floors(after, [1, 2, 3, 4, 5]).values())
    assert [x["espn_player_id"] for x in res["stashes"]] == [31] and res["counts"]["no_possible_gain"] == 55


def test_an_unsupported_slot_keeps_the_bound():
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "LB": 1})
    base = sp.project_team(roster(), slots, WIN, proj())
    assert all(f is not None for f in sp._floors(base, list(WIN.weeks)).values())


def test_a_drop_whose_removal_adds_points_is_part_of_the_bound():
    # Codex's case: a forced negative D/ST is the drop, so removing him alone gains +2 a week (+4). That change is
    # reported on its own and free agents are measured against the roster without him: RB 32 beats the rostered RB in
    # week 3 (+3), RB 31 (1 a week) adds nothing and is not credited with the drop's gain
    slots = lineup_slots({"RB": 1, "D/ST": 1})
    w = Window((1, 2, 3), 3)
    players = [P(1, "RB"), P(2, "D/ST")]
    pr = {1: {1: 10.0, 2: 10.0, 3: 10.0}, 2: {1: 0.0, 2: -2.0, 3: -2.0}, 31: {1: 1.0, 2: 1.0, 3: 1.0},
          32: {1: 1.0, 2: 1.0, 3: 13.0}}
    res = sp.plan_stashes(players, slots, w, pr, free_agents=[P(31, "RB"), P(32, "RB")],
                          free_agent_status={31: "FREEAGENT", 32: "FREEAGENT"}, active_capacity=2, horizon=2,
                          projection_source="ESPN")
    assert res["drop"]["espn_player_id"] == 2 and res["drop"]["horizon_cost"] == 4.0
    assert "alone changes the starting lineup by +4" in res["drop_notes"][0]
    assert [(x["espn_player_id"], x["horizon_delta"]) for x in res["stashes"]] == [(32, 3.0)]
    assert res["counts"]["no_possible_gain"] == 1 and res["counts"]["evaluated"] == 1


def test_starter_injuries_follow_projections_and_verified_unavailability_only():
    res, _ = plan()
    questionable = [P(2, "RB", injury_status="QUESTIONABLE") if p.espn_player_id == 2 else p for p in roster()]
    assert plan(questionable)[0] == res                       # a designation alone changes nothing: no probability
    hurt = [P(2, "RB", unavailable_weeks=frozenset({2, 3})) if p.espn_player_id == 2 else p for p in roster()]
    pr = proj({31: weekly(5.0, w3=11.0), 32: weekly(1.0), 33: weekly(2.0), 34: weekly(7.0, w1=0.0),
               36: weekly(9.0)})
    out = run(hurt, pr, [P(31, "RB"), P(34, "RB")])
    s34 = next(x for x in out["stashes"] if x["espn_player_id"] == 34)
    assert s34["first_start_week"] == 2 and s34["this_week_delta"] <= 0


def test_only_known_availability_is_planned_and_no_claim_is_promised():
    fas = [P(31, "RB"), P(37, "RB"), P(38, "RB"), P(39, "RB")]
    pr = proj({f.espn_player_id: weekly(5.0, w3=11.0 + i) for i, f in enumerate(fas)})
    res = run(roster(), pr, fas, status={31: "WAIVERS", 37: "ONTEAM", 38: None})
    assert [(x["espn_player_id"], x["availability"]) for x in res["stashes"]] == [(31, "WAIVERS")]
    assert res["counts"]["considered"] == 1 and "bid" not in repr(res).lower().replace("bids are not planned", "")


def test_unknown_values_say_so():
    pr = proj({2: {w: 15.0 for w in WIN.weeks if w != 2}, 31: weekly(5.0, w3=11.0)})
    res = run(roster(), pr, [P(31, "RB")], active_capacity=9)
    assert res["drop_basis"] == "open_spot" and res["counts"]["unknown"] == 1 and "unknown" in res["reason"]


def test_contingencies_are_if_then_evidence_for_claimable_players_only():
    effect = lambda pid, claim, basis: {"espn_player_id": pid, "full_name": f"N{pid}", "position": "RB",    # noqa: E731
                                        "ownership": {"claim": claim}, "basis": basis,
                                        "observed": {"with": 4.0, "without": 11.0}, "reasons": ["depth chart"]}
    game = {"week": 1, "started": False, "on_bye": False, "kickoff": "2026-09-13T17:00:00Z"}
    tree = {"generated_at": "2026-09-10T12:00:00Z", "sources": {"depth_chart_as_of": "2026-09-09"},
            "notices": [{"tone": "info", "code": "waiver_status_unknown", "text": "unknown"}],
            "teams": [{"team": "KC", "game": game, "injury_freshness": {"status": "fresh", "reason": None},
                       "depth_chart": {"known": False, "as_of": None}, "coverage": {"status": "complete"},
                       "scenarios": [
        {"kind": "teammate_unavailable", "subject": {"full_name": "Star RB", "position": "RB"},
         "certainty": "if_he_sits", "unit_label": "touches", "sample": {"team_games_without": 3}, "outcome": "supported",
         "effects": [effect(41, "free_agent", "observed"), effect(42, "opponent", "observed"),
                     effect(43, "waivers", "hypothesis")]},
        {"kind": "teammate_returning", "subject": {"full_name": "Back RB", "position": "RB"}, "outcome": "abstain",
         "abstain": {"code": "depth_unknown", "text": "no depth chart"}, "effects": []}]}]}
    c = sp.contingencies(tree)
    assert [x["espn_player_id"] for x in c["items"]] == [41, 43]           # observed first; the opponent's never
    assert c["items"][0]["if"]["starter"] == "Star RB" and c["scenarios_abstained"] == 1
    assert c["abstained_by_code"] == {"depth_unknown": 1}
    assert c["items"][0]["game"] == game and c["items"][0]["depth_chart"] == {"known": False, "as_of": None}
    assert c["generated_at"] == tree["generated_at"] and c["sources"] == tree["sources"]
    assert [n["code"] for n in c["notices"]] == ["waiver_status_unknown"]
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

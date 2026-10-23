"""Marginal roster utility (#216, app/analytics/marginal_utility.py) on synthetic rosters: every value is a difference of
exact optimal lineups (parity with assign_lineup and roster_marginal_value), joint removals are never summed, the
replacement pool is explicit, and a missing projection is unknown, never 0."""
import pytest

import compat_fixtures as cf
from app.analytics.lineup import assign_lineup, lineup_slots, lineup_total
from app.analytics.marginal_utility import MarginalUtilityError, marginal_utility, scenario
from app.analytics.team_lineups import RosterPlayer, Window, roster_marginal_value

STD = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
SF = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "OP": 1})
WIN = Window((1, 2, 3), 2)


def P(pid, pos, **kw):
    return RosterPlayer(pid, pos, pro_team="KC", name=f"P{pid}", **kw)


def roster():
    return [P(1, "QB"), P(2, "RB"), P(3, "RB"), P(4, "RB"), P(5, "WR"), P(6, "WR"), P(7, "WR"), P(8, "TE"),
            P(9, "QB")]


def proj(**over):
    base = {1: 20.0, 2: 15.0, 3: 12.0, 4: 9.0, 5: 14.0, 6: 11.0, 7: 3.0, 8: 8.0, 9: 16.0}
    base.update(over)
    return {pid: {w: v for w in WIN.weeks} for pid, v in base.items()}


def reference_total(players, slots, projections):
    """What the optimizer returns for every week, summed: assign_lineup over the active players with a projection."""
    return sum(lineup_total(assign_lineup([{"espn_player_id": p.espn_player_id, "position": p.position,
                                           "value": projections[p.espn_player_id][w]}
                                          for p in players if not p.reserved and w in projections.get(p.espn_player_id, {})],
                                         slots)) for w in WIN.weeks)


def test_a_removal_is_the_exact_optimizer_difference_and_matches_roster_marginal_value():
    players, pr = roster(), proj()
    res = marginal_utility(players, STD, WIN, pr, [2], projection_source="ESPN")
    rest = [p for p in players if p.espn_player_id != 2]
    expected = reference_total(rest, STD, pr) - reference_total(players, STD, pr)
    # RB4 (9) moves from FLEX to RB2's slot and WR3 (3) enters at FLEX: 12 points a week
    assert res["joint"]["ros_delta"] == pytest.approx(expected) == pytest.approx(-12.0 * 3)
    mv = roster_marginal_value(players, 2, STD, WIN, pr)
    assert res["joint"]["ros_delta"] == -mv["roster_marginal_lineup_points"]
    week = res["joint"]["weeks"][0]
    assert week["left_lineup"] == [2] and week["entered_lineup"] == [7]              # the replacement path
    assert res["projection_source"] == "ESPN" and res["horizon"]["weeks"] == [1, 2, 3] and res["unit"]


def test_an_unused_bench_player_changes_no_starting_lineup():
    res = marginal_utility(roster(), STD, WIN, proj(), [7])                           # WR3 never starts
    assert res["joint"]["ros_delta"] == 0.0
    assert all(w["entered_lineup"] == [] == w["left_lineup"] for w in res["joint"]["weeks"])


def test_joint_removals_are_evaluated_jointly_never_summed():
    # RB3 backfills FLEX when RB2 goes, and RB2 covers when RB3 goes: each alone costs little, together much more
    res = marginal_utility(roster(), STD, WIN, proj(), [2, 3])
    joint = res["joint"]["ros_delta"]
    assert res["sum_of_single_deltas"] == pytest.approx(sum(res["single_deltas"].values()))
    assert res["nonadditive"] is True and joint < res["sum_of_single_deltas"]
    rest = [p for p in roster() if p.espn_player_id not in (2, 3)]
    assert joint == pytest.approx(reference_total(rest, STD, proj()) - reference_total(roster(), STD, proj()))
    assert "backfill" in res["nonadditive_note"]


def test_superflex_qbs_back_each_other_up():
    res = marginal_utility(roster(), SF, WIN, proj(), [1, 9])
    assert res["nonadditive"] is True                       # QB2 fills OP; losing both empties QB and OP together
    rest = [p for p in roster() if p.espn_player_id not in (1, 9)]
    assert res["joint"]["ros_delta"] == pytest.approx(reference_total(rest, SF, proj())
                                                     - reference_total(roster(), SF, proj()))


def test_a_missing_starter_projection_makes_the_week_unknown_not_zero():
    pr = proj()
    del pr[5][2]                                            # WR1 has no week-2 projection
    res = marginal_utility(roster(), STD, WIN, pr, [2])
    week2 = next(w for w in res["joint"]["weeks"] if w["week"] == 2)
    assert week2["delta"] is None and week2["known_delta"] is not None
    assert res["joint"]["ros_delta"] is None and 2 in res["joint"]["weeks_incomplete"]
    assert res["joint"]["known_ros_delta"] is not None


def test_ir_and_taxi_players_start_no_lineup_and_say_so():
    players = roster() + [P(20, "RB", on_ir=True), P(21, "WR", on_taxi=True)]
    pr = {**proj(), 20: {w: 30.0 for w in WIN.weeks}, 21: {w: 30.0 for w in WIN.weeks}}
    res = marginal_utility(players, STD, WIN, pr, [20, 21])
    assert res["joint"]["ros_delta"] == 0.0 and res["reserved"] == [20, 21] and "IR or taxi" in res["reserved_note"]


def test_a_waiver_replacement_needs_a_supplied_pool_and_respects_slot_eligibility():
    unknown = marginal_utility(roster(), STD, WIN, proj(), [2], pool="waiver")
    assert unknown["status"] == "unknown_pool" and unknown["joint"] is None and unknown["replacement"] is None
    fas = [P(30, "QB"), P(31, "RB"), P(32, "K")]           # capped pool: only the RB can replace an RB
    pr = {**proj(), 30: {w: 40.0 for w in WIN.weeks}, 31: {w: 13.0 for w in WIN.weeks},
          32: {w: 50.0 for w in WIN.weeks}}
    res = marginal_utility(roster(), STD, WIN, pr, [2], pool="waiver", free_agents=fas)
    assert res["replacement"]["espn_player_id"] == 31 and res["replacement"]["kind"] == "waiver"
    assert res["joint"]["ros_delta"] == pytest.approx((13.0 - 15.0) * 3)       # RB 13 starts in RB2's place
    assert "not guaranteed" in res["replacement"]["note"]
    with pytest.raises(MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, pr, [2, 3], pool="waiver", free_agents=fas)


def test_a_hypothetical_acquisition_is_labelled_and_exact():
    pr = {**proj(), 40: {w: 18.0 for w in WIN.weeks}}
    res = marginal_utility(roster(), STD, WIN, pr, [2, 3], pool="acquisition", acquisition=P(40, "RB"))
    assert res["replacement"]["kind"] == "hypothetical acquisition"
    rest = [p for p in roster() if p.espn_player_id not in (2, 3)] + [P(40, "RB")]
    assert res["joint"]["ros_delta"] == pytest.approx(reference_total(rest, STD, pr)
                                                     - reference_total(roster(), STD, pr))
    # the nonadditivity check compares removals only, never the acquisition
    assert res["joint_removal_delta"] != res["joint"]["ros_delta"] and res["nonadditive"] is True


def test_a_negative_projection_still_follows_the_optimizer():
    pr = proj()
    pr[7] = {w: -2.0 for w in WIN.weeks}
    pr[4] = {w: -1.0 for w in WIN.weeks}                   # the only bench backfill is negative
    res = marginal_utility(roster(), STD, WIN, pr, [2])
    rest = [p for p in roster() if p.espn_player_id != 2]
    assert res["joint"]["ros_delta"] == pytest.approx(reference_total(rest, STD, pr) - reference_total(roster(), STD, pr))


def test_fx17_lineup_parity():
    slots = lineup_slots(cf.FX17)
    positions = ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 8 + ["TE"] * 3 + ["K", "D/ST"]
    players = [P(100 + i, pos) for i, pos in enumerate(positions)]
    pr = {p.espn_player_id: {w: float(5 + (p.espn_player_id * 7) % 13) for w in WIN.weeks} for p in players}
    for gone in ([100], [103, 104], [109, 110, 111]):
        res = marginal_utility(players, slots, WIN, pr, gone)
        rest = [p for p in players if p.espn_player_id not in gone]
        assert res["joint"]["ros_delta"] == pytest.approx(reference_total(rest, slots, pr)
                                                         - reference_total(players, slots, pr))


def test_bad_requests_are_refused():
    with pytest.raises(MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, proj(), [])
    with pytest.raises(MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, proj(), [999])
    with pytest.raises(MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, proj(), [2], pool="trade")
    with pytest.raises(MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, proj(), [2], pool="acquisition")
    assert scenario(roster(), STD, WIN, proj())["ros_delta"] == 0.0          # no change, no delta

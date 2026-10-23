"""Marginal roster utility (#216, app/analytics/marginal_utility.py) on synthetic rosters: every value is a difference of
exact optimal lineups (parity with assign_lineup and roster_marginal_value), joint removals are never summed, the
replacement pool is explicit, and a missing projection is unknown, never 0."""
from functools import partial

import compat_fixtures as cf
import pytest

from app.analytics import marginal_utility as mu
from app.analytics.lineup import assign_lineup, lineup_slots, lineup_total
from app.analytics.team_lineups import (
    STREAMER_IDS,
    RosterPlayer,
    StreamingProjections,
    Window,
    roster_marginal_value,
)

STD = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
SF = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "OP": 1})
WIN = Window((1, 2, 3), 2)
marginal_utility = partial(mu.marginal_utility, projection_source="ESPN")


def P(pid, pos, **kw):
    return RosterPlayer(pid, pos, pro_team="KC", name=f"P{pid}", **kw)


def roster():
    return [P(1, "QB"), P(2, "RB"), P(3, "RB"), P(4, "RB"), P(5, "WR"), P(6, "WR"), P(7, "WR"), P(8, "TE"),
            P(9, "QB")]


def proj(**over):
    base = {1: 20.0, 2: 15.0, 3: 12.0, 4: 9.0, 5: 14.0, 6: 11.0, 7: 3.0, 8: 8.0, 9: 16.0}
    base.update(over)
    return {pid: {w: v for w in WIN.weeks} for pid, v in base.items()}


def const(v):
    return {w: v for w in WIN.weeks}


def reference_total(players, slots, projections):
    """What the optimizer returns for every week, summed: assign_lineup over the active players with a projection
    who are not NFL-ineligible that week."""
    return sum(lineup_total(assign_lineup([{"espn_player_id": p.espn_player_id, "position": p.position,
                                           "value": projections[p.espn_player_id][w]}
                                          for p in players if not p.reserved and w not in p.unavailable_weeks
                                          and w in projections.get(p.espn_player_id, {})], slots))
               for w in WIN.weeks)


def delta(players, gone, slots, projections, added=()):
    rest = [p for p in players if p.espn_player_id not in gone] + list(added)
    return reference_total(rest, slots, projections) - reference_total(players, slots, projections)


def test_a_removal_is_the_exact_optimizer_difference_and_matches_roster_marginal_value():
    players, pr = roster(), proj()
    res = marginal_utility(players, STD, WIN, pr, [2])
    # RB4 (9) moves from FLEX to RB2's slot and WR3 (3) enters at FLEX: 12 points a week
    assert res["removal"]["ros_delta"] == pytest.approx(delta(players, {2}, STD, pr)) == pytest.approx(-12.0 * 3)
    assert res["change"] == res["removal"]                                           # bench: no replacement added
    mv = roster_marginal_value(players, 2, STD, WIN, pr)
    assert res["removal"]["ros_delta"] == -mv["roster_marginal_lineup_points"]
    week = res["change"]["weeks"][0]
    assert week["left_lineup"] == [2] and week["entered_lineup"] == [7]              # the replacement path
    assert res["projection_source"] == "ESPN" and res["horizon"]["weeks"] == [1, 2, 3] and res["unit"]
    assert res["baseline"]["ros_projected_starting_points"] == pytest.approx(reference_total(players, STD, pr))
    assert "locks" in res["horizon"]["note"] and res["streaming"] == {"enabled": False, "positions": []}


def test_the_projection_source_is_required():
    with pytest.raises(TypeError):
        mu.marginal_utility(roster(), STD, WIN, proj(), [2])
    for source in (None, ""):
        with pytest.raises(mu.MarginalUtilityError):
            mu.marginal_utility(roster(), STD, WIN, proj(), [2], projection_source=source)


def test_an_unused_bench_player_changes_no_starting_lineup():
    res = marginal_utility(roster(), STD, WIN, proj(), [7])                           # WR3 never starts
    assert res["change"]["ros_delta"] == 0.0
    assert all(w["entered_lineup"] == [] == w["left_lineup"] for w in res["change"]["weeks"])


def test_joint_removals_are_evaluated_jointly_never_summed():
    # RB3 backfills FLEX when RB2 goes, and RB2 covers when RB3 goes: each alone costs little, together much more
    res = marginal_utility(roster(), STD, WIN, proj(), [2, 3])
    joint = res["change"]["ros_delta"]
    assert res["nonadditive"] is True and joint < res["sum_of_single_deltas"]
    assert joint == pytest.approx(delta(roster(), {2, 3}, STD, proj()))
    assert "backfill" in res["nonadditive_note"]


def test_independent_removals_with_fractional_projections_are_additive():
    # QB9 backs up QB1 and nobody backs up the TE: the two removals are independent, so their sum is exact
    pr = proj()
    pr[1], pr[8] = const(20.002), const(8.002)
    res = marginal_utility(roster(), STD, WIN, pr, [1, 8])
    assert res["nonadditive"] is False                     # compared unrounded: the rounded singles differ by 0.01
    assert res["change"]["ros_delta"] == pytest.approx(delta(roster(), {1, 8}, STD, pr), abs=0.005)   # 2 dp shown


def test_superflex_qbs_back_each_other_up():
    res = marginal_utility(roster(), SF, WIN, proj(), [1, 9])
    assert res["nonadditive"] is True                       # QB2 fills OP; losing both empties QB and OP together
    assert res["change"]["ros_delta"] == pytest.approx(delta(roster(), {1, 9}, SF, proj()))


def test_a_missing_starter_projection_makes_the_week_unknown_not_zero():
    pr = proj()
    del pr[5][2]                                            # WR1 has no week-2 projection
    res = marginal_utility(roster(), STD, WIN, pr, [2])
    week2 = next(w for w in res["change"]["weeks"] if w["week"] == 2)
    assert week2["delta"] is None and week2["known_delta"] is not None
    assert res["change"]["ros_delta"] is None and 2 in res["change"]["weeks_incomplete"]
    assert res["change"]["known_ros_delta"] is not None


def test_an_nfl_ineligible_week_counts_as_the_optimizer_counts_it():
    players = [P(2, "RB", unavailable_weeks=frozenset({2})) if p.espn_player_id == 2 else p for p in roster()]
    res = marginal_utility(players, STD, WIN, proj(), [2])
    assert [w["delta"] for w in res["change"]["weeks"]] == [-12.0, 0.0, -12.0]
    assert res["change"]["ros_delta"] == pytest.approx(delta(players, {2}, STD, proj()))


def test_ir_and_taxi_players_start_no_lineup_and_say_so():
    players = roster() + [P(20, "RB", on_ir=True), P(21, "WR", on_taxi=True)]
    pr = {**proj(), 20: const(30.0), 21: const(30.0)}
    res = marginal_utility(players, STD, WIN, pr, [20, 21])
    assert res["change"]["ros_delta"] == 0.0 and res["reserved"] == [20, 21] and "IR or taxi" in res["reserved_note"]


def test_a_waiver_replacement_needs_a_supplied_pool_and_respects_slot_eligibility():
    unknown = marginal_utility(roster(), STD, WIN, proj(), [2], pool="waiver")
    assert unknown["status"] == "unknown_pool" and unknown["change"] is None and unknown["replacement"] is None
    fas = [P(30, "QB"), P(31, "RB"), P(32, "K")]           # capped pool: only the RB can replace an RB
    pr = {**proj(), 30: const(40.0), 31: const(13.0), 32: const(50.0)}
    res = marginal_utility(roster(), STD, WIN, pr, [2], pool="waiver", free_agents=fas)
    assert res["status"] == "evaluated" and res["replacement"]["espn_player_id"] == 31
    assert res["change"]["ros_delta"] == pytest.approx((13.0 - 15.0) * 3)       # RB 13 starts in RB2's place
    assert res["removal"]["ros_delta"] == pytest.approx(-12.0 * 3)              # the loss before the replacement
    assert "fully projected" in res["replacement"]["note"]
    known_empty = marginal_utility(roster(), STD, WIN, pr, [2], pool="waiver", free_agents=[])
    assert known_empty["status"] == "evaluated" and known_empty["replacement"] is None
    with pytest.raises(mu.MarginalUtilityError):
        marginal_utility(roster(), STD, WIN, pr, [2, 3], pool="waiver", free_agents=fas)


def test_a_rostered_or_removed_player_is_never_his_own_waiver_replacement():
    fas = [P(2, "RB"), P(3, "RB"), P(31, "RB")]            # a stale pool listing the removed player and a teammate
    pr = {**proj(), 31: const(10.0)}
    res = marginal_utility(roster(), STD, WIN, pr, [2], pool="waiver", free_agents=fas)
    assert res["replacement"]["espn_player_id"] == 31 and res["waiver_candidates"]["excluded_rostered"] == 2
    assert res["change"]["ros_delta"] == pytest.approx(delta(roster(), {2}, STD, pr, [P(31, "RB")]))


def test_a_free_agent_the_pool_cannot_score_makes_the_result_partial():
    fas = [P(31, "RB"), P(33, "RB")]
    pr = {**proj(), 31: {1: 30.0, 2: 30.0}, 33: const(10.0)}   # RB 31 has no week-3 projection
    res = marginal_utility(roster(), STD, WIN, pr, [2], pool="waiver", free_agents=fas)
    assert res["status"] == "partial_pool" and "better replacement may exist" in res["reason"]
    assert res["replacement"]["espn_player_id"] == 33 and res["waiver_candidates"]["excluded_unprojected"] == 1


def test_a_hypothetical_acquisition_is_labelled_and_exact():
    pr = {**proj(), 40: const(18.0)}
    res = marginal_utility(roster(), STD, WIN, pr, [2, 3], pool="acquisition", acquisition=P(40, "RB"))
    assert res["replacement"]["kind"] == "hypothetical acquisition"
    assert res["change"]["ros_delta"] == pytest.approx(delta(roster(), {2, 3}, STD, pr, [P(40, "RB")]))
    # the nonadditivity check compares removals only, never the acquisition
    assert res["removal"]["ros_delta"] != res["change"]["ros_delta"] and res["nonadditive"] is True


def test_a_kdst_streamer_appears_in_the_replacement_path():
    slots = lineup_slots({"QB": 1, "K": 1})
    players = [P(1, "QB"), P(50, "K")]
    base = {1: const(20.0), 50: const(10.0)}
    ref = {"K": {w: {"espn_player_id": 99, "name": "FA K", "points": 7.0} for w in WIN.weeks}}
    pr = StreamingProjections(base, {"K": const(7.0)}, ref, 1)
    res = marginal_utility(players, slots, WIN, pr, [50])
    week = res["change"]["weeks"][0]
    assert week["delta"] == -3.0 and week["left_lineup"] == [50] and week["entered_lineup"] == [STREAMER_IDS["K"]]
    assert res["streaming"]["enabled"] is True


def test_a_negative_projection_still_follows_the_optimizer():
    pr = proj()
    pr[7], pr[4] = const(-2.0), const(-1.0)                # the only bench backfill is negative
    res = marginal_utility(roster(), STD, WIN, pr, [2])
    assert res["change"]["ros_delta"] == pytest.approx(delta(roster(), {2}, STD, pr))


def test_fx17_lineup_parity():
    slots = lineup_slots(cf.FX17)
    positions = ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 8 + ["TE"] * 3 + ["K", "D/ST"]
    players = [P(100 + i, pos) for i, pos in enumerate(positions)]
    pr = {p.espn_player_id: const(float(5 + (p.espn_player_id * 7) % 13)) for p in players}
    for gone in ([100], [103, 104], [109, 110, 111]):
        res = marginal_utility(players, slots, WIN, pr, gone)
        assert res["change"]["ros_delta"] == pytest.approx(delta(players, set(gone), slots, pr))


def test_bad_requests_are_refused():
    for ids, kw in (([], {}), ([999], {}), (["x"], {}), ([2.9], {}), ([2], {"pool": "trade"}), ([2], {"pool": "acquisition"})):
        with pytest.raises(mu.MarginalUtilityError):
            marginal_utility(roster(), STD, WIN, proj(), ids, **kw)
    assert mu.scenario(roster(), STD, WIN, proj())["ros_delta"] == 0.0          # no change, no delta

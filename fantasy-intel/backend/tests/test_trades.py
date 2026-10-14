"""V2 trade engine: exact before/after weekly lineup simulation for both teams, roster capacity,
forced drops, open spots and result semantics. Pure, synthetic fantasy examples.

League: QB, RB x2, WR x2, TE, FLEX(RB/WR/TE). Weeks 10-11 regular season, week 12 playoffs."""

import copy

import pytest

from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import RosterPlayer, project_team
from app.analytics.trades import LeagueState, TradeError, evaluate_trade, result_label

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WEEKS = (10, 11, 12)


def P(pid, pos, team="KC"):
    return RosterPlayer(pid, pos, team, f"P{pid}")


def flat(v):
    return {w: v for w in WEEKS}


def team_a():
    """93/week: QB 20 | RB 15, 12 | WR 16, 11 | TE 10 | FLEX RB 9 || bench WR 8, TE 5"""
    players = [P(101, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"), P(107, "WR"),
               P(108, "TE"), P(109, "TE")]
    proj = {101: flat(20), 102: flat(15), 103: flat(12), 104: flat(9), 105: flat(16), 106: flat(11), 107: flat(8),
            108: flat(10), 109: flat(5)}
    return players, proj


def team_b():
    """QB 18 | RB 14, 11 | WR 13, 10 | TE 8 | FLEX RB 6 || bench WR 4, TE 3"""
    players = [P(201, "QB"), P(202, "RB"), P(203, "RB"), P(204, "WR"), P(205, "WR"), P(206, "TE"), P(207, "RB"),
               P(208, "WR"), P(209, "TE")]
    proj = {201: flat(18), 202: flat(14), 203: flat(11), 204: flat(13), 205: flat(10), 206: flat(8), 207: flat(6),
            208: flat(4), 209: flat(3)}
    return players, proj


def league(a=None, b=None, proj_over=None, fas=(), capacity=20, limits=None):
    pa, ja = team_a()
    pb, jb = team_b()
    proj = {**ja, **jb}
    for pid, v in (proj_over or {}).items():
        if v is None:
            proj.pop(pid, None)
        else:
            proj[pid] = v
    return LeagueState(season=2026, slots=SLOTS, final_week=12, last_regular=11, start_week=10,
                       rosters={1: a or pa, 2: b or pb}, free_agents=list(fas), projections=proj,
                       active_capacity=capacity, position_limits=limits or {}, team_names={1: "A", 2: "B"})


def usage(side, pid):
    return next(u for u in side["incoming_usage"] if u["espn_player_id"] == pid)


# --------------------------------------------------------------------- trade effects --

def test_one_for_one_each_player_simply_replaces_the_other():
    res = evaluate_trade(league(), 1, 2, [105], [204])
    a, b = res["team_a"], res["team_b"]
    assert res["trade"]["shape"] == "1-for-1"
    assert a["deltas"]["ros_delta"] == -9.0 and b["deltas"]["ros_delta"] == 9.0      # 16 <-> 13, three weeks
    assert a["deltas"]["regular_season_delta"] == -6.0 and a["deltas"]["playoff_delta"] == -3.0
    assert a["deltas"]["weeks_worsened"] == 3 and b["deltas"]["weeks_improved"] == 3
    assert a["deltas"]["average_weekly_delta"] == -3.0
    assert [(w["entered"], w["exited"]) for w in a["weekly"]] == [([204], [105])] * 3
    assert res["result"]["label"] == "TEAM_A_LOSS_TEAM_B_GAIN"
    assert res["result"]["combined_ros_delta"] == 0.0 and res["result"]["min_side_ros_delta"] == -9.0


def test_one_for_one_incoming_player_does_not_crack_the_lineup():
    res = evaluate_trade(league(), 1, 2, [109], [207])           # bench TE 5 for B's FLEX RB 6
    a, b = res["team_a"], res["team_b"]
    assert usage(a, 207)["weeks_started"] == []                   # RB 6 < A's FLEX RB 9
    assert a["deltas"]["ros_delta"] == 0.0 and a["deltas"]["weeks_unchanged"] == 3
    assert any("does not start" in n for n in a["fit_notes"])
    assert b["deltas"]["ros_delta"] == -3.0                       # B's FLEX falls from RB 6 to the incoming TE 5
    assert res["result"]["label"] == "TEAM_A_UNCHANGED_TEAM_B_LOSS"


def test_two_for_one_where_both_incoming_players_start():
    res = evaluate_trade(league(), 1, 2, [105], [204, 202])
    a = res["team_a"]
    assert res["trade"]["shape"] == "1-for-2"
    assert usage(a, 204)["weeks_started"] == [10, 11, 12] and usage(a, 202)["weeks_started"] == [10, 11, 12]
    # before 93; after QB 20 | RB 15, 14 | WR 13, 11 | TE 10 | FLEX RB 12 = 95
    assert a["deltas"]["ros_delta"] == 6.0
    lost = {d["espn_player_id"]: d for d in a["displaced"]}
    assert lost[104]["starts_lost"] == [10, 11, 12]              # old FLEX RB pushed to the bench


def test_two_for_one_where_only_one_incoming_player_starts():
    res = evaluate_trade(league(), 1, 2, [105], [204, 207])
    a = res["team_a"]
    assert usage(a, 204)["weeks_started"] == [10, 11, 12] and usage(a, 207)["weeks_started"] == []
    assert a["deltas"]["ros_delta"] == -9.0                       # RB 207 contributes 0 starting points


def test_two_for_one_where_second_incoming_player_only_helps_during_a_bye():
    st = league(proj_over={102: {10: 15, 11: 0.0, 12: 15}, 207: flat(8.5)})   # A's RB1 on bye in week 11
    res = evaluate_trade(st, 1, 2, [105], [204, 207])
    a = res["team_a"]
    assert usage(a, 207)["weeks_started"] == [11]
    assert any("starts only in week 11" in n for n in a["fit_notes"])
    by_week = {w["week"]: w for w in a["weekly"]}
    assert 207 in by_week[11]["entered"] and 207 not in by_week[10]["entered"]


def test_one_for_two_requires_a_forced_drop():
    res = evaluate_trade(league(capacity=9), 1, 2, [105], [204, 202])
    roster = res["team_a"]["roster"]
    assert roster["active_before"] == 9 and roster["active_after"] == 9
    assert len(roster["forced_drops"]) == 1 and not roster["forced_drops"][0]["is_incoming"]
    assert roster["forced_drop_id"] in (104, 107, 109)            # a bench player who no longer starts
    assert roster["forced_drop_lineup_cost"] == 0.0 and roster["forced_drop_name"]
    assert res["team_a"]["deltas"]["ros_delta"] == 6.0            # the drop costs nothing
    assert roster["forced_drop_id"] not in {p["player_id"] for w in res["team_a"]["lineups"]["after"]
                                            for p in w["assignment"]} | {b["player_id"] for w in
                                                                          res["team_a"]["lineups"]["after"] for b in w["bench"]}


def test_forced_drop_minimises_future_lineup_cost_not_raw_projection():
    a = [P(101, "QB"), P(110, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"),
         P(108, "TE"), P(109, "TE")]
    over = {110: flat(14),                                        # backup QB: big raw projection, never starts
            103: {10: 12, 11: 0.0, 12: 12},                       # RB2 bye week 11 -> RB 104 must start
            104: flat(5),                                         # lowest raw projection on the roster
            108: {10: 10, 11: 10, 12: 0.0},                       # TE1 out week 12 -> TE 109 must start
            109: flat(6)}
    st = league(a=a, proj_over=over, capacity=9)
    res = evaluate_trade(st, 1, 2, [106], [204, 205])
    roster = res["team_a"]["roster"]
    raw = {pid: sum(st.projections[pid].values()) for pid in (104, 109, 110)}
    assert min(raw, key=raw.get) == 104 and max(raw, key=raw.get) == 110
    assert roster["forced_drop_id"] == 110 and roster["forced_drop_lineup_cost"] == 0.0


def test_sending_two_for_one_opens_a_roster_spot_without_automatic_fa_points():
    fa = P(301, "RB")
    with_fa = league(capacity=9, proj_over={301: flat(9)}, fas=[fa])
    res = evaluate_trade(with_fa, 1, 2, [105], [204, 202])
    b = res["team_b"]
    assert b["roster"]["open_active_roster_spots_before"] == 0 and b["roster"]["open_active_roster_spots"] == 1
    assert b["roster"]["forced_drops"] == []
    fill = b["hypothetical_fa_fill"]
    assert fill["player"]["espn_player_id"] == 301 and fill["lineup_points_added"] > 0
    in_lineups = {x["player_id"] for k in ("before", "after") for w in b["lineups"][k] for x in w["assignment"] + w["bench"]}
    assert 301 not in in_lineups                                   # never on the canonical roster
    pb, _ = team_b()
    after = [p for p in pb if p.espn_player_id not in (204, 202)] + [team_a()[0][4]]
    expected = project_team(after, SLOTS, with_fa.window(), with_fa.projections)["summary"]
    assert b["after"]["ros_projected_starting_points"] == expected["ros_projected_starting_points"]
    no_fa = evaluate_trade(league(capacity=9), 1, 2, [105], [204, 202])
    assert no_fa["team_b"]["deltas"] == b["deltas"] and no_fa["team_a"]["deltas"] == res["team_a"]["deltas"]


def test_flex_displacement_is_handled_exactly():
    res = evaluate_trade(league(), 1, 2, [107], [208])            # A's bench WR 8 for B's bench WR 4
    b = res["team_b"]
    assert all(w["entered"] == [107] and w["exited"] == [207] for w in b["weekly"])
    assert b["deltas"]["ros_delta"] == 6.0                        # FLEX: WR 8 replaces RB 6
    assert next(d for d in b["displaced"] if d["espn_player_id"] == 207)["starts_lost"] == [10, 11, 12]
    assert usage(b, 107)["slots"] == {"FLEX": 3}
    assert res["team_a"]["deltas"]["ros_delta"] == 0.0           # A's FLEX RB 9 is untouched


def test_trade_helps_regular_season_but_hurts_playoffs():
    st = league(proj_over={204: {10: 25, 11: 25, 12: 2}})
    a = evaluate_trade(st, 1, 2, [105], [204])["team_a"]["deltas"]
    assert a["regular_season_delta"] == 18.0 and a["playoff_delta"] == -8.0 and a["ros_delta"] == 10.0


def test_trade_hurts_regular_season_but_helps_playoffs():
    st = league(proj_over={204: {10: 25, 11: 25, 12: 2}})
    res = evaluate_trade(st, 1, 2, [105], [204])
    b = res["team_b"]["deltas"]
    assert b["regular_season_delta"] == -18.0 and b["playoff_delta"] == 12.0 and b["ros_delta"] == -6.0
    assert res["result"]["regular_season_label"] == "TEAM_A_GAIN_TEAM_B_LOSS"
    assert res["result"]["playoff_label"] == "TEAM_A_LOSS_TEAM_B_GAIN"


def test_one_side_incomplete_projection_gives_no_fake_canonical_total():
    st = league(proj_over={206: {10: 8, 12: 8}})                  # B's TE (has an NFL team) unprojected week 11
    res = evaluate_trade(st, 1, 2, [105], [204])
    a, b = res["team_a"], res["team_b"]
    assert a["deltas"]["ros_delta"] == -9.0                       # A is fully known
    assert b["deltas"]["ros_delta"] is None and b["deltas"]["regular_season_delta"] is None
    assert b["deltas"]["playoff_delta"] == 3.0                    # week 12 is complete
    assert b["deltas"]["known_ros_delta"] == 9.0 and b["deltas"]["weeks_incomplete"] == [11]
    assert b["incomplete"] == {"missing_weeks": [11], "missing_players": [206]}
    assert b["after"]["ros_projected_starting_points"] is None
    assert res["result"]["label"] == "INCOMPLETE_DATA" and res["result"]["combined_ros_delta"] is None


# ------------------------------------------------------------------- roster legality --

def test_redundant_incoming_asset_is_flagged_not_hidden():
    a = [P(101, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"), P(108, "TE")]
    res = evaluate_trade(league(a=a, capacity=7), 1, 2, [105], [204, 209])   # TE 3 can't start for A
    roster = res["team_a"]["roster"]
    assert not roster["forced_drops"][0]["is_incoming"]            # never the default drop...
    assert roster["forced_drop_id"] == 104                          # ...the cheapest non-incoming one (FLEX RB 9 -> TE 3)
    assert roster["forced_drop_lineup_cost"] == 18.0
    red = roster["redundant_incoming_asset"]
    assert red["player_ids"] == [209] and red["lineup_points_saved_by_cutting_it"] == 18.0


def test_position_limit_forces_a_drop_at_that_position():
    res = evaluate_trade(league(limits={"TE": 2}), 1, 2, [107], [206])
    roster = res["team_a"]["roster"]
    assert roster["forced_drop_id"] == 109 and roster["active_after"] == 8   # TE 206 sits behind TE 108
    assert roster["redundant_incoming_asset"] is None             # tie at 0 cost: not "strictly cheaper"


def test_engine_never_mutates_the_league_state():
    st = league(capacity=9)
    snap = copy.deepcopy((st.rosters, st.projections, st.free_agents))
    evaluate_trade(st, 1, 2, [105], [204, 202])
    evaluate_trade(st, 2, 1, [204, 202], [105], start_week=11)
    assert (st.rosters, st.projections, st.free_agents) == snap


def test_start_week_override_shrinks_the_window():
    res = evaluate_trade(league(), 1, 2, [105], [204], start_week=12)
    assert res["evaluation"]["weeks"] == [12] and res["evaluation"]["start_week_overridden"]
    assert res["team_a"]["deltas"]["ros_delta"] == -3.0 and res["team_a"]["deltas"]["regular_season_delta"] == 0.0


@pytest.mark.parametrize("args,msg", [
    ((1, 1, [105], [106]), "different"),
    ((1, 2, [105], [105]), "more than once"),
    ((1, 2, [204], [205]), "not on team 1"),
    ((1, 2, [101, 102, 103], [204]), "1 or 2"),
    ((1, 2, [], [204]), "1 or 2"),
    ((1, 3, [105], [204]), "unknown team"),
])
def test_invalid_trades_are_rejected(args, msg):
    with pytest.raises(TradeError, match=msg):
        evaluate_trade(league(), *args)


def test_result_labels_use_zero_as_the_dividing_line():
    assert result_label(0.01, 0.01) == "MUTUAL_PROJECTED_GAIN"
    assert result_label(-0.01, -5) == "BOTH_LOSE"
    assert result_label(0.0, 0.0) == "NO_PROJECTED_CHANGE"
    assert result_label(None, 3) == "INCOMPLETE_DATA"
    assert result_label(2, -1) == "TEAM_A_GAIN_TEAM_B_LOSS"


def test_traded_players_carry_marginal_value_and_replacement_context():
    fa = P(301, "WR")
    res = evaluate_trade(league(proj_over={301: flat(12)}, fas=[fa]), 1, 2, [105], [204])
    p105 = next(p for p in res["players"] if p["espn_player_id"] == 105)
    assert p105["marginal_value_on_current_team"]["roster_marginal_lineup_points"] == 24.0   # WR 16 -> bench WR 8
    rc = p105["replacement_context"]
    assert rc["label"] == "hypothetical FA replacement" and rc["replacement"]["espn_player_id"] == 301
    assert rc["lineup_points_recovered"] + rc["residual_loss_vs_original"] == rc["lineup_points_lost_without_player"]
    # the hypothetical replacement never leaks into the canonical delta
    assert res["team_a"]["deltas"] == evaluate_trade(league(), 1, 2, [105], [204])["team_a"]["deltas"]

"""Current-IR semantics: a player in an IR slot stays rostered but inactive. He never starts a
canonical lineup, never uses an active spot and never poisons completeness; his ESPN projection is
kept and shown as a separate, optimistic IR activation sensitivity. A traded IR player arrives on the
receiving team as an active-roster asset.

Uses the synthetic league from test_trades: QB, RB x2, WR x2, TE, FLEX. Team A starts 93/week with
FLEX RB 9 (id 104); team B has 9 players."""

from dataclasses import replace

from app.analytics.team_lineups import (ACTIVATION_DROP_NOTE, IR_HOLD_NOTE, RosterPlayer, Window,
                                        ir_activation_sensitivity, project_team, replacement_context,
                                        roster_marginal_value, starters)
from app.analytics.trades import IR_WARNING, evaluate_trade
from test_trades import SLOTS, WEEKS, flat, league, team_a, team_b

W = Window(WEEKS, last_regular=11)
RETURNING = {10: 0.0, 11: 30.0, 12: 30.0}           # out week 10, back for 11-12


def ir(pid, pos="RB", team="KC"):
    return RosterPlayer(pid, pos, team, f"P{pid}", on_ir=True)


def a_with_ir(player=None, proj=RETURNING):
    players, p = team_a()
    player = player or ir(110)
    return players + [player], {**p, player.espn_player_id: proj} if proj is not None else p


def test_current_ir_player_with_positive_projection_does_not_start_canonically():
    players, proj = a_with_ir()
    team = project_team(players, SLOTS, W, proj)
    base = project_team(team_a()[0], SLOTS, W, proj)
    assert all(110 not in starters(w) for w in team["weeks"])
    assert team["summary"]["ros_projected_starting_points"] == base["summary"]["ros_projected_starting_points"] == 279.0
    assert [w["ir"] for w in team["weeks"]] == [[{"player_id": 110, "position": "RB", "points": RETURNING[w]}]
                                                for w in WEEKS]      # projection kept, not hidden or zeroed
    assert all(110 not in {b["player_id"] for b in w["bench"]} for w in team["weeks"])
    mv = roster_marginal_value(players, 110, SLOTS, W, proj, team, open_active_roster_spots=0)
    assert mv["on_ir"] and mv["roster_marginal_lineup_points"] == 0.0 and mv["weeks_started"] == []
    assert mv["ir_activation_sensitivity"]["lineup_points_upside"] == 42.0
    assert replacement_context(players, 110, [], SLOTS, W, proj, team)["replacement"] is None


def test_current_ir_player_does_not_consume_active_capacity():
    players, proj = a_with_ir()
    st = league(a=players, proj_over={110: RETURNING}, capacity=9)   # A: 9 active + 1 IR = exactly full
    res = evaluate_trade(st, 1, 2, [105], [204])                      # 1-for-1 between active players
    ro = res["team_a"]["roster"]
    assert ro["active_before"] == 9 and ro["active_after"] == 9
    assert ro["forced_drops"] == [] and ro["open_active_roster_spots"] == 0


def test_current_ir_player_missing_projection_does_not_poison_canonical_completeness():
    players, proj = a_with_ir(proj=None)                             # IR player with an NFL team, no projection
    team = project_team(players, SLOTS, W, proj)
    assert team["summary"]["projection_complete"]
    assert team["summary"]["ros_projected_starting_points"] == 279.0
    assert all(w["missing_projection_ids"] == [] for w in team["weeks"])
    assert team["weeks"][0]["ir"] == [{"player_id": 110, "position": "RB", "points": None}]


def test_ir_activation_sensitivity_identifies_weeks_he_would_hypothetically_start():
    players, proj = a_with_ir()
    s = ir_activation_sensitivity(players, 110, SLOTS, W, proj, open_active_roster_spots=0)
    assert s["weekly_projection"] == RETURNING
    assert s["projected_weeks"] == [11, 12] and s["weeks_would_start"] == [11, 12]
    assert s["lineup_points_upside"] == 42.0                          # RB 30 displaces FLEX RB 9, weeks 11-12
    assert s["regular_season_upside"] == 21.0 and s["playoff_upside"] == 21.0
    assert [w["upside"] for w in s["weeks"]] == [0.0, 21.0, 21.0]
    assert s["open_active_roster_spot"] is False and s["roster_move_note"] == ACTIVATION_DROP_NOTE
    roomy = ir_activation_sensitivity(players, 110, SLOTS, W, proj, open_active_roster_spots=1)
    assert roomy["open_active_roster_spot"] is True and roomy["roster_move_note"] is None
    assert roomy["lineup_points_upside"] == 42.0                      # upside itself never depends on the spot


def test_sensitivity_does_not_alter_canonical_trade_delta():
    players, proj = a_with_ir()
    plain = evaluate_trade(league(), 1, 2, [105], [204])
    with_ir = evaluate_trade(league(a=players, proj_over={110: RETURNING}), 1, 2, [105], [204])
    for side in ("team_a", "team_b"):
        assert with_ir[side]["deltas"] == plain[side]["deltas"]
        assert with_ir[side]["before"]["ros_projected_starting_points"] == \
            plain[side]["before"]["ros_projected_starting_points"]
    assert with_ir["result"] == plain["result"]
    sens = with_ir["team_a"]["ir_activation_sensitivity"]["players"]
    assert [x["espn_player_id"] for x in sens] == [110]
    assert sens[0]["before_trade"]["lineup_points_upside"] == 42.0 and sens[0]["after_trade"] is not None
    assert with_ir["ir_warning"] == IR_WARNING and plain["ir_warning"] is None


def test_incoming_traded_ir_player_becomes_active_and_is_subject_to_capacity():
    pb, _ = team_b()
    b = pb + [ir(210)]
    st = league(b=b, proj_over={210: flat(30)})
    res = evaluate_trade(st, 1, 2, [109], [210])                      # A: bench TE 5 for B's IR RB 30
    a, bside = res["team_a"], res["team_b"]
    assert a["roster"]["active_before"] == 9 and a["roster"]["active_after"] == 9     # he takes the TE's spot
    assert all(210 in starters_ids for starters_ids in
               ({s["player_id"] for s in w["assignment"]} for w in a["lineups"]["after"]))
    assert a["deltas"]["ros_delta"] == 63.0                           # 114 vs 93 every week
    assert bside["deltas"]["ros_delta"] == 0.0                        # sending an inactive IR player costs nothing
    assert bside["roster"]["active_after"] == 10                      # B gains an active body (capacity 20)
    p210 = next(p for p in res["players"] if p["espn_player_id"] == 210)
    assert p210["on_ir"] and p210["marginal_value_on_current_team"]["roster_marginal_lineup_points"] == 0.0
    assert p210["ir_activation_sensitivity"]["traded_away"] is True
    assert p210["ir_activation_sensitivity"]["after_trade"] is None


def test_incoming_ir_player_can_trigger_forced_drop():
    pa, _ = team_a()
    pb, _ = team_b()
    st = league(a=pa + [ir(110)], b=pb + [ir(210)], proj_over={110: flat(0), 210: flat(30)}, capacity=9)
    res = evaluate_trade(st, 1, 2, [110], [210])                      # IR-for-IR: both arrive active
    for side in ("team_a", "team_b"):
        ro = res[side]["roster"]
        assert ro["active_before"] == 9 and ro["active_after"] == 9
        assert len(ro["forced_drops"]) == 1 and not ro["forced_drops"][0]["is_incoming"]
    a = res["team_a"]
    assert a["roster"]["forced_drop_lineup_cost"] == 0.0            # a bench player goes
    assert a["deltas"]["ros_delta"] == 63.0


def test_player_becomes_canonically_usable_once_source_roster_has_on_ir_false():
    players, proj = a_with_ir()
    activated = [replace(p, on_ir=False) if p.espn_player_id == 110 else p for p in players]
    team = project_team(activated, SLOTS, W, proj)
    assert [110 in starters(w) for w in team["weeks"]] == [False, True, True]
    assert team["summary"]["ros_projected_starting_points"] == 279.0 + 42.0
    assert all(w["ir"] == [] for w in team["weeks"])
    mv = roster_marginal_value(activated, 110, SLOTS, W, proj, team)
    assert mv["roster_marginal_lineup_points"] == 42.0 and "ir_activation_sensitivity" not in mv


def test_ir_hold_assumption_is_explicit_and_sensitivity_follows_a_later_window():
    players, proj = a_with_ir()
    st = replace(league(a=players, proj_over={110: RETURNING}), start_week=11)   # week 10 already kicked off
    plain = evaluate_trade(replace(league(), start_week=11), 1, 2, [105], [204])
    res = evaluate_trade(st, 1, 2, [105], [204])
    assert res["evaluation"]["weeks"] == [11, 12]
    assert IR_HOLD_NOTE in res["assumptions"] and res["ir_assumption"] == IR_HOLD_NOTE
    block = res["team_a"]["ir_activation_sensitivity"]
    assert block["assumption"] == IR_HOLD_NOTE
    s = block["players"][0]["before_trade"]
    assert s["assumption"] == IR_HOLD_NOTE and list(s["weekly_projection"]) == [11, 12]
    assert s["lineup_points_upside"] == 42.0                             # no return date or probability injected
    for side in ("team_a", "team_b"):                                    # canonical delta still excludes him
        assert res[side]["deltas"] == plain[side]["deltas"]

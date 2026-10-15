"""Transaction correctness, independently of percentile ranking."""
import pytest

from app.analytics.waivers import PlayerValue, team_state, evaluate_candidate
from app.analytics.waiver_roster import plan_roster_move, roster_constraints
from app.analytics.waiver_decisions import acquisition_utility, classify_action, resource_diagnostics, evaluate_streaming


def player(pid, pos, value=5, **extra):
    return dict(espn_player_id=pid, full_name=f"P{pid}", position=pos, raw_value=value,
                value=value, available=True, lineup_slot_id=20,
                availability={"status": "AVAILABLE", "weight": 1}, **extra)


def state(players, slots=None):
    slots = slots if slots is not None else {"RB": 1, "BE": 1}
    vals = {p["espn_player_id"]: PlayerValue(p["espn_player_id"], p["position"], p["raw_value"],
                                           p["raw_value"], None, None, None, "xfp") for p in players}
    st = team_state(players, vals, slots)
    # Preserve explicit provider flags used by pure legality tests.
    for p, raw in zip(st["players"], players):
        p.update({k: raw[k] for k in ("locked", "undroppable") if k in raw})
    return st


def test_open_spot_never_suggests_a_drop():
    move = plan_roster_move(state([player(1, "RB", 8)]), player(99, "RB", 9))
    assert move["status"] == "ADD_WITHOUT_DROP" and move["drop"] is None
    assert move["drop_required"] is False and move["active_count"] == 1


def test_full_roster_requires_a_legal_drop():
    move = plan_roster_move(state([player(1, "RB", 8), player(2, "WR", 2)]), player(99, "RB", 9))
    assert move["status"] == "ADD_WITH_DROP" and move["drop"]["espn_player_id"] == 2
    assert move["drop_required"] is True and move["alternatives"]


def test_full_roster_with_no_defensible_drop():
    move = plan_roster_move(state([player(1, "RB", 9), player(2, "WR", None)]), player(99, "RB", 4))
    assert move["status"] == "MOVE_REQUIRED" and move["drop"] is None
    assert move["drop_required"] is True


def test_position_cap_requires_same_position_drop_even_with_open_spot():
    st = state([player(1, "RB", 8)], {"RB": 1, "BE": 2})
    move = plan_roster_move(st, player(99, "RB", 9), {"capacity": 3, "position_limits": {"RB": 1}})
    assert move["status"] == "ADD_WITH_DROP" and move["drop"]["espn_player_id"] == 1
    move = plan_roster_move(st, player(99, "RB", 9), {"capacity": 3, "position_limits": {"RB": 0}})
    assert move["status"] == "INCOMPATIBLE"


def test_ir_excluded_from_active_capacity_but_included_in_positional_counts():
    move = plan_roster_move(state([player(1, "RB", 8), player(2, "WR", 7, injury_status="IR")
                                  | {"lineup_slot_id": 21}]), player(99, "WR", 6))
    assert move["status"] == "ADD_WITHOUT_DROP" and move["active_count"] == 1


def test_redundant_defense_preserves_strong_backup_rb():
    st = state([player(1, "RB", 9), player(2, "RB", 6), player(3, "D/ST", 7), player(4, "D/ST", 6.1)],
               {"RB": 1, "D/ST": 1, "BE": 2})
    move = plan_roster_move(st, player(99, "WR", 7))
    assert move["drop"]["espn_player_id"] == 4
    assert "coverage" in " ".join(move["drop"]["sacrifice"]).lower()


def test_locked_and_undroppable_players_never_recommended():
    st = state([player(1, "RB", 8), player(2, "WR", 2, undroppable=True)])
    move = plan_roster_move(st, player(99, "RB", 9))
    assert all(d["espn_player_id"] != 2 for d in move["alternatives"])


def test_drop_cannot_create_a_flex_or_fixed_hole():
    st = state([player(1, "RB", 4), player(2, "TE", 8)], {"RB": 1, "WR/TE": 1})
    move = plan_roster_move(st, player(99, "WR", 9))
    assert move["drop"]["espn_player_id"] == 2


def test_missing_settings_and_missing_roster_are_unknown():
    assert plan_roster_move(state([], {}), player(99, "RB"))["status"] == "UNKNOWN"
    assert plan_roster_move(state([]), player(99, "RB"), {"capacity": 2, "snapshot_complete": False})["status"] == "UNKNOWN"
    assert roster_constraints({"roster_slots": None, "settings_raw": None})["capacity"] is None


def test_high_opportunity_cannot_manufacture_an_upgrade():
    st = state([player(1, "RB", 9), player(2, "RB", 8)])
    c = player(99, "RB", 3)
    move = plan_roster_move(st, c)
    utility = acquisition_utility(st, c, move, need=1, opportunity=10)
    decision = classify_action(utility, move, {"kind": "FREE_AGENT"}, available=True)
    assert decision["action"] == "NOT_AN_UPGRADE" and decision["recommendation"] == "IGNORE"


def test_free_agent_uses_no_priority_or_faab():
    r = resource_diagnostics("FREEAGENT", {"is_faab": False}, {"waiver_rank": 1}, meaningful=True)
    assert r["kind"] == "FREE_AGENT" and r["spends_priority"] is False
    assert "priority" in r["summary"].lower()


def test_elite_priority_preserved_for_marginal_gain_and_alternatives():
    r = resource_diagnostics("WAIVERS", {"is_faab": False, "waiver_rule": "ROLLING"},
                             {"waiver_rank": 1}, meaningful=False, alternative={"name": "Free RB"})
    assert r["preserve_priority"] and "Free RB" in r["summary"]
    assert not resource_diagnostics("WAIVERS", {"is_faab": False, "waiver_rule": "RESET"},
                                    {"waiver_rank": 1}, meaningful=True)["preserve_priority"]


def test_faab_budget_unknown_spending_and_zero_are_distinct():
    r = resource_diagnostics("WAIVERS", {"is_faab": True, "faab_budget": 100}, {"faab_spent": 35}, meaningful=True)
    assert r["remaining"] == 65 and r["bid"] is None and r["kind"] == "FAAB"
    assert resource_diagnostics("WAIVERS", {"is_faab": True, "faab_budget": 100}, {}, meaningful=True)["remaining"] is None
    assert resource_diagnostics("WAIVERS", {"is_faab": True, "faab_budget": 100}, {"faab_spent": 100}, meaningful=True)["remaining"] == 0


@pytest.mark.parametrize("pos", ["D/ST", "K"])
def test_streamer_uses_upcoming_projection_and_opponent(pos):
    evidence = {"weeks": [{"week": 7, "points": 10, "opponent": "NYJ", "bye": False}],
                "incumbent_points": 6, "alternative_points": 9, "opponent_implied_total": 16.5}
    s = evaluate_streaming(pos, evidence, required=1)
    assert s["category"] == "SHORT_TERM_STREAMER" and s["gain"] == 4
    assert "NYJ" in s["summary"] and s["complete"]


def test_streaming_unknown_and_unnecessary_stash():
    assert not evaluate_streaming("D/ST", {}, required=1)["complete"]
    s = evaluate_streaming("D/ST", {"weeks": [{"week": 7, "points": 5, "opponent": "KC"}],
                                   "incumbent_points": 9}, required=1)
    assert s["category"] == "NO_DEMONSTRATED_IMPROVEMENT"
    assert evaluate_streaming("K", {}, required=0)["category"] == "UNNECESSARY_STASH"


def test_no_actionable_week_or_unknown_injury_blocks_confident_action():
    move = {"status": "ADD_WITHOUT_DROP"}
    utility = {"meaningful": True, "starter_gain": 3, "stash": False}
    assert classify_action(utility, move, {"kind": "FREE_AGENT"}, available=True, actionable=False)["action"] == "SEASON_COMPLETE"
    assert classify_action(utility, move, {"kind": "FREE_AGENT"}, available=None)["action"] == "MONITOR"


def test_projection_only_breakout_can_replace_a_weaker_projected_starter():
    st = state([player(1, "RB", 7)], {"RB": 1})
    st["weeks"] = [7, 8]
    st["players"][0]["projections"] = {7: 8, 8: 8}
    candidate = player(99, "RB", None, projections={7: 15, 8: 14})
    move = plan_roster_move(st, candidate)
    assert move["status"] == "ADD_WITH_DROP"
    assert move["drop"]["forward"]["total_gain"] == 13


def test_future_bye_coverage_protects_backup_from_redundant_drop():
    st = state([player(1, "RB", 9), player(2, "RB", 6), player(3, "WR", 4)], {"RB": 1, "BE": 2})
    st["weeks"] = [7, 8]
    st["players"][0].update(projections={7: 18, 8: 0}, bye_weeks=[8])
    st["players"][1]["projections"] = {7: 10, 8: 12}
    st["players"][2]["projections"] = {7: 7, 8: 7}
    move = plan_roster_move(st, player(99, "WR", 8, projections={7: 15, 8: 15}))
    assert move["drop"]["espn_player_id"] == 3
    assert any(d["espn_player_id"] == 2 and "future" in d["reason"] for d in move["excluded"])


def test_second_streaming_option_does_not_become_a_multiyear_stash():
    evidence = {"weeks": [{"week": 7, "points": 12, "opponent": "NYJ", "incumbent_points": 6},
                          {"week": 8, "points": 11, "opponent": "LV", "incumbent_points": 7}], "incumbent_points": 6}
    assert evaluate_streaming("D/ST", evidence, required=1)["category"] == "MULTIWEEK_HOLD"


def test_streaming_with_missing_incumbent_projection_is_incomplete():
    assert not evaluate_streaming("K", {"weeks": [{"week": 7, "points": 10, "opponent": "NYJ"}]}, required=1)["complete"]


def test_exhausted_faab_requires_verifying_zero_dollar_claim_rules():
    resource = resource_diagnostics("WAIVERS", {"is_faab": True, "faab_budget": 100}, {"faab_spent": 100}, meaningful=True)
    d = classify_action({"meaningful": True, "starter_gain": 3}, {"status": "ADD_WITHOUT_DROP"}, resource, available=True)
    assert d["action"] == "VERIFY_COST" and not d["actionable"]


def test_depth_need_makes_same_gain_rb_a_higher_priority_than_deep_wr():
    st = state([player(1, "RB", 5), player(2, "WR", 5), player(3, "WR", 4), player(4, "WR", 4)],
               {"RB": 1, "WR": 1, "BE": 4})
    rb, wr = player(98, "RB", 7), player(99, "WR", 7)
    a = acquisition_utility(st, rb, plan_roster_move(st, rb), need=.8, opportunity=7)
    b = acquisition_utility(st, wr, plan_roster_move(st, wr), need=0, opportunity=7)
    assert a["ranking_utility"] > b["ranking_utility"]


@pytest.mark.parametrize("status", ["ACTIVE", "QUESTIONABLE", "DOUBTFUL", None])
def test_healthy_ir_occupant_requires_verification_before_an_add(status):
    st = state([player(1, "RB", 9), player(2, "WR", 8, injury_status=status) | {"lineup_slot_id": 21}])
    move = plan_roster_move(st, player(99, "RB", 10))
    assert move["status"] == "UNKNOWN" and move["drop_required"] is None


def test_unknown_starter_quality_does_not_become_a_zero_value_upgrade():
    st = state([player(1, "RB", None)], {"RB": 1, "BE": 1})
    c = player(99, "RB", 9)
    utility = acquisition_utility(st, c, plan_roster_move(st, c), need=.8, opportunity=9)
    assert utility["starter_gain"] is None and not utility["meaningful"]


def test_short_term_stream_can_trade_future_points_with_explicit_sacrifice():
    st = state([player(1, "K", 8)], {"K": 1})
    st["weeks"] = [7, 8, 9]
    st["players"][0]["projections"] = {7: 5, 8: 10, 9: 10}
    c = player(99, "K", 5, projections={7: 10, 8: 4, 9: 4})
    move = plan_roster_move(st, c)
    assert move["status"] == "ADD_WITH_DROP"
    assert move["drop"]["forward"]["total_gain"] == -7
    assert any("sacrifices 7.0" in r for r in move["drop"]["sacrifice"])


def test_streaming_swap_replaces_incumbent_instead_of_sacrificing_skill_depth():
    st = state([player(1, "K", 4), player(2, "RB", 6)], {"K": 1, "BE": 1})
    st["weeks"] = [7]
    st["players"][0]["projections"] = {7: 5}
    st["players"][1]["projections"] = {7: 12}
    move = plan_roster_move(st, player(99, "K", 7, projections={7: 10}))
    assert move["drop"]["espn_player_id"] == 1


def test_priority_tries_next_alternative_when_first_has_no_defensible_drop():
    st = state([player(1, "RB", 8)], {"RB": 1})
    st["weeks"] = [7, 8]
    st["players"][0]["projections"] = {7: 10, 8: 10}
    first = player(98, "RB", None, projections={7: 13})
    second = player(97, "RB", 8, projections={7: 12.5, 8: 12.5})
    choices = [{"player": first, "resource": {"name": "Uncertain FA", "espn_player_id": 98}},
               {"player": second, "resource": {"name": "Viable FA", "espn_player_id": 97}}]
    row = evaluate_candidate(dict(espn_player_id=99, position="RB", full_name="Waiver RB", injury_status="ACTIVE", availability_status="WAIVERS"),
                PlayerValue(99, "RB", 8, 8, 8, None, None, "xfp"), my_team_id=1, needs={1: {"RB": {"need_score": .5}}},
                my_state=st, profiles={}, league_pos_share={}, team_names={}, scarcity={}, decision_context={
                    "rules": {"is_faab": False, "waiver_rule": "ROLLING"}, "team": {"waiver_rank": 1},
                    "projections": {99: {7: 12, 8: 12}}, "alternatives": choices,
                    "alternative": choices[0]["resource"], "alternative_candidate": first})
    d = row["components"]["decision"]
    assert d["action"] == "PRESERVE_PRIORITY" and d["resources"]["alternative"]["espn_player_id"] == 97


def test_total_projection_gain_cannot_hide_a_future_positional_hole():
    st = state([player(1, "RB", 5), player(2, "WR", 7), player(3, "WR", 4)], {"RB": 1, "WR": 1, "BE": 1})
    st["weeks"] = [7, 8]
    st["players"][0]["projections"] = {7: 8, 8: 8}
    st["players"][1].update(projections={7: 0, 8: 14}, bye_weeks=[7])
    st["players"][2]["projections"] = {7: 7, 8: 6}
    move = plan_roster_move(st, player(99, "RB", 9, projections={7: 30, 8: 30}))
    assert move["drop"]["espn_player_id"] == 1
    assert any(d["espn_player_id"] == 3 and "future" in d["reason"] for d in move["excluded"])

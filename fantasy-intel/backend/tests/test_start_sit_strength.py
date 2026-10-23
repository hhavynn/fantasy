"""Start/Sit recommendation strength and legal alternatives (issue #172).

One close rule (CLOSE_CALL_POINTS, the backtests' close-call margin), a label with its reason and never a probability;
any unknown is unverified. Alternatives are single what-if moves against the recommendation, so every engine rule
(eligibility, kickoff locks, IR and taxi, unknown never 0) is the what-if's own. Uses the what-if test league (see
tests/test_lineup_what_if.py): QB_0, RB_0, RB_1, WR_0 and WR_1 are locked; RB 13 is on IR; WR 14 has no projection.
"""
import json

from app.analytics import forecast_backtest, start_sit_evaluation
from app.analytics.lineup_what_if import add_alternatives, alternatives, build_board, scenario_key
from app.analytics.start_sit import (
    ACTION_NO_CHANGE, ACTION_SWAP, ACTIONABILITY_ACTIONABLE, ACTIONABILITY_LOCKED, ACTIONABILITY_NO_CHANGE,
    ACTIONABILITY_UNCERTAIN, CLOSE_CALL_POINTS, STRENGTH_CLEAR, STRENGTH_CLOSE, STRENGTH_UNVERIFIED, StartSitDecision,
    decision_strength, load_start_sit_evaluation, no_change_strength, recommendation_strength,
)
from test_lineup_what_if import NOW, con, lineup, what_if  # noqa: F401  (fixture)


def _evaluate(con):
    cap: dict = {}
    ev = load_start_sit_evaluation(con, now=NOW, capture=cap)
    return ev, build_board(cap)


def test_there_is_one_close_rule_and_it_is_the_backtests_margin():
    assert CLOSE_CALL_POINTS == start_sit_evaluation.CLOSE_PAIR_POINTS == forecast_backtest.CLOSE_PAIR_POINTS == 3.0


def test_strength_is_a_label_with_its_rule_never_a_percentage():
    assert recommendation_strength(3.0, [])["label"] == STRENGTH_CLOSE              # the margin itself is a close call
    assert recommendation_strength(3.01, [])["label"] == STRENGTH_CLEAR
    assert recommendation_strength(0.0, [])["label"] == STRENGTH_CLOSE
    assert recommendation_strength(None, [])["label"] == STRENGTH_UNVERIFIED          # an unknown gain is never certain
    unknown = recommendation_strength(9.0, ["Kickoff status for X could not be verified."])
    assert unknown["label"] == STRENGTH_UNVERIFIED and "Kickoff status for X" in unknown["reason"]
    for s in (recommendation_strength(2.1, []), recommendation_strength(7.5, [])):
        assert "3-point margin the decision backtests treat as a close call" in s["reason"]
        assert "%" not in s["reason"] and "expert" not in s["reason"].lower()
    assert recommendation_strength(2.1, [])["reason"].startswith("Close call: +2.1 pts")


def test_keeping_the_lineup_is_graded_by_the_closest_legal_alternative():
    opt = {"name": "Ted End", "delta": -2.5}
    assert no_change_strength(opt, True)["label"] == STRENGTH_CLOSE
    assert "would cost 2.5 pts" in no_change_strength(opt, True)["reason"]
    assert no_change_strength({"name": "Ted End", "delta": -4.0}, True)["label"] == STRENGTH_CLEAR
    assert no_change_strength(None, True)["label"] == STRENGTH_CLEAR                 # nobody can legally come in
    assert no_change_strength({"name": "Wade X", "delta": None}, True)["label"] == STRENGTH_UNVERIFIED
    assert no_change_strength(opt, False)["label"] == STRENGTH_UNVERIFIED            # incomplete coverage


def test_alternatives_are_legal_single_moves_and_never_from_locked_slots_or_reserved_players(con):
    ev, board = _evaluate(con)
    alts = alternatives(board, scenario_key(ev["scoring_period"], ev["team"]["id"]))
    slots = {a["slot_id"]: a for a in alts}
    assert not set(slots) & {"QB_0", "RB_0", "RB_1", "WR_0", "WR_1"}               # kicked-off starters keep their slots
    offered = {o["player_id"] for a in alts for o in a["options"]}
    assert 13 not in offered and 15 not in offered                                   # IR and a kicked-off bench player
    starting = {pid for pid in board.baseline.values() if pid is not None}
    assert not offered & starting                                                    # alternatives come off the bench
    for a in alts:
        assert 1 <= len(a["options"]) <= 3
        known = [o["delta"] for o in a["options"] if o["delta"] is not None]
        assert known == sorted(known, reverse=True) and all(d <= 0 for d in known)  # closest first, never a gain
        assert all(o["delta"] is None for o in a["options"][len(known):])            # unknown totals last


def test_every_alternative_is_exactly_what_trying_it_in_the_what_if_gives(con):
    ev, board = _evaluate(con)
    alts = alternatives(board, scenario_key(ev["scoring_period"], ev["team"]["id"]))
    assert alts
    for a in alts:
        for o in a["options"]:
            after = what_if(con, o["whatif"])
            assert after["scenario"]["applied"], o
            assert lineup(after)[a["slot_id"]] == o["player_id"]
            assert after["totals"]["delta"] == o["delta"]


def test_a_player_without_a_projection_makes_his_alternative_unknown_never_zero(con):
    ev, board = _evaluate(con)
    key = scenario_key(ev["scoring_period"], ev["team"]["id"])
    wide = [o for a in alternatives(board, key, limit=10) for o in a["options"] if o["player_id"] == 14]
    assert wide and all(o["delta"] is None and o["projection"] is None for o in wide)   # legal for FLEX, total unknown
    # with the top 3 shown, an unknown total ranks after every known one (here: below three known FLEX options)
    flex = next(a for a in alternatives(board, key) if a["slot_id"] == "FLEX_0")
    assert 14 not in {o["player_id"] for o in flex["options"]} and all(o["delta"] is not None for o in flex["options"])


def test_the_response_grades_every_decision_and_the_summary_and_lists_alternatives(con):
    ev, board = _evaluate(con)
    out = add_alternatives(ev, board)
    assert out["alternatives"] == alternatives(board, scenario_key(out["scoring_period"], out["team"]["id"]))
    for d in out["decisions"]:
        assert d["recommendation_strength"]["label"] in (STRENGTH_CLEAR, STRENGTH_CLOSE, STRENGTH_UNVERIFIED)
    assert out["summary"]["recommendation_strength"] == out["decisions"][0]["recommendation_strength"]
    assert "%" not in json.dumps([d["recommendation_strength"] for d in out["decisions"]])


def test_a_next_week_preview_lists_no_alternatives_and_is_not_graded_as_a_decision(con):
    cap: dict = {}
    ev = load_start_sit_evaluation(con, now=NOW, week=8, capture=cap)
    out = add_alternatives(ev, build_board(cap))
    assert out["is_future_preview"] and out["alternatives"] == []
    assert out["summary"]["recommendation_strength"]["label"] == STRENGTH_UNVERIFIED


def test_handoff_edge_cases_a_tie_is_close_and_a_measured_zero_is_known():
    assert no_change_strength({"name": "Twin", "delta": 0.0}, True)["label"] == STRENGTH_CLOSE   # tied: the closest call
    assert "would cost 0.0 pts" in no_change_strength({"name": "Twin", "delta": 0.0}, True)["reason"]
    assert recommendation_strength(0.0, [])["label"] == STRENGTH_CLOSE                          # a zero gain is known
    # a partial lineup (slots no engine fills) has no complete total: keeping it is unverified, never "clear"
    assert no_change_strength({"name": "X", "delta": -9.0}, False)["label"] == STRENGTH_UNVERIFIED


def _decision(action=ACTION_SWAP, actionability=ACTIONABILITY_ACTIONABLE, gain=9.0, warnings=(), id="d1"):
    return StartSitDecision(id=id, action=action, actionability=actionability, start_players=[], bench_players=[],
                            target_slot="WR", projected_net_gain=gain, explanation="", warnings=list(warnings))


def test_only_a_move_that_can_be_made_as_shown_is_graded_clear_or_close():
    assert decision_strength(_decision(), False)["label"] == STRENGTH_CLEAR
    locked = decision_strength(_decision(actionability=ACTIONABILITY_LOCKED, warnings=["Ann Lee has kicked off."]), False)
    assert locked["label"] == STRENGTH_UNVERIFIED and "Ann Lee has kicked off." in locked["reason"]
    # not actionable and no warning text: still never clear
    bare = decision_strength(_decision(actionability=ACTIONABILITY_UNCERTAIN), False)
    assert bare["label"] == STRENGTH_UNVERIFIED and "cannot be made as shown" in bare["reason"]
    assert decision_strength(_decision(action=ACTION_NO_CHANGE, actionability=ACTIONABILITY_NO_CHANGE), False) is None


def test_a_week_without_a_submitted_lineup_says_so_and_a_preview_says_it_is_a_preview():
    note = _decision(action=ACTION_NO_CHANGE, actionability=ACTIONABILITY_NO_CHANGE, gain=None, id="future_preview_note")
    assert "no submitted lineup this week" in decision_strength(note, False)["reason"]
    assert "next-week preview" in decision_strength(note, True)["reason"]
    assert decision_strength(note, False)["label"] == decision_strength(note, True)["label"] == STRENGTH_UNVERIFIED


def _keep_lineup(ev: dict) -> dict:
    """The fixture week as if the submitted lineup were already the recommendation (complete projections)."""
    keep = {"id": "no_change", "action": ACTION_NO_CHANGE, "recommendation_strength": None}
    return {**ev, "decisions": [keep], "summary": {**ev["summary"], "projections_complete": True}}


def test_keeping_the_lineup_is_unverified_when_any_legal_alternative_is_unknown_even_past_the_shown_three(con):
    ev, board = _evaluate(con)
    out = add_alternatives(_keep_lineup(ev), board)
    shown = {o["player_id"] for a in out["alternatives"] for o in a["options"]}
    assert 14 not in shown                                                  # WR 14 ranks past the top 3 shown
    strength = out["summary"]["recommendation_strength"]
    assert strength["label"] == STRENGTH_UNVERIFIED and "unknown projected total" in strength["reason"]


def test_a_league_that_starts_slots_no_engine_fills_is_never_graded_clear_or_close(con):
    ev, board = _evaluate(con)
    ev = _keep_lineup(ev)
    ev["decisions"] = [{"id": "d1", "action": ACTION_SWAP, "recommendation_strength": recommendation_strength(9.0, [])},
                       *ev["decisions"]]
    ev["summary"]["lineup_coverage"] = {"status": "partial", "unsupported_slots": ["DL", "LB"]}
    out = add_alternatives(ev, board)
    for d in out["decisions"]:
        assert d["recommendation_strength"]["label"] == STRENGTH_UNVERIFIED
    assert "DL, LB, which no engine fills" in out["summary"]["recommendation_strength"]["reason"]

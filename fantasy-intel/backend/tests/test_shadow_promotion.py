"""Fail-closed SHADOW_PROMOTION_CRITERIA (weekly_learning.shadow_status). Each fixture breaks
exactly one condition of an otherwise passing report; synthetic numbers, no data or models."""

from __future__ import annotations

import copy

import pytest

from app.analytics import challenger as ch
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import POSITIONS

CRITERIA = ("frozen_pregame_evidence", "prospective_weeks", "matched_appearances", "mae_vs_champion", "bias_guard",
            "starters_pool_mae", "interval_coverage")


def _position() -> dict:
    return {"n": 450,
            "metrics": {"champion": {"n": 450, "mae": 5.0, "rmse": 6.5, "bias": 0.20},
                        "challenger": {"n": 450, "mae": 4.8, "rmse": 6.3, "bias": 0.25},
                        "A1": {"n": 450, "mae": 5.6, "rmse": 7.0, "bias": 0.1},
                        "A2": {"n": 450, "mae": 5.5, "rmse": 6.9, "bias": 0.1}},
            "challenger_minus_champion_mae": {"diff": -0.2, "ci95": [-0.31, -0.08]},
            "interval80_coverage": {"champion": 0.80, "challenger": 0.80},
            "starters_pool": {"n": 200, "definition": "x", "mae": {"champion": 6.0, "challenger": 5.9}}}


@pytest.fixture
def passing() -> dict:
    return {"kind": "pregame", "weeks": [(2026, w) for w in range(6, 14)],
            "positions": {p: _position() for p in (*POSITIONS, "ALL")}}


def _results(report: dict, pos: str = "WR") -> dict:
    return {c["name"]: c["result"] for c in wl.shadow_criteria(report, pos)}


def _only_failure(report: dict, name: str, result: str, pos: str = "WR") -> dict:
    res = _results(report, pos)
    assert res[name] == result, res
    assert all(v == wl.PASS for k, v in res.items() if k != name), res
    st = wl.shadow_status(report)[pos]
    assert st["status"] != "eligible_for_review"
    assert len(st["blocking"]) == 1 and st["lifecycle"] == "research_shadow" and not st["production_approved"]
    return st


def test_all_shadow_criteria_pass_reaches_review_only(passing):
    status = wl.shadow_status(passing)
    for pos in POSITIONS:
        st = status[pos]
        assert [c["name"] for c in st["criteria"]] == list(CRITERIA)
        assert all(c["result"] == wl.PASS for c in st["criteria"])
        assert set(st["criteria"][0]) == {"name", "observed", "required", "result", "explanation"}
        assert st["status"] == "eligible_for_review" and st["blocking"] == []
        # never a deployment: the lifecycle and the production gate are untouched
        assert st["lifecycle"] == "research_shadow" and st["production_approved"] is False
        assert st["production_gate"]["status"] == "not_evaluated"
    assert "approved_production" not in wl.READINESS
    blockers = wl.promotion_blockers(status)
    assert all(any("explicit user approval" in b for b in v["blocking"]) for v in blockers.values())


def test_insufficient_weeks(passing):
    passing["weeks"] = passing["weeks"][:7]
    st = _only_failure(passing, "prospective_weeks", wl.INSUFFICIENT)
    assert st["status"] == "insufficient_evidence" and "7 prospective weeks < 8" in st["blocking"][0]


def test_insufficient_position_appearances(passing):
    passing["positions"]["TE"]["n"] = 399
    st = _only_failure(passing, "matched_appearances", wl.INSUFFICIENT, "TE")
    assert st["status"] == "insufficient_evidence"
    assert wl.shadow_status(passing)["WR"]["status"] == "eligible_for_review"     # per position


def test_mae_ci_includes_zero(passing):
    passing["positions"]["WR"]["challenger_minus_champion_mae"] = {"diff": -0.1, "ci95": [-0.25, 0.04]}
    assert _only_failure(passing, "mae_vs_champion", wl.FAIL)["status"] == "failed_evaluation"


def test_worse_bias(passing):
    passing["positions"]["WR"]["metrics"]["challenger"]["bias"] = -0.36      # |0.36| > |0.20| + 0.15
    assert _only_failure(passing, "bias_guard", wl.FAIL)["status"] == "failed_evaluation"
    passing["positions"]["WR"]["metrics"]["challenger"]["bias"] = 0.35       # exactly at the tolerance
    assert _results(passing)["bias_guard"] == wl.PASS


def test_worse_starters_pool_mae(passing):
    passing["positions"]["WR"]["starters_pool"]["mae"]["challenger"] = 6.01
    assert _only_failure(passing, "starters_pool_mae", wl.FAIL)["status"] == "failed_evaluation"


@pytest.mark.parametrize("cov,word", [(0.74, "undercoverage"), (0.86, "overcoverage")])
def test_interval_miscoverage(passing, cov, word):
    passing["positions"]["WR"]["interval80_coverage"]["challenger"] = cov
    st = _only_failure(passing, "interval_coverage", wl.FAIL)
    assert st["status"] == "failed_evaluation" and word in st["blocking"][0]


def test_missing_coverage_never_passes(passing):
    del passing["positions"]["WR"]["interval80_coverage"]
    assert _only_failure(passing, "interval_coverage", wl.INSUFFICIENT)["status"] == "insufficient_evidence"


def test_withheld_qb_intervals_are_a_reported_limitation_not_an_exemption(passing):
    assert wl.CHALLENGER_STATUS["QB"]["intervals_published"] is False
    passing["positions"]["QB"]["interval80_coverage"] = {"champion": 0.8}
    st = _only_failure(passing, "interval_coverage", wl.INSUFFICIENT, "QB")
    assert "withheld by policy" in st["blocking"][0] and "not an exemption" in st["blocking"][0]


def test_missing_confidence_interval(passing):
    passing["positions"]["WR"]["challenger_minus_champion_mae"] = {"diff": None, "ci95": None}
    assert _only_failure(passing, "mae_vs_champion", wl.INSUFFICIENT)["status"] == "insufficient_evidence"
    del passing["positions"]["WR"]["challenger_minus_champion_mae"]
    assert _only_failure(passing, "mae_vs_champion", wl.INSUFFICIENT)["status"] == "insufficient_evidence"


def test_missing_bias(passing):
    passing["positions"]["WR"]["metrics"]["challenger"]["bias"] = None
    assert _only_failure(passing, "bias_guard", wl.INSUFFICIENT)["status"] == "insufficient_evidence"


@pytest.mark.parametrize("mutate", [
    lambda sp: sp.pop("starters_pool"),
    lambda sp: sp["starters_pool"].update(n=0),
    lambda sp: sp["starters_pool"]["mae"].update(challenger=None),
])
def test_missing_starter_metric(passing, mutate):
    mutate(passing["positions"]["WR"])
    assert _only_failure(passing, "starters_pool_mae", wl.INSUFFICIENT)["status"] == "insufficient_evidence"


def test_retrospective_only_evidence(passing):
    passing["kind"] = "retrospective"
    st = _only_failure(passing, "frozen_pregame_evidence", wl.FAIL)
    assert st["status"] == "insufficient_evidence" and "never prospective evidence" in st["blocking"][0]


def test_empty_report_is_insufficient_everywhere():
    status = wl.shadow_status({"kind": "pregame", "weeks": [], "positions": {}})
    for st in status.values():
        res = {c["name"]: c["result"] for c in st["criteria"]}
        assert st["status"] == "insufficient_evidence"
        assert res["frozen_pregame_evidence"] == wl.PASS
        assert all(v == wl.INSUFFICIENT for k, v in res.items() if k != "frozen_pregame_evidence")


def test_gate_matches_the_preregistered_criteria():
    g, c = ch.SHADOW_GATE, ch.SHADOW_PROMOTION_CRITERIA
    assert g["min_prospective_weeks"] == c["min_prospective_weeks"] == 8
    assert g["min_matched_appearances_per_position"] == c["min_matched_appearances_per_position"] == 400
    assert list(g["interval_coverage"]) == c["interval_coverage"] == [0.75, 0.85]
    assert "0.15" in c["bias_guard"] and g["bias_tolerance_points"] == 0.15
    assert "not worse" in c["starters_guard"] and g["starters_pool_mae_tolerance"] == 0.0
    assert "upper bound < 0" in c["mae_vs_champion"] and g["mae_ci95_upper_below"] == 0.0


def test_shadow_status_does_not_mutate_report(passing):
    before = copy.deepcopy(passing)
    wl.shadow_status(passing)
    assert passing == before

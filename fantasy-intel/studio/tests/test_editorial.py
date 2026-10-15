from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from studio import editorial as E
from studio.numbers import render
from studio.stages import qc
from studio.stages.formats import said


def _c(cid, value, fmt="int", kind="usage_value"):
    d, s = render(fmt, value)
    return {"id": cid, "value": value, "display": d, "spoken": s, "format": fmt, "label": cid,
            "source": {"kind": kind}}


def _mystery_player(team_targets=(31, 21), returned_snaps=51):
    claims = {c["id"]: c for c in [
        _c("s4", 39), _c("s5", 39), _c("p4", 0.55, "pct"), _c("p5", 0.58, "pct"), _c("t4", 8), _c("t5", 1),
        _c("tt4", team_targets[0]), _c("tt5", team_targets[1]), _c("rs", returned_snaps), _c("rt", 4),
        _c("chg", 0.16, "pp"), _c("rec", 0.56, "pct")]}
    p = {"gsis_id": "g", "name": "Test End II", "position": "TE", "role": "riser", "recent_weeks": [4, 5],
         "baseline_weeks": [1, 2, 3], "teammate": None,
         "metrics": [{"column": "offense_pct", "change": "chg", "recent": "rec", "delta_over_threshold": 3,
                      "sustained": True}],
         "details": {"games": [{"week": 4, "snaps": "s4", "snap_share": "p4", "targets": "t4", "team_targets": "tt4"},
                               {"week": 5, "snaps": "s5", "snap_share": "p5", "targets": "t5", "team_targets": "tt5"}],
                     "spread": {"returned": [{"gsis_id": "k", "name": "Other End", "snaps": "rs", "targets": "rt"}]}}}
    return p, claims


def test_same_snaps_and_a_target_swing_is_a_mystery_with_clues():
    p, claims = _mystery_player()
    case = E.mystery_case(p, claims)
    assert case and case["falling"]
    assert [c["kind"] for c in E.mystery_clues(p, claims, case)] == ["volume", "returned"]
    assert E.choose_format(p, claims) == "mystery"


def test_no_clues_means_no_mystery():
    # the team threw as much, and the returning teammate played less: nothing to show, so no mystery
    p, claims = _mystery_player(team_targets=(31, 30), returned_snaps=20)
    assert E.choose_format(p, claims) == "template"


def test_scores_have_reasons_and_a_weak_template_is_held():
    p, claims = _mystery_player()
    row = E.score(p, claims, "mystery", [])
    assert set(row["scores"]) == set(E.CRITERIA) and set(row["reasons"]) == set(E.CRITERIA)
    weak = E.score(p, claims, "template", [])
    assert weak["verdict"] == "reject" and any("reason to care" in r for r in weak["reject_reasons"])


def test_three_of_a_format_in_a_row_costs_distinctiveness():
    p, claims = _mystery_player()
    recent = [{"structure": {"format": "mystery"}, "players": []}] * 2
    assert E.score(p, claims, "mystery", recent)["scores"]["distinctiveness"] == 3


def test_narrated_names_drop_suffixes():
    assert said("Oronde Gadsden II") == "Oronde Gadsden" and said("Harold Fannin Jr.") == "Harold Fannin"


def test_live_injury_status_expires_at_kickoff():
    et = ZoneInfo("America/New_York")
    script = {"claims": [{"id": "c1", "source": {"kind": "player_status"}, "value": ["practice", 6, "x"],
                          "display": "x"}]}
    facts = {"players": [{"details": {"next_game": {"kickoff": "2026-10-18 13:00"}}}]}
    assert qc.gate_timeliness(script, facts, datetime(2026, 10, 16, 9, tzinfo=et)).passed
    late = qc.gate_timeliness(script, facts, datetime(2026, 10, 18, 13, 5, tzinfo=et))
    assert not late.passed and "kickoff" in late.failures[0]


def test_the_same_opening_with_new_names_is_caught():
    assert qc.opening_skeleton("Darius Cooper's role just jumped. But there's a catch.") == \
        qc.opening_skeleton("Emanuel Wilson's role just jumped. But there's a catch.")
    assert qc.opening_skeleton("Braelon Allen just got almost every Jets running back carry.") != \
        qc.opening_skeleton("Oronde Gadsden played the exact same snaps in back-to-back weeks.")

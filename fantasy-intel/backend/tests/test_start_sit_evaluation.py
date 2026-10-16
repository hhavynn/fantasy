"""Start/Sit decision evaluation on invented rosters and synthetic nflverse-shaped data (code paths and
leakage guards only; nothing here says anything about real forecasting accuracy)."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analytics import start_sit_evaluation as sse
from app.db.connection import connect, init_db
from synthetic_forecast import SCORING_ITEMS, add_league, add_participation, gsis, generate, write

UTC = timezone.utc
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
THU = datetime(2026, 10, 22, 0, 15, tzinfo=UTC)
SUN = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
FULL = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}


def cand(pid, pos, actual, preds=None, *, status="appeared", ko=SUN, slot="BE", starter=False, week=7, lg=1,
         tm=1, captured=None, synced=None, team="BUF"):
    """One roster player. `preds`: model -> value, captured a day before his kickoff unless `captured`."""
    cap = captured or (ko - timedelta(days=1))
    return {"league_id": lg, "fantasy_team_id": tm, "season": 2026, "week": week, "espn_player_id": pid,
            "gsis_id": f"g{pid}", "position": pos, "team": team, "kickoff": ko, "slot": slot, "is_starter": starter,
            "roster_synced_at": synced or (ko - timedelta(days=2)), "outcome_status": status,
            "actual_points": None if status != "appeared" else actual,
            "preds": {m: {"value": v, "captured_at": cap, "point_in_time": False} for m, v in (preds or {}).items()}}


CFG = sse.EvalConfig(min_decisions=1, min_weeks=1, resamples=200)


def rb_roster(*rows, **kw):
    return [cand(i, "RB", a, p, **kw) for i, (a, p) in enumerate(rows, start=1)]


# ----------------------------------------------------------------------------- pregame validity


def test_close_call_constant_matches_the_existing_pair_decisions():
    from app.analytics import forecast_backtest as fb

    assert sse.CLOSE_PAIR_POINTS == fb.CLOSE_PAIR_POINTS


def test_prediction_captured_at_or_after_kickoff_is_excluded_and_counted():
    ok = cand(1, "RB", 10, {"espn": 8.0, "A1": 7.0})
    late = cand(2, "RB", 12, {"espn": 9.0, "A1": 7.0}, captured=SUN + timedelta(hours=1))
    at_kick = cand(3, "RB", 5, {"espn": 4.0, "A1": 3.0}, captured=SUN)
    d = sse.decisions([ok, late, at_kick], ["espn", "A1"], {1: FULL}, CFG)
    assert d["pairs"] == []
    assert d["exclusions"]["pairs"] == {"post_kickoff_prediction:espn": 3}
    v, why = sse.prediction(late, "espn")
    assert v is None and why == "post_kickoff_prediction:espn"
    assert sse.prediction(at_kick, "espn")[1] == "post_kickoff_prediction:espn"       # strictly before


def test_thursday_game_snapshot_after_its_kickoff_only_invalidates_the_thursday_player():
    snap = THU + timedelta(hours=20)                    # Friday: before Sunday's kickoff, after Thursday's
    thu = cand(1, "RB", 10, {"espn": 8.0, "A1": 7.0}, ko=THU, captured=snap, team="BUF")
    sun1 = cand(2, "RB", 12, {"espn": 9.0, "A1": 7.5}, ko=SUN, captured=snap, team="KC")
    sun2 = cand(3, "RB", 5, {"espn": 6.0, "A1": 4.0}, ko=SUN, captured=snap, team="DEN")
    d = sse.decisions([thu, sun1, sun2], ["espn", "A1"], {1: FULL}, CFG)
    assert [r["pair"] for r in d["pairs"]] == [("g2", "g3")]                         # the Sunday pair only
    assert d["exclusions"]["pairs"] == {"post_kickoff_prediction:espn": 2}
    assert sse.prediction(thu, "espn")[0] is None and sse.prediction(sun1, "espn")[0] == 9.0


def test_unknown_kickoff_or_capture_time_is_never_pregame_but_point_in_time_baselines_are():
    a = cand(1, "RB", 1, {"espn": 1.0})
    a["kickoff"] = None
    assert sse.prediction(a, "espn")[1] == "unknown_kickoff:espn"
    b = cand(2, "RB", 1, {"espn": 1.0})
    b["preds"]["espn"]["captured_at"] = None
    assert sse.prediction(b, "espn")[1] == "unknown_capture_time:espn"
    b["preds"]["A1"] = {"value": 3.0, "captured_at": None, "point_in_time": True}
    assert sse.prediction(b, "A1") == (3.0, None)
    b["preds"]["A2"] = {"value": float("nan"), "captured_at": None, "point_in_time": True}
    assert sse.prediction(b, "A2")[1] == "no_prediction:A2"


# ----------------------------------------------------------------------------- equal population


def test_a_decision_needs_every_models_prediction_for_every_player():
    roster = [cand(1, "RB", 10, {"espn": 8.0, "A1": 5.0, "m": 9.0}), cand(2, "RB", 4, {"espn": 3.0, "A1": 6.0, "m": 2.0}),
              cand(3, "RB", 7, {"espn": 5.0, "A1": 5.5})]          # no model "m" for player 3
    three = sse.decisions(roster, ["espn", "A1", "m"], {1: FULL}, CFG)
    assert len(three["pairs"]) == 1 and three["exclusions"]["pairs"] == {"no_prediction:m": 2}
    two = sse.decisions(roster, ["espn", "A1"], {1: FULL}, CFG)
    assert len(two["pairs"]) == 3                                   # a smaller model set has its own population
    ev = sse.evaluate(roster, ["espn", "A1", "m"], {1: FULL}, CFG)
    n = {m: s["pairs"] for m, s in ev["same_position"]["stats"].items()}
    assert n == {"espn": 1, "A1": 1, "m": 1}


def test_model_sets_are_separate_populations_and_unavailable_models_are_never_backfilled():
    roster = rb_roster((10, {"espn": 8.0, "A1": 5.0, "A2": 4.0}), (4, {"espn": 3.0, "A1": 6.0, "A2": 5.0}))
    res = sse.evaluate_sets(roster, {1: FULL}, config=CFG)
    assert res["sets"]["baselines"]["evaluated"] and res["sets"]["baselines"]["same_position"]["decisions"] == 1
    assert res["availability"]["ifp2_frozen"]["unavailable"] is True
    assert res["sets"]["ifp2_frozen"] == {"models": ["espn", "A1", "ifp2_frozen"],
                                          "unavailable_models": ["ifp2_frozen"], "evaluated": False}


# ----------------------------------------------------------------------------- outcomes


def test_pending_and_unresolved_outcomes_are_never_scored_and_confirmed_absence_is_zero():
    both = {"espn": 5.0, "A1": 5.0}
    played = cand(1, "RB", 5.0, {"espn": 9.0, "A1": 4.0})
    absent = cand(2, "RB", None, {"espn": 8.0, "A1": 9.0}, status="confirmed_absent")
    pending = cand(3, "RB", None, both, status="pending_game")
    missing = cand(4, "RB", None, both, status="missing_ingestion")
    unresolved = cand(5, "RB", None, both, status="unresolved_identity")
    d = sse.decisions([played, absent, pending, missing, unresolved], ["espn", "A1"], {1: FULL}, CFG)
    assert len(d["pairs"]) == 1
    r = d["pairs"][0]
    assert r["pair"] == ("g1", "g2") and r["score"] == {"espn": 1.0, "A1": 0.0}     # absent scored 0 < 5
    assert r["regret"] == {"espn": 0.0, "A1": 5.0}
    assert d["exclusions"]["pairs"]["outcome_pending_game"] == 4 and d["exclusions"]["pairs"]["outcome_missing_ingestion"] == 3
    assert sse.outcome(absent) == (0.0, None) and sse.outcome(pending)[0] is None


def test_actual_ties_are_skipped_and_prediction_ties_count_half():
    a, b, c = rb_roster((6.0, {"espn": 5.0, "A1": 5.0}), (6.0, {"espn": 4.0, "A1": 5.0}), (2.0, {"espn": 3.0, "A1": 5.0}))
    d = sse.decisions([a, b, c], ["espn", "A1"], {1: FULL}, CFG)
    assert d["exclusions"]["actual_ties_skipped"] == 1
    by = {r["pair"]: r for r in d["pairs"]}
    assert by[("g1", "g3")]["score"]["A1"] == 0.5 and by[("g1", "g3")]["regret"]["A1"] == 2.0   # (6-2)/2
    assert by[("g1", "g3")]["score"]["espn"] == 1.0


# ----------------------------------------------------------------------------- close calls / flex / lineups


def test_close_decision_subset_uses_the_baseline_identical_for_all_models():
    r1, r2, r3 = rb_roster((10, {"espn": 1.0, "A1": 10.0}), (12, {"espn": 2.0, "A1": 11.0}), (30, {"espn": 3.0, "A1": 20.0}))
    ev = sse.evaluate([r1, r2, r3], ["espn", "A1"], {1: FULL}, CFG)
    assert ev["same_position"]["decisions"] == 3 and ev["close_calls"]["decisions"] == 1     # only 10 vs 11 is within 3
    assert ev["close_calls"]["stats"]["espn"]["accuracy"] == 1.0 and ev["close_calls"]["stats"]["A1"]["accuracy"] == 1.0
    far = sse.EvalConfig(min_decisions=1, min_weeks=1, resamples=50, close_points=100.0)
    assert sse.evaluate([r1, r2, r3], ["espn", "A1"], {1: FULL}, far)["close_calls"]["decisions"] == 3


def test_flex_pairs_exist_only_when_the_league_has_a_slot_both_positions_can_fill():
    rb, wr, te = (cand(1, "RB", 9, {"espn": 5.0, "A1": 4.0}), cand(2, "WR", 5, {"espn": 6.0, "A1": 3.0}),
                  cand(3, "TE", 2, {"espn": 1.0, "A1": 1.5}))
    no_flex = sse.decisions([rb, wr, te], ["espn", "A1"], {1: {"QB": 1, "RB": 2, "WR": 2, "TE": 1}}, CFG)
    assert no_flex["pairs"] == []
    flex = sse.decisions([rb, wr, te], ["espn", "A1"], {1: {**FULL, "FLEX": 1}}, CFG)
    assert sorted(r["positions"] for r in flex["pairs"]) == [("RB", "TE"), ("RB", "WR"), ("WR", "TE")]
    assert {r["kind"] for r in flex["pairs"]} == {"flex"}
    wt = sse.decisions([rb, wr, te], ["espn", "A1"], {1: {**FULL, "WR/TE": 1}}, CFG)    # an RB cannot fill WR/TE
    assert [r["positions"] for r in wt["pairs"]] == [("WR", "TE")]
    assert sse.evaluate([rb, wr, te], ["espn", "A1"], {1: {**FULL, "FLEX": 1}}, CFG)["flex"]["decisions"] == 3


def test_lineup_regret_matches_a_hand_computed_example():
    # slots RB, WR, FLEX. actual: RB1 10, RB2 14, WR1 6, WR2 12, TE 3.
    # hindsight: RB 14, WR 12, FLEX best of {RB1 10, WR1 6, TE?} -> TE is not FLEX-eligible here? FLEX = RB/WR/TE -> 10.
    pts = {1: ("RB", 10), 2: ("RB", 14), 3: ("WR", 6), 4: ("WR", 12), 5: ("TE", 3)}
    exact = {i: float(a) for i, (_, a) in pts.items()}
    bad = {1: 8.0, 2: 7.0, 3: 9.0, 4: 5.0, 5: 4.0}      # fixed slots: RB1 (8), WR1 (9); FLEX: RB2 (7) -> 10 + 6 + 14 = 30
    roster = [cand(i, p, a, {"espn": exact[i], "A1": bad[i]}) for i, (p, a) in pts.items()]
    d = sse.decisions(roster, ["espn", "A1"], {1: {"RB": 1, "WR": 1, "FLEX": 1}}, CFG)
    (row,) = d["lineups"]
    assert row["hindsight_points"] == 36.0 and row["slots"] == 3
    assert row["points"] == {"espn": 36.0, "A1": 30.0} and row["regret"] == {"espn": 0.0, "A1": 6.0}
    ev = sse.evaluate(roster, ["espn", "A1"], {1: {"RB": 1, "WR": 1, "FLEX": 1}}, CFG)
    lu = ev["lineups"]
    assert lu["regret"]["A1"] == {"mean": 6.0, "p90": 6.0, "max": 6.0} and lu["regret"]["espn"]["max"] == 0.0
    assert lu["points_vs_reference"]["A1"]["difference"] == -6.0          # points lost against ESPN's lineup
    assert lu["regret_difference"]["A1"]["vs_espn"]["difference"] == 6.0


def test_lineup_slots_respect_eligibility_and_incomplete_rosters_are_excluded():
    # An RB can fill neither a WR slot nor WR/TE: the roster cannot fill the lineup.
    two_rb = rb_roster((10, {"espn": 1.0, "A1": 1.0}), (8, {"espn": 2.0, "A1": 2.0}))
    d = sse.decisions(two_rb, ["espn", "A1"], {1: {"WR": 1}}, CFG)
    assert d["lineups"] == [] and d["exclusions"]["lineups"] == {"roster_cannot_fill_slots": 1}
    mixed = two_rb + [cand(3, "WR", 3, {"espn": 0.5, "A1": 0.5})]
    (row,) = sse.decisions(mixed, ["espn", "A1"], {1: {"WR": 1}}, CFG)["lineups"]
    assert row["hindsight_points"] == 3.0                                  # the WR, never an RB
    # one player lacking a model prediction: 'complete' drops the lineup, 'valid_subset' uses the rest
    broken = mixed + [cand(4, "WR", 20, {"espn": 5.0})]
    assert sse.decisions(broken, ["espn", "A1"], {1: {"WR": 1}}, CFG)["lineups"] == []
    sub = sse.EvalConfig(min_decisions=1, min_weeks=1, resamples=50, roster_policy="valid_subset")
    (row,) = sse.decisions(broken, ["espn", "A1"], {1: {"WR": 1}}, sub)["lineups"]
    assert row["dropped_players"] == 1 and row["hindsight_points"] == 3.0


def test_unknown_slot_settings_and_ir_players_are_not_decisions():
    roster = rb_roster((10, {"espn": 1.0, "A1": 1.0}), (8, {"espn": 2.0, "A1": 2.0}))
    roster[1]["slot"] = "IR"
    d = sse.decisions(roster, ["espn", "A1"], {1: FULL}, CFG)
    assert d["pairs"] == [] and d["exclusions"]["candidates"] == {"ir_slot": 1}
    assert sse.decisions(roster, ["espn", "A1"], {}, CFG)["exclusions"]["lineups"] == {"slot_settings_unknown": 1}


def test_manager_lineup_reference_and_roster_basis_labels():
    roster = [cand(1, "RB", 10, {"espn": 1.0, "A1": 2.0}, slot="RB", starter=True),
              cand(2, "RB", 3, {"espn": 2.0, "A1": 1.0})]
    (row,) = sse.decisions(roster, ["espn", "A1"], {1: {"RB": 1}}, CFG)["lineups"]
    assert row["manager_points"] == 10.0 and row["roster_basis"] == "pregame_snapshot"
    late = [dict(c, roster_synced_at=SUN + timedelta(days=2)) for c in roster]
    (row,) = sse.decisions(late, ["espn", "A1"], {1: {"RB": 1}}, CFG)["lineups"]
    assert row["roster_basis"] == "post_hoc_snapshot"
    ev = sse.evaluate(late, ["espn", "A1"], {1: {"RB": 1}}, CFG)
    assert ev["roster_basis_counts"]["lineups"] == 0 and ev["lineups"]["by_roster_basis"] == {"post_hoc_snapshot": 1}


# ----------------------------------------------------------------------------- statistics


def _rows(week_values):
    return [{"season": 2026, "week": w, "v": v} for w, vals in week_values.items() for v in vals]


def test_bootstrap_is_deterministic_and_resamples_whole_weeks():
    rows = _rows({1: [1.0] * 100, 2: [-1.0] * 100})
    a = sse.cluster_bootstrap(rows, lambda r: r["v"], resamples=500, seed=3)
    assert a == sse.cluster_bootstrap(rows, lambda r: r["v"], resamples=500, seed=3)
    # only whole-week resamples are possible: means of 100% / 50% / 0% of week 1, so the CI spans [-1, 1].
    # An i.i.d. bootstrap over the 200 decisions would give an interval of about +-0.14 around 0.
    assert a["ci95"] == [-1.0, 1.0] and a["mean"] == 0.0 and (a["n"], a["weeks"]) == (200, 2)
    shifted = sse.cluster_bootstrap(rows, lambda r: r["v"], resamples=500, seed=4)
    assert shifted["ci95"] == [-1.0, 1.0] and shifted["mean"] == a["mean"]
    assert sse.cluster_bootstrap([], lambda r: 0.0, resamples=10, seed=1) == {"mean": None, "ci95": None, "n": 0, "weeks": 0}


def test_verdicts_follow_thresholds_and_direction():
    boot = lambda lo, hi, n=500, w=10: {"mean": (lo + hi) / 2, "ci95": [lo, hi], "n": n, "weeks": w}   # noqa: E731
    kw = {"min_decisions": 200, "min_weeks": 8}
    assert sse.verdict(boot(0.01, 0.05, n=199), higher_is_better=True, **kw) == "insufficient_evidence"
    assert sse.verdict(boot(0.01, 0.05, w=7), higher_is_better=True, **kw) == "insufficient_evidence"
    assert sse.verdict({"mean": None, "ci95": None, "n": 0, "weeks": 0}, higher_is_better=True, **kw) == "insufficient_evidence"
    assert sse.verdict(boot(-0.02, 0.05), higher_is_better=True, **kw) == "no_significant_difference"
    assert sse.verdict(boot(0.01, 0.05), higher_is_better=True, **kw) == "better"
    assert sse.verdict(boot(-0.05, -0.01), higher_is_better=True, **kw) == "worse"
    assert sse.verdict(boot(-0.05, -0.01), higher_is_better=False, **kw) == "better"     # lower regret
    assert sse.verdict(boot(0.01, 0.05), higher_is_better=False, **kw) == "worse"


def test_small_samples_never_produce_a_claim():
    roster = rb_roster((10, {"espn": 1.0, "A1": 9.0}), (4, {"espn": 9.0, "A1": 1.0}))     # A1 right, ESPN wrong
    ev = sse.evaluate(roster, ["espn", "A1"], {1: FULL}, sse.EvalConfig(resamples=100))   # default thresholds 200 / 8
    cmp = ev["same_position"]["accuracy_difference"]["A1"]["vs_espn"]
    assert cmp["difference"] == 1.0 and cmp["verdict"] == "insufficient_evidence"
    assert ev["lineups"]["points_vs_reference"] == {} or all(
        v["verdict"] == "insufficient_evidence" for v in ev["lineups"]["points_vs_reference"].values())


def test_a_real_difference_over_enough_weeks_is_reported():
    cands = []
    for week in range(1, 11):
        for t in range(1, 7):                              # six rosters per week, A1 always right, ESPN always wrong
            cands += [cand(100 * week + 10 * t + 1, "RB", 10, {"espn": 1.0, "A1": 9.0}, week=week, tm=t),
                      cand(100 * week + 10 * t + 2, "RB", 4, {"espn": 9.0, "A1": 1.0}, week=week, tm=t)]
    ev = sse.evaluate(cands, ["espn", "A1"], {1: FULL}, sse.EvalConfig(resamples=100))
    c = ev["same_position"]["accuracy_difference"]["A1"]["vs_espn"]
    assert (c["n"], c["weeks"]) == (60, 10) and c["difference"] == 1.0
    assert c["verdict"] == "insufficient_evidence"          # 60 < 200 decisions
    ev = sse.evaluate(cands, ["espn", "A1"], {1: FULL}, sse.EvalConfig(min_decisions=50, resamples=100))
    assert ev["same_position"]["accuracy_difference"]["A1"]["vs_espn"]["verdict"] == "better"
    assert ev["same_position"]["regret_difference"]["A1"]["vs_espn"]["verdict"] == "better"     # lower regret
    assert ev["same_position"]["unique_player_pairs"] == 60


# ----------------------------------------------------------------------------- decision sets


def _set(cands, recorded, **kw):
    return sse.decision_set(2026, 7, cands, kw.get("refused", []), {1: FULL}, recorded, models=["espn", "A1"])


def test_post_kickoff_players_are_refused_when_recording():
    now = THU + timedelta(hours=3)
    thu = cand(1, "RB", 0, {"espn": 1.0}, ko=THU)
    sun = cand(2, "RB", 0, {"espn": 1.0}, ko=SUN)
    unknown = dict(cand(3, "RB", 0, {"espn": 1.0}), kickoff=None)
    kept, refused = sse.refuse_post_kickoff([thu, sun, unknown], now)
    assert [c["espn_player_id"] for c in kept] == [2]
    assert [(r["espn_player_id"], r["reason"]) for r in refused] == [(1, "kicked_off"), (3, "unknown_kickoff")]
    assert refused[0]["league_id"] == 1 and refused[0]["fantasy_team_id"] == 1 and refused[0]["week"] == 7
    assert sse.refuse_post_kickoff([thu], THU)[1][0]["reason"] == "kicked_off"       # at kickoff is already too late


def test_decision_sets_are_write_once_content_addressed_and_idempotent(tmp_path):
    cands = [dict(cand(1, "RB", 0, {"espn": 5.0, "A1": 4.0})), dict(cand(2, "RB", 0, {"espn": 3.0, "A1": 6.0}))]
    for c in cands:
        c.pop("actual_points"), c.pop("outcome_status")
    first = _set(cands, SUN - timedelta(days=1))
    path, created = sse.write_decision_set(tmp_path, first)
    assert created and path.parent == tmp_path / "decision_sets" and first["content_sha"][:16] in path.name
    again = _set(cands, SUN - timedelta(hours=5))          # same content, recorded later
    assert again["content_sha"] == first["content_sha"] and again["recorded_at"] != first["recorded_at"]
    assert again["sha"] != first["sha"]                 # the file hash covers the recording time
    path2, created2 = sse.write_decision_set(tmp_path, again)
    assert path2 == path and created2 is False
    assert json.loads(path.read_text())["recorded_at"] == first["recorded_at"]        # never rewritten
    assert len(list((tmp_path / "decision_sets").glob("*.json"))) == 1
    assert "actual_points" not in json.dumps(first)                                     # no outcomes in a frozen set
    changed = _set(cands[:1], SUN - timedelta(hours=5))
    assert changed["content_sha"] != first["content_sha"] and sse.write_decision_set(tmp_path, changed)[1]
    assert len(sse.load_decision_sets(tmp_path)) == 2


def test_tampered_sets_do_not_load_and_later_sets_cannot_rewrite_a_pregame_one(tmp_path):
    base = [dict(cand(1, "RB", 0, {"espn": 5.0}))]
    early = _set(base, SUN - timedelta(days=2))
    path, _ = sse.write_decision_set(tmp_path, early)
    data = json.loads(path.read_text())
    data["candidates"][0]["preds"]["espn"]["value"] = 99.0
    path.write_text(json.dumps(data))
    assert sse.verify_decision_set(data) is False and sse.load_decision_sets(tmp_path) == []
    path.write_text(json.dumps(early, default=str))
    # a set recorded AFTER the player's kickoff is ignored for him; the pregame one is kept
    late = _set([dict(base[0], preds={"espn": {"value": 50.0, "captured_at": SUN - timedelta(hours=1), "point_in_time": False}})],
                SUN + timedelta(hours=1))
    mid = _set([dict(base[0], preds={"espn": {"value": 6.0, "captured_at": SUN - timedelta(hours=3), "point_in_time": False}})],
               SUN - timedelta(hours=2))
    cands, settings = sse.candidates_from_sets([early, late, mid])
    assert len(cands) == 1 and cands[0]["preds"]["espn"]["value"] == 6.0 and settings == {1: FULL}
    assert cands[0]["kickoff"] == SUN and sse.prediction(cands[0], "espn")[0] == 6.0


# ----------------------------------------------------------------------------- database adapters

SEASONS = [2024, 2025]
FETCHED = datetime(2025, 12, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    path = tmp_path_factory.mktemp("sse") / "s.duckdb"
    con = connect(path)
    init_db(con)
    write(con, add_participation(generate(SEASONS, unfinished={2025: 10})))
    add_league(con, 2025, 10)
    con.execute("UPDATE leagues SET roster_slots = ? WHERE league_id = 99",
                [json.dumps({"QB": 1, "RB": 1, "WR": 2, "TE": 1, "FLEX": 1, "BE": 3, "IR": 1})])
    add_league_roster(con)
    con.close()
    return path


def ro(path):
    return connect(path, read_only=True)


def add_league_roster(con):
    """Fantasy team 1 holds seven AAA players every week 8..10; ids are 1000 + index."""
    slots = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE"]
    pos = {"QB": "QB", "RB1": "RB", "RB2": "RB", "WR1": "WR", "WR2": "WR", "WR3": "WR", "TE": "TE"}
    for i, s in enumerate(slots):
        pid = 1000 + i
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence, espn_position, notes) "
                    "VALUES (?, ?, 'high', ?, NULL)", [pid, gsis("AAA", s), pos[s]])
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, 'AAA')",
                    [pid, s, pos[s]])
        for week in (8, 9, 10):
            ko = datetime(2025, 9, week, 17, 0)                           # synthetic kickoff, naive UTC
            con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, "
                        "lineup_slot, is_starter, synced_at) VALUES (99, 2025, ?, 1, ?, ?, ?, ?)",
                        [week, pid, "BE" if s == "WR3" else pos[s], s != "WR3",
                         ko + timedelta(days=3) if week < 10 else ko - timedelta(days=2)])
            for days, val in ((2, 5.0 + i), (0.5, 6.0 + i)):             # two ESPN snapshots, both before kickoff
                con.execute("INSERT INTO espn_projection_history VALUES (?, 2025, ?, ?, ?, ?)",
                            [(ko - timedelta(days=days)).date(), pid, week, val, ko - timedelta(days=days)])
    # a late snapshot (after kickoff) for week 9 RB1 must never be used
    con.execute("INSERT INTO espn_projection_history VALUES ('2025-09-10', 2025, 1001, 9, 99.0, '2025-09-09 20:00:00')")
    con.execute("UPDATE player_id_map SET gsis_id = ?, match_confidence = 'medium' WHERE espn_player_id = 1005",
                [gsis("AAA", "WR3")])                                      # an untrusted mapping: unresolved identity


def test_roster_candidates_follow_the_trusted_identity_rule_and_label_the_snapshot(env):
    con = ro(env)
    cands = sse.roster_candidates(con, 2025, [9])
    assert len(cands) == 7 and {c["league_id"] for c in cands} == {99}
    te = next(c for c in cands if c["espn_player_id"] == 1005)
    assert te["gsis_id"] is None                                          # medium confidence: never guessed
    qb = next(c for c in cands if c["espn_player_id"] == 1000)
    assert qb["gsis_id"] == gsis("AAA", "QB") and qb["team"] == "AAA" and qb["kickoff"] == datetime(2025, 9, 9, 17, tzinfo=UTC)
    assert sse.roster_basis(cands) == "post_hoc_snapshot"                  # week 9 was synced after the games
    assert sse.roster_basis(sse.roster_candidates(con, 2025, [10])) == "pregame_snapshot"
    assert sse.league_settings(con, 2025)[99]["roster_slots"]["FLEX"] == 1
    con.close()


def test_espn_pregame_snapshot_is_the_latest_one_strictly_before_the_players_kickoff(env):
    con = ro(env)
    cands = sse.roster_candidates(con, 2025, [9])
    snaps = sse.espn_pregame_predictions(con, 2025, cands)
    assert snaps[(9, 1001)]["value"] == 7.0 and snaps[(9, 1001)]["captured_at"] == datetime(2025, 9, 8, 17, 0, tzinfo=UTC) + timedelta(hours=12)
    assert all(v["value"] != 99.0 for v in snaps.values())                # the post-kickoff 99.0 snapshot is ignored
    early = sse.espn_pregame_predictions(con, 2025, cands, as_of=datetime(2025, 9, 7, 17, 0, tzinfo=UTC))
    assert early[(9, 1001)]["value"] == 6.0                               # recorded earlier: only the earlier snapshot
    con.close()


def test_legacy_production_reader_requires_a_run_before_kickoff_and_matching_version(env, tmp_path):
    import shutil
    shutil.copy(env, tmp_path / "copy.duckdb")
    con = connect(tmp_path / "copy.duckdb")
    con.execute("INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint, "
                "artifact_created_at, created_at) VALUES ('r9', 2025, 9, 'ifp-1.0.0', 'fp', '2025-09-01T00:00:00+00:00', "
                "'2025-09-08 12:00:00'), ('r8', 2025, 8, 'ifp-1.0.0', 'fp', '2025-09-01T00:00:00+00:00', '2025-09-09 12:00:00'), "
                "('r10', 2025, 10, 'ifp-9', 'fp', '2025-09-01T00:00:00+00:00', '2025-09-08 12:00:00')")
    for run, week in (("r9", 9), ("r8", 8), ("r10", 10)):
        con.execute("INSERT INTO independent_player_projections (season, week, espn_player_id, gsis_id, run_id, position, "
                    "nfl_team, projected_points, p_play, expected_points, coverage_status) VALUES "
                    "(2025, ?, 1001, ?, ?, 'RB', 'AAA', 10.0, 0.9, 9.0, 'projected')", [week, gsis("AAA", "RB1"), run])
    rows, excl = sse.legacy_forecast_reader(con, 2025, None, model_version="ifp-1.0.0", scoring_fingerprint="fp")
    assert [(r["week"], r["expected_points"]) for r in rows] == [(9, 9.0)]
    reasons = {(e["week"], e["reason"]) for e in excl}
    assert reasons == {(8, "run_created_after_kickoff"), (10, "model_version_mismatch")}
    assert {"run_id", "run_created_at", "model_version", "scoring_fingerprint", "expected_points", "kickoff"} <= set(rows[0])
    look = sse.production_lookup(rows)
    assert look[(9, 1001)]["value"] == 9.0 and look[(9, 1001)]["captured_at"] < rows[0]["kickoff"]
    assert sse.legacy_forecast_reader(connect(":memory:"), 2025, None, model_version="x", scoring_fingerprint="y")[0] == []
    con.close()


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("start_sit_evaluation_cli", SCRIPTS / "start_sit_evaluation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_builds_equal_population_decisions_from_real_tables(env, cli):
    path = env
    live = cli.duckdb.connect(str(path), read_only=True)
    try:
        src, scoring = cli.load_context(live, None, 2025)
        cands, settings, info = cli.build_report_candidates(live, None, src, scoring, 2025, [8, 9], None, {2025: FETCHED})
    finally:
        live.close()
    assert settings == {99: {"QB": 1, "RB": 1, "WR": 2, "TE": 1, "FLEX": 1, "BE": 3, "IR": 1}}
    statuses = {c["outcome_status"] for c in cands}
    assert statuses <= {"appeared", "confirmed_absent", "unresolved_identity"} and "unresolved_identity" in statuses
    assert all(c["actual_points"] is None for c in cands if c["outcome_status"] == "unresolved_identity")
    assert info["attached"]["espn"] == 14 and info["attached"]["A1"] > 0
    assert info["attached"]["ifp1_frozen"] == 0 and info["attached"]["ifp1_production"] == 0
    res = sse.evaluate_sets(cands, settings, config=sse.EvalConfig(min_decisions=1, min_weeks=1, resamples=50))
    assert res["availability"]["ifp1_frozen"]["unavailable"] and res["availability"]["espn"]["with_pregame_prediction"] == 14
    base = res["sets"]["baselines"]
    assert base["evaluated"] and base["same_position"]["decisions"] > 0 and base["flex"]["decisions"] > 0
    # the untrusted-id WR is unresolved: 'complete' drops both week-8/9 lineups and counts them ...
    assert base["lineups"]["decisions"] == 0 and base["exclusions"]["lineups"] == {"outcome_unresolved_identity": 2}
    # ... pairs that involve him are excluded by reason, never scored
    assert base["exclusions"]["pairs"]["outcome_unresolved_identity"] > 0
    # 'valid_subset' keeps the six resolved players; every such lineup is a post-hoc roster snapshot
    sub = sse.evaluate(cands, ["espn", "A1"], settings, sse.EvalConfig(min_decisions=1, min_weeks=1, resamples=50,
                                                                       roster_policy="valid_subset"))
    assert sub["lineups"]["decisions"] == 2 and sub["lineups"]["by_roster_basis"] == {"post_hoc_snapshot": 2}
    assert sub["roster_basis_counts"]["lineups"] == 0


def _clock(cli, monkeypatch, iso):
    monkeypatch.setattr(cli, "_wall_clock", lambda: datetime.fromisoformat(iso))


def test_record_cli_is_write_once_and_refuses_after_kickoff(env, cli, tmp_path, capsys, monkeypatch):
    path = env
    out = tmp_path / "ss"
    before = ["--season", "2025", "--week", "10", "--live-db", str(path), "--history-db", str(tmp_path / "none.duckdb"),
              "--out", str(out), "--ledger-dir", str(tmp_path / "noledger")]
    now = "2025-09-09T12:00:00+00:00"                                   # day before week 10 kicks off
    _clock(cli, monkeypatch, now)
    assert cli.main(["record", *before, "--now", now]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["recorded"] and first["created"] and first["players"] == 7 and first["refused"] == 0
    assert "ifp2_frozen" in first["unavailable_models"] and "espn" not in first["unavailable_models"]
    assert cli.main(["record", *before, "--now", now]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["created"] is False and again["sha"] == first["sha"] and again["path"] == first["path"]
    ds = json.loads(Path(first["path"]).read_text())
    assert sse.verify_decision_set(ds) and "actual_points" not in json.dumps(ds)
    assert {c["espn_player_id"] for c in ds["candidates"]} == set(range(1000, 1007))
    # after the games kicked off nothing may be recorded for them
    _clock(cli, monkeypatch, "2025-09-10T18:00:00+00:00")
    assert cli.main(["record", *before, "--now", "2025-09-10T18:00:00+00:00"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["recorded"] is False and refused["refused"] == 7
    assert len(list((out / "decision_sets").glob("*.json"))) == 1
    # later scoring joins outcomes onto the recorded set (still pending here: week 10 is not final)
    cands, settings = sse.candidates_from_sets(sse.load_decision_sets(out))
    assert len(cands) == 7 and settings == {99: {"QB": 1, "RB": 1, "WR": 2, "TE": 1, "FLEX": 1, "BE": 3, "IR": 1}}
    live = cli.duckdb.connect(str(path), read_only=True)
    try:
        src, _ = cli.load_context(live, None, 2025)
    finally:
        live.close()
    cli.attach_outcomes(cands, src, {2025: FETCHED})
    assert {c["outcome_status"] for c in cands} <= {"pending_game", "unresolved_identity", "bye"}
    assert sse.decisions(cands, ["espn", "A1"], settings, CFG)["pairs"] == []


def test_report_cli_runs_read_only_and_prints_unavailable_models(env, cli, capsys, tmp_path):
    path = env
    args = ["report", "--season", "2025", "--weeks", "8-9", "--live-db", str(path), "--history-db",
            str(tmp_path / "none.duckdb"), "--out", str(tmp_path / "empty"), "--ledger-dir", str(tmp_path / "noledger"),
            "--min-decisions", "1", "--min-weeks", "1", "--resamples", "50"]
    assert cli.main(args) == 0
    text = capsys.readouterr().out
    assert "64.7%" in text and "UNAVAILABLE" in text and "ifp2_frozen" in text
    assert cli.main([*args, "--json", "--source", "snapshot"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "recorded" not in data and data["snapshot"]["availability"]["ifp2_frozen"]["unavailable"]


# ----------------------------------------------------------------------------- review fixes


def _neg_roster(flex_actual):
    return [cand(1, "QB", 20, {"espn": 18, "A1": 15}), cand(2, "RB", 15, {"espn": 12, "A1": 14}),
            cand(3, "RB", 9, {"espn": 13, "A1": 8}), cand(4, "WR", 14, {"espn": 10, "A1": 12}),
            cand(5, "WR", 12, {"espn": 11, "A1": 13}), cand(6, "TE", 8, {"espn": 7, "A1": 9}),
            cand(7, "WR", flex_actual, {"espn": 9, "A1": 6})]


NEG_SETTINGS = {1: {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}}


def test_fillability_never_depends_on_outcomes_a_negative_flex_score_keeps_the_lineup():
    for flex in (0.5, -1.5):
        d = sse.decisions(_neg_roster(flex), ["espn", "A1"], NEG_SETTINGS, sse.EvalConfig())
        assert len(d["lineups"]) == 1 and d["exclusions"]["lineups"] == {}
    (row,) = d["lineups"]
    # hindsight: QB 20, RB 15 + 9, WR 14 + 12, TE 8, FLEX = the only left-over WR at -1.5
    assert row["hindsight_points"] == 20 + 15 + 9 + 14 + 12 + 8 - 1.5 == 76.5
    assert row["regret"]["espn"] >= 0 and row["regret"]["A1"] >= 0


def test_negative_predictions_below_the_optimiser_floor_are_handled_too():
    roster = _neg_roster(3.0)
    roster[6]["preds"]["espn"]["value"] = -5.0
    roster[5]["preds"]["espn"]["value"] = -9.0          # flex sums far below -1 for the ESPN lineup
    (row,) = sse.decisions(roster, ["espn", "A1"], NEG_SETTINGS, sse.EvalConfig())["lineups"]
    assert row["points"]["espn"] > 0 and row["slots"] == 7


def test_a_roster_that_cannot_fill_by_position_is_still_excluded_whatever_the_scores():
    two_wr = [cand(1, "WR", -2.0, {"espn": 1.0, "A1": 1.0}), cand(2, "WR", -3.0, {"espn": 2.0, "A1": 2.0})]
    d = sse.decisions(two_wr, ["espn", "A1"], {1: {"WR": 3}}, sse.EvalConfig())
    assert d["lineups"] == [] and d["exclusions"]["lineups"] == {"roster_cannot_fill_slots": 1}


def test_recorded_at_is_inside_the_hash_and_editing_it_fails_verification(tmp_path):
    ds = _set([dict(cand(1, "RB", 0, {"espn": 5.0}))], SUN - timedelta(days=1))
    assert sse.verify_decision_set(ds)
    moved = dict(ds, recorded_at=(SUN - timedelta(days=30)).isoformat())
    assert sse.verify_decision_set(moved) is False
    path, _ = sse.write_decision_set(tmp_path, moved)
    assert sse.load_decision_sets(tmp_path) == []


def test_future_evidence_is_dropped_at_recording_time():
    t = SUN - timedelta(days=1)
    ok = cand(1, "RB", 0, {"espn": 5.0}, synced=t - timedelta(hours=1))
    late_roster = cand(2, "RB", 0, {"espn": 5.0}, synced=t + timedelta(hours=1))
    late_pred = cand(3, "RB", 0, {"espn": 5.0, "A1": 4.0}, synced=t - timedelta(hours=1))
    late_pred["preds"]["espn"]["captured_at"] = t + timedelta(minutes=5)
    late_pred["preds"]["A1"] = {"value": 4.0, "captured_at": None, "point_in_time": True}
    kept, refused = sse.drop_future_evidence([ok, late_roster, late_pred], t)
    assert [c["espn_player_id"] for c in kept] == [1, 3] and refused[0]["reason"] == "roster_synced_after_record"
    assert set(kept[1]["preds"]) == {"A1"}                 # the late ESPN capture is removed, the baseline stays
    # and defensively when reading a set back: a row claiming evidence from after its recording is unusable
    bad = _set([dict(late_roster)], t)
    assert sse.candidates_from_sets([bad])[0] == []


def test_the_latest_pregame_set_defines_the_roster_and_earlier_rows_survive_only_for_kicked_off_players():
    thu, sat = SUN - timedelta(days=3), SUN - timedelta(days=1)
    a, b, c = (cand(1, "WR", 10, {"espn": 9.0}, synced=THU - timedelta(days=2)), cand(2, "WR", 0, {"espn": 8.0}, synced=THU - timedelta(days=2)),
               cand(3, "WR", 7, {"espn": 6.0}))
    early = sse.decision_set(2026, 7, [a, b], [], {1: {"WR": 1}}, thu, models=["espn"])
    late = sse.decision_set(2026, 7, [a, c], [], {1: {"WR": 1}}, sat, models=["espn"])          # B dropped, C added
    cands, _ = sse.candidates_from_sets([early, late])
    assert sorted(x["espn_player_id"] for x in cands) == [1, 3]                       # no phantom roster
    # a player who kicked off before the later set keeps his earlier pregame row
    thu_game = dict(b, kickoff=THU, preds={"espn": {"value": 8.0, "captured_at": THU - timedelta(hours=5), "point_in_time": False}},
                    roster_synced_at=THU - timedelta(days=1))
    first = sse.decision_set(2026, 7, [a, thu_game], [], {1: {"WR": 1}}, THU - timedelta(hours=3), models=["espn"])
    kept, refused = sse.refuse_post_kickoff([dict(thu_game), dict(a)], THU + timedelta(days=1))
    assert [r["reason"] for r in refused] == ["kicked_off"]
    second = sse.decision_set(2026, 7, kept, refused, {1: {"WR": 1}}, THU + timedelta(days=1), models=["espn"])
    cands, _ = sse.candidates_from_sets([first, second])
    assert sorted(x["espn_player_id"] for x in cands) == [1, 2]
    assert next(x for x in cands if x["espn_player_id"] == 2)["kickoff"] == THU


def test_audit_only_sets_are_ignored_as_prospective_evidence():
    ds = sse.decision_set(2026, 7, [dict(cand(1, "RB", 0, {"espn": 5.0}))], [], {1: FULL}, SUN - timedelta(days=1),
                          models=["espn"], audit_only=True)
    assert sse.verify_decision_set(ds) and ds["audit_only"] is True
    assert sse.candidates_from_sets([ds]) == ([], {})


def test_record_under_an_overridden_clock_refuses_stale_rosters_and_tags_the_rest_audit_only(env, cli, tmp_path, capsys):
    base = ["--live-db", str(env), "--history-db", str(tmp_path / "x"), "--ledger-dir", str(tmp_path / "noledger")]
    # week 9 roster rows were synced AFTER kickoff: a backdated record must not turn them into a pregame set
    out = tmp_path / "a"
    assert cli.main(["record", "--season", "2025", "--week", "9", *base, "--out", str(out),
                     "--now", "2025-09-08T12:00:00+00:00"]) == 2
    assert not (out / "decision_sets").exists()
    capsys.readouterr()
    # week 10 rows are pregame at that time, but the real clock is later: audit-only, never scored as recorded
    out = tmp_path / "b"
    assert cli.main(["record", "--season", "2025", "--week", "10", *base, "--out", str(out),
                     "--now", "2025-09-09T12:00:00+00:00"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["audit_only"] is True
    ds = json.loads(Path(res["path"]).read_text())
    assert ds["audit_only"] and ds["meta"]["clock_override"]["requested_now"].startswith("2025-09-09T12")
    assert sse.candidates_from_sets(sse.load_decision_sets(out))[0] == []


def test_a_now_in_the_future_of_the_real_clock_is_refused(env, cli, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["record", "--season", "2025", "--week", "10", "--live-db", str(env), "--history-db",
                  str(tmp_path / "x"), "--out", str(tmp_path / "c"), "--now", "2999-01-01T00:00:00+00:00"])


def test_pregame_roster_population_is_reported_separately_with_its_own_verdicts():
    pre = [cand(i, "RB", a, {"espn": e, "A1": b}, tm=1) for i, (a, e, b) in enumerate([(10, 1.0, 9.0), (4, 9.0, 1.0)], 1)]
    post = [dict(cand(10 + i, "RB", a, {"espn": e, "A1": b}, tm=2), roster_synced_at=SUN + timedelta(days=1))
            for i, (a, e, b) in enumerate([(10, 1.0, 9.0), (4, 9.0, 1.0)], 1)]
    res = sse.evaluate_sets(pre + post, {1: FULL}, {"s": ["espn", "A1"]}, CFG)["sets"]["s"]
    assert res["same_position"]["decisions"] == 2 and "pooled" in res["population"]
    only = res["pregame_roster_only"]
    assert only["same_position"]["decisions"] == 1 and "before its player's kickoff" in only["population"]
    assert only["exclusions"]["candidates"] == {"other_roster_basis": 2}


def test_ifp2_has_a_head_to_head_set_with_the_champion_on_one_population():
    assert set(sse.DEFAULT_MODEL_SETS["ifp_head_to_head"]) == {"espn", "A1", "ifp1_frozen", "ifp2_frozen"}
    roster = rb_roster((10, {"espn": 1.0, "A1": 2.0, "ifp1_frozen": 3.0, "ifp2_frozen": 9.0}),
                       (4, {"espn": 2.0, "A1": 1.0, "ifp1_frozen": 9.0, "ifp2_frozen": 1.0}))
    ev = sse.evaluate_sets(roster, {1: FULL}, config=CFG)["sets"]["ifp_head_to_head"]
    assert ev["evaluated"] and ev["same_position"]["stats"]["ifp2_frozen"]["accuracy"] == 1.0
    assert ev["same_position"]["stats"]["ifp1_frozen"]["accuracy"] == 0.0


def test_no_other_app_module_references_the_evaluation_module():
    app = Path(__file__).resolve().parents[1] / "app"
    me = Path(sse.__file__).resolve()
    offenders = [str(f.relative_to(app)) for f in app.rglob("*.py")
                 if f.resolve() != me and re.search(r"\bstart_sit_evaluation\b", f.read_text())]
    assert offenders == []


# ----------------------------------------------------------------------------- immutable production store (A)

from app.analytics.independent_projection_store import build_live, write_run  # noqa: E402
from test_independent_projections import SCORING, _artifact, live  # noqa: E402,F401  (live is a fixture)


def _run(con, src, created, scale):
    """One week-5 ifp-1.0.0 production run through the real writer; unconditional points = scale * conditional.
    It records the pinned ifp-1.0.0 artifact (production_pin), as a run of the real artifact file would."""
    from app.analytics.production_pin import APPROVED_ARTIFACT_SHA256

    meta, rows = build_live(con, src, _artifact(src, created_at="2026-09-01T00:00:00+00:00"), 2026, 5,
                            artifact_sha256=APPROVED_ARTIFACT_SHA256)
    meta["created_at"] = created
    for r in rows:
        if r["projected_points"] is not None:
            r["p_play"], r["expected_points"] = 0.9, round(r["projected_points"] * scale, 3)
    return write_run(con, meta, rows)


def test_production_reader_default_is_the_immutable_store_and_legacy_is_a_labeled_fallback(tmp_path):
    con = connect(tmp_path / "r.duckdb")
    init_db(con)
    reader, label = sse.forecast_reader_for(con)
    assert reader.__module__.endswith("production_forecasts") and label == sse.PRODUCTION_SOURCE
    con.execute("DROP TABLE independent_projection_rows")                   # a pre-v8 database
    reader, label = sse.forecast_reader_for(con)
    assert reader is sse.legacy_forecast_reader and label == sse.LEGACY_SOURCE
    con.execute("DROP TABLE independent_player_projections")
    assert sse.forecast_reader_for(con) == (None, "none (no production forecast tables)")
    con.close()


def test_runs_from_write_run_feed_decisions_and_a_post_kickoff_rerun_is_never_credited(live, cli):  # noqa: F811
    con, src, _ = live
    # week 5 kicks off 2026-09-05 17:00 UTC in the synthetic schedule
    early = _run(con, src, datetime(2026, 9, 4, 12), 1.0)
    latest_pre = _run(con, src, datetime(2026, 9, 5, 10), 2.0)
    rerun = _run(con, src, datetime(2026, 9, 5, 20), 50.0)                  # after the games started
    assert len({early["run_id"], latest_pre["run_id"], rerun["run_id"]}) == 3
    con.execute("UPDATE leagues SET roster_slots = ? WHERE league_id = 99",
                [json.dumps({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BE": 5})])
    con.execute("INSERT INTO lineup_snapshots SELECT league_id, season, 5, team_id, espn_player_id, lineup_slot_id, "
                "lineup_slot, is_starter, acquisition_type, acquisition_date, injury_status, points, projected_points, "
                "TIMESTAMP '2026-09-04 12:00:00' FROM lineup_snapshots WHERE scoring_period = 6")
    cands, settings, info = cli.build_report_candidates(con, None, src, SCORING, 2026, [5], None,
                                                        {2026: datetime(2027, 1, 1, tzinfo=UTC)})
    att = info["attached"]
    assert att["production_source"] == sse.PRODUCTION_SOURCE and att["ifp1_production"] > 0
    # each production version is read under its own model version, never pooled: no ifp-2.0.0 run is stored here,
    # so the production model gets nothing (it is not credited with the fallback's forecasts)
    assert att["ifp2_production"] == 0 and not any("ifp2_production" in c["preds"] for c in cands)
    # the post-kickoff rerun contributes nothing, and its exclusion reason is passed through unchanged
    assert [e["run_id"] for e in att["production_exclusions"]] == [rerun["run_id"]]
    assert att["production_exclusions"][0]["reason"].startswith("post-kickoff")
    # every player's value is the LATEST PRE-kickoff run's (scale 2.0), captured at that run's creation time
    checked = 0
    for c in cands:
        p = c["preds"].get("ifp1_production")
        if not p:
            continue
        stored = con.execute("SELECT expected_points FROM independent_projection_rows WHERE run_id = ? AND "
                             "espn_player_id = ?", [latest_pre["run_id"], c["espn_player_id"]]).fetchone()[0]
        assert p["value"] == stored and p["captured_at"] == datetime(2026, 9, 5, 10, tzinfo=UTC)
        if stored is None:                                                      # the run abstained for him
            assert sse.prediction(c, "ifp1_production")[1] == "no_prediction:ifp1_production"
            continue
        assert sse.prediction(c, "ifp1_production") == (stored, None)          # genuinely pregame
        later = con.execute("SELECT expected_points FROM independent_projection_rows WHERE run_id = ? AND "
                            "espn_player_id = ?", [rerun["run_id"], c["espn_player_id"]]).fetchone()[0]
        assert later is None or p["value"] != later
        checked += 1
    assert checked > 0
    d = sse.decisions(cands, ["A1", "ifp1_production"], settings, CFG)
    assert len(d["pairs"]) > 0 and all(r["roster_basis"] == "pregame_snapshot" for r in d["pairs"])
    res = sse.evaluate_sets(cands, settings, {"prod": ["A1", "ifp1_production"]}, CFG)["sets"]["prod"]
    assert res["evaluated"] and res["pregame_roster_only"]["same_position"]["decisions"] == res["same_position"]["decisions"]

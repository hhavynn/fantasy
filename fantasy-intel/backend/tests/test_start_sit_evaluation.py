"""Start/Sit decision evaluation on invented rosters and synthetic nflverse-shaped data (code paths and
leakage guards only; nothing here says anything about real forecasting accuracy)."""

from __future__ import annotations

import importlib.util
import json
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
    assert ev["pregame_roster_only"]["lineups"] == 0 and ev["lineups"]["by_roster_basis"] == {"post_hoc_snapshot": 1}


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
    assert refused == [{"espn_player_id": 1, "reason": "kicked_off"}, {"espn_player_id": 3, "reason": "unknown_kickoff"}]
    assert sse.refuse_post_kickoff([thu], THU)[1][0]["reason"] == "kicked_off"       # at kickoff is already too late


def test_decision_sets_are_write_once_content_addressed_and_idempotent(tmp_path):
    cands = [dict(cand(1, "RB", 0, {"espn": 5.0, "A1": 4.0})), dict(cand(2, "RB", 0, {"espn": 3.0, "A1": 6.0}))]
    for c in cands:
        c.pop("actual_points"), c.pop("outcome_status")
    first = _set(cands, SUN - timedelta(days=1))
    path, created = sse.write_decision_set(tmp_path, first)
    assert created and path.parent == tmp_path / "decision_sets" and first["sha"][:16] in path.name
    again = _set(cands, SUN - timedelta(hours=5))          # same content, recorded later
    assert again["sha"] == first["sha"] and again["recorded_at"] != first["recorded_at"]
    path2, created2 = sse.write_decision_set(tmp_path, again)
    assert path2 == path and created2 is False
    assert json.loads(path.read_text())["recorded_at"] == first["recorded_at"]        # never rewritten
    assert len(list((tmp_path / "decision_sets").glob("*.json"))) == 1
    assert "actual_points" not in json.dumps(first)                                     # no outcomes in a frozen set
    changed = _set(cands[:1], SUN - timedelta(hours=5))
    assert changed["sha"] != first["sha"] and sse.write_decision_set(tmp_path, changed)[1]
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
    assert sub["pregame_roster_only"]["lineups"] == 0


def test_record_cli_is_write_once_and_refuses_after_kickoff(env, cli, tmp_path, capsys):
    path = env
    out = tmp_path / "ss"
    before = ["--season", "2025", "--week", "10", "--live-db", str(path), "--history-db", str(tmp_path / "none.duckdb"),
              "--out", str(out), "--ledger-dir", str(tmp_path / "noledger")]
    now = "2025-09-09T12:00:00+00:00"                                   # day before week 10 kicks off
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

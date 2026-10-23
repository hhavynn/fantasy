"""Teammate response (issue #213 v1, research only): the scenario core on stored inputs (no-op exact, unknowns
refused), the neutral counterfactual, the observational split baseline (point in time) and the evaluation with its
pre-registered decision rule, on synthetic data."""
import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import production_forecasts as pf
from app.analytics import teammate_response as tr

from test_forecast_inputs import _build, _verified
from test_independent_projections import live  # noqa: F401  (fixture)
from app.analytics.independent_projection_store import write_run


# ------------------------------------------------------------------------------------------- scenario core

def _model(cols, means, stds, coef, intercept):
    return {"columns": cols, "means": means, "stds": stds, "coef": coef, "intercept": intercept}


WR = _model(["xfpl", "tm_tgt_vacated", "tm_tgt_gain_pg"], [10.0, 0.0, 0.0], [5.0, 0.1, 1.0], [2.5, 0.4, 0.3], 12.0)


def _inp(values, imputed=None):
    return {"input_values": values, "imputed": imputed or [False] * len(values), "mixture_column": None}


def test_an_unchanged_scenario_reproduces_every_stored_point_exactly(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    stored = write_run(con, meta, rows, inputs=captured)
    got, back = _verified(con, stored["run_id"])
    assert got
    for (_, pid), v in got.items():
        same = tr.scenario_point(v["model"], v["inputs"], {})
        assert same["available"] and same["delta"] == 0.0
        assert same["points"] == v["rebuilt"]["points"]
        assert abs(same["points"] - back[stored["run_id"]][pid]["projected_points"]) <= pf.REPRODUCE_TOLERANCE
        # a model without teammate inputs (the synthetic artifact): the neutral scenario changes nothing
        assert tr.teammate_neutral(v["model"], v["inputs"], "WR")["delta"] == 0.0


def test_a_teammate_change_moves_the_point_by_its_weight_and_nothing_else():
    inp = _inp([14.0, 0.2, 1.5])                         # a teammate with 20% of the targets is out
    base = pf.reproduce(WR, inp)["points"]
    s = tr.teammate_neutral(WR, inp, "WR")
    assert s["available"] and s["baseline"] == pytest.approx(base)
    assert s["delta"] == pytest.approx(-(0.4 / 0.1 * 0.2 + 0.3 / 1.0 * 1.5))
    assert pf.reproduce(WR, inp)["points"] == base       # the stored inputs are not mutated


def test_a_change_to_an_unknown_input_is_refused_never_filled_in():
    s = tr.scenario_point(WR, _inp([14.0, 0.0, 0.0], imputed=[False, True, False]), {"tm_tgt_vacated": 0.3})
    assert s == {"available": False, "reason": "input_unknown", "column": "tm_tgt_vacated"}
    s = tr.scenario_point(WR, _inp([14.0, 0.0, 0.0]), {"qb_changed": 1.0})
    assert s["reason"] == "not_a_model_input"
    assert tr.scenario_point(WR, _inp([14.0, 0.0]), {})["reason"] == "inputs_do_not_fit_the_model"


def test_a_floored_point_is_flagged():
    low = _model(["xfpl", "tm_tgt_vacated"], [10.0, 0.0], [5.0, 0.1], [2.5, -1.0], 1.0)
    s = tr.teammate_neutral(low, _inp([0.0, 0.3]), "WR")
    assert s["floored"] is True


# ------------------------------------------------------------------------------------------- feature rows

def _rows():
    return pl.DataFrame({"tm_tgt_gain_pg": [0.0, 1.2, None, 0.0], "tm_tgt_vacated": [0.0, 0.15, None, 0.0],
                         "tm_tgt_returning": [0.0, 0.0, None, 0.05], "xfpl": [9.0, 11.0, 8.0, 7.0]})


def test_neutral_sets_known_teammate_inputs_to_zero_and_keeps_unknowns_unknown():
    n = tr.neutral(_rows(), "WR")
    assert n["tm_tgt_vacated"].to_list() == [0.0, 0.0, None, 0.0]
    assert n["tm_tgt_returning"].to_list() == [0.0, 0.0, None, 0.0]
    assert n["xfpl"].to_list() == _rows()["xfpl"].to_list()


def test_an_event_is_any_known_non_zero_teammate_input():
    assert tr.event_mask(_rows(), "WR").tolist() == [False, True, False, True]
    assert tr.event_mask(_rows().drop("tm_tgt_gain_pg", "tm_tgt_vacated", "tm_tgt_returning"), "WR").tolist() == [False] * 4


def test_the_rb_and_wr_groups_are_the_shipped_ones():
    assert tr.TM_FEATURES == {"RB": ("tm_car_gain_pg", "tm_tgt_gain_pg", "tm_car_vacated", "tm_car_returning"),
                              "WR": ("tm_tgt_gain_pg", "tm_tgt_vacated", "tm_tgt_returning")}


# ------------------------------------------------------------------------------------------- the split baseline

def test_the_split_uses_only_earlier_games_of_the_season_and_needs_two_of_each():
    df = pl.DataFrame({"season": [2024] * 6, "gsis_id": ["A"] * 6, "week": [1, 2, 3, 4, 5, 6],
                       "label": [10.0, 12.0, 20.0, 18.0, None, 30.0],
                       "is_event": [False, False, True, True, True, True]})
    d = {w: v for w, v in tr.split_deltas(df).select("week", "split_delta").iter_rows()}
    assert d[1] is None and d[3] is None and d[4] is None     # fewer than 2 event games before
    assert d[5] == pytest.approx(19.0 - 11.0)                  # weeks 3-4 vs 1-2; week 5's own label is never used
    assert d[6] == pytest.approx(19.0 - 11.0)                  # week 5 has no label (did not play): not history


def test_the_split_never_crosses_seasons_or_players():
    df = pl.DataFrame({"season": [2023, 2023, 2023, 2023, 2024], "gsis_id": ["A"] * 4 + ["A"],
                       "week": [1, 2, 3, 4, 1], "label": [10.0, 10.0, 20.0, 20.0, 5.0],
                       "is_event": [False, False, True, True, True]})
    d = tr.split_deltas(df).filter(pl.col("season") == 2024)["split_delta"].to_list()
    assert d == [None]


# ------------------------------------------------------------------------------------------- evaluation

def _preds(n_weeks=30, effect=2.0, response=2.0, position="WR", seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(1, n_weeks + 1):
        for p in range(8):
            ev = (w + p) % 3 == 0
            base = 10.0 + p
            y = base + (effect if ev else 0.0) + rng.normal(0, 1.0)
            rows.append({"season": 2024, "week": w, "gsis_id": f"P{p}", "position": position, "label": y,
                         "is_event": ev, "pred_zero": base, "pred_model": base + (response if ev else 0.0),
                         "split_delta": None})
    return pl.DataFrame(rows)


def test_a_response_that_matches_the_effect_passes_both_baselines():
    out = tr.evaluate(_preds())["positions"]["WR"]
    assert out["decision"] == "pass"
    assert out["gaps"]["model_minus_zero"]["difference"] < 0
    assert out["response"]["calibration_slope"] == pytest.approx(1.0, abs=0.15)
    assert out["non_event_identical"] is True


def test_a_response_with_no_effect_behind_it_fails():
    out = tr.evaluate(_preds(effect=0.0))["positions"]["WR"]
    assert out["decision"] == "fail"
    assert out["gaps"]["model_minus_zero"]["difference"] > 0


def test_the_split_baseline_is_pred_zero_plus_the_split_and_beating_it_is_also_required():
    p = _preds().with_columns(pl.when(pl.col("is_event")).then(2.0).otherwise(None).alias("split_delta"))
    out = tr.evaluate(p)["positions"]["WR"]
    assert out["mae"]["split"] == pytest.approx(out["mae"]["model"])          # the split equals the response here
    assert out["decision"] == "fail"                                           # not better than the split
    assert out["split_available"] == out["event_rows"]


def test_rows_without_a_label_or_a_forecast_are_left_out_and_positions_without_rows_say_so():
    p = _preds().with_columns(pl.when(pl.col("week") == 1).then(None).otherwise(pl.col("label")).alias("label"))
    out = tr.evaluate(p)
    assert out["positions"]["WR"]["rows"] == p.filter(pl.col("label").is_not_null()).height
    assert out["positions"]["RB"] == {"event_rows": 0, "decision": "no_data"}


def test_the_decision_rule_is_reported_with_the_result():
    out = tr.evaluate(_preds())
    assert out["status"] == "research_only" and "BOTH clusterings" in out["decision_rule"]


def test_intervals_are_clustered_two_ways_and_seeded():
    g = tr.evaluate(_preds())["positions"]["WR"]["gaps"]["model_minus_zero"]
    assert g["by_week"]["clusters"] == 30 and g["by_player_season"]["clusters"] == 8
    assert tr.evaluate(_preds())["positions"]["WR"]["gaps"]["model_minus_zero"] == g


# ------------------------------------------------------------------------------------------- isolation

def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "teammate_response.py":
            continue
        assert "teammate_response" not in path.read_text(), path
    code = "import sys, app.main; assert 'app.analytics.teammate_response' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=root.parent, check=True)


def test_the_module_writes_nothing():
    text = pathlib.Path(tr.__file__).read_text()
    for word in ("INSERT", "UPDATE ", "write_parquet", "open(", "connect("):
        assert word not in text, word


def test_the_retrained_diagnostic_is_reported_but_never_changes_the_decision():
    p = _preds()
    with_diag = p.with_columns(pl.col("pred_model").alias("pred_retrained"))      # retrained == model: gap 0
    a, b = tr.evaluate(p)["positions"]["WR"], tr.evaluate(with_diag)["positions"]["WR"]
    assert a["diagnostics"] == {}
    assert b["diagnostics"]["model_minus_retrained"]["difference"] == 0
    assert (a["decision"], a["gaps"]) == (b["decision"], b["gaps"])

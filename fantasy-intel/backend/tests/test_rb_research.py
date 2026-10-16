"""RB research: segments, candidate predictors, point-in-time safety, identical test populations, seeded
bootstrap, decision rule, cache fingerprints and the preregistration binding. SYNTHETIC data only: nothing
here measures real forecasting accuracy."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import app.analytics.rb_research as rb
from app.analytics import forecast_backtest as fb
from app.analytics.forecast_features import PANEL_COLUMNS, INJURY_SCHEMA, Sources, _panel_schema, load_sources
from app.analytics.league_scoring import resolve_skill_scoring

from synthetic_forecast import SCORING_ITEMS, participation_database

SCORING = resolve_skill_scoring(SCORING_ITEMS)
APP = Path(rb.__file__).resolve().parents[1]
DOC = Path(__file__).resolve().parents[2] / "docs" / "research" / "rb_projection_diagnostics.md"


# ----------------------------------------------------------------------------- synthetic fixtures


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    con = participation_database(tmp_path_factory.mktemp("rb") / "s.duckdb", [2021, 2022, 2023])
    src = load_sources(con, SCORING)
    con.close()
    store = rb.build_research_store(src)
    return src, store


@pytest.fixture(scope="module")
def synth_refs(synth):
    _, store = synth
    weeks = [(2023, w) for w in range(10, 18)]
    return weeks, rb.reference_predictions(store, weeks)


def panel_row(season, week, gid, team="AAA", carries=10.0, targets=3.0, pos="RB"):
    return {"season": season, "week": week, "gsis_id": gid, "position": pos, "team": team,
            "opponent": "BBB", "has_stats": True, "has_snaps": True, "league_points": 8.0, "pass_attempts": 0.0,
            "carries": carries, "targets": targets, "receptions": 2.0, "air_yards": 10.0, "passing_yards": 0.0,
            "rushing_yards": 40.0, "receiving_yards": 20.0, "offense_pct": 0.6, "xfp": 9.0, "rz_targets": 0.0,
            "rz_carries": 1.0, "gl_carries": 0.0, "designed_rushes": carries, "team_targets": 30.0,
            "team_rush_attempts": 25.0, "team_rb_carries": 20.0, "td_points": 0.0, "xfpl": 8.0, "xfpl_td": 1.0,
            "team_plays": 62.0}


def injury_row(season, week, gid, status, team="AAA"):
    return {"season": season, "week": week, "gsis_id": gid, "team": team, "report_status": status,
            "practice_status": "Did Not Participate In Practice", "injury": "Knee", "not_injury": False}


def tiny_sources(panel_rows, injury_rows):
    panel = pl.DataFrame(panel_rows).cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)
    empty = pl.DataFrame()
    inj = pl.DataFrame(injury_rows, schema=INJURY_SCHEMA)
    return Sources(panel, empty, empty, None, inj, None)


def tm(src, season, week, gid="RB2", team="AAA"):
    t = pl.DataFrame({"gsis_id": [gid], "team": [team]})
    return rb.teammate_out_features(src, season, week, t).row(0, named=True)


# ----------------------------------------------------------------------------- teammate availability


def history(first, last, gid, team="AAA", season=2023, **kw):
    return [panel_row(season, w, gid, team, **kw) for w in range(first, last + 1)]


def test_teammate_out_sums_the_unavailable_backs_recent_usage():
    rows = history(1, 5, "RB1", carries=18.0, targets=4.0) + history(1, 5, "RB2", carries=6.0, targets=1.0)
    src = tiny_sources(rows, [injury_row(2023, 6, "RB1", "Out")])
    f = tm(src, 2023, 6)
    assert f["tm_out_carries_pg"] == pytest.approx(18.0) and f["tm_out_targets_pg"] == pytest.approx(4.0)
    assert f["tm_out_xfpl_pg"] == pytest.approx(8.0) and f["tm_out_n"] == 1.0
    assert tm(src, 2023, 6, gid="RB1")["tm_out_n"] == 0.0        # a back is never his own unavailable teammate


def test_teammate_out_counts_doubtful_not_questionable_and_only_this_weeks_report():
    rows = history(1, 5, "RB1", carries=18.0) + history(1, 5, "RB2", carries=6.0)
    for status, expected in (("Doubtful", 1.0), ("Questionable", 0.0)):
        src = tiny_sources(rows, [injury_row(2023, 6, "RB1", status)])
        assert tm(src, 2023, 6)["tm_out_n"] == expected
    later = tiny_sources(rows, [injury_row(2023, 7, "RB1", "Out")])
    assert tm(later, 2023, 6)["tm_out_n"] == 0.0                  # next week's report is not a week-6 fact


def test_teammate_out_ignores_stale_other_team_and_other_position():
    rows = (history(1, 2, "OLD", carries=20.0)                   # last game 4 weeks before week 6... still in window
            + history(1, 5, "RB2", carries=6.0) + history(1, 5, "OTHER", team="CCC", carries=20.0)
            + history(1, 5, "WR1", pos="WR", carries=0.0, targets=9.0))
    inj = [injury_row(2023, 6, "OTHER", "Out", team="CCC"), injury_row(2023, 6, "WR1", "Out")]
    src = tiny_sources(rows, inj + [injury_row(2023, 6, "OLD", "Out")])
    f = tm(src, 2023, 6)
    assert f["tm_out_carries_pg"] == pytest.approx(20.0)          # only OLD (same team, within 6 weeks)
    stale = tiny_sources(history(1, 2, "OLD", carries=20.0) + history(1, 5, "RB2", carries=6.0),
                         [injury_row(2023, 12, "OLD", "Out")])
    assert tm(stale, 2023, 12)["tm_out_n"] == 0.0                 # last game 10 weeks earlier


def test_teammate_out_is_point_in_time():
    base = history(1, 5, "RB1", carries=18.0) + history(1, 5, "RB2", carries=6.0)
    clean = tiny_sources(base, [injury_row(2023, 6, "RB1", "Out")])
    corrupted = tiny_sources(base + [panel_row(2023, w, "RB1", carries=99.0) for w in (6, 7, 8)]
                             + [panel_row(2023, w, "RB2", carries=77.0) for w in (6, 7)],
                             [injury_row(2023, 6, "RB1", "Out")])
    assert tm(clean, 2023, 6) == tm(corrupted, 2023, 6)
    # the previous season counts, the target season's later weeks do not
    cross = tiny_sources(history(14, 17, "RB1", season=2022, carries=12.0), [injury_row(2023, 1, "RB1", "Out")])
    assert tm(cross, 2023, 1)["tm_out_carries_pg"] == pytest.approx(12.0)


def test_teammate_features_zero_without_reports():
    src = tiny_sources(history(1, 5, "RB1"), [])
    assert all(v == 0.0 for k, v in tm(src, 2023, 6).items() if k != "gsis_id")


def test_attached_teammate_features_exist_for_rbs_only_and_have_no_nulls(synth):
    _, store = synth
    rbs = store.filter(pl.col("position") == "RB")
    assert rbs.select(pl.col(c).null_count() for c in rb.TEAMMATE_COLUMNS).row(0) == (0, 0, 0, 0)
    assert store.filter(pl.col("position") != "RB")["tm_out_n"].null_count() == store.filter(pl.col("position") != "RB").height
    assert (rbs["tm_out_n"] > 0).sum() > 0                        # the synthetic injuries do create absent teammates


# ----------------------------------------------------------------------------- segments


def seg_frame(**cols):
    base = {"carries_pg": 0.0, "targets_pg": 0.0, "rush_share": 0.2, "snap_pct": 0.5, "gl_carries_pg": 0.0,
            "rz_opps_pg": 0.0, "last_role_low": 0, "snap_trend": 0.0, "team_changed": 0, "prev_team_changed": 0,
            "last_injury_exit": 0, "inj_exits_recent": 0, "weeks_since_last": 1, "games_recent": 5, "week": 8,
            "td_over_exp": 0.0, "pred_A1": 1.0, "season": 2023, "position": "RB"}
    base.update(cols)
    return pl.DataFrame({k: [v] for k, v in base.items()})


def active(df, expr):
    return bool(df.select(expr.fill_null(False)).item())


def test_segment_boundaries():
    segs = rb.rb_segments()
    assert active(seg_frame(carries_pg=4.99), segs["rush_volume: carries/g < 5"])
    assert not active(seg_frame(carries_pg=5.0), segs["rush_volume: carries/g < 5"])
    assert active(seg_frame(carries_pg=5.0), segs["rush_volume: carries/g 5-10"])
    assert active(seg_frame(carries_pg=15.0), segs["rush_volume: carries/g >= 15"])
    assert active(seg_frame(targets_pg=3.5), segs["target_volume: targets/g >= 3.5"])
    assert active(seg_frame(rush_share=0.35), segs["team_rush_share >= 0.35"])
    assert active(seg_frame(snap_pct=0.30), segs["snap_share 0.30-0.60"])
    assert active(seg_frame(gl_carries_pg=0.2), segs["goal_line: inside-5 carries/g > 0"])
    assert active(seg_frame(carries_pg=2.0, targets_pg=3.0), segs["receiving_back (targets/g >= 3, carries/g < 10)"])
    assert active(seg_frame(carries_pg=12.0, targets_pg=1.0), segs["early_down_back (carries/g >= 10, targets/g < 3)"])
    assert active(seg_frame(week=4), segs["sample: early season (weeks 1-4)"])
    assert active(seg_frame(week=13), segs["sample: late season (weeks 13+)"])
    assert active(seg_frame(carries_pg=10.0, targets_pg=5.0), segs["high_volume_back (carries/g + targets/g >= 15)"])
    assert active(seg_frame(td_over_exp=0.5), segs["td_history: recent TDs above expectation (td_over_exp >= 0.5)"])
    assert active(seg_frame(weeks_since_last=3), segs["injury_return: >= 3 weeks since last game"])


def test_pregame_segments_never_read_the_target_game():
    for name, expr in rb.rb_segments().items():
        assert not any(c.startswith("o_") or c == "label" for c in expr.meta.root_names()), name
    for name, expr in rb.outcome_segments().items():
        assert all(c.startswith("o_") for c in expr.meta.root_names()), name


def test_outcome_segments_and_td_neutral_label():
    df = pl.DataFrame({"label": [14.0, 3.0], "o_td_points": [6.0, 0.0], "o_xfpl_td": [1.5, 0.5], "o_snap_pct": [0.2, 0.9],
                       "o_carries": [0.0, 4.0], "o_targets": [0.0, 1.0]})
    segs = rb.outcome_segments()
    assert df.select(segs["outcome: scored a touchdown"]).to_series().to_list() == [True, False]
    assert df.select(segs["outcome: zero opportunities (no carry or target)"]).to_series().to_list() == [True, False]
    assert df.select(rb.td_neutral_label()).to_series().to_list() == [9.5, 3.5]


def test_error_decomposition_arithmetic():
    df = pl.DataFrame({"label": [10.0, 2.0], "pred_C": [4.0, 2.0], "o_td_points": [6.0, 0.0], "o_xfpl_td": [2.0, 0.0]})
    out = rb.error_decomposition(df, ["C"])["C"]
    assert out["mae_realized"] == 3.0 and out["mae_td_neutral_label"] == 1.0
    assert out["td_share_of_mae"] == pytest.approx(2 / 3, abs=1e-3)
    assert out["mae_td_games"] == 6.0 and out["mae_no_td_games"] == 0.0


def test_volume_tier_table_reports_calibration_slope():
    rng = np.random.default_rng(3)
    opp = rng.uniform(0, 3, 60)
    df = pl.DataFrame({"carries_pg": opp, "targets_pg": np.zeros(60), "label": 2 * opp, "pred_C": opp, "pred_A2": opp * 0 + 1.0,
                       "o_carries": np.ones(60), "o_targets": np.zeros(60), "key": np.arange(60) % 6 + 202301})
    row = rb.volume_tier_table(df, ["C", "A2"])[0]
    assert row["opp_per_game"] == "0-2" and row["slope"]["C"] == pytest.approx(2.0, abs=1e-3)
    assert row["slope"]["A2"] is None and row["zero_opportunity_share"] == 0.0


# ----------------------------------------------------------------------------- candidate predictors


def test_shrink_toward_season_average():
    c, a2 = np.array([10.0, 10.0, 10.0, 10.0]), np.array([2.0, 2.0, 2.0, np.nan])
    opp = np.array([0.0, 6.0, 12.0, 1.0])
    out = rb.shrink_toward_season_average(c, a2, opp, tau=12.0)
    assert out[0] == pytest.approx(2.0)                           # no volume: the season average
    assert out[1] == pytest.approx(6.0)                           # half weight
    assert out[2] == pytest.approx(10.0)                          # full volume: C
    assert out[3] == 10.0                                         # A2 abstains: C
    big = rb.shrink_toward_season_average(c[:3], a2[:3], opp[:3], tau=1e9)
    assert np.allclose(big, 2.0, atol=1e-6)                       # huge tau is the season average
    assert np.isnan(rb.shrink_toward_season_average(np.array([np.nan]), np.array([2.0]), np.array([20.0]), 4.0)[0])


def candidates(synth, synth_refs, extra=None):
    _, store = synth
    weeks, ref = synth_refs
    st = rb.rb_store(store)
    out = {}
    for name, params in {"vol_shrink": {"tau": 8.0}, "tm_transfer": {"lam": 0.03}, "hurdle": {"logit_lam": 0.01},
                         **(extra or {})}.items():
        out[name] = rb.candidate_predictions(st, ref, weeks, name, params)
    return out


def test_candidates_abstain_exactly_where_c_abstains_and_are_finite(synth, synth_refs):
    weeks, ref = synth_refs
    for name, pred in candidates(synth, synth_refs).items():
        j = ref.select("key", "gsis_id", "pred_C").join(pred, on=["key", "gsis_id"], how="left")
        assert pred.height == ref.height
        assert j["pred_C"].is_null().sum() == j[f"pred_{name}"].is_null().sum(), name
        vals = j[f"pred_{name}"].drop_nulls().to_numpy()
        assert len(vals) > 50 and np.isfinite(vals).all() and (vals >= -1e-9).all()


def test_candidates_are_point_in_time(synth, synth_refs):
    """Corrupting the label and the outcome columns (o_carries, o_td_points) of the test week and later must
    not change a single forecast: training rows are strictly earlier. The teammate features are not
    corrupted here; their point-in-time behaviour is tested in test_teammate_out_is_point_in_time."""
    src, store = synth
    weeks, ref = synth_refs
    k = 202312
    base = candidates(synth, synth_refs, None)
    st = rb.rb_store(store)
    bad = st.with_columns([
        pl.when(pl.col("key") >= k).then(pl.col("label") + 123.0).otherwise(pl.col("label")).alias("label"),
        pl.when(pl.col("key") >= k).then(pl.col("o_carries") + 50.0).otherwise(pl.col("o_carries")).alias("o_carries"),
        pl.when(pl.col("key") >= k).then(pl.col("o_td_points") + 9.0).otherwise(pl.col("o_td_points")).alias("o_td_points")])
    for name, params in {"tm_transfer": {"lam": 0.03}, "hurdle": {"logit_lam": 0.01}, "vol_shrink": {"tau": 8.0}}.items():
        again = rb.candidate_predictions(bad, ref, [w for w in weeks if w == (2023, 12)], name, params)
        a = base[name].filter(pl.col("key") == k).sort("gsis_id")
        b = again.filter(pl.col("key") == k).sort("gsis_id")
        assert a["gsis_id"].to_list() == b["gsis_id"].to_list()
        assert np.allclose(a[f"pred_{name}"].fill_null(-1).to_numpy(), b[f"pred_{name}"].fill_null(-1).to_numpy())


def test_candidate_feature_sets_never_contain_target_game_columns():
    for name, feats in rb.CANDIDATE_FEATURES.items():
        assert not any(f.startswith("o_") or f == "label" for f in feats), name
    assert set(rb.TEAMMATE_COLUMNS[:3]) <= set(rb.CANDIDATE_FEATURES["tm_transfer"])
    assert set(rb.CANDIDATE_FEATURES["hurdle"]) == set(rb.CONTEXT[rb.POSITION])


def test_tm_transfer_matches_context_model_when_features_are_dropped(synth, synth_refs):
    """With the teammate features constant (zeros) the tm_transfer ridge is C's ridge: same features
    plus constant columns carry no signal after standardisation."""
    _, store = synth
    weeks, ref = synth_refs
    st = rb.rb_store(store).with_columns([pl.lit(0.0).alias(c) for c in rb.TEAMMATE_COLUMNS])
    pred = rb.candidate_predictions(st, ref, weeks[:2], "tm_transfer", {"lam": rb.C_LAMBDA_RB})
    j = ref.select("key", "gsis_id", "pred_C").join(pred, on=["key", "gsis_id"]).drop_nulls()
    assert j.height > 20
    assert np.allclose(j["pred_C"].to_numpy(), j["pred_tm_transfer"].to_numpy(), atol=1e-6)


def test_hurdle_is_a_probability_mixture_of_nonnegative_parts(synth, synth_refs):
    pred = candidates(synth, synth_refs)["hurdle"]
    v = pred["pred_hurdle"].drop_nulls().to_numpy()
    assert v.min() >= -1e-9 and v.max() < 80


def test_unknown_candidate_is_rejected(synth, synth_refs):
    _, store = synth
    weeks, ref = synth_refs
    with pytest.raises(ValueError):
        rb.candidate_predictions(rb.rb_store(store), ref, weeks[:1], "magic", {})


def test_grid_search_picks_lowest_mae_and_ties_go_to_the_smaller_value(monkeypatch):
    ref = pl.DataFrame({"key": [1, 1, 2, 2], "gsis_id": ["a", "b", "a", "b"], "label": [1.0, 2.0, 3.0, 4.0],
                        "pred_C": [0.0, 0.0, 0.0, 0.0]})
    table = {4.0: [1.0, 2.0, 3.0, 4.5], 8.0: [1.0, 2.0, 3.0, 4.5], 12.0: [2.0, 3.0, 4.0, 5.0]}

    def fake(store, ref_, weeks, name, params):
        return ref_.select("key", "gsis_id").with_columns(pl.Series(f"pred_{name}", table[params["tau"]]))

    monkeypatch.setattr(rb, "candidate_predictions", fake)
    out = rb.grid_search(None, ref, [], "vol_shrink")
    assert out["value"] == 4.0 and out["dev_mae_by_value"]["4"] == out["dev_mae_by_value"]["8"]
    assert out["dev_mae_by_value"]["12"] > out["dev_mae_by_value"]["4"] and out["dev_mae_C"] == 2.5


# ----------------------------------------------------------------------------- evaluation


def fake_preds(n_weeks=12, per_week=14, seed=5):
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(1, n_weeks + 1):
        for i in range(per_week):
            y = float(max(0.0, rng.normal(8, 5)))
            base = y + rng.normal(0, 3)
            rows.append({"season": 2023, "week": w, "key": 202300 + w, "gsis_id": f"p{i}", "position": "RB",
                         "label": y, "pred_A1": base + 0.5, "pred_A2": base + rng.normal(0, 1), "pred_C": base,
                         "pred_ifp2": base + rng.normal(0, .1), "carries_pg": 8.0, "targets_pg": 2.0,
                         "last_injury_exit": 0, "weeks_since_last": 1, "team_changed": 0, "prev_team_changed": 0,
                         "week_": w, "tm_out_n": 0.0, "pred_good": y * 0.5 + base * 0.5, "pred_same": base})
    return pl.DataFrame(rows)


def test_evaluate_window_uses_identical_rows_for_every_model():
    d = fake_preds()
    holes = d.with_columns(pl.when(pl.col("gsis_id") == "p3").then(None).otherwise(pl.col("pred_good")).alias("pred_good"))
    rep = rb.evaluate_window(holes, ["good", "same"], resamples=200)
    assert rep["n"] == d.height - holes.filter(pl.col("gsis_id") == "p3").height
    assert {v["n"] for v in rep["pooled"].values()} == {rep["n"]}
    assert all(c["n"] <= rep["n"] for c in rep["cohorts"].values())


def test_evaluate_window_reports_identical_models_as_zero_difference():
    rep = rb.evaluate_window(fake_preds(), ["same", "good"], resamples=200)
    same = rep["diffs"]["same-C"]
    assert same["diff"] == 0.0 and same["ci95"] == [0.0, 0.0] and same["ci_adj"] == [0.0, 0.0]
    assert rep["diffs"]["good-C"]["diff"] < 0
    assert set(rep["by_season"]) == {"2023"} and "pred_good" in rep["pairs"]["all"]


def test_bootstrap_is_seeded_and_matches_the_backtest_implementation():
    d = fake_preds().with_columns(pl.lit(None).cast(pl.Float64).alias("x"))
    a = rb.boot_diff(d, "pred_good", "pred_C", resamples=300, seed=11)
    b = rb.boot_diff(d, "pred_good", "pred_C", resamples=300, seed=11)
    c = rb.boot_diff(d, "pred_good", "pred_C", resamples=300, seed=12)
    assert a == b and a["ci95"] != c["ci95"]
    ref = fb.week_bootstrap_mae_diff(d, "pred_good", "pred_C", 300, 11)
    assert round(a["diff"], 3) == ref["diff"] and [round(x, 3) for x in a["ci95"]] == ref["ci95"]
    assert a["ci_adj"][0] <= a["ci95"][0] and a["ci_adj"][1] >= a["ci95"][1]
    assert rb.boot_diff(d, "pred_good", "x")["diff"] is None


def test_adjusted_alpha_is_bonferroni_across_three_candidates():
    assert rb.ADJUSTED_ALPHA == pytest.approx(0.05 / 3) and set(rb.CANDIDATE_GRIDS) == {"vol_shrink", "tm_transfer", "hurdle"}


def passing_report(name="cand"):
    ci = {"diff": -0.05, "ci95": [-0.08, -0.02], "ci_adj": [-0.09, -0.01], "n": 100}
    return {"pooled": {name: {"rmse": 5.9, "bias": 0.0}, "C": {"rmse": 6.0, "bias": 0.1}},
            "diffs": {f"{name}-C": dict(ci), f"{name}-A2": dict(ci)},
            "diffs_by_season": {s: {f"{name}-C": {"diff": -0.05}} for s in ("2023", "2024", "2025", "2026")},
            "cohorts": {"starters_pool (top-48 RB by A1)": {"n": 50, "mae": {name: 5.0, "C": 5.1}, "diffs": {}},
                        **{g: {"n": 50, "mae": {}, "diffs": {f"{name}-C": dict(ci)}} for g in rb.GUARD_COHORTS}},
            "pairs": {"all": {f"pred_{name}": {"accuracy": 0.67}, "pred_C": {"accuracy": 0.67}}}}


def test_decision_rule_passes_only_when_every_check_passes():
    assert rb.decide(passing_report(), "cand")["verdict"] == "supported_for_shadow_tracking"

    def fails(mutate, check):
        rep = passing_report()
        mutate(rep)
        out = rb.decide(rep, "cand")
        assert out["verdict"] == "not_supported" and not out["checks"][check]["pass"], check
        assert [k for k, v in out["checks"].items() if not v["pass"]] == [check]

    fails(lambda r: r["diffs"]["cand-C"].update(diff=-0.01), "c1_effect")                  # too small
    fails(lambda r: r["diffs"]["cand-C"].update(ci_adj=[-0.09, 0.01]), "c1_effect")        # CI includes 0
    fails(lambda r: r["diffs_by_season"].update({"2024": {"cand-C": {"diff": 0.01}}, "2025": {"cand-C": {"diff": 0.01}}}),
          "c2_consistency")
    fails(lambda r: r["pooled"]["cand"].update(rmse=6.1), "c3_rmse")
    fails(lambda r: r["pooled"]["cand"].update(bias=0.5), "c4_bias")
    fails(lambda r: r["cohorts"]["starters_pool (top-48 RB by A1)"]["mae"].update(cand=5.2), "c5_starters")
    fails(lambda r: r["pairs"]["all"]["pred_cand"].update(accuracy=0.66), "c6_pairs")
    fails(lambda r: r["cohorts"][rb.GUARD_COHORTS[0]]["diffs"]["cand-C"].update(ci_adj=[0.01, 0.05]), "c7_guard_cohorts")


def test_decision_rule_text_matches_the_preregistered_document():
    text = DOC.read_text()
    pre = text.split(rb.PREREG_START)[1].split(rb.PREREG_END)[0]
    for needle in ("-0.02 points", "Bonferroni", "3 of the 4 sub-windows", "0.5%", "0.15 points", "0.5 percentage points",
                   "tau in {4, 8, 12}", "{0.01, 0.03, 0.1}", "{0.001, 0.01, 0.1}"):
        assert needle in pre, needle
    assert rb.CANDIDATE_GRIDS == {"vol_shrink": {"tau": [4.0, 8.0, 12.0]}, "tm_transfer": {"lam": [0.01, 0.03, 0.1]},
                                  "hurdle": {"logit_lam": [0.001, 0.01, 0.1]}}
    assert rb.DEVELOPMENT == (2021, 2022) and rb.CONFIRMATION == (2023, 2024, 2025, 2026)


def test_placebo_shuffles_teammate_columns_within_each_week_only(synth):
    _, store = synth
    st = rb.rb_store(store)
    a, b = rb.placebo_teammate_store(st, seed=1), rb.placebo_teammate_store(st, seed=1)
    c = rb.placebo_teammate_store(st, seed=2)
    assert a.equals(b) and not a.equals(c)
    assert a.drop(rb.TEAMMATE_COLUMNS).equals(st.drop(rb.TEAMMATE_COLUMNS))          # nothing else moves
    for col in rb.TEAMMATE_COLUMNS:                                                   # each week keeps its multiset
        x = st.group_by(["season", "week"]).agg(pl.col(col).sort().alias("v")).sort("season", "week")
        y = a.group_by(["season", "week"]).agg(pl.col(col).sort().alias("v")).sort("season", "week")
        assert x["v"].to_list() == y["v"].to_list()
    assert not a["tm_out_carries_pg"].equals(st["tm_out_carries_pg"])


def test_posthoc_report_slices_by_teammate_status_and_timestamp_verification(synth, synth_refs):
    _, store = synth
    weeks, ref = synth_refs
    st = rb.rb_store(store)
    preds = ref
    for name, params in {"vol_shrink": {"tau": 8.0}, "tm_transfer": {"lam": 0.03}, "hurdle": {"logit_lam": 0.01}}.items():
        preds = preds.join(rb.candidate_predictions(st, ref, weeks, name, params), on=["key", "gsis_id"], how="left")
    placebo = rb.candidate_predictions(rb.placebo_teammate_store(st), ref, weeks, "tm_transfer", {"lam": 0.03}).rename(
        {"pred_tm_transfer": "pred_placebo"})
    rep = rb.posthoc_report(preds, placebo)
    tmr = rep["tm_transfer"]
    assert rep["n"] == tmr["all"]["n"] == tmr["teammate_unavailable"]["n"] + tmr["no_teammate_unavailable"]["n"]
    assert "placebo_vs_C" in tmr and set(rep["C_vs_A2_by_season"]) == {"2023"}
    assert rep["mean_outcome"] is not None and set(rep["mean_forecast"]) >= {"C", "vol_shrink", "tm_transfer", "hurdle"}


# ----------------------------------------------------------------------------- freezing and caching


def test_preregistration_hash_binds_only_the_marked_block():
    doc = f"intro\n{rb.PREREG_START}\nspec A\n{rb.PREREG_END}\nresults 1"
    h = rb.preregistration_hash(doc)
    assert h == rb.preregistration_hash(doc.replace("results 1", "results 2, a lot more"))
    assert h != rb.preregistration_hash(doc.replace("spec A", "spec B"))
    with pytest.raises(ValueError):
        rb.preregistration_hash("no markers")
    assert len(rb.preregistration_hash(DOC.read_text())) == 64


def test_check_frozen_refuses_changed_text_data_or_grid():
    selected = {n: {"param": next(iter(g)), "value": next(iter(g.values()))[0]} for n, g in rb.CANDIDATE_GRIDS.items()}
    cfg = rb.frozen_config(selected, "hash1", "fp1")
    rb.check_frozen(cfg, "hash1", "fp1")
    with pytest.raises(ValueError, match="preregistration"):
        rb.check_frozen(cfg, "hash2", "fp1")
    with pytest.raises(ValueError, match="input data"):
        rb.check_frozen(cfg, "hash1", "fp2")
    off = {**selected, "vol_shrink": {"param": "tau", "value": 5.0}}
    with pytest.raises(ValueError, match="grid"):
        rb.check_frozen(rb.frozen_config(off, "hash1", "fp1"), "hash1", "fp1")
    with pytest.raises(ValueError, match="exactly"):
        rb.check_frozen(rb.frozen_config({"vol_shrink": selected["vol_shrink"]}, "hash1", "fp1"), "hash1", "fp1")


def test_modified_candidate_code_changes_the_frozen_fingerprint_and_check_frozen_refuses(monkeypatch):
    selected = {n: {"param": next(iter(g)), "value": next(iter(g.values()))[0]} for n, g in rb.CANDIDATE_GRIDS.items()}
    cfg = rb.frozen_config(selected, "hash1", "fp1")
    assert cfg["candidate_code_sha256"] == rb.candidate_code_fingerprint() and len(cfg["candidate_code_sha256"]) == 64
    rb.check_frozen(cfg, "hash1", "fp1")

    def shrink_changed(c, a2, opp_pg, tau):                       # a "tweaked" vol_shrink
        return c
    monkeypatch.setattr(rb, "shrink_toward_season_average", shrink_changed)
    assert rb.candidate_code_fingerprint() != cfg["candidate_code_sha256"]
    assert rb.frozen_config(selected, "hash1", "fp1")["candidate_code_sha256"] != cfg["candidate_code_sha256"]
    with pytest.raises(ValueError, match="candidate code"):
        rb.check_frozen(cfg, "hash1", "fp1")
    monkeypatch.undo()
    rb.check_frozen(cfg, "hash1", "fp1")
    # a changed candidate constant is caught too, and an old configuration without the binding is refused
    monkeypatch.setattr(rb, "HURDLE_RIDGE_LAM", rb.HURDLE_RIDGE_LAM * 2)
    with pytest.raises(ValueError, match="candidate code"):
        rb.check_frozen(cfg, "hash1", "fp1")
    monkeypatch.undo()
    # the hurdle's logistic dependencies in participation.py are bound too
    from app.analytics import participation as part

    def sigmoid_changed(x):
        return x
    monkeypatch.setattr(part, "_sigmoid", sigmoid_changed)
    assert rb.candidate_code_fingerprint() != cfg["candidate_code_sha256"]
    monkeypatch.undo()
    assert rb.candidate_code_fingerprint() == cfg["candidate_code_sha256"]

    def predict_changed(self, df):
        return self.logits(df)
    monkeypatch.setattr(part.LogitModel, "predict", predict_changed)
    assert rb.candidate_code_fingerprint() != cfg["candidate_code_sha256"]
    with pytest.raises(ValueError, match="candidate code"):
        rb.check_frozen(cfg, "hash1", "fp1")
    monkeypatch.undo()
    legacy = {k: v for k, v in cfg.items() if k != "candidate_code_sha256"}
    with pytest.raises(ValueError, match="candidate code"):
        rb.check_frozen(legacy, "hash1", "fp1")


def test_frame_hash_and_fingerprint_cover_every_value(tmp_path):
    a = pl.DataFrame({"x": [1.0, 2.0], "y": ["a", "b"]})
    assert rb.frame_hash(a) == rb.frame_hash(a.reverse())                    # row order is irrelevant
    assert rb.frame_hash(a) != rb.frame_hash(a.with_columns(pl.when(pl.col("x") == 2.0).then(2.5).otherwise(pl.col("x")).alias("x")))
    assert rb.frame_hash(a) != rb.frame_hash(a.rename({"y": "z"})) and rb.frame_hash(None) == "none"
    f1 = rb.fingerprint({"panel": a}, {"k": 1})
    assert f1 == rb.fingerprint({"panel": a}, {"k": 1})
    assert f1 != rb.fingerprint({"panel": a}, {"k": 2}) and f1 != rb.fingerprint({"panel": a, "games": a}, {"k": 1})
    assert f1 != rb.fingerprint({"panel": a.head(1)}, {"k": 1})


def test_cache_is_valid_only_for_the_identical_fingerprint(tmp_path):
    a = pl.DataFrame({"season": [2023], "week": [1], "gsis_id": ["g"], "v": [1.0]})
    b = a.with_columns(pl.lit(2.0).alias("v"))                               # same player-week ids, different values
    fa, fb_ = rb.fingerprint({"panel": a}, {}), rb.fingerprint({"panel": b}, {})
    path = tmp_path / "store.parquet"
    assert rb.cache_load(path, fa) is None
    rb.cache_save(path, a, fa, {"note": 1})
    assert rb.cache_load(path, fa).equals(a)
    assert rb.cache_load(path, fb_) is None                                  # ids match, content does not
    (tmp_path / "store.parquet.meta.json").write_text("not json")
    assert rb.cache_load(path, fa) is None


def test_data_fingerprint_changes_with_any_source_frame(synth):
    src, _ = synth
    base = rb.data_fingerprint(src)
    assert base == rb.data_fingerprint(src)
    panel = src.panel.with_columns(pl.when(pl.col("week") == 3).then(pl.col("carries") + 1).otherwise(pl.col("carries")).alias("carries"))
    assert rb.data_fingerprint(Sources(panel, src.games, src.defense, src.rosters, src.injuries, src.exits)) != base
    inj = src.injuries.filter(pl.col("week") != 5)
    assert rb.data_fingerprint(Sources(src.panel, src.games, src.defense, src.rosters, inj, src.exits)) != base


def test_cached_store_rebuilds_when_inputs_change_and_hits_otherwise(synth, tmp_path, monkeypatch):
    src, _ = synth
    calls = []
    real = rb.build_research_store

    def counting(s):
        calls.append(1)
        return real(s)

    monkeypatch.setattr(rb, "build_research_store", counting)
    s1, fp1 = rb.cached_store(src, tmp_path)
    s2, fp2 = rb.cached_store(src, tmp_path)
    assert fp1 == fp2 and len(calls) == 1 and s1.shape == s2.shape
    changed = Sources(src.panel.with_columns(pl.when(pl.col("week") == 4).then(pl.col("league_points") + 1).otherwise(pl.col("league_points")).alias("league_points")),
                      src.games, src.defense, src.rosters, src.injuries, src.exits)
    _, fp3 = rb.cached_store(changed, tmp_path)
    assert fp3 != fp1 and len(calls) == 2


def test_verify_c_lambda_fails_loudly_on_drift(monkeypatch):
    fake = {"positions": {"RB": {"primary": "C", "lambda": {"B": 0.01, "C": rb.C_LAMBDA_RB}, "validation_mae": {"B": 4.1, "C": 4.0}}}}
    monkeypatch.setattr(fb, "select", lambda store, protocol: fake)
    assert rb.verify_c_lambda(pl.DataFrame())["primary"] == "C"
    fake["positions"]["RB"]["lambda"]["C"] = 0.1
    with pytest.raises(ValueError, match="expected C@"):
        rb.verify_c_lambda(pl.DataFrame())


def test_scoring_fingerprint_matches_production():
    assert rb.league_scoring_for_research().fingerprint == rb.PRODUCTION_SCORING_FINGERPRINT == SCORING.fingerprint


# ----------------------------------------------------------------------------- reference models and isolation


def test_reference_models_share_rows_and_have_expected_columns(synth, synth_refs):
    weeks, ref = synth_refs
    assert {"pred_A1", "pred_A2", "pred_C", "pred_ifp2", "label"} <= set(ref.columns)
    assert set(ref["position"].unique()) == {"RB"} and ref["key"].min() >= 202310
    d = rb.common_support(ref, ["A1", "A2", "C", "ifp2"])
    assert 0 < d.height <= ref.height
    assert d.select(pl.all_horizontal([pl.col(f"pred_{m}").is_not_null() for m in ("A1", "A2", "C", "ifp2")]).all()).item()


def test_segment_table_has_one_row_per_nonempty_segment_with_bootstrap(synth, synth_refs):
    _, ref = synth_refs
    d = rb.common_support(ref, ["A1", "A2", "C", "ifp2"])
    rows = rb.segment_table(d, ["A2", "C"], {"all": pl.lit(True), "none": pl.lit(False)}, min_n=1, vs=(("C", "A2"),))
    assert [r["segment"] for r in rows] == ["all"] and rows[0]["n"] == d.height
    assert rows[0]["C-A2"]["ci95"][0] <= rows[0]["C-A2"]["diff"] <= rows[0]["C-A2"]["ci95"][1]


def test_nothing_in_the_app_imports_the_rb_research_module():
    for f in APP.rglob("*.py"):
        if f.name == "rb_research.py":
            continue
        assert "rb_research" not in f.read_text(), f.name


def test_module_never_reads_espn_projections_or_consumer_code():
    tree = ast.parse(Path(rb.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if any(x in m for x in ("projection_source", "espn", "start_sit", "matchup", "promotion"))}
    text = Path(rb.__file__).read_text()
    assert "espn_projection" not in text and "player_id_map" not in text
    assert "MODEL_VERSION" in text and "MODEL_VERSION =" not in text      # compared against, never redefined

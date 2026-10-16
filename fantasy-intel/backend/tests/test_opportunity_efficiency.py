"""Research-only two-target (opportunity + efficiency) projections: targets, missing xFP, cutoffs,
abstention, mean semantics, linearity vs the direct ridge, artifact isolation."""

from __future__ import annotations

import pathlib
import random

import numpy as np
import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import opportunity_efficiency as oe
from app.analytics.forecast_features import EXPECTED_COMPONENTS
from app.analytics.independent_projections import ARTIFACT_SCHEMA, MIN_TRAIN_ROWS, Artifact

FEATS = {p: ["x1", "x2"] for p in ("QB", "RB", "WR", "TE")}
HL = {p: None for p in ("QB", "RB", "WR", "TE")}
LAMS = {p: (0.01,) for p in ("QB", "RB", "WR", "TE")}


def _store(weeks=6, players=60, seed=3, missing_rate=0.2) -> pl.DataFrame:
    rng = random.Random(seed)
    rows = []
    for w in range(1, weeks + 1):
        for i in range(players):
            x1, x2 = rng.uniform(0, 20), rng.uniform(-1, 1)
            opp = 0.8 * x1 + rng.gauss(0, 2)
            eff = 1.5 * x2 - 0.4 + rng.gauss(0, 3)
            t_opp = None if rng.random() < missing_rate else opp
            rows.append({"season": 2025, "week": w, "key": 202500 + w, "gsis_id": f"p{i}", "position": "RB",
                         "games_recent": 3, "x1": x1, "x2": x2, "label": opp + eff, "xfpl": t_opp})
    store = pl.DataFrame(rows)
    # every synthetic player had opportunities, so an absent xFP is unknown (never a measured zero)
    panel = store.select("season", "week", "gsis_id", "xfpl", pl.lit("AAA").alias("team"), pl.lit(5.0).alias("carries"),
                         pl.lit(2.0).alias("targets"), pl.lit(0.0).alias("pass_attempts"))
    return oe.attach_targets(store.drop("xfpl"), panel)


def _forecast(store, week=6, comp="opp", feats=FEATS):
    return oe.walk_forward(store, [(2025, week)], "M", feats, comp, LAMS, HL)


# ----------------------------------------------------------------------------- the eleven requirements


def test_target_week_xfp_is_never_a_forecast_input():
    cfg = ch.ChallengerConfig.load()
    for cand in oe.CANDIDATES:
        for pos in ("QB", "RB", "WR", "TE"):
            for comp, feats in oe.component_features(cand, pos, cfg).items():
                assert not {"t_opp", "t_eff", "label", "xfpl"} & set(feats), (cand.name, pos, comp)
    store = _store()
    a = _forecast(store)
    b = _forecast(store.with_columns(pl.when(pl.col("week") == 6).then(999.0).otherwise(pl.col("t_opp")).alias("t_opp")))
    assert a.equals(b)


def test_missing_historical_xfp_stays_missing():
    store = _store()
    miss = store.filter(pl.col("t_opp").is_null())
    assert miss.height > 0
    assert miss["t_eff"].null_count() == miss.height          # not "label - 0"
    tr = oe.component_train_rows(store, 202506, "RB", "opp")
    assert tr["t_opp"].null_count() == 0
    assert tr.height == store.filter((pl.col("key") < 202506) & pl.col("t_opp").is_not_null()).height
    assert oe.coverage(store)[0]["missing_rate"] == pytest.approx(miss.height / store.height, abs=1e-4)


def test_zero_opportunity_is_a_measured_zero_only_in_a_covered_team_week():
    store = pl.DataFrame({"season": [2025] * 4, "week": [1] * 4, "key": [202501] * 4,
                          "gsis_id": ["a", "b", "c", "d"], "position": ["RB"] * 4, "label": [12.0, 0.0, 0.5, 0.0]})
    panel = pl.DataFrame({"season": [2025] * 4, "week": [1] * 4, "gsis_id": ["a", "b", "c", "d"],
                          "team": ["AAA", "AAA", "AAA", "BBB"], "xfpl": [10.0, None, None, None],
                          "carries": [10.0, 0.0, 3.0, 0.0], "targets": [2.0, 0.0, 0.0, 0.0],
                          "pass_attempts": [0.0] * 4})
    t = oe.attach_targets(store, panel).sort("gsis_id")
    assert t["t_opp_source"].to_list() == ["nflverse", "zero_opportunity", None, None]
    assert t["t_opp"].to_list() == [10.0, 0.0, None, None]   # c: touches but no xFP; d: team not covered
    assert t["t_eff"].to_list() == [2.0, 0.0, None, None]


def test_points_and_xfp_use_the_same_league_scoring(tmp_path):
    from synthetic_forecast import add_league, database

    from app.analytics.forecast_features import load_panel
    from app.analytics.independent_projection_store import league_scoring

    con = database(tmp_path / "x.duckdb", [2025])
    add_league(con, 2025, 10)
    _, scoring = league_scoring(con, 2025)
    panel = load_panel(con, scoring.require_supported(), [2025])
    ff = pl.from_arrow(con.execute("SELECT * FROM nfl_ff_opportunity").arrow())
    comp = oe.scoring_compatibility(scoring.weights)
    expected = ff.select(pl.col("player_id").alias("gsis_id"), "season", "week",
                         pl.sum_horizontal([pl.col(c).fill_null(0) * scoring.weights[k]
                                            for k, c in comp["expected_columns"].items()]).alias("_x"))
    j = panel.join(expected.with_columns(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)),
                   on=["gsis_id", "season", "week"])
    assert j.height > 100
    assert np.allclose(j["xfpl"].to_numpy(), j["_x"].to_numpy())
    assert set(comp["covered"]) <= set(EXPECTED_COMPONENTS)
    assert "fumbles_lost_total" in comp["uncovered"] or any("fumble" in k for k in comp["uncovered"])
    con.close()


def test_efficiency_target_is_points_minus_xfp_and_may_be_negative():
    store = _store()
    have = store.filter(pl.col("t_opp").is_not_null())
    assert np.allclose(have["t_eff"].to_numpy(), (have["label"] - have["t_opp"]).to_numpy())
    assert (have["t_eff"] < 0).sum() > 0
    eff = _forecast(store, comp="eff")["pred_M_eff@0.01"].to_numpy()
    assert (eff < 0).any()                                     # negative efficiency forecasts are allowed


def test_no_target_week_leakage_in_either_component():
    store = _store()
    later = pl.col("week") >= 6
    poisoned = store.with_columns(pl.when(later).then(-50.0).otherwise(pl.col("label")).alias("label"),
                                  pl.when(later).then(80.0).otherwise(pl.col("t_opp")).alias("t_opp"),
                                  pl.when(later).then(-130.0).otherwise(pl.col("t_eff")).alias("t_eff"))
    for comp in ("opp", "eff"):
        assert _forecast(store, comp=comp).equals(_forecast(poisoned, comp=comp))


def test_both_components_train_strictly_before_the_week(monkeypatch):
    seen = []
    real = oe.fit_linear

    def spy(train, *a, **kw):
        seen.append((kw["label"], int(train["key"].max()), kw["target_key"],
                     int(train[kw["label"]].null_count())))
        return real(train, *a, **kw)

    monkeypatch.setattr(oe, "fit_linear", spy)
    store = _store()
    for comp in ("opp", "eff"):
        _forecast(store, comp=comp)
    assert {s[0] for s in seen} == {"t_opp", "t_eff"}
    assert all(mx < k and nulls == 0 for _, mx, k, nulls in seen)


def test_missing_component_is_an_explicit_abstention():
    store = _store(missing_rate=0.0)
    # efficiency targets missing for almost every training row -> too few rows -> abstain
    sparse = store.with_columns(pl.when(pl.col("gsis_id").is_in(["p0", "p1"])).then(pl.col("t_eff"))
                                .otherwise(None).alias("t_eff"))
    o, e = _forecast(sparse, comp="opp"), _forecast(sparse, comp="eff")
    assert o["pred_M_opp@0.01"].null_count() == 0
    assert e["pred_M_eff@0.01"].null_count() == e.height
    both = o.join(e, on=["key", "gsis_id"])
    summed = both.select(oe.combine(both, "M", 0.01, 0.01).alias("p"))["p"]
    assert summed.null_count() == summed.len()                 # no fallback to the opportunity part alone
    assert oe.component_train_rows(sparse, 202506, "RB", "eff").height < MIN_TRAIN_ROWS


def test_summed_forecast_keeps_mean_semantics_and_equals_direct_ridge_on_identical_rows():
    store = _store(missing_rate=0.0)
    k = 202506
    tr = oe.component_train_rows(store, k, "RB", "opp")
    mo = ch.fit_linear(tr, "RB", ["x1", "x2"], 0.01, label="t_opp")
    me = ch.fit_linear(tr, "RB", ["x1", "x2"], 0.01, label="t_eff")
    md = ch.fit_linear(tr, "RB", ["x1", "x2"], 0.01, label="label")
    raw = oe.raw_predict(mo, tr) + oe.raw_predict(me, tr)
    assert raw.mean() == pytest.approx(tr["label"].mean())        # a mean, not a median
    test = store.filter(pl.col("key") == k)
    summed = np.maximum(oe.raw_predict(mo, test) + oe.raw_predict(me, test), 0.0)
    assert np.allclose(summed, md.predict(test))                  # ridge is linear in the target
    assert (oe.raw_predict(me, test) < 0).any()                   # components are never floored
    assert all(c.name for c in oe.CANDIDATES)              # every candidate is squared-loss ridge (fit_linear default)


def test_identical_inputs_give_identical_forecasts():
    store = _store()
    assert _forecast(store).equals(_forecast(store))
    shuffled = store.sample(fraction=1.0, shuffle=True, seed=1)
    a = _forecast(store).sort("gsis_id")
    b = _forecast(shuffled).sort("gsis_id")
    assert np.allclose(a["pred_M_opp@0.01"].to_numpy(), b["pred_M_opp@0.01"].to_numpy())


def test_research_artifact_is_never_a_production_model(monkeypatch):
    monkeypatch.setattr(oe, "component_features", lambda cand, pos, cfg: {"opp": ["x1", "x2"], "eff": ["x1"]})
    monkeypatch.setattr(oe, "half_life", lambda pos, cfg: None)
    store = _store(missing_rate=0.0)
    art = oe.fit_artifact(store, 202507, oe.CANDIDATES[1], {p: {"opp": 0.01, "eff": 3.0} for p in ("QB", "RB", "WR", "TE")},
                          None, "2026-10-15T00:00:00+00:00", "fp")
    d = art.to_dict()
    assert d["status"] == "research_only" and d["schema"] != ARTIFACT_SCHEMA
    with pytest.raises(ValueError):
        Artifact.from_dict(d)                                  # the production loader refuses it
    with pytest.raises(ValueError):
        oe.ResearchArtifact.from_dict({**d, "status": "approved"})
    back = oe.ResearchArtifact.from_dict(d)
    assert back.hash() == art.hash()
    rows = store.filter(pl.col("key") == 202506)
    p = back.predict(rows)
    assert np.isfinite(p).all()
    qb = rows.with_columns(pl.lit("QB").alias("position"))
    assert np.isnan(back.predict(qb)).all()                    # no fitted QB components -> abstain
    no_history = rows.with_columns(pl.lit(0).alias("games_recent"))
    assert np.isnan(back.predict(no_history)).all()           # same abstention rule as the backtest


# ----------------------------------------------------------------------------- isolation


def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "opportunity_efficiency.py":
            continue
        assert "opportunity_efficiency" not in path.read_text(), path


def test_candidates_are_preregistered_and_distinct():
    names = [c.name for c in oe.CANDIDATES]
    assert names == ["OE_C", "OE_D", "OE_E"]
    assert oe.lambda_pairs(oe.CANDIDATES[0]) == [(lam, lam) for lam in oe.CANDIDATES[0].lam_grid_opp]
    assert all(le >= lo for lo, le in oe.lambda_pairs(oe.CANDIDATES[1]))
    cfg = ch.ChallengerConfig.load()
    e = oe.component_features(oe.CANDIDATES[2], "RB", cfg)
    assert not set(oe.PRODUCTION) & set(e["opp"]) and len(e["eff"]) < len(e["opp"])


def test_naive_opportunity_baselines_share_the_models_rows():
    d = pl.DataFrame({"t_opp": [10.0, 4.0, 6.0, 8.0], "opp": [9.0, 5.0, 6.0, 0.0],
                      "xfpl_eb": [10.0, None, 3.0, 8.0], "xfpl_recent": [12.0, 4.0, 6.0, None]})
    r = oe._naive_opportunity(d, "opp")
    # only rows 0 and 2 have both naive features: every MAE is measured on exactly those rows
    assert r["naive_n"] == 2
    assert r["model_mae_naive_rows"] == pytest.approx(0.5)
    assert r["naive_xfpl_eb_mae"] == pytest.approx(1.5)
    assert r["naive_xfpl_recent_mae"] == pytest.approx(1.0)


def test_exact_mode_skips_reporting_rounding_only():
    from app.analytics import forecast_backtest as fb
    a = np.array([1.0, 2.0, 3.0, 4.0])
    p = a + 0.0004                                  # a regression far below the 3-decimal rounding step
    assert fb.metrics(a, p)["mae"] == 0.0           # reported (rounded) value unchanged
    assert fb.metrics(a, p, exact=True)["mae"] == pytest.approx(0.0004)
    df = pl.DataFrame({"key": [1, 1, 2, 2], "label": a, "x": p, "y": a})
    assert fb.week_bootstrap_mae_diff(df, "x", "y", 50, 0)["diff"] == 0.0
    assert fb.week_bootstrap_mae_diff(df, "x", "y", 50, 0, exact=True)["diff"] == pytest.approx(0.0004)


def test_a_sub_rounding_pooled_regression_still_rejects():
    m = {"n": 100, "mae": 4.0, "rmse": 6.0, "bias": 0.0}
    rep = lambda diff: {"ALL": {"metrics": {"X": dict(m), "ifp2": dict(m)},   # noqa: E731
                                "minus_baseline_mae": {"X": {"diff": diff, "ci95": [-0.02, -0.001]}}},
                        "pairs": {"all": {"pred_X": {"accuracy": 0.65, "mean_regret": 2.0},
                                          "pred_ifp2": {"accuracy": 0.65, "mean_regret": 2.0}}},
                        "population": {"coverage": {"X": 1.0, "ifp2": 1.0}}}
    assert oe.decision(rep(-0.01), rep(-0.0001), "X")["outcome"] == "continue_research"
    worse = oe.decision(rep(-0.01), rep(0.0003), "X")      # rounds to 0.000 but is a regression
    assert worse["rejections"]["pooled_mae_worse"] and worse["outcome"] == "negative"

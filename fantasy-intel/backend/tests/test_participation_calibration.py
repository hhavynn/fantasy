"""Participation calibration research (app.analytics.participation_calibration): temporal honesty,
fallbacks, probability validity, determinism and production isolation. Synthetic data only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import participation as pm
from app.analytics import participation_calibration as pc
from app.analytics.participation import P_CLIP

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
APP = Path(__file__).resolve().parents[1] / "app"


def _oos(n_weeks=30, per_week=60, seed=3, a=0.6, b=1.8, positions=("QB", "RB")) -> pl.DataFrame:
    """Out-of-sample-like rows whose true appearance probability is a known miscalibration of p_model."""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n_weeks):
        season, week = 2021 + i // 18, i % 18 + 1
        for pos in positions:
            p = rng.uniform(0.02, 0.98, per_week)
            true = 1 / (1 + np.exp(-(a + b * np.log(p / (1 - p)))))
            y = (rng.random(per_week) < true).astype(int)
            for j in range(per_week):
                recs.append({"season": season, "week": week, "key": season * 100 + week, "gsis_id": f"{pos}{j}",
                             "position": pos, "team": "AAA", "p_model": float(p[j]), "played": int(y[j]),
                             "cond": 10.0, "label": 10.0 * y[j]})
    return pl.DataFrame(recs)


# ----------------------------------------------------------------------------- 1, 2: temporal honesty


def test_calibration_never_uses_future_outcomes():
    oos = _oos()
    keys = sorted(oos["key"].unique().to_list())
    k = keys[20]
    before = pc.walk_forward_calibration(oos, [k])
    # corrupt every outcome at or after week k: week k's calibrated probabilities must not move
    flipped = oos.with_columns(pl.when(pl.col("key") >= k).then(1 - pl.col("played")).otherwise(pl.col("played"))
                               .alias("played"))
    after = pc.walk_forward_calibration(flipped, [k])
    cols = [c for c in pc.candidate_columns()]
    assert before.select(cols).equals(after.select(cols))
    # and the history used for week k ends strictly before it
    for w in pc.WINDOWS:
        assert pc.calibration_history(oos, "QB", k, w)["key"].max() < k
    rolling = pc.calibration_history(oos, "QB", k, "rolling")["key"].n_unique()
    assert rolling == min(pc.ROLLING_WEEKS, keys.index(k))


@pytest.fixture(scope="module")
def synthetic_oos(tmp_path_factory):
    from app.analytics.forecast_backtest import Protocol
    from app.analytics.forecast_features import load_sources
    from app.analytics.league_scoring import resolve_skill_scoring
    from synthetic_forecast import SCORING_ITEMS, participation_database
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("participation_calibration_cli", SCRIPTS / "participation_calibration.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    con = participation_database(tmp_path_factory.mktemp("pcal") / "p.duckdb", [2024, 2025])
    src = load_sources(con, resolve_skill_scoring(SCORING_ITEMS))
    con.close()
    protocol = Protocol(first_feature_season=2023, train_start_season=2024, validation_seasons=(2024,),
                        heldout_seasons=(2025,))
    ifp_sel = {"positions": {p: {"primary": "C", "lambda": {"B": 0.01, "C": 0.01}} for p in pm.POSITIONS}}
    psel = {"positions": {p: {"lambda": 0.01} for p in pm.POSITIONS}}
    oos, _ = cli.oos_predictions(src, protocol, ifp_sel, psel, tmp_path_factory.mktemp("cache"))
    weeks = sorted({(int(s), int(w)) for s, w in oos.select("season", "week").unique().iter_rows()})
    store = pm.build_store(src, weeks)
    return cli, src, oos, store


def test_base_predictions_are_out_of_sample(synthetic_oos):
    """Every p_model equals a logistic model refit ONLY on rows of earlier weeks (never in-sample)."""
    _, _, oos, store = synthetic_oos
    k = sorted(oos.filter(pl.col("p_model").is_not_null())["key"].unique().to_list())[5]
    for pos in ("QB", "WR"):
        train = store.filter((pl.col("key") < k) & (pl.col("position") == pos))
        model = pm.fit_logit(train, pos, [0.01])[0.01]
        rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos)).sort("gsis_id")
        got = oos.filter((pl.col("key") == k) & (pl.col("position") == pos)).sort("gsis_id")
        assert got["gsis_id"].to_list() == rows["gsis_id"].to_list()
        assert np.allclose(got["p_model"].to_numpy(), model.predict(rows), atol=1e-12)
        insample = pm.fit_logit(store.filter((pl.col("key") <= k) & (pl.col("position") == pos)), pos, [0.01])[0.01]
        assert not np.allclose(got["p_model"].to_numpy(), insample.predict(rows), atol=1e-9)


# ----------------------------------------------------------------------------- 3, 4: fallbacks and sparsity


def test_position_without_history_keeps_the_base_probability():
    oos = _oos(positions=("QB",))
    new_pos = oos.filter(pl.col("key") == oos["key"].max()).with_columns(pl.lit("TE").alias("position"))
    preds = pc.walk_forward_calibration(pl.concat([oos, new_pos]), [int(oos["key"].max())])
    te = preds.filter(pl.col("position") == "TE")
    for c in pc.candidate_columns():
        assert np.allclose(te[c].to_numpy(), te["p_model"].to_numpy())
    cal = pc.fit_calibrator([], [], "isotonic")
    assert cal.fallback and np.allclose(cal.apply([0.3, 0.7]), [0.3, 0.7])
    assert pc.fit_calibrator([0.5] * 400, [1] * 400, "platt").fallback == "calibration rows have a single outcome class"


def test_sparse_regions_are_pooled_and_never_extrapolated():
    rng = np.random.default_rng(0)
    p = np.concatenate([rng.uniform(0.3, 0.7, 600), [0.99, 0.995]])          # almost nothing above 0.7
    y = (rng.random(len(p)) < p).astype(float)
    cal = pc.fit_calibrator(p, y, "isotonic")
    assert min(cal.params["block_n"]) >= pc.ISO_MIN_BLOCK
    assert all(np.diff(cal.params["y"]) >= 0) and all(np.diff(cal.params["x"]) > 0)
    top = cal.params["y"][-1]
    assert np.allclose(cal.apply([0.999, 0.9999]), [top, top])            # edge block's rate, no extrapolation
    assert np.allclose(cal.apply([0.0001]), cal.params["y"][0])
    assert pc.fit_calibrator(p[:pc.MIN_CAL_ROWS - 1], y[:pc.MIN_CAL_ROWS - 1], "isotonic").fallback


def test_platt_recovers_a_known_miscalibration():
    oos = _oos(n_weeks=40, per_week=200)
    qb = oos.filter(pl.col("position") == "QB")
    cal = pc.fit_calibrator(qb["p_model"].to_numpy(), qb["played"].to_numpy(), "platt")
    assert cal.params["a"] == pytest.approx(0.6, abs=0.1) and cal.params["b"] == pytest.approx(1.8, abs=0.15)
    shrunk = pc.fit_calibrator(qb["p_model"].to_numpy()[:400], qb["played"].to_numpy()[:400], "platt_shrunk")
    plain = pc.fit_calibrator(qb["p_model"].to_numpy()[:400], qb["played"].to_numpy()[:400], "platt")
    assert abs(shrunk.params["b"] - 1) < abs(plain.params["b"] - 1)        # pulled toward the identity map


# ----------------------------------------------------------------------------- 5, 6: validity and identities


@pytest.mark.parametrize("method", [m for m in pc.METHODS if m != "none"])
def test_probabilities_are_finite_and_within_bounds(method):
    oos = _oos()
    cal = pc.fit_calibrator(oos["p_model"].to_numpy(), oos["played"].to_numpy(), method)
    out = cal.apply([0.0, 1e-12, 0.5, 1.0, -0.5, 1.5, np.nan, np.inf])
    finite = out[:6]
    assert np.all(np.isfinite(finite)) and np.all(finite >= P_CLIP) and np.all(finite <= 1 - P_CLIP)
    assert np.isnan(out[6]) and np.isnan(out[7])                          # no base prediction -> no probability


def test_unconditional_points_use_the_same_conditional_estimate():
    oos = _oos()
    preds = pc.walk_forward_calibration(oos, sorted(oos["key"].unique().to_list())[25:])
    rep = pc.candidate_report(preds.filter(pl.col("position") == "QB"), "p_isotonic_expanding", seed=1, resamples=50)
    q = preds.filter(pl.col("position") == "QB")
    expected = pm.point_metrics(q["label"].to_numpy(), q["p_isotonic_expanding"].to_numpy() * q["cond"].to_numpy())
    assert rep["unconditional_points"] == expected
    base = pc.candidate_report(q, "p_none", seed=1, resamples=50)
    assert base["minus_base_log_loss"]["diff"] == 0 and np.allclose(q["p_none"].to_numpy(), q["p_model"].to_numpy())


# ----------------------------------------------------------------------------- 7, 8: serialization, determinism


def test_artifact_round_trip_preserves_predictions_and_stays_shadow(tmp_path):
    oos = _oos()
    sel = {"QB": {"selected": "p_isotonic_expanding"}, "RB": {"selected": "p_platt_rolling"}}
    art = pc.fit_artifact(oos, sel, created_at="2026-10-15T00:00:00+00:00", base_model={"lambda": {}},
                          fingerprints={"scoring": "x"}, validation={})
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(art.to_dict()))
    back = pc.CalibrationArtifact.from_dict(json.loads(path.read_text()))
    p = np.linspace(0.01, 0.99, 37)
    for pos in ("QB", "RB", "WR", "TE"):
        assert np.array_equal(art.apply(pos, p), back.apply(pos, p))
    assert back.calibrators["WR"].method == "none" and back.status == "shadow"
    assert back.training_cutoff_key == int(oos["key"].max())
    bad = {**art.to_dict(), "status": "production"}
    with pytest.raises(ValueError, match="shadow"):
        pc.CalibrationArtifact.from_dict(bad)
    with pytest.raises(ValueError, match="schema"):
        pc.CalibrationArtifact.from_dict({**art.to_dict(), "schema": "participation-artifact/v1"})


def test_repeated_fitting_is_deterministic():
    oos = _oos()
    keys = sorted(oos["key"].unique().to_list())[22:]
    a, b = pc.walk_forward_calibration(oos, keys), pc.walk_forward_calibration(oos, keys)
    assert a.equals(b)
    for m in pc.METHODS:
        c1 = pc.fit_calibrator(oos["p_model"].to_numpy(), oos["played"].to_numpy(), m)
        c2 = pc.fit_calibrator(oos["p_model"].to_numpy(), oos["played"].to_numpy(), m)
        assert c1.to_dict() == c2.to_dict()                                   # identical inputs: identical map
        c3 = pc.fit_calibrator(oos["p_model"].to_numpy()[::-1], oos["played"].to_numpy()[::-1], m)
        assert np.allclose(c1.apply([0.2, 0.8]), c3.apply([0.2, 0.8]), atol=1e-12)   # row order is irrelevant


# ----------------------------------------------------------------------------- 9: missing injury reports


def test_missing_injury_report_is_not_healthy():
    rows = pl.DataFrame({"season": [2025, 2025], "week": [3, 3], "team": ["AAA", "BBB"],
                         "inj_out": [0, 0], "inj_doubtful": [0, 0], "inj_questionable": [0, 0],
                         "reserve_since_last_game": [0, 0], "injury_exit_last": [0, 0], "games_recent": [5, 5]})
    reported = {(2025, 3, "AAA")}
    flagged = pc.with_report_flag(rows, reported)
    cohorts = pc.report_cohorts(reported)
    healthy = flagged.filter(cohorts["no_designation_report_published"])["team"].to_list()
    missing = flagged.filter(cohorts["no_team_injury_report"])["team"].to_list()
    assert healthy == ["AAA"] and missing == ["BBB"]


# ----------------------------------------------------------------------------- 10: production isolation


def test_production_participation_and_consumers_are_unchanged():
    assert pm.PARTICIPATION_VERSION == "ipm-1.0.0" and pm.PARTICIPATION_SCHEMA == "participation-artifact/v1"
    for path in APP.rglob("*.py"):
        if path.name == "participation_calibration.py":
            continue
        assert "participation_calibration" not in path.read_text(), path
    # the production artifact cannot be read as a calibration artifact (and vice versa)
    with pytest.raises(ValueError):
        pc.CalibrationArtifact.from_dict({"schema": pm.PARTICIPATION_SCHEMA, "status": "shadow"})

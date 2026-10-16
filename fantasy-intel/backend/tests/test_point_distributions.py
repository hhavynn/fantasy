"""Calibrated fantasy-point distributions (research only): mixture math, withholding, chronology,
frozen-artifact reproducibility and isolation from the app."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import point_distributions as pd_
from app.analytics.point_distributions import Mixture, QuantileFunction

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
UNIFORM_0_20 = QuantileFunction((0.0, 1.0), (0.0, 20.0))          # U(0, 20): positive outcomes only
UNIFORM_NEG = QuantileFunction((0.0, 1.0), (-4.0, 16.0))           # U(-4, 16): P(X < 0) = 0.2


# ----------------------------------------------------------------------------- quantile functions


def test_quantile_function_cdf_quantile_and_mean_are_exact_for_uniform():
    q = UNIFORM_0_20
    assert q.mean() == pytest.approx(10.0)
    assert q.quantile(0.1) == pytest.approx(2.0)
    assert q.cdf(5.0) == pytest.approx(0.25)
    assert q.prob_at_least(15.0) == pytest.approx(0.25)
    assert q.cdf(-1.0) == 0.0 and q.cdf(25.0) == 1.0


def test_flat_stretch_is_a_point_mass():
    q = QuantileFunction((0.0, 0.3, 0.6, 1.0), (0.0, 0.0, 6.0, 18.0))    # 30% exactly 0
    assert q.cdf(0.0) == pytest.approx(0.3) and q.cdf_left(0.0) == 0.0
    assert q.quantile(0.2) == 0.0 and q.prob_at_least(0.0) == 1.0


def test_negative_outcomes():
    q = UNIFORM_NEG
    assert q.cdf_left(0.0) == pytest.approx(0.2)
    assert q.quantile(0.1) == pytest.approx(-2.0)
    assert q.mean() == pytest.approx(6.0)


def test_crossing_quantiles_are_rejected():
    with pytest.raises(ValueError, match="crossing"):
        QuantileFunction((0.1, 0.5, 0.9), (5.0, 3.0, 9.0))
    with pytest.raises(ValueError):
        QuantileFunction((0.5, 0.1), (1.0, 2.0))


# ----------------------------------------------------------------------------- mixtures (Phase 4 exact cases)


def test_certain_appearance_equals_conditional():
    m = Mixture(1.0, UNIFORM_0_20)
    for u in (0.1, 0.5, 0.9):
        assert m.quantile(u) == pytest.approx(UNIFORM_0_20.quantile(u))
    assert m.mean() == pytest.approx(10.0)


def test_certain_nonappearance_is_zero():
    m = Mixture(0.0, UNIFORM_0_20)
    assert all(m.quantile(u) == 0.0 for u in (0.01, 0.1, 0.5, 0.9, 0.99))
    assert m.mean() == 0.0 and m.prob_at_least(10.0) == 0.0 and m.cdf(0.0) == 1.0


def test_half_appearance_quantiles_come_from_the_mixture_cdf():
    m = Mixture(0.5, UNIFORM_0_20)
    # F(0) = 0.5, then 0.5 + 0.5 * x / 20
    assert m.quantile(0.1) == 0.0 and m.quantile(0.5) == 0.0
    assert m.quantile(0.6) == pytest.approx(4.0)
    assert m.quantile(0.9) == pytest.approx(16.0)
    assert m.prob_at_least(10.0) == pytest.approx(0.25)
    # never p * conditional percentile
    assert m.quantile(0.9) != pytest.approx(0.5 * UNIFORM_0_20.quantile(0.9))


def test_mixture_with_negative_conditional_outcomes():
    m = Mixture(0.5, UNIFORM_NEG)
    # F(x<0) = 0.5 * (x + 4) / 20; F(0-) = 0.1; F(0) = 0.6
    assert m.quantile(0.05) == pytest.approx(-2.0)
    assert m.quantile(0.1) == pytest.approx(0.0, abs=1e-9)
    assert m.quantile(0.5) == 0.0
    assert m.quantile(0.8) == pytest.approx(8.0)                    # Q_c((0.8 - 0.5) / 0.5) = Q_c(0.6)
    assert m.cdf(-0.001) < 0.1 + 1e-9 and m.cdf(0.0) == pytest.approx(0.6)
    assert m.mean() == pytest.approx(3.0)


def test_large_point_mass_at_zero_combines_absence_and_zero_point_games():
    cond = QuantileFunction((0.0, 0.4, 1.0), (0.0, 0.0, 12.0))          # 40% of appearances score 0
    m = Mixture(0.6, cond)
    assert m.cdf(0.0) == pytest.approx(0.4 + 0.6 * 0.4)
    assert m.quantile(0.64) == 0.0 and m.quantile(0.65) > 0.0


@pytest.mark.parametrize("p", [0.0, 0.13, 0.5, 0.87, 1.0])
def test_mixture_mean_identity(p):
    cond = QuantileFunction(tuple(pd_.TAUS.tolist()), tuple(np.linspace(-3, 30, len(pd_.TAUS)).tolist()))
    assert Mixture(p, cond).mean() == pytest.approx(p * cond.mean())
    # the vectorized mixture quantile agrees with the scalar one
    v = np.asarray(cond.values)[None, :]
    for u in (0.1, 0.5, 0.9):
        assert pd_.mixture_quantile(v, pd_.TAUS, np.array([p]), u)[0] == pytest.approx(Mixture(p, cond).quantile(u))


def test_invalid_appearance_probability_is_rejected():
    with pytest.raises(ValueError):
        Mixture(1.2, UNIFORM_0_20)
    with pytest.raises(ValueError):
        Mixture(float("nan"), UNIFORM_0_20)


# ----------------------------------------------------------------------------- withholding semantics


def test_missing_appearance_probability_never_assumes_one():
    s = pd_.summarize(UNIFORM_0_20, 10.0, None, conditional_status="validated", unconditional_ok=True)
    assert s["conditional"]["p50"] == pytest.approx(10.0)
    assert s["unconditional"] is None and s["expected_points"] is None
    assert "never assumed" in s["unconditional_withheld_reason"]


def test_withheld_distribution_publishes_no_percentiles():
    s = pd_.summarize(UNIFORM_0_20, 10.0, 0.8, conditional_status="withheld", unconditional_ok=True,
                      withheld_reason="coverage80: observed 0.74")
    assert s["conditional"] is None and s["unconditional"] is None
    assert s["expected_points"] == pytest.approx(8.0)          # the mean identity still holds
    assert "0.74" in s["withheld_reason"]


def test_unapproved_participation_withholds_unconditional_only():
    s = pd_.summarize(UNIFORM_0_20, 10.0, 0.5, conditional_status="validated", unconditional_ok=False)
    assert s["conditional"] is not None and s["unconditional"] is None
    s = pd_.summarize(UNIFORM_0_20, 10.0, 0.5, conditional_status="validated", unconditional_ok=True)
    assert s["unconditional"]["p50"] == 0.0 and s["unconditional"]["prob_no_appearance"] == pytest.approx(0.5)
    assert s["unconditional"]["prob_at_least_10"] == pytest.approx(0.25)


def test_independent_pair_probability_is_labelled_and_coherent():
    a, b = UNIFORM_0_20, QuantileFunction((0.0, 1.0), (5.0, 25.0))
    pab, pba = pd_.prob_greater_independent(a, b), pd_.prob_greater_independent(b, a)
    assert pab + pba == pytest.approx(1.0, abs=1e-3)
    assert pab == pytest.approx(0.28125, abs=2e-3)             # exact for independent uniforms
    assert "INDEPENDENT" in pd_.prob_greater_independent.__doc__


# ----------------------------------------------------------------------------- methods & chronology


def _synthetic_oos(seed: int = 0, seasons=(2020, 2021, 2022)) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in seasons:
        for w in range(1, 18):
            for pos in pd_.POSITIONS:
                for i in range(40):
                    m = float(rng.gamma(2.0, 4.0))
                    sd = 2.0 + 0.5 * m
                    y = max(-2.0, m + sd * float(rng.standard_normal()))
                    rows.append({"key": s * 100 + w, "season": s, "week": w, "position": pos, "gsis_id": f"{pos}{i}",
                                 "team": f"T{i % 8}", "opponent": f"T{(i + 1) % 8}", "mean": m, "label": round(y, 1),
                                 "pts_std": abs(sd + float(rng.normal())), "carries_pg": float(rng.uniform(0, 15)),
                                 "targets_pg": float(rng.uniform(0, 9)), "adot": float(rng.uniform(2, 15)),
                                 "pred_A1": m + float(rng.normal()), "games_recent": 5, "weeks_since_last": 1,
                                 "team_changed": 0, "snap_trend": 0.0, "last_injury_exit": 0, "last_role_low": 0})
    return pl.DataFrame(rows)


def test_residual_pool_uses_only_earlier_weeks():
    oos = _synthetic_oos()
    k = 202105
    cols = [("B", "B", None), ("C@300", "C", 300), ("D", "D", None)]
    rows, mats = pd_.walk_forward_quantiles(oos, [k], cols)
    # corrupt every label at or after the forecast week: the forecast must not move
    bad = oos.with_columns(pl.when(pl.col("key") >= k).then(pl.lit(99.0)).otherwise(pl.col("label")).alias("label"))
    rows2, mats2 = pd_.walk_forward_quantiles(bad, [k], cols)
    for c, _, _ in cols:
        np.testing.assert_allclose(mats[c], mats2[c])
    assert rows["key"].unique().to_list() == [k]


def test_residual_pool_is_appearances_only():
    oos = _synthetic_oos().with_columns(pl.lit(1).alias("played"))
    k = 202105
    absent = oos.filter(pl.col("key") < k).head(500).with_columns(pl.lit(0).alias("played"), pl.lit(0.0).alias("label"),
                                                                  (pl.col("gsis_id") + "_x").alias("gsis_id"))
    a = pd_.walk_forward_quantiles(oos, [k], [("B", "B", None)])[1]["B"]
    b = pd_.walk_forward_quantiles(pl.concat([oos, absent]), [k], [("B", "B", None)])[1]["B"]
    np.testing.assert_allclose(a, b)


def test_every_full_method_keeps_the_published_mean_and_never_crosses():
    oos = _synthetic_oos()
    cols = [("N", "N", None), ("B", "B", None), ("C@300", "C", 300), ("F@300", "F", 300), ("D", "D", None),
            ("Bz", "Bz", None), ("Cz@300", "Cz", 300), ("Dz", "Dz", None), ("R@300", "R", 300), ("Dr", "Dr", None)]
    rows, mats = pd_.walk_forward_quantiles(oos, [202110], cols)
    for c, _, _ in cols:
        rq = mats[c]
        assert np.all(np.diff(rq, axis=1) >= -1e-9), c
        np.testing.assert_allclose(rq @ pd_.MEAN_WEIGHTS, 0.0, atol=1e-9)
        q = pd_.outcome_quantiles(rows, rq)
        np.testing.assert_allclose(q @ pd_.MEAN_WEIGHTS, rows["mean"].to_numpy(), atol=1e-9)


def test_ratio_method_keeps_zero_outcomes_exactly_zero_and_the_mean():
    oos = _synthetic_oos().with_columns(pl.when(pl.col("label") < 1).then(0.0).otherwise(pl.col("label")).alias("label"))
    rows, mats = pd_.walk_forward_quantiles(oos, [202110], [("R@300", "R", 300), ("L@300", "L", 300)])
    q = pd_.outcome_quantiles(rows, mats["R@300"])
    np.testing.assert_allclose(q @ pd_.MEAN_WEIGHTS, rows["mean"].to_numpy(), atol=1e-9)
    low = rows["mean"].to_numpy() < 3
    assert (q[low] == 0.0).mean() > 0.2 and q.min() >= 0.0               # a real point mass at exactly 0
    # the diagnostic L keeps its own mean (not mean-consistent, never eligible)
    assert "L" not in pd_.FULL_METHODS and "L" in pd_.DIAGNOSTIC_METHODS


def test_quantile_regression_rearrangement_prevents_crossing():
    rng = np.random.default_rng(3)
    x = np.column_stack([np.ones(400), rng.normal(size=400)])
    y = 3 * x[:, 1] + rng.normal(size=400)
    beta = pd_.fit_quantile_regression(x, y, pd_.QR_TAUS)
    xt = np.column_stack([np.ones(5), np.array([-40, -5, 0, 5, 40.0])])     # far extrapolation may cross
    q = np.sort(xt @ beta.T, axis=1)
    assert np.all(np.diff(q, axis=1) >= 0)
    # the fitted median slope is close to the truth
    assert beta[len(pd_.QR_TAUS) // 2, 1] == pytest.approx(3.0, abs=0.3)


def test_interval_coverage_measurement():
    rng = np.random.default_rng(1)
    y = rng.uniform(0, 20, 20000)
    q = np.tile(np.asarray(pd_.TAUS) * 20, (len(y), 1))                  # the true quantiles
    cal = pd_.calibration(y, q)
    assert cal["coverage80"] == pytest.approx(0.8, abs=0.01)
    assert cal["p10_below_rate"] == pytest.approx(0.1, abs=0.01)
    assert cal["p90_above_rate"] == pytest.approx(0.1, abs=0.01)
    assert cal["p50_mid_rate"] == pytest.approx(0.5, abs=0.01)
    assert cal["width80"] == pytest.approx(16.0)
    # outcomes equal to a quantile count as covered (inclusive interval, as interval_for evaluation)
    small = pd_.calibration(np.array([2.0, 18.0, 0.0, 20.0]), np.tile(np.asarray(pd_.TAUS) * 20, (4, 1)))
    assert small["inclusive_coverage80"] == 0.5


def test_tail_rates_are_exact_with_a_point_mass_at_zero():
    """40% of outcomes are exactly 0 and the distribution says so: P10 = P30 = 0. Plain counting
    calls every zero 'inside' the range; the randomized PIT credits a zero 1/4 below P10."""
    rng = np.random.default_rng(2)
    n = 40000
    y = np.where(rng.uniform(size=n) < 0.4, 0.0, rng.uniform(0, 12, n))
    taus = np.asarray(pd_.TAUS)
    knots = np.where(taus <= 0.4, 0.0, (taus - 0.4) / 0.6 * 12)
    cal = pd_.calibration(y, np.tile(knots, (n, 1)))
    assert cal["p10_below_rate"] == pytest.approx(0.1, abs=0.01)
    assert cal["coverage80"] == pytest.approx(0.8, abs=0.01)
    assert cal["inclusive_coverage80"] == pytest.approx(0.9, abs=0.01)


def test_support_floor_keeps_the_mean_and_creates_the_zero_mass():
    m = np.array([0.0, 1.0, 6.0, 20.0])
    rq = pd_.center(np.tile(np.linspace(-8, 14, len(pd_.TAUS)), (4, 1)))
    out = pd_.floor_preserving_mean(m, rq, 0.0)
    q = m[:, None] + out
    np.testing.assert_allclose(q @ pd_.MEAN_WEIGHTS, m, atol=1e-9)
    assert q.min() >= 0.0 and np.all(np.diff(q, axis=1) >= -1e-12)
    assert np.all(q[0] == 0.0)                                            # mean 0 at the floor: all mass at 0
    assert (q[1] == 0.0).mean() > 0.3                                     # low forecast: large mass at 0
    assert pd_.support_floor(np.array([0.0] * 99 + [-3.0])) == 0.0         # 1% negatives: floor at 0
    assert pd_.support_floor(np.array([-2.0] * 5 + [10.0] * 95)) == pytest.approx(-2.0)


def test_week_clustered_bootstrap_is_deterministic():
    keys = np.repeat(np.arange(10), 20)
    d = np.random.default_rng(0).normal(size=200)
    assert pd_.week_bootstrap(keys, d) == pd_.week_bootstrap(keys, d)
    assert pd_.week_bootstrap(keys, d)["weeks"] == 10


def test_selection_rule_prefers_simpler_methods_within_tolerance():
    def rep(crps, cov=0.8):
        return {"crps": crps, "coverage80": cov, "p10_below_rate": 0.1, "p90_above_rate": 0.1}
    dev = {"WR": {"methods": {"A": rep(3.0), "B": rep(3.010), "C@300": rep(3.000), "D": rep(2.99, cov=0.70)}}}
    pick = pd_.choose(dev)["WR"]
    assert pick["method"] == "B" and pick["lowest_crps"] == "C@300"       # D is ineligible (coverage), A not full
    dev["WR"]["methods"]["B"] = rep(3.2)
    assert pd_.choose(dev)["WR"]["method"] == "C@300"


# ----------------------------------------------------------------------------- data rules


def test_only_fully_final_and_synced_weeks_are_used():
    games = pl.DataFrame({"season": [2026] * 6, "week": [1, 1, 2, 2, 3, 3], "team": ["A", "B"] * 3,
                          "points_for": [20.0, 17.0, 10.0, None, 3.0, 9.0], "points_against": [17.0, 20.0, None, 10.0, 9.0, 3.0]})
    panel = pl.DataFrame({"season": [2026] * 5, "week": [1, 1, 2, 2, 3], "team": ["A", "B", "A", "B", "A"]})
    # week 2 has an unfinished game; week 3's team B has no synced box scores
    assert pd_.fully_final_weeks(games, panel, [(2026, 1), (2026, 2), (2026, 3)]) == [(2026, 1)]


def test_frozen_inputs_must_match_scoring_and_selection():
    sel = {"scoring_fingerprint": "e165b59686110c7d"}
    psel = {"ifp_selection_sha": "abc", "scoring_fingerprint": "e165b59686110c7d"}
    pd_.check_frozen_inputs(sel, psel, "e165b59686110c7d", "abc")
    with pytest.raises(ValueError, match="scoring"):
        pd_.check_frozen_inputs(sel, psel, "0000000000000000", "abc")
    with pytest.raises(ValueError, match="another selection"):
        pd_.check_frozen_inputs(sel, psel, "e165b59686110c7d", "xyz")


def test_nonappearance_rows_carry_confirmed_zero_labels_in_unconditional_scoring():
    df = pl.DataFrame({"key": [202301] * 4, "position": ["WR"] * 4, "mean": [10.0] * 4, "p": [0.5] * 4,
                       "played": [0, 0, 1, 1], "label": [0.0, 0.0, 5.0, 15.0]})
    q = np.tile(np.asarray(pd_.TAUS) * 20, (4, 1))                        # U(0, 20) given an appearance
    rep = pd_.unconditional_block(df, q)
    assert rep["absence"] == {"predicted": 0.5, "observed": 0.5}
    assert rep["at_or_below_zero"]["observed"] == 0.5
    assert rep["thresholds"][">=10"]["predicted"] == pytest.approx(0.25)


# ----------------------------------------------------------------------------- frozen artifact


def _artifact(oos):
    choice = {"QB": {"method": "B"}, "RB": {"method": "R@300"}, "WR": {"method": "Dr"}, "TE": {"method": None}}
    status = {"QB": {"status": "withheld", "reasons": ["coverage80: observed 0.74, required [0.75, 0.85]"]},
              "RB": {"status": "validated"}, "WR": {"status": "validated"}, "TE": {"status": "withheld"}}
    return pd_.fit_artifact(oos, choice, status, through_key=202217, fingerprint="f" * 16,
                            scoring_fingerprint="e165b59686110c7d", selection_sha="s" * 16,
                            participation_status={"QB": "not_approved", "RB": "not_approved", "WR": "validated", "TE": "validated"})


def test_frozen_artifact_is_reproducible_and_keeps_the_mean():
    oos = _synthetic_oos()
    a, b = _artifact(oos), _artifact(oos)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and a["content_hash"] == b["content_hash"]
    a2 = json.loads(json.dumps(a))                                         # survives a JSON round trip
    for pos, feats in (("RB", None), ("WR", {"pts_std": 6.0, "adot": 11.0})):
        qf = pd_.artifact_quantiles(a2, pos, 12.3, feats)
        assert qf.mean() == pytest.approx(12.3, abs=1e-6)
        assert qf.quantile(0.1) <= qf.quantile(0.5) <= qf.quantile(0.9)
    later = oos.with_columns(pl.when(pl.col("key") > 202217).then(pl.lit(50.0)).otherwise(pl.col("label")).alias("label"))
    assert _artifact(later)["content_hash"] == a["content_hash"]           # nothing after the cutoff is used


def test_withheld_qb_publishes_no_percentiles():
    art = json.loads(json.dumps(_artifact(_synthetic_oos())))
    s = pd_.artifact_summary(art, "QB", 18.0, 0.9)
    assert s["conditional"] is None and s["unconditional"] is None
    assert s["distribution_status"] == "withheld" and "0.74" in s["withheld_reason"]
    assert s["expected_points"] == pytest.approx(16.2)
    rb = pd_.artifact_summary(art, "RB", 12.0, 0.9)
    assert rb["conditional"] is not None and rb["unconditional"] is None   # participation not approved for RB
    wr = pd_.artifact_summary(art, "WR", 12.0, 0.9, {"pts_std": 6.0, "adot": 11.0})
    assert wr["unconditional"]["p10"] <= wr["unconditional"]["p50"] <= wr["unconditional"]["p90"]
    assert pd_.artifact_summary(art, "K", 8.0, 0.9)["conditional"] is None


# ----------------------------------------------------------------------------- isolation


def test_research_module_is_never_imported_by_the_app():
    for f in APP.rglob("*.py"):
        if f.name == "point_distributions.py":
            continue
        assert "point_distributions" not in f.read_text(), f
    code = "import sys, app.main; assert 'app.analytics.point_distributions' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=APP.parent)


def test_mean_projection_code_is_untouched_by_the_research_layer():
    """The distribution layer reads means; it never writes a model, a selection or an API field."""
    src = (APP / "analytics" / "point_distributions.py").read_text()
    for needle in ("write_parquet", "write_text", "INSERT", "UPDATE ", "selection.json\")", "MODEL_VERSION ="):
        assert needle not in src, needle

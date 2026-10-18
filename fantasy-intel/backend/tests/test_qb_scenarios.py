"""ifp-ctx-p2 (research only): start-probability model, appearance-conditional start weight,
two-QB scenario weighting, week-1 candidates, point-in-time safety and isolation. Hand-built
frames with invented values (the hypothetical CHI/GB world of test_roster_context): these tests
check behaviour, not forecasting accuracy."""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import context_challenger as cc
from app.analytics import roster_context as rc
from test_roster_context import CALEB, KEENUM, S, W, inj, world

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import qb_scenarios as qs  # noqa: E402

FEATS = {f: 0.0 for f in qs.START_FEATURES}


def cand(k, team, gid, started, appeared=0.0, **f):
    return {"season": k // 100, "week": k % 100, "key": k, "team": team, "gsis_id": gid, **FEATS, **f,
            "prof_epa": f.get("prof_epa", 0.0), "prof_cpoe": f.get("prof_cpoe", 0.0), "first_game": False,
            "p1_confidence": "supported", "started": started, "appeared": appeared}


def synthetic_candidates(n_weeks=60, hit=0.9, seed=7):
    """Two candidates per team-week; the previous starter starts with probability `hit`."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_weeks):
        k = 202001 + (i // 18) * 100 + i % 18
        for t in ("CHI", "GB", "DET", "MIN"):
            fav = rng.random() < hit
            rows += [cand(k, t, f"{t}-QB1", float(fav), appeared=1.0, prev_start=1.0, season_start_share=0.9),
                     cand(k, t, f"{t}-QB2", float(not fav), appeared=float(not fav or rng.random() < 0.2))]
    return pl.DataFrame(rows)


# ----------------------------------------------------------------------------- start model


def test_start_probabilities_sum_to_one_per_team_week_and_learn_the_evidence():
    c = synthetic_candidates()
    m = qs.fit_start_model(c)
    p = qs.predict_start(m, c)
    sums = p.group_by("key", "team").agg(pl.col("p_start").sum())["p_start"].to_numpy()
    assert np.allclose(sums, 1.0)
    fav = p.filter(pl.col("prev_start") == 1)["p_start"].mean()
    assert 0.8 < fav < 0.97          # close to the simulated 90%, shrunk only slightly by the ridge
    assert qs.fit_start_model(c)["beta"] == m["beta"]     # deterministic


def test_start_model_ignores_team_weeks_without_exactly_one_realized_starter():
    c = synthetic_candidates(n_weeks=10)
    unknown = c.with_columns(pl.lit(None, pl.Float64).alias("started"))
    assert qs.fit_start_model(unknown)["groups"] == 0
    both = c.with_columns(pl.lit(1.0).alias("started"))     # inconsistent label: two starters
    assert qs.fit_start_model(both)["groups"] == 0


def test_walk_forward_start_uses_only_earlier_team_weeks():
    c = synthetic_candidates()
    weeks = [(2022, 5)]
    a, _ = qs.walk_forward_start(c, weeks)
    # flipping the TARGET week's realized starter (a postgame fact) and anything later changes nothing
    flipped = c.with_columns(pl.when(pl.col("key") >= 202205).then(1.0 - pl.col("started"))
                             .otherwise(pl.col("started")).alias("started"))
    b, models = qs.walk_forward_start(flipped, weeks)
    assert a.sort("team", "gsis_id")["p_start"].to_list() == b.sort("team", "gsis_id")["p_start"].to_list()
    assert models[202205]["train_team_weeks"] == c.filter(pl.col("key") < 202205).select("key", "team").n_unique()


# ----------------------------------------------------------------------------- P(start | appears)


def test_pi_given_appearance_bounds_and_monotonicity():
    assert qs.pi_given_appearance(1.0, 0.1) == 1.0
    assert qs.pi_given_appearance(0.0, 0.1) == 0.0
    assert qs.pi_given_appearance(None, 0.1) is None
    xs = [qs.pi_given_appearance(p, 0.1) for p in np.linspace(0, 1, 11)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))
    # appearing is strong evidence of starting for a backup: p = 0.05 with a 10% relief rate -> pi ~ 0.34
    assert qs.pi_given_appearance(0.05, 0.1) == pytest.approx(0.05 / (0.05 + 0.95 * 0.1))
    assert qs.pi_given_appearance(0.05, 0.3) < qs.pi_given_appearance(0.05, 0.1)


def test_relief_rates_are_measured_on_non_starters_by_rank():
    c = synthetic_candidates()
    p = qs.predict_start(qs.fit_start_model(c), c)
    r = qs.relief_rates(p)
    non = p.filter(pl.col("started") == 0)
    # rank 2 = the less likely candidate; its relief rate is the simulated 20% (when he did not start)
    q2 = non.filter(pl.col("gsis_id").str.ends_with("QB2"))["appeared"].mean()
    assert r[2] == pytest.approx(q2)
    assert r[1] == pytest.approx(non.filter(pl.col("gsis_id").str.ends_with("QB1"))["appeared"].mean())


# ----------------------------------------------------------------------------- scenario weighting


class Stub:
    """Linear stand-in for a RidgeModel: 2 + 10 x s_start (QB) or 5 + 10 x qb_epa_delta - qb_changed;
    a null QB feature contributes 0 (the missing path). Floors at 0 like RidgeModel.predict."""

    def predict(self, df):
        if "s_start" in df.columns and df["s_start"].null_count() < df.height:
            v = 2 + 10 * df["s_start"].fill_null(0).to_numpy()
        else:
            v = 5 + 10 * df["qb_epa_delta"].fill_null(0).to_numpy() - df["qb_changed"].fill_null(0).to_numpy()
        return np.maximum(v, 0.0)


def test_qb_rows_mix_the_start_and_no_start_projections_with_pi():
    rows = pl.DataFrame({"pi_start": [1.0, 0.3, 0.0, None], "pi_hard": [1.0, 0.0, 0.0, None],
                         "s_start": [None] * 4}, schema={"pi_start": pl.Float64, "pi_hard": pl.Float64,
                                                         "s_start": pl.Float64})
    soft = qs.mix_predict(Stub(), rows, "QB")
    assert soft[:3].tolist() == pytest.approx([12.0, 0.3 * 12 + 0.7 * 2, 2.0])
    assert np.isnan(soft[3])        # no candidate evidence -> caller falls back to ifp-2
    assert qs.mix_predict(Stub(), rows, "QB", hard=True)[:3].tolist() == pytest.approx([12.0, 2.0, 2.0])


def test_receiver_rows_mix_the_two_most_likely_starters_with_renormalized_weights():
    rows = pl.DataFrame({
        "w1": [0.8, 1.0, None], "w2": [0.2, None, None],
        "s1_qb_epa_delta": [0.1, 0.0, None], "s1_qb_cpoe_delta": [0.0, 0.0, None], "s1_qb_changed": [0.0, 0.0, None],
        "s2_qb_epa_delta": [-0.3, None, None], "s2_qb_cpoe_delta": [0.0, None, None], "s2_qb_changed": [1.0, None, None],
        "qb_epa_delta": [9.0] * 3, "qb_cpoe_delta": [9.0] * 3, "qb_changed": [9.0] * 3}, schema_overrides={
        c: pl.Float64 for c in ("w1", "w2", "s1_qb_epa_delta", "s1_qb_cpoe_delta", "s1_qb_changed", "s2_qb_epa_delta",
                                "s2_qb_cpoe_delta", "s2_qb_changed")})
    p = qs.mix_predict(Stub(), rows, "WR")
    assert p[0] == pytest.approx(0.8 * 6.0 + 0.2 * (5 - 3 - 1))     # the pregame qb_* columns are never read
    assert p[1] == pytest.approx(5.0)                                 # one candidate: weight 1
    assert p[2] == pytest.approx(5.0)                                 # none: the model's missing path
    assert qs.mix_predict(Stub(), rows, "WR", hard=True)[0] == pytest.approx(6.0)


def test_scenario_columns_realized_for_training_and_probabilistic_for_forecasts():
    k = S * 100 + W
    store = pl.DataFrame({"key": [k] * 5, "gsis_id": [CALEB, KEENUM, "00-THIRD", "00-WR", "00-GBQB"],
                          "team": ["CHI", "CHI", "CHI", "CHI", "GB"], "position": ["QB", "QB", "QB", "WR", "QB"]})
    cands = pl.DataFrame([cand(k, "CHI", CALEB, 0.0, prof_epa=0.20, prof_cpoe=3.0),
                          cand(k, "CHI", KEENUM, 1.0, prof_epa=-0.10, prof_cpoe=-4.0),
                          cand(k, "CHI", "00-QB3", 0.0)]).with_columns(
        pl.Series("p_start", [0.7, 0.2, 0.1]), pl.Series("pi_start", [0.95, 0.6, 0.3]))
    teams = pl.DataFrame({"key": [k, k], "team": ["CHI", "GB"], "act_qb_id": [KEENUM, None],
                          "act_epa": [-0.10, None], "act_cpoe": [-4.0, None]})
    recent = pl.DataFrame({"key": [k] * 5, "gsis_id": [CALEB, KEENUM, "00-THIRD", "00-WR", "00-GBQB"],
                           "rec_primary": [CALEB] * 4 + [None], "rec_epa": [0.2] * 4 + [None],
                           "rec_cpoe": [3.0] * 4 + [None]})
    out = {r["gsis_id"]: r for r in qs.scenario_columns(store, cands, teams, recent).iter_rows(named=True)}
    # training: the realized starter (Keenum)
    assert out[KEENUM]["s_start"] == 1.0 and out[CALEB]["s_start"] == 0.0
    assert out["00-WR"]["a_qb_epa_delta"] == pytest.approx(-0.30) and out["00-WR"]["a_qb_changed"] == 1.0
    assert out["00-GBQB"]["s_start"] is None                  # realized starter unknown -> null, never 0
    # forecast: probabilities, never the realized starter
    assert out[CALEB]["pi_start"] == 0.95 and out[CALEB]["pi_hard"] == 1.0
    assert out["00-THIRD"]["pi_start"] == 0.0                 # not a candidate of a team-week that has candidates
    assert out["00-GBQB"]["pi_start"] is None                 # team-week without candidates
    wr = out["00-WR"]
    assert wr["w1"] + wr["w2"] == pytest.approx(1.0) and wr["w1"] == pytest.approx(0.7 / 0.9)
    assert wr["s1_qb"] == CALEB and wr["s1_qb_changed"] == 0.0 and wr["s1_qb_epa_delta"] == pytest.approx(0.0)
    assert wr["s2_qb"] == KEENUM and wr["s2_qb_changed"] == 1.0 and wr["s2_qb_epa_delta"] == pytest.approx(-0.30)


# ----------------------------------------------------------------------------- candidates


def _prev_season(ctx, starter=CALEB, team="CHI"):
    rows = pl.DataFrame({"season": [S - 1] * 17, "week": list(range(1, 18)), "team": [team] * 17,
                         "qb_id": [starter] * 17}, schema=rc.STARTER_SCHEMA)
    return rc.ContextSources(pl.concat([rows, ctx.starters]), ctx.qb_stats)


def test_week_one_candidates_come_from_the_week_one_roster_with_last_season_evidence():
    src, ctx = world(roster_status={(KEENUM, 1): "INA"})
    c = qs.start_candidates(src, _prev_season(ctx), S, 1, ["CHI"])
    got = {r["gsis_id"]: r for r in c.iter_rows(named=True)}
    assert set(got) == {CALEB, KEENUM}                         # the old p1 path had no candidate at all
    assert all(r["first_game"] for r in got.values())
    assert got[CALEB]["prev_season_start_share"] == 1.0 and got[CALEB]["first_game_x_prev_share"] == 1.0
    assert got[KEENUM]["prev_season_start_share"] == 0.0
    assert got[KEENUM]["unavailable"] == 0.0                   # INA (a game-day decision) is not used
    assert got[CALEB]["started"] == 1.0                        # label only (week-1 starter of the world)


def test_week_one_excludes_departed_and_practice_squad_qbs():
    src, ctx = world(roster_team={(KEENUM, 1): "GB"})
    assert set(qs.start_candidates(src, _prev_season(ctx), S, 1, ["CHI"])["gsis_id"]) == {CALEB}
    src, ctx = world(roster_status={(KEENUM, 1): "DEV"})
    assert set(qs.start_candidates(src, _prev_season(ctx), S, 1, ["CHI"])["gsis_id"]) == {CALEB}


def test_report_status_and_p1_evidence_enter_as_features():
    src, ctx = world(injuries=[inj(CALEB, "Out")])
    c = {r["gsis_id"]: r for r in qs.start_candidates(src, ctx, S, W, ["CHI"]).iter_rows(named=True)}
    assert c[CALEB]["unavailable"] == 1.0 and c[CALEB]["prev_start"] == 1.0
    assert c[KEENUM]["p1_expected"] == 1.0                    # p1 picks the backup when the starter is out
    assert c[CALEB]["season_start_share"] == 1.0


def test_week_w_and_later_facts_change_only_the_label_columns():
    src, ctx = world()
    base = qs.start_candidates(src, ctx, S, W, ["CHI"]).sort("gsis_id")
    st = ctx.starters.filter(~((pl.col("season") == S) & (pl.col("week") >= W)))
    later = pl.DataFrame({"season": [S, S], "week": [W, W + 1], "team": ["CHI", "CHI"], "qb_id": [KEENUM, KEENUM]},
                         schema=rc.STARTER_SCHEMA)
    ctx2 = rc.ContextSources(pl.concat([st, later]), ctx.qb_stats)
    moved = qs.start_candidates(src, ctx2, S, W, ["CHI"]).sort("gsis_id")
    feats = [*qs.START_FEATURES, "prof_epa", "prof_cpoe"]
    assert base.select(feats).equals(moved.select(feats))
    assert moved.filter(pl.col("gsis_id") == KEENUM)["started"].item() == 1.0


# ----------------------------------------------------------------------------- isolation


def test_p1_is_untouched_and_the_app_never_imports_p2():
    assert cc.PROSPECTIVE_VERSION == "ifp-ctx-p1"
    assert cc.PROSPECTIVE_SPEC == {"QB": "A", "RB": "C", "WR": "C", "TE": "ifp2"}
    assert cc.GROUPS["qb"]["QB"] == ["qb_is_expected", "qb_self_epa"]     # p2 adds s_start in its own group
    assert qs.P2_VERSION != cc.PROSPECTIVE_VERSION
    assert not set(qs.P2_CANDIDATES) & set(cc.CANDIDATES)
    backend = pathlib.Path(__file__).resolve().parents[1]
    for path in (backend / "app").rglob("*.py"):
        assert "qb_scenario" not in path.read_text(), path


def test_evaluate_refuses_a_frozen_selection_made_from_other_inputs_or_code(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import qb_scenario_research as qsr
    from app.analytics import challenger as ch

    prov = {"fingerprint": "store-a", "scoring_fingerprint": "sc", "p2_build_fingerprint": "build-a"}
    monkeypatch.setattr(qsr, "build", lambda args: (None, None, prov))
    args = SimpleNamespace(ifp_dir=tmp_path)
    frozen = {"per_position": {"QB": "ifp2"}, "config_hash": "x", "frozen_at": "t",
              **qsr._binding(ch.ChallengerConfig.load(), prov)}
    assert set(qsr.FROZEN_BINDING) == {"ifp2_config_hash", "scoring_fingerprint", "store_fingerprint",
                                       "p2_build_fingerprint", "evaluator_hash"}
    for key, value in (("store_fingerprint", "store-b"), ("p2_build_fingerprint", "build-b"),
                       ("evaluator_hash", "old-code")):
        qsr._frozen_path(args).write_text(json.dumps({**frozen, key: value}))
        with pytest.raises(SystemExit, match=key):
            qsr.cmd_evaluate(args)

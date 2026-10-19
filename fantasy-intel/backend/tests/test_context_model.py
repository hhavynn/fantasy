"""The production model ifp-2.2.0 (app.analytics.context_model): ifp-2.1.0 plus the research-shadow components,
versioning and the fallback, the training artifact, the live run (QB scenario mixture, the frozen QB P(plays) map),
the FTN exclusion and isolation from the hosted app. Synthetic data only (the world of test_ros_projections)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import adaptive_workload as aw
from app.analytics import challenger as ch
from app.analytics import context_model as cm
from app.analytics import injury_model as im
from app.analytics import production_pin
from app.analytics import projection_source as ps
from app.analytics import weekly_learning as wl
from app.analytics.independent_projection_store import build_live, register_approved_artifact, write_run
from app.analytics.independent_projections import RidgeModel

from test_ros_projections import SCORING, W, build_world, by_id, participation_artifact

CREATED = "2026-08-01T00:00:00+00:00"
CFG = ch.ChallengerConfig.load()
QB_MAP = {"x": [0.0001, 0.5, 0.9999], "y": [0.05, 0.4, 0.97]}


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """One synthetic world, its context inputs and its ifp-2.2.0 week-6 artifact (shared: training takes seconds)."""
    con, src = build_world(tmp_path_factory.mktemp("cm") / "live.duckdb")
    inputs = cm.load_inputs([con], None, 2026)
    art = wl.train_context(src, 2026, W, cfg=CFG, scoring_desc=SCORING.describe(), created_at=CREATED, inputs=inputs,
                           participation_map={**QB_MAP, "sha256": "x"}, participation_note="ok")
    yield con, src, inputs, art
    con.close()


# ----------------------------------------------------------------------------- versions and governance


def test_versions_are_wired_consistently():
    assert ps.MODEL_VERSION == cm.MODEL_VERSION == "ifp-2.2.0"
    assert ps.FALLBACK_VERSION == cm.BASE_VERSION == im.MODEL_VERSION == "ifp-2.1.0"
    assert ps.RETIRED_WEEKLY_VERSION == ch.CHALLENGER_VERSION == "ifp-2.0.0"
    assert ps.CONSUMER_VERSIONS == (ps.MODEL_VERSION, ps.FALLBACK_VERSION)
    assert production_pin.REGISTERED_MODEL_VERSIONS == (*ps.CONSUMER_VERSIONS, ps.RETIRED_WEEKLY_VERSION)
    assert production_pin.REGISTERED_MODEL_VERSION == ps.MODEL_VERSION
    assert production_pin.PINNED_MODEL_VERSION == ps.RETIRED_VERSION == "ifp-1.0.0"


def test_consumer_text_is_plain_and_confident():
    for v in ps.CONSUMER_VERSIONS:
        note = ps.GOVERNANCE[v]["status_note"]
        assert len(note) < 120 and "not shown to be better" not in note and "shadow" not in note.lower()
    for text in (ps.NOTE, ps.GOVERNANCE[ps.MODEL_VERSION]["status_note"]):
        assert "research" not in text.lower() and "override" not in text.lower() and "FTN" not in text
    g = ps.governance(ps.MODEL_VERSION)
    assert g["production_version"] == "ifp-2.2.0" and g["fallback_version"] == "ifp-2.1.0" and not g["is_fallback"]
    assert g["production_approval"] == "owner_override" and g["start_sit_ready"] is False


# ----------------------------------------------------------------------------- definition


def test_every_position_extends_ifp_2_1_with_its_shipped_components():
    for pos in ps.POSITIONS:
        base = im.features(CFG, pos)
        full = cm.features(CFG, pos)
        comps = cm.SHIPPED[pos]
        if cm.DECLINE_ROLE in comps:
            assert "xfpl_eb" not in full and cm.ROLE_FEATURE in full
            assert [cm.ROLE_FEATURE if f == "xfpl_eb" else f for f in base] == full[:len(base)]
        else:
            assert full[:len(base)] == base
        assert ("s_start" in full) == (pos == "QB" and cm.QB_SCENARIOS in comps)
        assert (cm.DEPTH_FEATURE in full) == (cm.DEPTH_TIER in comps)
        assert set(cm.SHIPPED[pos]) <= set(cm.CANDIDATE[pos])
        assert set(cm.GUARD_DROPPED[pos]) == set(cm.CANDIDATE[pos]) - set(cm.SHIPPED[pos])
    assert cm.features(CFG, "RB", ()) == im.features(CFG, "RB")


def test_overlap_qb_uses_the_scenario_mixture_not_p1s_committed_starter():
    """ifp-ctx-p1 and ifp-ctx-p2q both change QB; QB takes p2q (its pre-registered successor). p1's QB-row features
    (qb_is_expected) are never a QB input, its receiver groups are."""
    assert cm.COMPONENTS[cm.QB_SCENARIOS]["positions"] == ("QB",)
    assert "QB" not in cm.COMPONENTS[cm.QB_CONTEXT]["positions"] and "QB" not in cm.COMPONENTS[cm.TEAMMATES]["positions"]
    assert "qb_is_expected" not in cm.features(CFG, "QB", cm.CANDIDATE["QB"])
    assert cm.COMPONENTS[cm.DECLINE_ROLE]["positions"] == tuple(aw.AWD_POSITIONS)


def test_ftn_first_reads_are_never_an_input():
    assert "ifp-fr-p1" in cm.EXCLUDED and "ifp-fr-p1" not in {c["shadow"] for c in cm.COMPONENTS.values()}
    for pos in ps.POSITIONS:
        feats = cm.features(CFG, pos, cm.CANDIDATE[pos])
        assert not any(f.startswith("fr_") or f.startswith("ftn") for f in feats)


def test_qb_scenario_code_is_the_shadows_frozen_file():
    qs = cm._qs()
    assert qs.QB2_GROUP["QB"] == cm.QB2_GROUP
    assert cm.config(CFG)["qb_scenario_code_sha256"] == hashlib.sha256(
        (cm.REPO_SCRIPTS / "qb_scenarios.py").read_bytes()).hexdigest()


def test_mixture_is_pi_weighted_and_falls_back_without_candidates():
    m = RidgeModel("QB", "ifp22", ["s_start"], ["s_start"], [0.5], [1.0], [4.0], 10.0, 0.01, 500)
    fb = RidgeModel("QB", "ifp22", ["s_start"], ["s_start"], [0.5], [1.0], [0.0], 7.0, 0.01, 500)
    rows = pl.DataFrame({"pi_start": [1.0, 0.0, 0.25, None], "s_start": [None] * 4},
                        schema={"pi_start": pl.Float64, "s_start": pl.Float64})
    got = cm.predict_points({"QB": m}, {"QB": fb}, rows, "QB", (cm.QB_SCENARIOS,))
    # f(start) = 10 + 4 * (1 - 0.5) = 12, f(no start) = 10 + 4 * (0 - 0.5) = 8
    assert got.tolist() == pytest.approx([12.0, 8.0, 0.25 * 12 + 0.75 * 8, 7.0])


# ----------------------------------------------------------------------------- training artifact


def test_training_artifact_records_its_components_and_start_model(trained):
    _, src, _, art = trained
    assert art.version == cm.MODEL_VERSION and cm.is_context_artifact(art) and im.is_injury_artifact(art)
    for pos, m in art.models.items():
        assert m.model == cm.MODEL_NAME and m.features == cm.features(CFG, pos)
    c = art.config[cm.CONFIG_KEY]
    assert c["base"] == "ifp-2.1.0" and c["components"] == {p: list(cm.SHIPPED[p]) for p in ps.POSITIONS}
    assert c["excluded"] == cm.EXCLUDED and c["start_model"]["version"] == "qbstart-0.1.0"
    assert c["participation_map"]["sha256"] == "x" and c["depth_note"] == "no depth snapshot directory"
    if "QB" in art.models and cm.uses_scenarios("QB"):
        assert set(c["fallback_models"]) == {"QB"} and "s_start" not in c["fallback_models"]["QB"]["features"]
    prov = art.config["provenance"]
    assert prov["role"] == "production" and prov["data_cutoff_key"] < 2026 * 100 + W and prov["inputs_hash"]
    assert art.config["config_hash"] == cm.config_hash(CFG)
    store = wl.training_store(src, 2026, W)
    assert prov["rows"] == {p: ch.train_rows(store, 2026 * 100 + W, p).height for p in art.models}


def test_training_inputs_are_reproducible(trained):
    _, src, inputs, _ = trained
    store = wl.training_store(src, 2026, W)
    a, b = cm.with_training_inputs(src, store, inputs), cm.with_training_inputs(src, store, inputs)
    assert a.height == store.height and cm.inputs_hash(a) == cm.inputs_hash(b)
    assert {"s_start", cm.ROLE_FEATURE, cm.DEPTH_FEATURE, "qb_epa_delta", "tm_tgt_vacated"} <= set(a.columns)
    assert a.filter(pl.col("position") != "QB")["s_start"].null_count() == a.filter(pl.col("position") != "QB").height


def test_artifact_saves_and_registers_under_its_own_version(trained, tmp_path):
    con, _, _, art = trained
    aid, path, created = wl.save_artifact(art, tmp_path)
    assert created and path.parent.name == cm.MODEL_VERSION
    sha, new = wl.register_saved_artifact(con, art, path, tmp_path, "learning_cycle")
    assert new and con.execute("SELECT model_version FROM approved_artifacts WHERE artifact_sha256 = ?",
                               [sha]).fetchone()[0] == cm.MODEL_VERSION
    loaded, aid2, _ = wl.latest_artifact(tmp_path, cm.MODEL_VERSION, 2026, W, art.scoring_fingerprint)
    assert aid2 == aid and cm.is_context_artifact(loaded)
    con.execute("DELETE FROM approved_artifacts")


# ----------------------------------------------------------------------------- live runs


def test_a_context_artifact_needs_its_inputs(trained):
    con, src, _, art = trained
    with pytest.raises(ValueError, match="context_inputs"):
        build_live(con, src, art, 2026, W, participation=participation_artifact(), artifact_sha256="a" * 64)


def test_horizon_zero_run_reads_every_component_and_calibrates_qbs(trained, monkeypatch):
    con, src, inputs, art = trained
    seen = {}
    real = cm.augment

    def spy(*a, **kw):
        out = real(*a, **kw)
        seen["cols"] = set(out.columns)
        return out

    monkeypatch.setattr(cm, "augment", spy)
    part = participation_artifact()
    meta, rows = build_live(con, src, art, 2026, W, participation=part, artifact_sha256="a" * 64,
                            context_inputs=inputs)
    assert {"pi_start", "s_start", cm.ROLE_FEATURE, cm.DEPTH_FEATURE, "qb_epa_delta"} <= seen["cols"]
    assert set(im.HANDOFF_FEATURES) <= seen["cols"]                       # ifp-2.1.0's handoff is still read
    assert meta["model_version"] == cm.MODEL_VERSION
    projected = [r for r in rows if r["coverage_status"] == "projected"]
    assert projected and all(r["model"] == "ifp22_context_ridge" for r in projected)
    for r in projected:
        q = r["participation"] or {}
        if r["position"] == "QB" and cm.QB_PARTICIPATION in cm.SHIPPED["QB"]:
            assert q["calibration"] == cm.CAL_VERSION and r["p_play"] == round(cm.calibrate(QB_MAP, q["uncalibrated_p_play"]), 4)
            assert r["expected_points"] == round(r["p_play"] * r["projected_points"], 2)
        else:
            assert "calibration" not in q


def test_rest_of_season_rows_keep_the_participation_models_p_play(trained):
    con, src, inputs, art = trained
    _, rows = build_live(con, src, art, 2026, 7, participation=participation_artifact(), artifact_sha256="a" * 64,
                         projected_from_week=W, context_inputs=inputs)
    assert all("calibration" not in (r["participation"] or {}) for r in rows)
    assert any(r["coverage_status"] == "projected" for r in rows)


def test_calibration_never_touches_return_rate_rows():
    art = type("A", (), {"config": {cm.CONFIG_KEY: {"participation_map": QB_MAP, "components": {"QB": [cm.QB_PARTICIPATION]}}}})()
    rows = [{"position": "QB", "coverage_status": "projected", "p_play": 0.5, "projected_points": 20.0,
             "participation": {"p_play": 0.5, "model_version": im.RETURN_RATE_MODEL}},
            {"position": "QB", "coverage_status": "projected", "p_play": 0.5, "projected_points": 20.0,
             "participation": {"p_play": 0.5, "model_version": "ipm-1.0.0"}},
            {"position": "RB", "coverage_status": "projected", "p_play": 0.5, "projected_points": 20.0,
             "participation": {"p_play": 0.5, "model_version": "ipm-1.0.0"}}]
    assert cm.calibrate_rows(art, rows, 0) == 1
    assert rows[0]["p_play"] == 0.5 and rows[2]["p_play"] == 0.5 and rows[1]["p_play"] == 0.4
    assert rows[1]["expected_points"] == 8.0 and rows[1]["participation"]["uncalibrated_p_play"] == 0.5
    assert cm.calibrate_rows(art, rows, 1) == 0


def test_qb_points_are_the_scenario_mixture(trained):
    con, src, inputs, art = trained
    if "QB" not in art.models or not cm.uses_scenarios("QB"):
        pytest.skip("QB scenarios not shipped")
    from app.analytics.forecast_features import features_for_week
    from app.analytics.participation import live_population

    pop = live_population(src, 2026, W)
    feats = features_for_week(src, 2026, W, pop)
    feats = cm.augment(art, src, inputs, 2026, W, W, feats, as_of=datetime.now(timezone.utc))
    preds = {p["gsis_id"]: p for p in cm.predict_frame(art, feats)}
    qb = feats.filter((pl.col("position") == "QB") & pl.col("pi_start").is_not_null() & (pl.col("games_recent") > 0))
    for r in qb.iter_rows(named=True):
        row = qb.filter(pl.col("gsis_id") == r["gsis_id"])
        want = float(cm.mix_points(art.models["QB"], row)[0])
        assert preds[r["gsis_id"]]["model_points"] == pytest.approx(round(want, 2))


def test_earlier_model_artifacts_never_read_the_new_components(trained):
    con, src, _, art = trained
    base = replace(art, version=ps.FALLBACK_VERSION,
                   config={k: v for k, v in art.config.items() if k != cm.CONFIG_KEY}, models={})
    assert not cm.is_context_artifact(base) and im.is_injury_artifact(base)
    _, rows = build_live(con, src, base, 2026, W, participation=participation_artifact(), artifact_sha256="a" * 64)
    assert all(x["coverage_status"] != "projected" for x in rows)       # no inputs needed, nothing crashed


def test_switch_serves_ifp_2_2_and_falls_back_to_ifp_2_1(trained):
    con, src, inputs, art = trained
    part = participation_artifact()
    for v, sha in ((ps.FALLBACK_VERSION, "e"), (ps.MODEL_VERSION, "f")):
        register_approved_artifact(con, model_version=v, artifact_sha256=sha * 64, target_season=2026, target_week=W,
                                   training_cutoff_key=202605, source="learning_cycle")
    fallback = replace(art, version=ps.FALLBACK_VERSION, config={k: v for k, v in art.config.items() if k != cm.CONFIG_KEY})
    meta0, rows0 = build_live(con, src, fallback, 2026, W, participation=part, artifact_sha256="e" * 64)
    write_run(con, meta0, rows0)
    w = ps.weekly(con, 2026, W, "independent")
    assert w["run"]["model_version"] == ps.FALLBACK_VERSION and w["governance"]["is_fallback"]
    meta1, rows1 = build_live(con, src, art, 2026, W, participation=part, artifact_sha256="f" * 64,
                              context_inputs=inputs)
    write_run(con, meta1, rows1)
    w = ps.weekly(con, 2026, W, "independent")
    assert w["run"]["model_version"] == ps.MODEL_VERSION and not w["governance"]["is_fallback"]
    assert w["model_status"] == {p: ps.OVERRIDE_STATUS for p in ps.POSITIONS}
    for t in ("independent_projection_rows", "independent_projection_runs", "approved_artifacts"):
        con.execute(f"DELETE FROM {t}")


# ----------------------------------------------------------------------------- the frozen QB P(plays) map


def _cal_artifact(method="isotonic"):
    return {"schema": "participation-calibration/v1", "version": cm.CAL_VERSION, "created_at": CREATED,
            "base_model": {"participation_version": "ipm-1.0.0"}, "training_cutoff_key": 202605,
            "calibrators": {"QB": {"method": method, "params": {**QB_MAP, "window": "expanding"}, "n": 6440,
                                   "fallback": None}},
            "sample_counts": {"QB": 6440}, "fingerprints": {}, "validation": {}, "status": "shadow"}


def test_participation_map_prefers_the_verified_frozen_copy(tmp_path):
    assert cm.load_participation_map(tmp_path)[0] is None
    src = tmp_path / "research" / f"participation_calibration_{cm.CAL_VERSION}.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps(_cal_artifact()))
    pmap, note = cm.load_participation_map(tmp_path)
    assert note == "ok" and pmap["x"] == QB_MAP["x"] and pmap["source"] == str(src)
    frozen = tmp_path / "research" / "participation_cal_shadow" / f"frozen_{cm.CAL_VERSION}.json"
    frozen.parent.mkdir(parents=True)
    art = _cal_artifact()
    art["calibrators"]["QB"]["params"]["y"] = [0.1, 0.5, 0.9]
    rec = {"schema": "ipm-cal-shadow-freeze/v1", "artifact": art}
    rec["config_hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()[:16]
    rec["frozen_at"] = CREATED
    frozen.write_text(json.dumps(rec))
    pmap, _ = cm.load_participation_map(tmp_path)
    assert pmap["y"] == [0.1, 0.5, 0.9] and pmap["source"] == str(frozen)
    rec["artifact"]["calibrators"]["QB"]["params"]["y"] = [0.2, 0.5, 0.9]      # edited after the freeze
    frozen.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="config_hash"):
        cm.load_participation_map(tmp_path)


def test_a_platt_or_missing_qb_map_is_never_used(tmp_path):
    src = tmp_path / "research" / f"participation_calibration_{cm.CAL_VERSION}.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps(_cal_artifact("platt")))
    assert cm.load_participation_map(tmp_path)[0] is None
    assert cm.calibrate(QB_MAP, 0.5) == pytest.approx(0.4)
    assert np.isclose(cm.calibrate(QB_MAP, 1.0), 0.97, atol=1e-3)


# ----------------------------------------------------------------------------- isolation


def test_hosted_app_never_imports_the_model_or_the_shadow_code():
    code = ("import sys, app.main; bad = [m for m in ('app.analytics.context_model', 'qb_scenarios', "
            "'app.analytics.usage_signals', 'app.analytics.adaptive_workload', 'app.analytics.roster_context') "
            "if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=str(cm.REPO_SCRIPTS.parent / "backend"))

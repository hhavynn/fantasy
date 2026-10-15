"""Research-only HGB challenger: leakage, cutoffs, determinism, missing values, abstention, the
JSON tree artifact (no pickle), column order, production isolation and provenance."""

from __future__ import annotations

import json
import pathlib
import random
import subprocess
import sys

import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")

from app.analytics import challenger as ch  # noqa: E402
from app.analytics import hgb_research as hg  # noqa: E402
from app.analytics.independent_projections import ARTIFACT_SCHEMA, MIN_TRAIN_ROWS, Artifact  # noqa: E402

POS = ("QB", "RB", "WR", "TE")
FEATS = {p: ["x1", "x2", "x3"] for p in POS}
HL = {p: None for p in POS}
SMALL = {"s": {**hg.FIXED, "max_leaf_nodes": 7, "min_samples_leaf": 20, "max_iter": 30}}


def _store(weeks=6, players=80, seed=5) -> pl.DataFrame:
    rng = random.Random(seed)
    rows = []
    for w in range(1, weeks + 1):
        for i in range(players):
            x1, x2 = rng.uniform(0, 20), rng.uniform(-1, 1)
            x3 = None if i % 7 == 0 else rng.choice([0.0, 1.0, 2.0])      # a missing source, and real zeros
            y = max(0.0, 0.6 * x1 + 3 * x2 * (x1 > 10) + rng.gauss(0, 2))
            rows.append({"season": 2025, "week": w, "key": 202500 + w, "gsis_id": f"p{i}", "position": "RB",
                         "games_recent": 0 if i == 1 else 3, "x1": x1, "x2": x2, "x3": x3, "label": y})
    return pl.DataFrame(rows)


def _wf(store, week=6, configs=SMALL, feats=FEATS):
    return hg.walk_forward(store, [(2025, week)], feats, configs, HL)


def _fit(store, cutoff=202506):
    tr = ch.train_rows(store, cutoff, "RB")
    return tr, hg.fit_hgb(tr, FEATS["RB"], SMALL["s"], None, cutoff)


def _artifact(store, cutoff=202506):
    cfg = ch.ChallengerConfig.load()
    art = hg.ResearchArtifact(hg.HGB_VERSION, "2026-10-15T00:00:00+00:00", "fp", cutoff, "cfg123", "data456", "x",
                              {"RB": SMALL["s"]}, {"RB": FEATS["RB"]},
                              {"RB": hg.export_trees(_fit(store, cutoff)[1])}, {"RB": 400})
    assert cfg.version == "ifp-2.0.0"
    return art.seal()


# ----------------------------------------------------------------------------- the fourteen requirements


def test_target_week_leakage_is_rejected():
    store = _store()
    poisoned = store.with_columns(pl.when(pl.col("week") >= 6).then(99.0).otherwise(pl.col("label")).alias("label"))
    assert _wf(store).equals(_wf(poisoned))
    cfg = ch.ChallengerConfig.load()
    for p in POS:
        assert not {"label", "t_opp", "t_eff", "last_pts", "last_td_pts", "last_opps"} & set(hg.hgb_features(p, cfg))


def test_chronological_training_cutoff(monkeypatch):
    seen = []
    real = hg.fit_hgb

    def spy(train, features, params, half_life_weeks, target_key):
        seen.append((int(train["key"].max()), target_key))
        return real(train, features, params, half_life_weeks, target_key)

    monkeypatch.setattr(hg, "fit_hgb", spy)
    hg.walk_forward(_store(), [(2025, 5), (2025, 6)], FEATS, SMALL, HL)
    assert seen and all(mx < k for mx, k in seen)


def test_deterministic_predictions():
    store = _store()
    a, b = _wf(store), _wf(store)
    assert a.equals(b)
    shuffled = _wf(store.sample(fraction=1.0, shuffle=True, seed=2)).sort("gsis_id")
    assert np.allclose(a.sort("gsis_id")["pred_s"].fill_null(np.nan).to_numpy(),
                       shuffled["pred_s"].fill_null(np.nan).to_numpy(), equal_nan=True)


def test_missing_features_stay_missing_and_zeros_stay_zero():
    store = _store()
    X = hg.matrix(store, FEATS["RB"])
    x3 = store["x3"]
    assert np.isnan(X[:, 2]).sum() == x3.null_count() > 0
    assert (X[:, 2] == 0).sum() == (x3 == 0).sum() > 0
    with pytest.raises(KeyError):
        hg.matrix(store, ["x1", "not_a_feature"])
    p = _wf(store)
    has_missing = store.filter((pl.col("week") == 6) & pl.col("x3").is_null() & (pl.col("games_recent") > 0))
    assert p.join(has_missing.select("key", "gsis_id"), on=["key", "gsis_id"])["pred_s"].is_not_null().all()


def test_frozen_configuration_requires_the_same_scoring_fingerprint():
    frozen = {"scoring_fingerprint": "abc", "ifp2_config_hash": "h"}
    hg.check_frozen(frozen, "abc", "h")
    with pytest.raises(ValueError, match="scoring"):
        hg.check_frozen(frozen, "zzz", "h")
    with pytest.raises(ValueError, match="ifp-2"):
        hg.check_frozen(frozen, "abc", "other")


def test_zero_training_rows_abstain():
    store = _store()
    p = _wf(store, week=1)                      # nothing before week 1
    assert p.height and p["pred_s"].null_count() == p.height
    art = _artifact(store)
    empty = hg.ResearchArtifact(**{**art.to_dict(), "models": {}}).seal()
    assert np.isnan(empty.predict(store.filter(pl.col("week") == 6))).all()


def test_low_sample_sizes_abstain():
    store = _store(players=30)                  # 30 players x 5 weeks = 150 < MIN_TRAIN_ROWS
    assert ch.train_rows(store, 202506, "RB").height < MIN_TRAIN_ROWS
    p = _wf(store)
    assert p["pred_s"].null_count() == p.height


def test_research_artifact_round_trips_through_json_without_pickle():
    store = _store()
    tr, model = _fit(store)
    art = _artifact(store)
    text = json.dumps(art.to_dict())
    assert "pickle" not in text
    back = hg.ResearchArtifact.from_dict(json.loads(text))
    rows = store.filter((pl.col("week") == 6) & (pl.col("games_recent") > 0))
    assert np.allclose(back.predict(rows), np.maximum(model.predict(hg.matrix(rows, FEATS["RB"])), 0.0))
    tampered = json.loads(text)
    tampered["models"]["RB"]["trees"][0]["value"][0] += 1.0
    with pytest.raises(ValueError, match="hash"):
        hg.ResearchArtifact.from_dict(tampered)


def test_feature_columns_are_used_in_the_stored_order():
    store = _store()
    art = _artifact(store)
    rows = store.filter(pl.col("week") == 6)
    reordered = rows.select(["x3", "label", "gsis_id", "x2", "position", "games_recent", "x1", "key", "week", "season"])
    assert np.allclose(art.predict(rows), art.predict(reordered), equal_nan=True)
    X = hg.matrix(rows, FEATS["RB"])
    assert not np.allclose(hg.predict_trees(art.models["RB"], X), hg.predict_trees(art.models["RB"], X[:, ::-1]),
                           equal_nan=True)


def test_hosted_runtime_never_imports_research_ml_packages():
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = ("import sys, app.main, app.analytics.hgb_research; "
            "bad = [m for m in ('sklearn', 'scipy') if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], cwd=backend, check=True)
    for path in (backend / "app").rglob("*.py"):
        text = path.read_text()
        if path.name != "hgb_research.py":
            assert "sklearn" not in text and "hgb_research" not in text, path
    pyproject = (backend / "pyproject.toml").read_text()
    runtime = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "scikit" not in runtime                          # only in the research group


def test_identical_comparison_population():
    store = _store()
    p = _wf(store).join(store.select("key", "gsis_id", "label", "games_recent"), on=["key", "gsis_id"])
    p = p.with_columns(pl.when(pl.col("games_recent") > 0).then(5.0).otherwise(None).alias("pred_ridge"))
    common = hg.common_rows(p, ["pred_s", "pred_ridge"])
    assert common.height == p.filter(pl.col("games_recent") > 0).height
    assert p.filter(pl.col("games_recent") == 0)["pred_s"].null_count() == p.filter(pl.col("games_recent") == 0).height


def test_model_and_ensemble_predictions_are_finite():
    store = _store()
    p = _wf(store).join(store.select("key", "gsis_id", "games_recent"), on=["key", "gsis_id"]).filter(
        pl.col("games_recent") > 0)
    v = p["pred_s"].to_numpy()
    assert np.isfinite(v).all() and (v >= 0).all()
    e = p.with_columns(pl.lit(4.0).alias("r")).select(hg.ensemble(pl.col("r"), pl.col("pred_s"), 0.3).alias("e"))["e"]
    assert np.isfinite(e.to_numpy()).all()
    nulls = pl.DataFrame({"r": [None, 2.0], "h": [1.0, None]}).select(hg.ensemble(pl.col("r"), pl.col("h"), 0.5).alias("e"))
    assert nulls["e"].null_count() == 2


def test_research_model_cannot_silently_become_production():
    art = _artifact(_store())
    d = art.to_dict()
    assert d["status"] == "research_only" and d["schema"] != ARTIFACT_SCHEMA
    with pytest.raises(ValueError):
        Artifact.from_dict(d)
    with pytest.raises(ValueError, match="status"):
        hg.ResearchArtifact.from_dict({**d, "status": "production"})
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    assert not [p for p in root.rglob("*.py") if p.name != "hgb_research.py" and "hgb_research" in p.read_text()]


def test_research_artifact_preserves_frozen_provenance():
    art = _artifact(_store())
    d = art.to_dict()
    for k in ("training_cutoff_key", "config_hash", "data_hash", "scoring_fingerprint", "sklearn_version", "version"):
        assert d[k] not in (None, "")
    for k, v in (("training_cutoff_key", 209901), ("config_hash", "x"), ("data_hash", "y"), ("scoring_fingerprint", "z")):
        with pytest.raises(ValueError, match="hash"):
            hg.ResearchArtifact.from_dict({**d, k: v})


# ----------------------------------------------------------------------------- tree export


def test_exported_trees_reproduce_sklearn_exactly():
    store = _store()
    tr, model = _fit(store)
    X = hg.matrix(store.filter(pl.col("week") == 6), FEATS["RB"])
    X[0, 0] = np.nan                                         # a value missing at predict time only
    assert np.allclose(hg.predict_trees(hg.export_trees(model), X), model.predict(X))

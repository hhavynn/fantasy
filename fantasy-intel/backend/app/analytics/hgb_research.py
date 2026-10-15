"""Research only: shallow histogram gradient boosting challenger (ifp-hgb) and a ridge/HGB ensemble.

Nothing here changes ifp-1.0.0 or ifp-2.0.0, and nothing in the app imports it
(tests/test_hgb_research.py). scikit-learn lives in the `research` dependency group (Vercel installs
with --no-dev and never gets it) and is imported lazily inside the fitting functions only.
Inference from a saved artifact needs numpy alone: the boosted trees are exported to an explicit
JSON schema (no pickle) and evaluated by `predict_trees`, which is tested to reproduce
scikit-learn's own predictions. See docs/research/hgb_challenger.md.

Same footing as ridge everywhere: ifp-2's features, ifp-2's training rows (challenger.train_rows:
strictly earlier weeks, labeled, at least one recent game) and recency weights, the same
availability conditioning (rows without a recent game are not forecast), squared loss (a mean, as
expected points need), and the prediction floored at 0 as RidgeModel does. Missing values stay NaN
(scikit-learn routes them per split) and the existing missing-source indicators stay features;
measured zeros stay zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, recency_weights, train_rows
from .forecast_features import POSITIONS, key
from .independent_projections import MIN_TRAIN_ROWS

HGB_VERSION = "ifp-hgb-0.1.0"
HGB_SCHEMA = "hgb-research-artifact/v1"
HGB_STATUS = "research_only"
LEDGER_SCHEMA = "hgb-research-ledger/v1"

# Pre-registered search space (8 configurations, chosen per position on development 2021-22 only).
FIXED = {"loss": "squared_error", "max_depth": 3, "l2_regularization": 1.0, "learning_rate": 0.05,
         "early_stopping": False, "random_state": 0}
SEARCH_SPACE = {f"L{leaves}_m{leaf}_it{it}": {**FIXED, "max_leaf_nodes": leaves, "min_samples_leaf": leaf,
                                              "max_iter": it}
                for leaves in (7, 15) for leaf in (50, 200) for it in (150, 300)}
# Interaction diagnostic (not a candidate): depth-1 trees are additive in the features, so the
# additive model sits between ridge (linear, additive) and HGB (nonlinear with interactions).
ADDITIVE = {**FIXED, "max_depth": 1, "max_leaf_nodes": 2, "min_samples_leaf": 50, "max_iter": 300}
ENSEMBLE_WEIGHTS = tuple(round(w, 1) for w in np.linspace(0, 1, 11))   # weight on ridge (ifp-2)
ENSEMBLE_MAX_ERROR_CORR = 0.97      # ensemble only if development errors are complementary enough
DEVELOPMENT, DIAGNOSTIC = (2021, 2022), ((2023, 2024), (2025, 2026))

FEATURE_GROUPS = {
    "usage": ("xfp_recent", "carries_pg", "targets_pg", "rush_share", "target_share", "snap_pct", "rz_opps_pg",
              "gl_carries_pg", "pass_att_pg", "air_yards_pg", "games_recent"),
    "production": ("pts_recent", "pts_l3", "pts_season", "pts_prev_season", "pts_std", "pts_minus_xfp"),
    "trend_role": ("snap_trend", "opp_trend", "weeks_since_last", "games_season", "team_changed"),
    "matchup_team": ("team_pts_pg", "opp_pos_allowed", "is_home", "opp_pass_epa", "opp_rush_epa"),
    "game_class_flags": ("inj_exits_recent", "last_injury_exit", "last_role_low", "last_blowout_low"),
    "established_prior": ("xfpl_eb", "cur_h_games", "prev_xfpl_pg", "prev_team_changed"),
    "missing_indicators": ("xfp_missing", "snap_missing", "rz_missing"),
}

HGB_DECISION_RULES = {
    "selection": "development 2021-2022 only: per position the configuration with the lowest MAE; ensemble weight "
                 "(if errors are complementary: development error correlation < 0.97) by development MAE",
    "frozen_before": "configuration and ensemble weights are written before any 2023-2026 or prospective result",
    "shadow_if": "development: candidate - ifp2 pooled MAE CI upper < 0; and on the already-inspected 2023-2026 "
                 "replay: pooled MAE not worse, no position significantly worse (CI lower > 0), RMSE not worse by "
                 "> 0.5%, |bias| not worse by > 0.15, starters-pool MAE not worse by > 1%, pair accuracy not lower "
                 "by > 0.5 pp, close-call regret not worse by > 2%",
    "outcome": "all pass -> 'prospective_shadow' (research ledger only); otherwise 'retain_ridge'",
    "never": "no promotion; historical results alone never establish promotion readiness",
}


def group_of(feature: str) -> str:
    for g, fs in FEATURE_GROUPS.items():
        if feature in fs:
            return g
    return "other"


def hgb_features(position: str, ifp2: ChallengerConfig) -> list[str]:
    """ifp-2's features in ifp-2's order (the column order of every matrix and artifact)."""
    return list(ifp2.features(position))


def matrix(rows: pl.DataFrame, features: list[str]) -> np.ndarray:
    """Features in the given order as float64; null -> NaN (a missing source), zeros stay zeros.
    A column that does not exist is an error, never silently NaN."""
    absent = [f for f in features if f not in rows.columns]
    if absent:
        raise KeyError(f"feature columns absent: {absent}")
    return rows.select([pl.col(f).cast(pl.Float64) for f in features]).to_numpy().astype(np.float64)


def fit_hgb(train: pl.DataFrame, features: list[str], params: dict, half_life_weeks: float | None,
            target_key: int):
    """HistGradientBoostingRegressor with ridge's recency weights. Lazy import: research only."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    keys = train["key"].to_numpy()
    w = recency_weights(keys, target_key, half_life_weeks)
    model = HistGradientBoostingRegressor(**params)
    model.fit(matrix(train, features), train["label"].cast(pl.Float64).to_numpy(), sample_weight=w)
    return model


def predict_model(model, rows: pl.DataFrame, features: list[str]) -> np.ndarray:
    return np.maximum(model.predict(matrix(rows, features)), 0.0)


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], features: dict[str, list[str]],
                 configs: dict[str, dict], half_lives: dict[str, float | None],
                 permute_groups: bool = False, seed: int = 20261015) -> pl.DataFrame:
    """pred_<config> per row (NaN = abstention: fewer than MIN_TRAIN_ROWS training rows, or no recent
    game). With `permute_groups` (single config), also pred_<config>__perm_<group>: the same model
    with one feature group shuffled within the week's rows (out-of-sample permutation importance).
    Training rows strictly earlier (asserted)."""
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            ok = rows["games_recent"].to_numpy() > 0
            cols = {}
            for name, params in configs.items():
                if train.height < MIN_TRAIN_ROWS:
                    cols[f"pred_{name}"] = np.full(rows.height, np.nan)
                    continue
                m = fit_hgb(train, features[pos], params, half_lives[pos], k)
                X = matrix(rows, features[pos])
                cols[f"pred_{name}"] = np.where(ok, np.maximum(m.predict(X), 0.0), np.nan)
                if permute_groups:
                    rng = np.random.default_rng(seed + k * 10 + POSITIONS.index(pos))
                    perm = rng.permutation(rows.height)
                    for g in FEATURE_GROUPS:
                        idx = [i for i, f in enumerate(features[pos]) if group_of(f) == g]
                        if not idx:
                            continue
                        Xp = X.copy()
                        Xp[:, idx] = X[perm][:, idx]
                        cols[f"pred_{name}__perm_{g}"] = np.where(ok, np.maximum(m.predict(Xp), 0.0), np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def ensemble(ridge: pl.Expr, hgb: pl.Expr, w_ridge: float) -> pl.Expr:
    """Convex combination; null when either part is missing (no fallback)."""
    return pl.when(ridge.is_not_null() & hgb.is_not_null()).then(w_ridge * ridge + (1 - w_ridge) * hgb).otherwise(None)


# ----------------------------------------------------------------------------- JSON trees (no pickle)


_NODE_FIELDS = ("value", "feature_idx", "num_threshold", "missing_go_to_left", "left", "right", "is_leaf")


def export_trees(model) -> dict:
    """The fitted model's trees as plain lists. Only numerical splits (no categorical features are
    declared); raises otherwise."""
    if getattr(model, "is_categorical_", None) is not None and np.any(model.is_categorical_):
        raise ValueError("categorical splits are not exportable")
    trees = []
    for iteration in model._predictors:
        (pred,) = iteration
        n = pred.nodes
        trees.append({f: n[f].tolist() for f in _NODE_FIELDS})
    return {"baseline": float(np.ravel(model._baseline_prediction)[0]), "trees": trees}


def predict_trees(exported: dict, X: np.ndarray) -> np.ndarray:
    """numpy-only evaluation of exported trees (raw, unfloored)."""
    X = np.asarray(X, dtype=np.float64)
    out = np.full(X.shape[0], exported["baseline"])
    rows = np.arange(X.shape[0])
    for t in exported["trees"]:
        value, feat = np.asarray(t["value"]), np.asarray(t["feature_idx"])
        thr, miss_left = np.asarray(t["num_threshold"]), np.asarray(t["missing_go_to_left"], dtype=bool)
        left, right, leaf = np.asarray(t["left"]), np.asarray(t["right"]), np.asarray(t["is_leaf"], dtype=bool)
        node = np.zeros(X.shape[0], dtype=np.int64)
        while not leaf[node].all():
            x = X[rows, feat[node]]
            go_left = np.where(np.isnan(x), miss_left[node], x <= thr[node])
            node = np.where(leaf[node], node, np.where(go_left, left[node], right[node]))
        out += value[node]
    return out


@dataclass
class ResearchArtifact:
    """Frozen research model for one training cutoff. Distinct schema and status: the production
    loader (independent_projections.Artifact.from_dict) refuses it, and from_dict refuses anything
    that is not research-only or whose content no longer matches its recorded hash."""
    version: str
    created_at: str
    scoring_fingerprint: str
    training_cutoff_key: int                      # trained on weeks strictly before (season*100+week)
    config_hash: str                              # the frozen configuration (selection) it implements
    data_hash: str                                # training rows it saw (input provenance)
    sklearn_version: str
    params: dict[str, dict]                       # position -> HistGradientBoostingRegressor params
    features: dict[str, list[str]]                # position -> ordered feature columns
    models: dict[str, dict]                       # position -> export_trees output
    training_rows: dict[str, int]
    status: str = HGB_STATUS
    schema: str = HGB_SCHEMA
    content_hash: str = ""
    notes: list = field(default_factory=list)

    def _content(self) -> dict:
        d = asdict(self)
        d.pop("content_hash")
        return d

    def seal(self) -> "ResearchArtifact":
        self.content_hash = hashlib.sha256(json.dumps(self._content(), sort_keys=True).encode()).hexdigest()[:16]
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ResearchArtifact":
        if d.get("schema") != HGB_SCHEMA:
            raise ValueError(f"not an HGB research artifact: {d.get('schema')!r}")
        if d.get("status") != HGB_STATUS:
            raise ValueError(f"HGB artifact with status {d.get('status')!r} is not loadable")
        art = ResearchArtifact(**d)
        expected = d.get("content_hash")
        if not expected or ResearchArtifact(**d).seal().content_hash != expected:
            raise ValueError("HGB artifact content does not match its recorded hash")
        return art

    def predict(self, rows: pl.DataFrame) -> np.ndarray:
        out = np.full(rows.height, np.nan)
        for pos, exported in self.models.items():
            idx = np.flatnonzero((rows["position"] == pos).to_numpy())
            if len(idx):
                out[idx] = np.maximum(predict_trees(exported, matrix(rows[idx], self.features[pos])), 0.0)
        if "games_recent" in rows.columns:          # same availability conditioning as ridge: no recent game,
            out[~(rows["games_recent"].fill_null(0).to_numpy() > 0)] = np.nan   # no conditional forecast
        return out


def fit_artifact(store: pl.DataFrame, cutoff_key: int, params: dict[str, dict], ifp2: ChallengerConfig,
                 created_at: str, scoring_fingerprint: str, config_hash: str, data_hash: str) -> ResearchArtifact:
    import sklearn

    models, feats, n = {}, {}, {}
    for pos in POSITIONS:
        feats[pos] = hgb_features(pos, ifp2)
        tr = train_rows(store, cutoff_key, pos)
        n[pos] = tr.height
        if tr.height < MIN_TRAIN_ROWS:
            continue
        models[pos] = export_trees(fit_hgb(tr, feats[pos], params[pos], ifp2.positions[pos].get("half_life_weeks"),
                                           cutoff_key))
    return ResearchArtifact(HGB_VERSION, created_at, scoring_fingerprint, cutoff_key, config_hash, data_hash,
                            sklearn.__version__, params, feats, models, n).seal()


def check_frozen(frozen: dict, scoring_fingerprint: str, ifp2_config_hash: str) -> None:
    """A frozen configuration may only be used under the scoring and ifp-2 config it was frozen on."""
    if frozen.get("scoring_fingerprint") != scoring_fingerprint:
        raise ValueError("league scoring changed since the HGB configuration was frozen")
    if frozen.get("ifp2_config_hash") != ifp2_config_hash:
        raise ValueError("the HGB configuration was frozen against a different ifp-2 configuration")


def common_rows(df: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """The one comparison population: labeled rows where EVERY model has a forecast."""
    return df.filter(pl.col("label").is_not_null() & pl.all_horizontal([pl.col(c).is_not_null() for c in columns]))


def config_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


__all__ = ["HGB_VERSION", "SEARCH_SPACE", "ADDITIVE", "HGB_DECISION_RULES", "walk_forward", "fit_hgb",
           "export_trees", "predict_trees", "ResearchArtifact", "fit_artifact", "ensemble"]

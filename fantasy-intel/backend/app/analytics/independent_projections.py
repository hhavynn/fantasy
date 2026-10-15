"""Independent weekly fantasy-point models (QB/RB/WR/TE), trained on NFL evidence only.

ESPN projections are NEVER an input: they appear only as an external comparison. Models:

  A1 recent_avg   raw league points per game over the last <=5 games played
  A2 season_avg   raw league points per game this season (previous season if none yet)
  B  opportunity  per-position ridge on prior opportunity/role only (xFP, targets, carries,
                  shares, snap share, red-zone work) - no production or context
  C  context      per-position ridge: B + production, efficiency, role change, team offense,
                  opponent tendencies, home/away, sample-size and missing-data indicators

Ridge on standardized features with an unpenalized intercept (closed form, numpy). Missing
values are imputed with the TRAINING mean and paired with an indicator, so "unknown" never
becomes zero. Predictions are floored at 0 (an expected score below zero is not credible for
a player who suits up). Players with no game in the lookback window are abstentions, never a
position-average guess.

Training, evaluation and inference are separate: app.analytics.forecast_backtest selects and
evaluates; `fit_models` trains a frozen configuration; `predict_frame` only applies it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import polars as pl

from .forecast_features import FEATURE_DOCS, POSITIONS

MODEL_VERSION = "ifp-1.0.0"
ARTIFACT_SCHEMA = "independent-projections-artifact/v1"

OPPORTUNITY = {
    "QB": ["xfp_recent", "pass_att_pg", "carries_pg", "snap_pct", "rz_opps_pg", "games_recent",
           "xfp_missing", "snap_missing", "rz_missing"],
    "RB": ["xfp_recent", "carries_pg", "targets_pg", "rush_share", "target_share", "snap_pct", "rz_opps_pg",
           "gl_carries_pg", "games_recent", "xfp_missing", "snap_missing", "rz_missing"],
    "WR": ["xfp_recent", "targets_pg", "target_share", "air_yards_pg", "snap_pct", "rz_opps_pg", "games_recent",
           "xfp_missing", "snap_missing", "rz_missing"],
    "TE": ["xfp_recent", "targets_pg", "target_share", "air_yards_pg", "snap_pct", "rz_opps_pg", "games_recent",
           "xfp_missing", "snap_missing", "rz_missing"],
}
CONTEXT_EXTRA = ["pts_recent", "pts_l3", "pts_season", "pts_prev_season", "pts_std", "pts_minus_xfp",
                 "snap_trend", "opp_trend", "weeks_since_last", "games_season", "team_changed", "team_pts_pg",
                 "opp_pos_allowed", "is_home"]
CONTEXT = {
    "QB": OPPORTUNITY["QB"] + CONTEXT_EXTRA + ["opp_pass_epa", "opp_rush_epa"],
    "RB": OPPORTUNITY["RB"] + CONTEXT_EXTRA + ["opp_rush_epa"],
    "WR": OPPORTUNITY["WR"] + CONTEXT_EXTRA + ["opp_pass_epa"],
    "TE": OPPORTUNITY["TE"] + CONTEXT_EXTRA + ["opp_pass_epa"],
}
FEATURE_SETS = {"B": OPPORTUNITY, "C": CONTEXT}
MODEL_NAMES = {"A1": "recent_avg", "A2": "season_avg", "B": "opportunity", "C": "context"}
LAMBDA_GRID = (0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0)
MIN_TRAIN_ROWS = 200

for _feats in [*OPPORTUNITY.values(), *CONTEXT.values()]:
    assert all(f in FEATURE_DOCS for f in _feats), _feats


# ----------------------------------------------------------------------------- baselines


def baseline_predictions(features: pl.DataFrame) -> pl.DataFrame:
    """A1/A2 for each row; null = abstain (no qualifying games)."""
    return features.with_columns(
        pl.when(pl.col("games_recent") > 0).then(pl.col("pts_recent_raw")).otherwise(None).alias("pred_A1"),
        pl.when(pl.col("games_season") > 0).then(pl.col("pts_season_raw"))
          .otherwise(pl.col("pts_prev_season")).alias("pred_A2"),
    )


# ----------------------------------------------------------------------------- ridge


@dataclass
class RidgeModel:
    position: str
    model: str                       # "B" | "C"
    features: list[str]              # base features (indicators appended as "<f>__missing")
    columns: list[str]               # final design columns
    means: list[float]               # imputation + centering (training means)
    stds: list[float]
    coef: list[float]                # per standardized column
    intercept: float
    lam: float
    n_train: int
    train_window: list[int] = field(default_factory=list)   # [first key, last key] of training rows

    def design(self, df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Raw design matrix (imputed, unstandardized) and a boolean matrix of imputed cells."""
        raw, imputed = [], []
        mean = dict(zip(self.columns, self.means))
        for col in self.columns:
            if col.endswith("__missing"):
                base = col[: -len("__missing")]
                vals = df[base].is_null().cast(pl.Float64).to_numpy() if base in df.columns else np.ones(df.height)
                raw.append(vals)
                imputed.append(np.zeros(df.height, dtype=bool))
            else:
                s = df[col].cast(pl.Float64) if col in df.columns else pl.Series([None] * df.height, dtype=pl.Float64)
                arr = s.to_numpy().astype(float)
                miss = np.isnan(arr)
                arr[miss] = mean[col]
                raw.append(arr)
                imputed.append(miss)
        return np.column_stack(raw) if raw else np.zeros((df.height, 0)), np.column_stack(imputed)

    def predict(self, df: pl.DataFrame) -> np.ndarray:
        x, _ = self.design(df)
        z = (x - np.array(self.means)) / np.array(self.stds)
        return np.maximum(z @ np.array(self.coef) + self.intercept, 0.0)

    def contributions(self, df: pl.DataFrame) -> np.ndarray:
        """Per-row, per-column contribution in league points relative to the training-average
        player (intercept). Sum of contributions + intercept = unfloored prediction."""
        x, _ = self.design(df)
        z = (x - np.array(self.means)) / np.array(self.stds)
        return z * np.array(self.coef)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RidgeModel":
        return RidgeModel(**d)


def _design_columns(train: pl.DataFrame, features: list[str]) -> list[str]:
    cols = list(features)
    for f in features:
        if f in train.columns and train[f].null_count() > 0 and not f.endswith("_missing"):
            cols.append(f"{f}__missing")
    return cols


def fit_ridge(train: pl.DataFrame, position: str, model: str, lams: tuple[float, ...] | list[float],
              label: str = "label") -> dict[float, RidgeModel]:
    """Fit one position/model for several penalties at once (shared Gram matrix).
    lam is scaled by n so a value means the same for any training size."""
    features = FEATURE_SETS[model][position]
    columns = _design_columns(train, features)
    proto = RidgeModel(position, model, features, columns, [0.0] * len(columns), [1.0] * len(columns),
                       [0.0] * len(columns), 0.0, 0.0, train.height)
    means = []
    for c in columns:
        if c.endswith("__missing"):
            base = c[: -len("__missing")]
            means.append(float(train[base].is_null().mean()))
        else:
            m = train[c].cast(pl.Float64).mean() if c in train.columns else None
            means.append(float(m) if m is not None and not math.isnan(m) else 0.0)
    proto.means = means
    x, _ = proto.design(train)
    stds = x.std(axis=0)
    stds[stds < 1e-9] = 1.0
    z = (x - np.array(means)) / stds
    y = train[label].cast(pl.Float64).to_numpy()
    ybar = float(y.mean())
    gram, rhs = z.T @ z, z.T @ (y - ybar)
    n = len(y)
    keys = train.select((pl.col("season") * 100 + pl.col("week")).alias("k"))["k"]
    window = [int(keys.min()), int(keys.max())] if n else []
    out = {}
    for lam in lams:
        beta = np.linalg.solve(gram + lam * n * np.eye(gram.shape[0]), rhs)
        out[lam] = RidgeModel(position, model, features, columns, means, stds.tolist(), beta.tolist(), ybar,
                              float(lam), n, window)
    return out


# ----------------------------------------------------------------------------- artifacts


@dataclass
class Artifact:
    version: str
    created_at: str
    scoring_fingerprint: str
    scoring: dict
    config: dict
    models: dict[str, RidgeModel]               # position -> primary fitted model
    intervals: dict[str, dict]                  # position -> residual quantile bands (or {} if withheld)
    status: dict[str, dict]                     # position -> {status, validated_for, reasons, ...}
    evaluation: dict                            # held-out summary (aggregates only)
    schema: str = ARTIFACT_SCHEMA

    def to_dict(self) -> dict:
        d = asdict(self)
        d["models"] = {p: m.to_dict() for p, m in self.models.items()}
        return d

    @staticmethod
    def from_dict(d: dict) -> "Artifact":
        if d.get("schema") != ARTIFACT_SCHEMA:
            raise ValueError(f"unsupported artifact schema {d.get('schema')!r}")
        d = dict(d)
        d["models"] = {p: RidgeModel.from_dict(m) for p, m in d["models"].items()}
        return Artifact(**d)


def interval_for(intervals: dict, position: str, pred: float) -> tuple[float, float] | None:
    """Empirical 80% prediction interval: prediction + residual quantiles of the band the
    prediction falls in. Bands/quantiles come from validation residuals only."""
    spec = intervals.get(position)
    if not spec:
        return None
    for band in spec["bands"]:
        if (band["low"] is None or pred >= band["low"]) and (band["high"] is None or pred < band["high"]):
            return max(pred + band["q_low"], 0.0), pred + band["q_high"]
    return None


# ----------------------------------------------------------------------------- explanation


LABELS = {
    "xfp_recent": "expected fantasy points (nflverse xFP) per game", "pass_att_pg": "pass attempts per game",
    "carries_pg": "carries per game", "targets_pg": "targets per game", "rush_share": "share of team rushes",
    "target_share": "target share", "snap_pct": "offensive snap share", "rz_opps_pg": "red-zone opportunities per game",
    "gl_carries_pg": "goal-line carries per game", "air_yards_pg": "air yards per game",
    "games_recent": "games in recent window", "pts_recent": "recent points per game (shrunk)",
    "pts_l3": "points per game, last 3", "pts_season": "season points per game (shrunk)",
    "pts_prev_season": "previous-season points per game", "pts_std": "recent scoring volatility",
    "pts_minus_xfp": "points above nflverse xFP per game", "snap_trend": "change in snap share last game",
    "opp_trend": "change in targets+carries last game", "weeks_since_last": "weeks since last game",
    "games_season": "games played this season", "team_changed": "changed NFL team",
    "team_pts_pg": "team points per game vs league", "opp_pos_allowed": "opponent points allowed to position vs league",
    "opp_pass_epa": "opponent pass EPA/play allowed vs league", "opp_rush_epa": "opponent rush EPA/play allowed vs league",
    "is_home": "home game", "xfp_missing": "no xFP coverage", "snap_missing": "no snap data", "rz_missing": "no red-zone data",
}


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "unknown"
    if isinstance(v, float) and abs(v) < 1 and v != 0:
        return f"{v:.3f}"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def explain_row(model: RidgeModel, row: pl.DataFrame, top: int = 4) -> list[dict]:
    """Deterministic largest-magnitude contributions for one row (a 1-row frame). Each item
    names a real model input, its value, the training-average value, and points contributed."""
    contrib = model.contributions(row)[0]
    _, imputed = model.design(row)
    items = []
    mean = dict(zip(model.columns, model.means))
    for i, col in enumerate(model.columns):
        c = float(contrib[i])
        if abs(c) < 0.05:
            continue
        base = col[: -len("__missing")] if col.endswith("__missing") else col
        value = None if bool(imputed[0][i]) else (row[base][0] if base in row.columns else None)
        if col.endswith("__missing"):
            text = f"{LABELS.get(base, base)} unavailable"
        else:
            text = (f"{LABELS.get(col, col)}: {_fmt(value)} (training average {_fmt(mean[col])})"
                    if value is not None else f"{LABELS.get(col, col)} unknown (imputed with training average)")
        items.append({"feature": col, "value": None if value is None else float(value),
                      "training_mean": None if col.endswith("__missing") else round(mean[col], 4),
                      "contribution": round(c, 2), "text": text})
    items.sort(key=lambda d: (-abs(d["contribution"]), d["feature"]))
    return items[:top]

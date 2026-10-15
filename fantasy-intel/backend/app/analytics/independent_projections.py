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
# Research variant (not a production model): C with the last-game snap change split into an
# injury exit and a role change. Evaluated on validation seasons only (forecast_backtest.snap_split_ablation).
CONTEXT_SNAP_SPLIT = {p: [f for f in feats if f != "snap_trend"] + ["snap_trend_role", "injury_exit_last"]
                      for p, feats in CONTEXT.items()}
FEATURE_SETS = {"B": OPPORTUNITY, "C": CONTEXT, "Cs": CONTEXT_SNAP_SPLIT}
MODEL_NAMES = {"A1": "recent_avg", "A2": "season_avg", "B": "opportunity", "C": "context"}
LAMBDA_GRID = (0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0)
MIN_TRAIN_ROWS = 200

for _feats in [*OPPORTUNITY.values(), *CONTEXT.values(), *CONTEXT_SNAP_SPLIT.values()]:
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


def design_matrix(columns: list[str], means: list[float], df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Raw design matrix (nulls imputed with `means`, unstandardized) and a boolean matrix of
    imputed cells. A "<f>__missing" column is the null indicator of base feature f."""
    raw, imputed = [], []
    mean = dict(zip(columns, means))
    for col in columns:
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


def training_means(train: pl.DataFrame, columns: list[str]) -> list[float]:
    """Imputation/centering means from TRAINING rows: feature means, or null rates for indicators."""
    means = []
    for c in columns:
        if c.endswith("__missing"):
            base = c[: -len("__missing")]
            means.append(float(train[base].is_null().mean()) if base in train.columns else 1.0)
        else:
            m = train[c].cast(pl.Float64).mean() if c in train.columns else None
            means.append(float(m) if m is not None and not math.isnan(m) else 0.0)
    return means


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
        return design_matrix(self.columns, self.means, df)

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
    means = training_means(train, columns)
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


# ----------------------------------------------------------------------------- final fit


def fit_final(store: pl.DataFrame, selection: dict, evaluation: dict, scoring_desc: dict,
              created_at: str, train_start: int) -> Artifact:
    """Train each position's primary model on every labeled store row with the FROZEN penalty.
    Status and intervals come from the held-out evaluation; nothing is re-tuned here."""
    models = {}
    for pos, s in selection["positions"].items():
        rows = store.filter((pl.col("position") == pos) & (pl.col("season") >= train_start)
                            & (pl.col("games_recent") > 0) & pl.col("label").is_not_null())
        if rows.height < MIN_TRAIN_ROWS:
            continue
        lam = s["lambda"][s["primary"]]
        models[pos] = fit_ridge(rows, pos, s["primary"], [lam])[lam]
    status = {}
    for pos in POSITIONS:
        st = dict(evaluation.get("status", {}).get(pos)
                  or {"status": "insufficient_data", "reasons": ["not evaluated"], "start_sit_ready": False})
        if pos not in models:
            st = {"status": "insufficient_data", "reasons": ["too few training rows"], "start_sit_ready": False}
        status[pos] = st
    intervals = {pos: selection["positions"][pos]["intervals"] for pos in models
                 if status[pos].get("intervals_published")}
    keep = ("common_support_n", "metrics", "best_trailing_baseline", "relative_mae_improvement",
            "primary_minus_best_baseline_mae", "interval80_empirical_coverage", "coverage", "primary_model")
    summary = {pos: {k: v for k, v in rep.items() if k in keep} for pos, rep in evaluation.get("positions", {}).items()}
    summary["heldout_seasons"] = evaluation.get("heldout_seasons")
    summary["espn_matched_observations"] = evaluation.get("espn_comparison", {}).get("matched_observations", 0)
    summary["pair_decisions"] = evaluation.get("pair_decisions", {}).get("all")
    return Artifact(version=MODEL_VERSION, created_at=created_at, scoring_fingerprint=scoring_desc["fingerprint"],
                    scoring=scoring_desc,
                    config={"selection": {p: {"primary": s["primary"], "lambda": s["lambda"]}
                                          for p, s in selection["positions"].items()},
                            "protocol": selection["protocol"], "train_start_season": train_start},
                    models=models, intervals=intervals, status=status, evaluation=summary)


# ----------------------------------------------------------------------------- inference

LIMITATIONS = [
    "Conditional on the player suiting up; availability is reported separately, not modeled.",
    "Trained on nflverse box scores scored with this league's offensive rules (QB/RB/WR/TE only).",
    "Uses only games completed before the target week; no betting lines, injury reports or depth charts.",
    "Not validated against ESPN unless the model status says an ESPN comparison criterion was met.",
]


def predict_frame(artifact: Artifact, features: pl.DataFrame) -> list[dict]:
    """Apply frozen models to point-in-time features. One dict per row; model_points is None
    for an abstention (no game in the lookback window or no model for the position)."""
    out = []
    for pos in POSITIONS:
        rows = features.filter(pl.col("position") == pos)
        if rows.height == 0:
            continue
        model = artifact.models.get(pos)
        preds = model.predict(rows) if model else None
        for i, rec in enumerate(rows.iter_rows(named=True)):
            item = {**rec, "model_points": None, "interval_low": None, "interval_high": None, "explanation": []}
            if model is not None and (rec.get("games_recent") or 0) > 0:
                p = round(float(preds[i]), 2)
                iv = interval_for(artifact.intervals, pos, p)
                item.update(model_points=p, explanation=explain_row(model, rows.slice(i, 1)),
                            interval_low=None if iv is None else round(iv[0], 2),
                            interval_high=None if iv is None else round(iv[1], 2))
            out.append(item)
    return out


def missing_inputs(row: dict) -> list[str]:
    out = []
    if row.get("xfp_missing"):
        out.append("nflverse xFP unavailable in recent games")
    if row.get("snap_missing"):
        out.append("snap counts unavailable in recent games")
    if row.get("rz_missing"):
        out.append("play-by-play red-zone data unavailable in recent games")
    if row.get("pts_prev_season") is None:
        out.append("no previous-season games")
    if (row.get("games_season") or 0) == 0:
        out.append("no games this season before the target week")
    if (row.get("opp_games") or 0) == 0:
        out.append("opponent has no completed games this season (league-average matchup assumed)")
    return out


def flags_for(row: dict) -> list[str]:
    out = []
    g = row.get("games_recent") or 0
    if 0 < g < 3:
        out.append(f"low sample: {g} recent game(s)")
    if row.get("team_changed"):
        out.append(f"changed team ({row.get('last_team')} -> {row.get('team')}): role shares come from the old team")
    if (row.get("weeks_since_last") or 0) >= 4:
        out.append(f"returning after {row.get('weeks_since_last')} weeks without a game")
    if row.get("snap_pct") is not None and row["snap_pct"] < 0.5:
        out.append(f"part-time role (recent snap share {row['snap_pct']:.0%}): the estimate assumes he plays, "
                   "not that he starts or plays a full game")
    if row.get("snap_trend") is not None and abs(row["snap_trend"]) >= 0.2:
        out.append(f"snap share changed {row['snap_trend']:+.0%} in the last game")
    return out

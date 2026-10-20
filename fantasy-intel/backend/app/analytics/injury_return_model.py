"""ifp-2.3.0: the production "Ours" model. ifp-2.2.0 plus the injury-return pair, by owner decision.

ifp-2.2.0 (app.analytics.context_model) is kept whole: its features, training rows, frozen penalties and recency, the
QB scenario model and P(plays). On top of it RB, WR and TE add the frozen candidates of the injury-return shadow
(scripts/injury_return_shadow.py, docs/research/injury_return_split.md), promoted by owner override before that
shadow's one look (docs/research/ifp23_promotion.md):

=============  ==========  ==================================================================  ==========
component      shadow      what it adds                                                        positions
=============  ==========  ==================================================================  ==========
return_slope   ifp-irs-p1  snap_trend_ret = snap_trend if returner else 0 (returners' own      RB, WR, TE
                           snap_trend slope; the existing slope is then fitted by the others)
return_bias    ifp-irb-p1  ret_ind = 1 if returner else 0 (each position's returner            RB, WR, TE
                           adjustment)
=============  ==========  ==================================================================  ==========

A returner is an RB/WR/TE whose last appearance before the cutoff week was an injury exit (injury_exit_last = 1) and
whose last team has played at least one game since, counted up to the cutoff week (games_missed >= 1). return_columns
is the shadow's definition (injury_return_research.with_cohort), tested equal to it.

SHIPPED is the backtest guard's outcome (scripts/ifp23_backtest.py): a component whose leave-one-out difference makes a
position clearly worse (week-clustered MAE CI entirely above 0) is left out for that position.

context_model stays ifp-2.2.0, so the injury-return shadow keeps comparing against its frozen ifp-2.2.0 base.
"""

from __future__ import annotations

import hashlib

import numpy as np
import polars as pl

from . import context_model as cm
from .forecast_features import POSITIONS, Sources, key

MODEL_VERSION = "ifp-2.3.0"
BASE_VERSION = cm.MODEL_VERSION               # ifp-2.2.0: kept whole, this model extends it
MODEL_NAME = "ifp23"                         # RidgeModel.model of RB/WR/TE (QB keeps ifp-2.2.0's model, "ifp22")
CONFIG_KEY = "injury_return_model"           # Artifact.config key that marks an ifp-2.3.0 artifact
RETURN_POSITIONS = ("RB", "WR", "TE")

RETURN_SLOPE, RETURN_BIAS = "return_slope", "return_bias"
COMPONENTS = {
    RETURN_SLOPE: {"shadow": "ifp-irs-p1", "feature": "snap_trend_ret", "positions": RETURN_POSITIONS,
                   "label": "returners' own snap-trend slope"},
    RETURN_BIAS: {"shadow": "ifp-irb-p1", "feature": "ret_ind", "positions": RETURN_POSITIONS,
                  "label": "returner adjustment"},
}
CANDIDATE = {p: tuple(c for c, d in COMPONENTS.items() if p in d["positions"]) for p in POSITIONS}
# The backtest guard's outcome (docs/research/ifp23_promotion.md): a component listed here was left out for the position.
GUARD_DROPPED: dict[str, dict[str, str]] = {"QB": {}, "RB": {}, "WR": {}, "TE": {}}
SHIPPED: dict[str, tuple[str, ...]] = {p: tuple(c for c in CANDIDATE[p] if c not in GUARD_DROPPED[p]) for p in POSITIONS}
RETURN_COLUMNS = ("games_missed", "ret_cohort", "snap_trend_ret", "ret_ind")


# ----------------------------------------------------------------------------- definition


def return_columns(rows: pl.DataFrame, games: pl.DataFrame, cutoff: str = "key") -> pl.DataFrame:
    """`rows` plus games_missed (games of the player's last team strictly between his last appearance and the cutoff
    week), ret_cohort (RB/WR/TE, injury exit, games_missed >= 1), snap_trend_ret and ret_ind. `rows` needs gsis_id,
    position, the cutoff column, last_team, last_season, last_week, injury_exit_last and snap_trend."""
    if rows.height == 0:
        return rows.with_columns(pl.lit(None, dtype=pl.Int64).alias("games_missed"),
                                 pl.lit(None, dtype=pl.Boolean).alias("ret_cohort"),
                                 pl.lit(None, dtype=pl.Float64).alias("snap_trend_ret"),
                                 pl.lit(None, dtype=pl.Float64).alias("ret_ind"))
    g = games.select(pl.col("team").alias("last_team"),
                     (pl.col("season").cast(pl.Int64) * 100 + pl.col("week").cast(pl.Int64)).alias("_gk")).unique()
    # one row per player and cutoff: rest-of-season rows repeat a player under one cutoff
    r = rows.select("gsis_id", pl.col(cutoff).cast(pl.Int64).alias("_ck"), "last_team",
                    (pl.col("last_season").cast(pl.Int64) * 100 + pl.col("last_week").cast(pl.Int64)).alias("_lk")
                    ).unique(["gsis_id", "_ck"])
    miss = (r.drop_nulls(["last_team", "_lk"]).join(g, on="last_team")
            .filter((pl.col("_gk") > pl.col("_lk")) & (pl.col("_gk") < pl.col("_ck")))
            .group_by("gsis_id", "_ck").agg(pl.len().cast(pl.Int64).alias("games_missed")))
    base = rows.drop([c for c in RETURN_COLUMNS if c in rows.columns]).with_columns(
        pl.col(cutoff).cast(pl.Int64).alias("_ck"))
    out = base.join(miss, on=["gsis_id", "_ck"], how="left", validate="m:1").drop("_ck")
    assert out.height == rows.height, "return_columns changed the row count"
    ret = ((pl.col("injury_exit_last") == 1) & (pl.col("games_missed") >= 1)
           & pl.col("position").is_in(RETURN_POSITIONS)).fill_null(False)
    out = out.with_columns(pl.col("games_missed").fill_null(0)).with_columns(ret.alias("ret_cohort"))
    return out.with_columns(
        pl.when(pl.col("ret_cohort")).then(pl.col("snap_trend").cast(pl.Float64).fill_null(0.0)).otherwise(0.0)
          .round(cm.FEATURE_DECIMALS).alias("snap_trend_ret"),
        pl.col("ret_cohort").cast(pl.Float64).alias("ret_ind"))


def features(cfg, position: str, components: tuple[str, ...] | None = None) -> list[str]:
    """ifp-2.2.0's features for the position plus the components' columns (default: SHIPPED)."""
    comps = SHIPPED[position] if components is None else components
    feats = list(cm.features(cfg, position))
    return feats + [COMPONENTS[c]["feature"] for c in comps if COMPONENTS[c]["feature"] not in feats]


def config(cfg) -> dict:
    """The frozen model definition stored in every artifact (and hashed into its config_hash)."""
    return {"version": MODEL_VERSION, "base": BASE_VERSION, "base_config_hash": cm.config_hash(cfg),
            "components": {p: list(SHIPPED[p]) for p in POSITIONS},
            "shadows": {c: COMPONENTS[c]["shadow"] for c in COMPONENTS}, "guard_dropped": GUARD_DROPPED,
            "returner": "injury_exit_last = 1 and games_missed >= 1 (RB/WR/TE), games counted up to the cutoff week",
            "features": {p: features(cfg, p) for p in POSITIONS}}


def config_hash(cfg) -> str:
    from .challenger import config_hash as h

    return h(config(cfg))


def is_return_artifact(artifact) -> bool:
    return isinstance((artifact.config or {}).get(CONFIG_KEY), dict)


# ----------------------------------------------------------------------------- training


def with_training_inputs(sources: Sources, store: pl.DataFrame, inputs, roster_codes: pl.DataFrame | None = None
                         ) -> pl.DataFrame:
    """context_model.with_training_inputs plus the return columns, each row as known before its own week."""
    return return_columns(cm.with_training_inputs(sources, store, inputs, roster_codes), sources.games)


def inputs_hash(store: pl.DataFrame) -> str:
    """Digest of the return columns of a training store (part of the artifact's provenance)."""
    cols = ["key", "gsis_id", *[c for c in RETURN_COLUMNS if c in store.columns]]
    return hashlib.sha256(store.select(cols).sort("key", "gsis_id").write_csv().encode()).hexdigest()[:16]


def fit(store: pl.DataFrame, season: int, week: int, cfg, components: dict[str, tuple[str, ...]] | None = None,
        positions: tuple[str, ...] = POSITIONS) -> tuple[dict, dict, dict]:
    """({position: RidgeModel}, {position: training rows}, {position: fallback RidgeModel}) for target week (season,
    week). QB is ifp-2.2.0's fit unchanged (scenario model and fallback). RB/WR/TE: ifp-2.2.0's training rows and
    fitting rule with the components' columns added. `store` must carry with_training_inputs' columns."""
    from . import challenger as ch

    comps = SHIPPED if components is None else components
    models, rows, fallback = {}, {}, {}
    if "QB" in positions:
        models, rows, fallback = cm.fit(store, season, week, cfg, positions=("QB",))
    k = key(season, week)
    for pos in positions:
        if pos == "QB":
            continue
        tr = ch.train_rows(store, k, pos)
        if tr.height < ch.MIN_TRAIN_ROWS:
            continue
        spec = cfg.spec(pos)
        models[pos] = ch.fit_linear(tr, pos, features(cfg, pos, comps[pos]), spec.lam[pos], loss=spec.loss,
                                    huber_c=spec.huber_c, half_life_weeks=spec.half_life_weeks, target_key=k,
                                    name=MODEL_NAME)
        rows[pos] = tr.height
    return models, rows, fallback


def predict_points(models: dict, fallback: dict, rows: pl.DataFrame, position: str) -> np.ndarray:
    """Conditional points of one position's rows (NaN = no model): QB through ifp-2.2.0's scenario mixture."""
    return cm.predict_points(models, fallback, rows, position, cm.SHIPPED[position] if position == "QB" else ())


# ----------------------------------------------------------------------------- live (independent_projection_store)


def augment(artifact, sources: Sources, inputs, season: int, week: int, feature_week: int, feats: pl.DataFrame, *,
            as_of) -> pl.DataFrame:
    """context_model.augment (every ifp-2.2.0 column, as known at the cutoff week W), then the return columns with
    games missed counted up to W."""
    out = cm.augment(artifact, sources, inputs, season, week, feature_week, feats, as_of=as_of)
    if not is_return_artifact(artifact) or out.height == 0:
        return out
    return return_columns(out.with_columns(pl.lit(key(season, feature_week), dtype=pl.Int64).alias("key")),
                          sources.games)


def predict_frame(artifact, feats: pl.DataFrame) -> list[dict]:
    """context_model.predict_frame: the QB scenario mixture, every other position through its own ridge (ifp-2.3.0's
    for RB/WR/TE)."""
    return cm.predict_frame(artifact, feats)


def calibrate_rows(artifact, rows: list[dict], horizon: int) -> int:
    return cm.calibrate_rows(artifact, rows, horizon)


def artifact_config(cfg) -> dict:
    return config(cfg)


def describe(artifact) -> dict:
    c = (artifact.config or {}).get(CONFIG_KEY) or {}
    return {"version": c.get("version"), "base": c.get("base"), "components": c.get("components"),
            "guard_dropped": c.get("guard_dropped"), "context": cm.describe(artifact)}


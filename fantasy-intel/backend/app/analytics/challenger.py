"""ifp-2 research challenger: linear models with optional robust loss and recency weighting,
walk-forward evaluation over candidate specs, and the frozen challenger configuration.

Nothing here changes ifp-1.0.0. The challenger is a shadow model: it is trained and evaluated
alongside the champion (docs/independent_projection_engine.md, "Weekly learning and the ifp-2
challenger") and is never read by lineups, start/sit, trades or waivers.

Windows (fixed before any challenger result was seen):
  development   2021-2022   feature sets, penalties, loss and recency choices
  confirmation  2023-2024   one comparison of the frozen challenger vs C (lambdas of C were also
                            chosen on these seasons, which favours C, not the challenger)
  exploratory   2025 + 2026 weeks 1-5 (already inspected by ifp-1.0.0 work; reported, never used to choose)
  prospective   2026 week 6 onward, frozen pregame forecasts only (app.analytics.weekly_learning)
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import polars as pl

from .forecast_backtest import (PROMOTION_CRITERIA, STARTERS_POOL_DEFINITION, _arr, interval_bands,
                                metrics, pair_decisions, starters_pool_expr, week_bootstrap_mae_diff)
from .forecast_features import FEATURE_DOCS, POSITIONS, key
from .independent_projections import (CONTEXT, LAMBDA_GRID, MIN_TRAIN_ROWS, OPPORTUNITY, RidgeModel, _design_columns,
                                      design_matrix, interval_for, training_means)

CHALLENGER_VERSION = "ifp-2.0.0"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}
TRAIN_START = 2020
WEEKS_PER_SEASON = 18          # recency age: a season boundary counts as this many weeks

# ----------------------------------------------------------------------------- feature groups

EFFICIENCY = ["xfpl_recent", "xfpl_last", "yd_over_exp", "td_over_exp", "pts_median"]           # Candidate 1/2
CONTEXT_PLUS = ["opp_share", "opp_share_last", "team_plays_pg", "last_team_plays_rel", "last_margin"]  # Candidate 4
HEALTHY = ["h_xfpl_pg", "h_carries_pg", "h_targets_pg", "h_snap_pct", "h_opp_share", "h_games_recent",
           "inj_exits_recent", "last_injury_exit", "last_role_low", "last_blowout_low"]            # injury-aware usage
PRIOR = ["prev_xfpl_pg", "prev_carries_pg", "prev_targets_pg", "prev_snap_pct", "prev_h_games", "prev_team_changed"]
BLEND = ["xfpl_eb", "cur_h_games"]                                                               # adaptive EB blend
FLAGS = ["inj_exits_recent", "last_injury_exit", "last_role_low", "last_blowout_low"]          # game classes only
USAGE = [f for f in HEALTHY if f not in FLAGS]                                                    # healthy averages only
PRIOR_CORE = ["prev_xfpl_pg", "prev_team_changed"]
GROUPS = {"X": EFFICIENCY, "K": CONTEXT_PLUS, "H": HEALTHY, "P": PRIOR, "E": BLEND, "F": FLAGS, "U": USAGE,
          "Q": PRIOR_CORE}
RAW_PRODUCTION = ["pts_recent", "pts_l3", "pts_minus_xfp"]   # dropped by the opportunity-first variant

for _f in [f for g in GROUPS.values() for f in g]:
    assert _f in FEATURE_DOCS, _f


# "R": replace last-5 usage by its healthy-game counterpart (injury exits removed) instead of adding it
HEALTHY_REPLACEMENT = {"xfp_recent": "h_xfpl_pg", "targets_pg": "h_targets_pg", "carries_pg": "h_carries_pg",
                       "snap_pct": "h_snap_pct", "snap_trend": "snap_trend_role"}


def feature_set(groups: str, opportunity_first: bool = False) -> dict[str, list[str]]:
    """Context model C plus the named groups (letters of GROUPS). `opportunity_first` drops the raw
    production averages so production enters only through the expected-points decomposition.
    "R" in `groups` swaps raw last-5 usage for healthy-game usage (HEALTHY_REPLACEMENT)."""
    out = {}
    replace, groups = "R" in groups, groups.replace("R", "")
    for pos in POSITIONS:
        feats = [f for f in CONTEXT[pos] if not (opportunity_first and f in RAW_PRODUCTION)]
        if replace:
            feats = [HEALTHY_REPLACEMENT.get(f, f) for f in feats]
            feats += [f for f in ("last_injury_exit", "inj_exits_recent") if f not in feats]
        for g in groups:
            feats += [f for f in GROUPS[g] if f not in feats]
        out[pos] = feats
    return out


@dataclass(frozen=True)
class Spec:
    name: str
    groups: str = ""                    # "" = exactly model C's features
    opportunity_first: bool = False
    loss: str = "squared"               # "squared" | "huber"
    huber_c: float = 1.345              # Huber threshold in robust residual SDs
    half_life_weeks: float | None = None
    lam: dict | None = None             # position -> penalty (None = tune on the grid)
    usage_prior_games: float | None = None   # research only: recompute xfpl_eb with another prior strength

    def features(self) -> dict[str, list[str]]:
        return feature_set(self.groups, self.opportunity_first)


# ----------------------------------------------------------------------------- fitting


def recency_weights(keys: np.ndarray, target_key: int, half_life_weeks: float | None) -> np.ndarray:
    if not half_life_weeks:
        return np.ones(len(keys))
    idx = (keys // 100) * WEEKS_PER_SEASON + keys % 100
    t = (target_key // 100) * WEEKS_PER_SEASON + target_key % 100
    return 0.5 ** ((t - idx) / half_life_weeks)


def _solve(z: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    sw = w.sum()
    zm, ym = (w[:, None] * z).sum(0) / sw, float((w * y).sum() / sw)
    zc = z - zm
    beta = np.linalg.solve(zc.T @ (w[:, None] * zc) + lam * sw * np.eye(z.shape[1]), zc.T @ (w * (y - ym)))
    return beta, ym - float(zm @ beta)


def fit_linear(train: pl.DataFrame, position: str, features: list[str], lam: float, *, loss: str = "squared",
               huber_c: float = 1.345, half_life_weeks: float | None = None, target_key: int | None = None,
               label: str = "label", name: str = "ifp2") -> RidgeModel:
    """Weighted ridge (penalty lam * sum of weights, intercept unpenalized) on standardized
    features; `loss="huber"` refits by iteratively reweighted least squares with a fixed
    threshold huber_c * 1.4826 * MAD of the squared-loss residuals. Deterministic. With unit
    weights and squared loss this equals independent_projections.fit_ridge."""
    columns = _design_columns(train, features)
    means = training_means(train, columns)
    x, _ = design_matrix(columns, means, train)
    stds = x.std(axis=0)
    stds[stds < 1e-9] = 1.0
    z = (x - np.array(means)) / stds
    y = train[label].cast(pl.Float64).to_numpy()
    keys = train.select((pl.col("season") * 100 + pl.col("week")).alias("k"))["k"].to_numpy()
    w = recency_weights(keys, target_key if target_key is not None else int(keys.max()) + 1, half_life_weeks)
    beta, b0 = _solve(z, y, w, lam)
    if loss == "huber":
        r = y - (z @ beta + b0)
        delta = huber_c * 1.4826 * float(np.median(np.abs(r - np.median(r)))) or 1.0
        for _ in range(100):
            a = np.minimum(1.0, delta / np.maximum(np.abs(r), 1e-12))
            new_beta, b0 = _solve(z, y, w * a, lam)
            done = float(np.max(np.abs(new_beta - beta))) < 1e-9
            beta = new_beta
            r = y - (z @ beta + b0)
            if done:
                break
    window = [int(keys.min()), int(keys.max())] if len(keys) else []
    return RidgeModel(position, name, list(features), columns, means, stds.tolist(), beta.tolist(), b0, float(lam),
                      int(train.height), window)


def with_usage_prior(store: pl.DataFrame, k: float | None) -> pl.DataFrame:
    """Recompute xfpl_eb with prior strength k from its stored ingredients (research only)."""
    if k is None:
        return store
    prior = pl.coalesce(pl.col("prev_xfpl_pg"), pl.col("prior_xfpl"))
    n = pl.col("cur_h_games").fill_null(0)
    return store.with_columns(
        pl.when((n > 0) | pl.col("prev_xfpl_pg").is_not_null())
          .then((pl.col("cur_h_xfpl_pg").fill_null(0) * n + prior * k) / (n + k)).otherwise(None).round(10)
          .alias("xfpl_eb"))


# ----------------------------------------------------------------------------- walk-forward


def train_rows(store: pl.DataFrame, k: int, position: str, train_start: int = TRAIN_START) -> pl.DataFrame:
    return store.filter((pl.col("key") < k) & (pl.col("season") >= train_start) & (pl.col("position") == position)
                        & (pl.col("games_recent") > 0) & pl.col("label").is_not_null())


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], specs: list[Spec],
                 lams: tuple[float, ...] = LAMBDA_GRID) -> pl.DataFrame:
    """One prediction column per (spec, lambda): pred_<spec>@<lam>. A spec with fixed `lam`
    produces pred_<spec>. Training rows are strictly earlier weeks (asserted)."""
    stores = {s.usage_prior_games: with_usage_prior(store, s.usage_prior_games) for s in specs}
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            cols = {}
            base = None
            for spec in specs:
                st = stores[spec.usage_prior_games]
                rows = st.filter((pl.col("key") == k) & (pl.col("position") == pos))
                if rows.height == 0:
                    continue
                base = rows if base is None else base
                train = train_rows(st, k, pos)
                assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
                grid = [spec.lam[pos]] if spec.lam else list(lams)
                for lam in grid:
                    col = f"pred_{spec.name}" if spec.lam else f"pred_{spec.name}@{lam:g}"
                    if train.height < MIN_TRAIN_ROWS:
                        cols[col] = np.full(rows.height, np.nan)
                        continue
                    m = fit_linear(train, pos, spec.features()[pos], lam, loss=spec.loss, huber_c=spec.huber_c,
                                   half_life_weeks=spec.half_life_weeks, target_key=k)
                    p = m.predict(rows)
                    cols[col] = np.where(rows["games_recent"].to_numpy() > 0, p, np.nan)
            if base is not None:
                out.append(base.with_columns([pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def window_weeks(store: pl.DataFrame, seasons) -> list[tuple[int, int]]:
    return sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season").is_in(list(seasons)))
                   .select("season", "week").unique().iter_rows()})


# ----------------------------------------------------------------------------- cohorts & reports


def add_last_game(store: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """Attach the player's last pre-week game (points, opportunities) for boom/bust cohorts."""
    last = panel.select(pl.col("season").alias("last_season"), pl.col("week").alias("last_week"), "gsis_id",
                        pl.col("league_points").alias("last_pts"), pl.col("td_points").alias("last_td_pts"),
                        (pl.col("targets") + pl.col("carries")).alias("last_opps"))
    return store.join(last, on=["last_season", "last_week", "gsis_id"], how="left")


def cohorts(df: pl.DataFrame) -> dict[str, pl.Expr]:
    """Cohort filters (all pregame quantities)."""
    prev_mean = (pl.col("pts_recent_raw") * pl.col("games_recent") - pl.col("last_pts")) / (pl.col("games_recent") - 1)
    return {
        "starters_pool": starters_pool_expr(),
        "low_volume_lt5_opps": (pl.col("targets_pg") + pl.col("carries_pg")) < 5,
        "limited_history_lt3_games": pl.col("games_recent") < 3,
        "after_boom": (pl.col("games_recent") >= 3) & (pl.col("last_pts") >= prev_mean + 10),
        "after_td_boom": (pl.col("games_recent") >= 3) & (pl.col("last_pts") >= prev_mean + 10)
                         & (pl.col("last_td_pts") >= 12),
        "after_bust": (pl.col("games_recent") >= 3) & (pl.col("last_pts") <= prev_mean - 8),
        "after_injury_exit": pl.col("last_injury_exit") == 1,
        "repeated_injury_exits": pl.col("inj_exits_recent") >= 2,
        "after_role_drop": pl.col("last_role_low") == 1,
        "team_changed": (pl.col("team_changed") == 1) | (pl.col("prev_team_changed") == 1),
        "high_volume": pl.col("h_xfpl_pg").fill_null(0) >= 15,
        "high_projection_ge15": pl.col("pred_C") >= 15,
    }


def compare(preds: pl.DataFrame, models: dict[str, str], baseline: str = "C", seed: int = 20261014,
            resamples: int = 1000, intervals: dict | None = None) -> dict:
    """Per-position and pooled metrics on common support. `models` maps display name -> column.
    `intervals`: {model: {pos: bands}} from an EARLIER window, for coverage."""
    p = preds.with_columns([pl.col(c).alias(f"pred_{n}") for n, c in models.items() if c != f"pred_{n}"])
    cols = [f"pred_{n}" for n in models]
    p = p.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in cols]))
    flt = cohorts(p)
    out = {}
    for pos in [*POSITIONS, "ALL"]:
        g = p if pos == "ALL" else p.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        rep = {"n": g.height, "weeks": g.select("key").n_unique(),
               "metrics": {n: metrics(a, _arr(g, f"pred_{n}")) for n in models}}
        rep["minus_baseline_mae"] = {n: week_bootstrap_mae_diff(g, f"pred_{n}", f"pred_{baseline}", resamples, seed)
                                     for n in models if n != baseline}
        rep["cohorts"] = {}
        for cname, expr in flt.items():
            c = g.filter(expr.fill_null(False))
            rep["cohorts"][cname] = {"n": c.height, **{n: metrics(_arr(c, "label"), _arr(c, f"pred_{n}"))["mae"]
                                                        for n in models},
                                     "bias": {n: metrics(_arr(c, "label"), _arr(c, f"pred_{n}"))["bias"] for n in models}}
        if intervals:
            rep["interval80_coverage"] = {}
            for n, bands in intervals.items():
                if pos == "ALL" or not bands.get(pos):
                    continue
                hits = [lo <= y <= hi for p_, y in zip(g[f"pred_{n}"].to_list(), g["label"].to_list())
                        for lo, hi in [interval_for(bands, pos, p_)]]
                rep["interval80_coverage"][n] = round(sum(hits) / len(hits), 4) if hits else None
        out[pos] = rep
    out["pairs"] = pair_decisions(p, cols)
    return out


def bands_for(preds: pl.DataFrame, col: str) -> dict:
    return {pos: interval_bands(preds.filter(pl.col("position") == pos), col) for pos in POSITIONS}


# ----------------------------------------------------------------------------- frozen configuration

CONFIG_PATH = Path(__file__).with_name("challenger_config.json")

# Decision rules for the confirmation window, committed before the confirmation run. The frozen
# challenger is compared once with the frozen context model C (its ifp-1.0.0 penalties).
CONFIRMATION_RULES = {
    "window": WINDOWS["confirmation"],
    "pooled_mae": "challenger - C pooled MAE < 0 with week-clustered bootstrap 95% CI upper bound < 0",
    "no_position_significantly_worse": "no position with a CI lower bound > 0 (significantly worse than C)",
    "rmse_guard": "pooled RMSE not worse than C by more than 0.5%",
    "bias_guard": "|pooled bias| not larger than C's by more than 0.15 points",
    "starters_guard": "MAE on the starters pool not worse than C by more than 1%",
    "pairs_guard": "Start/Sit pair accuracy not lower than C's by more than 0.5 percentage points",
    "outcome": "all pass -> 'shadow' challenger (prospective evaluation only); any fail -> 'rejected' "
               "(weekly pipeline still records it for diagnosis, flagged rejected)",
    "never": "a confirmation pass does not promote anything; promotion needs SHADOW_PROMOTION_CRITERIA and approval",
}

# Prospective (frozen pregame forecasts, 2026 week 6 onward) bar before the challenger may even be
# proposed to replace ifp-1.0.0 for a position. Separate from PROMOTION_CRITERIA, which stays as is.
SHADOW_PROMOTION_CRITERIA = {
    "min_prospective_weeks": 8,
    "min_matched_appearances_per_position": 400,
    "mae_vs_champion": "challenger - champion MAE week-clustered bootstrap 95% CI upper bound < 0 (same rows, "
                       "both from frozen pregame runs)",
    "bias_guard": "|bias| not worse than the champion's by more than 0.15 points",
    "starters_guard": "starters-pool MAE not worse than the champion's",
    "interval_coverage": [0.75, 0.85],
    "also_required": "the existing PROMOTION_CRITERIA (>= 3% vs best trailing baseline, ESPN rule) for any "
                     "consumer-facing use, and explicit user approval",
}

# The same criteria as enforceable thresholds (weekly_learning.shadow_status). Kept separate so the
# pre-registered text above, which is stored in every challenger artifact, stays byte-identical.
# A criterion whose evidence is missing is `insufficient_data`, never a pass.
SHADOW_GATE = {
    "evidence_kind": "pregame",                     # frozen pregame ledger rows only; never retrospective
    "min_prospective_weeks": SHADOW_PROMOTION_CRITERIA["min_prospective_weeks"],          # fully final weeks
    "min_matched_appearances_per_position": SHADOW_PROMOTION_CRITERIA["min_matched_appearances_per_position"],
    "mae_ci95_upper_below": 0.0,                    # challenger - champion, week-clustered, identical rows
    "bias_tolerance_points": 0.15,                  # |bias(challenger)| <= |bias(champion)| + 0.15
    "starters_pool": STARTERS_POOL_DEFINITION,
    "starters_pool_mae_tolerance": 0.0,             # MAE(challenger) <= MAE(champion) on the pool
    "interval_coverage": tuple(SHADOW_PROMOTION_CRITERIA["interval_coverage"]),
}


@dataclass
class ChallengerConfig:
    version: str
    positions: dict[str, dict]          # position -> Spec fields (groups, half_life_weeks, lam, ...)
    intervals: dict[str, dict]          # position -> residual bands from development-window predictions
    usage_prior_games: float
    selected_on: list[int]
    evidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def spec(self, position: str) -> Spec:
        d = dict(self.positions[position])
        return Spec(name=f"ifp2_{position}", groups=d.get("groups", ""),
                    opportunity_first=d.get("opportunity_first", False), loss=d.get("loss", "squared"),
                    huber_c=d.get("huber_c", 1.345), half_life_weeks=d.get("half_life_weeks"),
                    lam={position: d["lam"]})

    def features(self, position: str) -> list[str]:
        return self.spec(position).features()[position]

    def hash(self) -> str:
        return config_hash(asdict(self))

    @staticmethod
    def load(path: Path = CONFIG_PATH) -> "ChallengerConfig":
        from .forecast_features import USAGE_PRIOR_GAMES

        cfg = ChallengerConfig(**json.loads(Path(path).read_text()))
        if cfg.usage_prior_games != USAGE_PRIOR_GAMES:
            raise ValueError(f"challenger config expects USAGE_PRIOR_GAMES={cfg.usage_prior_games}, "
                             f"features compute {USAGE_PRIOR_GAMES}")
        return cfg


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]


def challenger_predictions(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                           col: str = "pred_ifp2") -> pl.DataFrame:
    """Walk-forward predictions of the frozen challenger (each position with its own spec)."""
    parts = []
    for pos in POSITIONS:
        spec = cfg.spec(pos)
        preds = walk_forward(store.filter(pl.col("position") == pos), weeks, [spec])
        if preds.height:
            parts.append(preds.select("key", "gsis_id", pl.col(f"pred_{spec.name}").alias(col)))
    return pl.concat(parts) if parts else pl.DataFrame(schema={"key": pl.Int64, "gsis_id": pl.String, col: pl.Float64})


def round_floats(obj, nd: int = 6):
    if isinstance(obj, float):
        return None if math.isnan(obj) else round(obj, nd)
    if isinstance(obj, dict):
        return {k: round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, nd) for v in obj]
    return obj


__all__ = ["CHALLENGER_VERSION", "WINDOWS", "Spec", "fit_linear", "walk_forward", "compare", "feature_set",
           "OPPORTUNITY", "PROMOTION_CRITERIA"]

"""Participation model: P(a rostered QB/RB/WR/TE appears in NFL week W), from pregame facts only.

The independent projection models (independent_projections) estimate league points CONDITIONAL
on the player appearing. Multiplying by P(appears) gives an unconditional expected score:

    expected_points = p_play * E[points | appears]

"Appears" uses the same definition as the conditional model's training rows (forecast_features
load_panel: an nflverse box-score row or offensive snaps > 0), so the product is consistent.

Population (one row per player-week; docs/independent_projection_engine.md "Participation"):
  * on the nflverse weekly roster for (S, W) with status ACT or INA (the game-day 53), position
    QB/RB/WR/TE (FB -> RB), team playing in week W, and >= 1 game in the conditional model's
    lookback window (so a conditional estimate exists);
  * label played = 1 if the player appears in week W, else 0 (then his league points are 0).

Leakage contract (tests/test_participation.py):
  * The week-W roster status is used ONLY to define the population. ACT vs INA is the game-day
    inactive decision itself (no INA player ever appears), so it is never a feature.
  * Box scores, snaps and roster statuses come only from weeks strictly before W.
  * The week-W injury report is used: it is published before the game, and rows modified at
    or after kickoff are dropped at load time (forecast_features.load_injuries).
  * Depth order is ranked from earlier games only; who is on the 53 in week W is pregame.

Model: per-position L2 logistic regression (Newton/IRLS, numpy) on standardized features with
training-mean imputation plus missing indicators, the same design rules as the ridge models.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import polars as pl

from .forecast_features import POSITIONS, SNAP_DROP, Sources, features_for_week, key
from .independent_projections import MIN_TRAIN_ROWS, _design_columns, design_matrix, fit_ridge, training_means

PARTICIPATION_VERSION = "ipm-1.0.0"
PARTICIPATION_SCHEMA = "participation-artifact/v1"
ACTIVE_ROSTER = ("ACT", "INA")
DESIGNATIONS = ("Out", "Doubtful", "Questionable")
TEAM_WINDOW = 4             # recent team games for the appearance rate
DEPTH_CAP = 3               # depth ranks >= 3 are pooled
LOGIT_LAMBDAS = (0.0001, 0.0003, 0.001, 0.003, 0.01, 0.03)
MIN_LOGIT_ROWS = 200
P_CLIP = 1e-4               # probabilities are clipped to [P_CLIP, 1 - P_CLIP] for log loss
RELIABILITY_BINS = 10

# Frozen BEFORE the first held-out participation run (same discipline as PROMOTION_CRITERIA).
PARTICIPATION_CRITERIA = {
    "frozen_before_heldout": True,
    "heldout_min_observations_per_position": 400,
    "brier_must_beat_best_baseline": True,
    "require_bootstrap_ci_upper_below_zero": True,   # 95% CI of Brier(model) - Brier(best baseline) < 0
    "bootstrap_resamples": 1000,
    "bootstrap_unit": "NFL week (all player-weeks of a week resampled together)",
    "max_expected_calibration_error": 0.02,           # ECE over 10 equal-width bins
    "max_bin_gap": 0.08,                              # |observed - mean predicted| in any bin with n >= bin_min_n
    "bin_min_n": 100,
    "unconditional_min_relative_mae_improvement": 0.03,   # expected points vs the conditional estimate as-is
    "unconditional_require_ci_upper_below_zero": True,
    "baselines": "always-plays (1.0); injury-designation rate; shrunk trailing team-game appearance rate",
}

# name -> (meaning, provenance). All pregame for week W.
FEATURE_DOCS = {
    "inj_out": ("1 if the week-W injury report designates the player Out", "nfl_injuries (week W, pregame)"),
    "inj_doubtful": ("1 if designated Doubtful in week W", "nfl_injuries (week W, pregame)"),
    "inj_questionable": ("1 if designated Questionable in week W", "nfl_injuries (week W, pregame)"),
    "prac_dnp": ("1 if the final week-W practice status is Did Not Participate", "nfl_injuries (week W, pregame)"),
    "prac_limited": ("1 if the final week-W practice status is Limited", "nfl_injuries (week W, pregame)"),
    "inj_out_prev": ("1 if designated Out for the team's previous game week (null before the team's first game)",
                     "nfl_injuries"),
    "played_last_team_game": ("1 if the player appeared in his week-W team's most recent game (null before it "
                              "has played)", "panel + nfl_schedules"),
    "appear_rate_team": (f"Appearances in the week-W team's last <={TEAM_WINDOW} game weeks / those weeks "
                         "(null before the team has played)", "panel + nfl_schedules"),
    "appear_rate_prev": ("Appearances in the previous season / 17 (null if not on any previous-season roster)",
                         "panel + nfl_rosters_weekly"),
    "prev_ina": ("1 if the latest roster status before week W is INA (game-day inactive last week)",
                 "nfl_rosters_weekly (weeks < W)"),
    "prev_res": ("1 if the latest roster status before week W is a reserve list (IR, PUP, NFI)",
                 "nfl_rosters_weekly (weeks < W)"),
    "prev_other": ("1 if the latest roster status before week W is practice squad, cut, or another non-53 status",
                   "nfl_rosters_weekly (weeks < W)"),
    "reserve_since_last_game": ("1 if a reserve status appears between the player's last game and week W "
                                "(returning from IR/PUP)", "nfl_rosters_weekly (weeks < W)"),
    "depth_2": ("1 if second at his position on the week-W team by recent snap share (then xFP, points)",
                "nfl_snap_counts (weeks < W) + week-W 53"),
    "depth_3plus": (f"1 if third or lower ({DEPTH_CAP}+) at his position on the team", "as depth_2"),
    "ahead_unavailable": ("1 if a teammate ranked ahead at the position is designated Out or Doubtful in week W",
                          "nfl_injuries (week W, pregame)"),
    "ahead_questionable": ("1 if a teammate ranked ahead is designated Questionable in week W",
                           "nfl_injuries (week W, pregame)"),
    "snap_pct": ("Mean offensive snap share over the last <=5 games with a snap record", "forecast_features"),
    "snap_last": ("Snap share in the most recent game", "forecast_features"),
    "snap_trend_role": ("Last-game snap change with injury exits removed", "forecast_features"),
    "injury_exit_last": ("1 if the last game was an injury exit (snap drop + injury report after it)",
                         "forecast_features"),
    "games_recent": ("Games in the recent window (0-5)", "forecast_features"),
    "games_season": ("Games this season before week W", "forecast_features"),
    "weeks_since_last": ("Weeks since the player's last game", "forecast_features"),
    "team_changed": ("1 if the week-W team differs from his last game's team", "forecast_features"),
    "xfp_recent": ("nflverse expected fantasy points per game (shrunk)", "forecast_features"),
}
FEATURES = list(FEATURE_DOCS)


# ----------------------------------------------------------------------------- population


def roster_population(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Week-W ACT/INA roster rows with the team's game. `played`/`label`/`actual_snap` are the
    outcome (label side only; never features)."""
    if sources.rosters is None:
        raise ValueError("participation needs nflverse weekly rosters (nfl_rosters_weekly)")
    r = sources.rosters.filter((pl.col("season") == season) & (pl.col("week") == week)
                               & pl.col("status").is_in(ACTIVE_ROSTER) & pl.col("position").is_in(POSITIONS))
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week)).select(
        "team", "opponent", "is_home")
    app = sources.panel.filter((pl.col("season") == season) & (pl.col("week") == week)).select(
        "gsis_id", "league_points", pl.col("offense_pct").alias("actual_snap"))
    return (r.select("gsis_id", "position", "team").join(games, on="team", how="inner")
            .join(app, on="gsis_id", how="left")
            .with_columns(pl.col("league_points").is_not_null().cast(pl.Int8).alias("played"),
                          pl.col("league_points").fill_null(0.0).alias("label"))
            .drop("league_points").unique("gsis_id", keep="first").sort("gsis_id"))


# ----------------------------------------------------------------------------- features


def _injury_flags(inj: pl.DataFrame) -> pl.DataFrame:
    ps = pl.col("practice_status").fill_null("").str.to_lowercase()
    return inj.select(
        "gsis_id",
        (pl.col("report_status") == "Out").cast(pl.Int8).alias("inj_out"),
        (pl.col("report_status") == "Doubtful").cast(pl.Int8).alias("inj_doubtful"),
        (pl.col("report_status") == "Questionable").cast(pl.Int8).alias("inj_questionable"),
        ps.str.contains("did not").cast(pl.Int8).alias("prac_dnp"),
        ps.str.contains("limited").cast(pl.Int8).alias("prac_limited"))


def participation_features(sources: Sources, season: int, week: int, base: pl.DataFrame) -> pl.DataFrame:
    """Add participation features to `base` (features_for_week output for week-W roster rows).
    Uses box scores, snaps and roster statuses from weeks < W and the week-W injury report."""
    cutoff = key(season, week)
    ids = base.select("gsis_id", "position", "team", "last_season", "last_week")
    flag_cols = ["inj_out", "inj_doubtful", "inj_questionable", "prac_dnp", "prac_limited"]

    # --- week-W injury report (pregame). Unlisted = 0 when the report exists; null without reports.
    if sources.injuries is not None:
        inj = sources.injuries
        now = _injury_flags(inj.filter((pl.col("season") == season) & (pl.col("week") == week)))
        out = ids.join(now, on="gsis_id", how="left").with_columns([pl.col(c).fill_null(0) for c in flag_cols])
    else:
        out = ids.with_columns([pl.lit(None, dtype=pl.Int8).alias(c) for c in flag_cols])

    # --- the target team's completed game weeks this season before W
    tg = (sources.games.filter((pl.col("season") == season) & (pl.col("week") < week) & pl.col("points_for").is_not_null())
          .select("team", "week").sort("week", descending=True))
    last_tg = tg.group_by("team").agg(pl.col("week").first().alias("_team_last_week"))
    window = tg.group_by("team", maintain_order=True).head(TEAM_WINDOW)
    appeared = sources.panel.filter((pl.col("season") == season) & (pl.col("week") < week)).select(
        "gsis_id", "week").with_columns(pl.lit(1).alias("_app"))
    rate = (ids.select("gsis_id", "team").join(window, on="team", how="inner")
            .join(appeared, on=["gsis_id", "week"], how="left")
            .group_by("gsis_id").agg(pl.col("_app").fill_null(0).mean().alias("appear_rate_team")))
    played_last = (ids.select("gsis_id", "team").join(last_tg, on="team", how="inner")
                   .join(appeared.rename({"week": "_team_last_week"}), on=["gsis_id", "_team_last_week"], how="left")
                   .select("gsis_id", "_team_last_week", pl.col("_app").fill_null(0).cast(pl.Int8).alias("played_last_team_game")))
    out = out.join(rate, on="gsis_id", how="left").join(played_last, on="gsis_id", how="left")

    if sources.injuries is not None:
        prev = sources.injuries.filter(pl.col("season") == season).select(
            "gsis_id", pl.col("week").alias("_team_last_week"), (pl.col("report_status") == "Out").cast(pl.Int8).alias("_po"))
        out = out.join(prev, on=["gsis_id", "_team_last_week"], how="left").with_columns(
            pl.when(pl.col("_team_last_week").is_null()).then(None).otherwise(pl.col("_po").fill_null(0))
              .cast(pl.Int8).alias("inj_out_prev"))
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Int8).alias("inj_out_prev"))

    # --- previous season appearance rate and roster history (weeks < W only)
    prev_apps = (sources.panel.filter(pl.col("season") == season - 1).group_by("gsis_id")
                 .agg((pl.len() / 17).clip(upper_bound=1.0).alias("_prev_rate")))
    out = out.join(prev_apps, on="gsis_id", how="left")
    if sources.rosters is not None:
        ros = sources.rosters.filter(pl.col("season") * 100 + pl.col("week") < cutoff)
        on_prev = ros.filter(pl.col("season") == season - 1).select("gsis_id").unique().with_columns(
            pl.lit(True).alias("_on_prev"))
        latest = (ros.filter(pl.col("season") >= season - 1).sort(["season", "week"], descending=True)
                  .group_by("gsis_id", maintain_order=True).first().select("gsis_id", pl.col("status").alias("_ps")))
        res = (ros.filter(pl.col("status") == "RES").select("gsis_id", (pl.col("season") * 100 + pl.col("week")).alias("_rk"))
               .join(ids.select("gsis_id", "last_season", "last_week"), on="gsis_id", how="inner")
               .filter(pl.col("_rk") > pl.col("last_season") * 100 + pl.col("last_week"))
               .select("gsis_id").unique().with_columns(pl.lit(1, dtype=pl.Int8).alias("reserve_since_last_game")))
        out = (out.join(on_prev, on="gsis_id", how="left").join(latest, on="gsis_id", how="left")
               .join(res, on="gsis_id", how="left")
               .with_columns(
                   pl.when(pl.col("_on_prev").fill_null(False)).then(pl.col("_prev_rate").fill_null(0.0))
                     .otherwise(None).alias("appear_rate_prev"),
                   pl.col("reserve_since_last_game").fill_null(0),
                   *[pl.when(pl.col("_ps").is_null()).then(None).otherwise(cond).cast(pl.Int8).alias(name)
                     for name, cond in (("prev_ina", pl.col("_ps") == "INA"), ("prev_res", pl.col("_ps") == "RES"),
                                        ("prev_other", ~pl.col("_ps").is_in(["ACT", "INA", "RES"])))]))
    else:
        out = out.with_columns(pl.col("_prev_rate").alias("appear_rate_prev"),
                               *[pl.lit(None, dtype=pl.Int8).alias(c) for c in
                                 ("prev_ina", "prev_res", "prev_other", "reserve_since_last_game")])

    feats = base.join(out.select("gsis_id", *flag_cols, "inj_out_prev", "played_last_team_game", "appear_rate_team",
                                 "appear_rate_prev", "prev_ina", "prev_res", "prev_other", "reserve_since_last_game"),
                      on="gsis_id", how="left")
    return _depth(feats)


def _depth(feats: pl.DataFrame) -> pl.DataFrame:
    """Rank each player within (team, position) of the week-W 53 by recent snap share, then xFP,
    then points (all from earlier games), then id for determinism; flag designated teammates ahead."""
    ranked = feats.sort(["team", "position", "snap_pct", "xfp_recent", "pts_recent", "gsis_id"],
                        descending=[False, False, True, True, True, False], nulls_last=True).with_columns(
        pl.int_range(1, pl.len() + 1).over(["team", "position"]).alias("depth_rank"))
    unavailable = ((pl.col("inj_out") == 1) | (pl.col("inj_doubtful") == 1)).fill_null(False).cast(pl.Int32)
    q = (pl.col("inj_questionable") == 1).fill_null(False).cast(pl.Int32)
    ranked = ranked.with_columns(
        (unavailable.cum_sum().over(["team", "position"]) - unavailable).alias("_ahead_unav"),
        (q.cum_sum().over(["team", "position"]) - q).alias("_ahead_q"))
    has_report = pl.col("inj_out").is_not_null()
    return ranked.with_columns(
        (pl.col("depth_rank") == 2).cast(pl.Int8).alias("depth_2"),
        (pl.col("depth_rank") >= DEPTH_CAP).cast(pl.Int8).alias("depth_3plus"),
        pl.when(has_report).then((pl.col("_ahead_unav") > 0).cast(pl.Int8)).otherwise(None).alias("ahead_unavailable"),
        pl.when(has_report).then((pl.col("_ahead_q") > 0).cast(pl.Int8)).otherwise(None).alias("ahead_questionable"),
    ).drop("_ahead_unav", "_ahead_q").sort("gsis_id")


def week_rows(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Population + conditional-model features + participation features + outcome for one week.
    Rows without a lookback game (no conditional estimate) are excluded."""
    pop = roster_population(sources, season, week)
    if pop.height == 0:
        return pl.DataFrame()
    base = features_for_week(sources, season, week, pop.select("gsis_id", "position", "team", "opponent", "is_home"))
    base = base.filter(pl.col("games_recent") > 0)
    if base.height == 0:
        return pl.DataFrame()
    feats = participation_features(sources, season, week, base)
    return feats.join(pop.select("gsis_id", "played", "label", "actual_snap"), on="gsis_id", how="left").with_columns(
        pl.lit(key(season, week)).alias("key"))


def build_store(sources: Sources, weeks: list[tuple[int, int]]) -> pl.DataFrame:
    frames = [f for s, w in weeks if (f := week_rows(sources, s, w)).height]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


# ----------------------------------------------------------------------------- logistic model


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -35.0, 35.0)))


@dataclass
class LogitModel:
    position: str
    features: list[str]
    columns: list[str]
    means: list[float]
    stds: list[float]
    coef: list[float]
    intercept: float
    lam: float
    n_train: int
    train_window: list[int] = field(default_factory=list)
    converged: bool = True

    def logits(self, df: pl.DataFrame) -> np.ndarray:
        x, _ = design_matrix(self.columns, self.means, df)
        z = (x - np.array(self.means)) / np.array(self.stds)
        return z @ np.array(self.coef) + self.intercept

    def predict(self, df: pl.DataFrame) -> np.ndarray:
        return _sigmoid(self.logits(df))

    def contributions(self, df: pl.DataFrame) -> np.ndarray:
        """Per-column contribution to the logit relative to the training-average player."""
        x, _ = design_matrix(self.columns, self.means, df)
        return (x - np.array(self.means)) / np.array(self.stds) * np.array(self.coef)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LogitModel":
        return LogitModel(**d)


def fit_logit(train: pl.DataFrame, position: str, lams, label: str = "played",
              features: list[str] | None = None) -> dict[float, LogitModel]:
    """L2 logistic regression by Newton steps; penalty lam * n on standardized coefficients,
    intercept unpenalized. Deterministic for a given training frame."""
    features = features or FEATURES
    columns = _design_columns(train, features)
    means = training_means(train, columns)
    x, _ = design_matrix(columns, means, train)
    stds = x.std(axis=0)
    stds[stds < 1e-9] = 1.0
    z = np.column_stack([np.ones(len(x)), (x - np.array(means)) / stds])
    y = train[label].cast(pl.Float64).to_numpy()
    n, k = z.shape
    keys = train["key"] if "key" in train.columns else train.select((pl.col("season") * 100 + pl.col("week")).alias("k"))["k"]
    window = [int(keys.min()), int(keys.max())] if n else []
    out = {}
    for lam in lams:
        pen = np.full(k, lam * n)
        pen[0] = 0.0
        beta = np.zeros(k)
        base = min(max(y.mean(), 1e-3), 1 - 1e-3)
        beta[0] = math.log(base / (1 - base))
        converged = False
        for _ in range(100):
            p = _sigmoid(z @ beta)
            grad = z.T @ (y - p) - pen * beta
            w = np.clip(p * (1 - p), 1e-9, None)
            hess = (z * w[:, None]).T @ z + np.diag(pen) + 1e-9 * np.eye(k)
            step = np.linalg.solve(hess, grad)
            beta += step
            if np.max(np.abs(step)) < 1e-8:
                converged = True
                break
        out[lam] = LogitModel(position, list(features), columns, means, stds.tolist(), beta[1:].tolist(),
                              float(beta[0]), float(lam), n, window, converged)
    return out


# ----------------------------------------------------------------------------- baselines


def baseline_probabilities(train: pl.DataFrame, test: pl.DataFrame) -> pl.DataFrame:
    """Two simple pregame baselines fitted on `train` (same position, earlier weeks):
    p_status   - training appearance rate for the player's week-W designation (Out / Doubtful /
                 Questionable / none), Laplace-smoothed;
    p_trail    - appearance rate in the team's last games shrunk one game toward the training
                 base rate (base rate when the team has not played yet)."""
    cat = (pl.when(pl.col("inj_out") == 1).then(pl.lit("Out")).when(pl.col("inj_doubtful") == 1).then(pl.lit("Doubtful"))
           .when(pl.col("inj_questionable") == 1).then(pl.lit("Questionable")).otherwise(pl.lit("none")))
    base = float(train["played"].mean()) if train.height else 0.5
    rates = (train.with_columns(cat.alias("_cat")).group_by("_cat")
             .agg(((pl.col("played").sum() + base) / (pl.len() + 1)).alias("p_status")))
    n_team = pl.col("_n_team")
    return (test.with_columns(cat.alias("_cat")).join(rates, on="_cat", how="left")
            .with_columns(pl.col("p_status").fill_null(base),
                          pl.min_horizontal(pl.lit(TEAM_WINDOW), pl.col("team_games")).cast(pl.Float64).alias("_n_team"))
            .with_columns(pl.when(pl.col("appear_rate_team").is_null()).then(pl.lit(base))
                          .otherwise((pl.col("appear_rate_team") * n_team + base) / (n_team + 1)).alias("p_trail"))
            .drop("_cat", "_n_team"))


# ----------------------------------------------------------------------------- walk-forward


def walk_forward(store: pl.DataFrame, cond_store: pl.DataFrame, test_weeks: list[tuple[int, int]],
                 lams: dict[str, list[float]], cond_spec: dict[str, tuple[str, float]], train_start: int) -> pl.DataFrame:
    """For every test week: refit each position's logistic model (all `lams`) and baselines on
    participation rows with key < k, refit the conditional ridge model `cond_spec[pos] = (model,
    lambda)` on appearance rows with key < k, and predict the week's roster rows.
    Columns: p_<lam>, p_status, p_trail, cond (E[points | appears])."""
    out = []
    for season, week in test_weeks:
        k = key(season, week)
        test = store.filter(pl.col("key") == k)
        for pos in POSITIONS:
            rows = test.filter(pl.col("position") == pos)
            if rows.height == 0:
                continue
            train = store.filter((pl.col("key") < k) & (pl.col("season") >= train_start) & (pl.col("position") == pos))
            assert train.height == 0 or train["key"].max() < k, "participation training row at or after the test week"
            rows = baseline_probabilities(train, rows) if train.height else rows.with_columns(
                pl.lit(None, dtype=pl.Float64).alias("p_status"), pl.lit(None, dtype=pl.Float64).alias("p_trail"))
            fitted = fit_logit(train, pos, lams[pos]) if train.height >= MIN_LOGIT_ROWS else {}
            cols = [pl.Series(f"p_{lam:g}", fitted[lam].predict(rows) if lam in fitted else [None] * rows.height,
                              dtype=pl.Float64) for lam in lams[pos]]
            ctrain = cond_store.filter((pl.col("key") < k) & (pl.col("season") >= train_start)
                                       & (pl.col("position") == pos) & (pl.col("games_recent") > 0)
                                       & pl.col("label").is_not_null())
            assert ctrain.height == 0 or ctrain["key"].max() < k, "conditional training row at or after the test week"
            model, lam = cond_spec[pos]
            cond = (fit_ridge(ctrain, pos, model, [lam])[lam].predict(rows) if ctrain.height >= MIN_TRAIN_ROWS
                    else [None] * rows.height)
            out.append(rows.with_columns(*cols, pl.Series("cond", cond, dtype=pl.Float64)))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- metrics


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(p, P_CLIP, 1 - P_CLIP)


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = _clip(p)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(y: np.ndarray, p: np.ndarray, bins: int = RELIABILITY_BINS, min_n: int | None = None) -> dict:
    """Equal-width probability bins: count, mean predicted, observed rate. ECE = n-weighted mean
    |observed - predicted|; max_gap over bins with n >= min_n."""
    min_n = PARTICIPATION_CRITERIA["bin_min_n"] if min_n is None else min_n
    idx = np.minimum((p * bins).astype(int), bins - 1)
    table, ece, gap = [], 0.0, 0.0
    for b in range(bins):
        m = idx == b
        n = int(m.sum())
        if n == 0:
            table.append({"bin": [b / bins, (b + 1) / bins], "n": 0, "mean_predicted": None, "observed": None})
            continue
        mp, ob = float(p[m].mean()), float(y[m].mean())
        ece += n * abs(ob - mp)
        if n >= min_n:
            gap = max(gap, abs(ob - mp))
        table.append({"bin": [round(b / bins, 2), round((b + 1) / bins, 2)], "n": n,
                      "mean_predicted": round(mp, 4), "observed": round(ob, 4)})
    return {"bins": table, "ece": round(ece / len(y), 4) if len(y) else None,
            "max_gap_bins_n_ge_min": round(gap, 4), "bin_min_n": min_n}


def prob_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    if len(y) == 0:
        return {"n": 0, "brier": None, "log_loss": None, "mean_predicted": None, "observed": None}
    return {"n": int(len(y)), "brier": round(brier(y, p), 5), "log_loss": round(log_loss(y, p), 5),
            "mean_predicted": round(float(p.mean()), 4), "observed": round(float(y.mean()), 4)}


def point_metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    err = actual - pred
    return {"n": int(len(actual)), "mae": round(float(np.abs(err).mean()), 3),
            "rmse": round(float(math.sqrt((err ** 2).mean())), 3), "bias": round(float(err.mean()), 3)}


def week_bootstrap(df: pl.DataFrame, loss_a: str, loss_b: str, resamples: int, seed: int) -> dict:
    """95% CI of mean(loss_a) - mean(loss_b), resampling whole NFL weeks."""
    if df.height == 0:
        return {"diff": None, "ci95": None}
    by = df.group_by("key").agg(pl.col(loss_a).sum().alias("a"), pl.col(loss_b).sum().alias("b"),
                                pl.len().alias("n")).sort("key")
    a, b, n = by["a"].to_numpy(), by["b"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (a[idx].sum(1) - b[idx].sum(1)) / n[idx].sum(1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": round(float((a.sum() - b.sum()) / n.sum()), 5), "ci95": [round(float(lo), 5), round(float(hi), 5)]}


# ----------------------------------------------------------------------------- selection / held-out


def _ifp_cond_spec(ifp_selection: dict) -> dict[str, tuple[str, float]]:
    return {p: (s["primary"], s["lambda"][s["primary"]]) for p, s in ifp_selection["positions"].items()}


def _weeks(store: pl.DataFrame, seasons) -> list[tuple[int, int]]:
    return sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season").is_in(list(seasons)))
                   .select("season", "week").unique().iter_rows()})


def select(store: pl.DataFrame, cond_store: pl.DataFrame, ifp_selection: dict, protocol) -> dict:
    """Choose each position's logistic penalty by validation log loss (validation seasons only)."""
    protocol.check()
    weeks = _weeks(store, protocol.validation_seasons)
    lams = {p: list(LOGIT_LAMBDAS) for p in POSITIONS}
    preds = walk_forward(store, cond_store, weeks, lams, _ifp_cond_spec(ifp_selection), protocol.train_start_season)
    chosen, grid = {}, {}
    for pos in POSITIONS:
        rows = preds.filter(pl.col("position") == pos)
        rows = rows.filter(pl.all_horizontal([pl.col(f"p_{lam:g}").is_not_null() for lam in LOGIT_LAMBDAS]))
        y = rows["played"].cast(pl.Float64).to_numpy()
        scores = {lam: log_loss(y, rows[f"p_{lam:g}"].to_numpy()) for lam in LOGIT_LAMBDAS} if rows.height else {}
        grid[pos] = {f"{lam:g}": round(v, 5) for lam, v in scores.items()}
        best = min(LOGIT_LAMBDAS, key=lambda lam: (scores.get(lam, 1e9), -lam))
        chosen[pos] = {"lambda": best, "validation_n": rows.height,
                       "validation": prob_metrics(y, rows[f"p_{best:g}"].to_numpy()) if rows.height else {},
                       "validation_reliability": reliability(y, rows[f"p_{best:g}"].to_numpy()) if rows.height else {},
                       "validation_baselines": {b: prob_metrics(y, rows[b].to_numpy()) for b in ("p_status", "p_trail")}
                       if rows.height else {}}
    return {"participation_version": PARTICIPATION_VERSION, "protocol": asdict(protocol),
            "validation_weeks": len(weeks), "positions": chosen, "grid_log_loss": grid,
            "conditional_model": {p: {"model": m, "lambda": lam} for p, (m, lam) in _ifp_cond_spec(ifp_selection).items()},
            "criteria": PARTICIPATION_CRITERIA}


def evaluate(store: pl.DataFrame, cond_store: pl.DataFrame, selection: dict, ifp_selection: dict, protocol) -> dict:
    """Frozen participation selection -> held-out seasons; calibration, unconditional points and
    statuses per PARTICIPATION_CRITERIA. Aggregates only."""
    protocol.check()
    if tuple(selection["protocol"]["validation_seasons"]) != tuple(protocol.validation_seasons):
        raise ValueError("participation selection was made on different validation seasons")
    if {p: (v["model"], v["lambda"]) for p, v in selection["conditional_model"].items()} != _ifp_cond_spec(ifp_selection):
        raise ValueError("participation selection was made with a different conditional-model selection")
    weeks = _weeks(store, protocol.heldout_seasons)
    lams = {p: [selection["positions"][p]["lambda"]] for p in POSITIONS}
    preds = walk_forward(store, cond_store, weeks, lams, _ifp_cond_spec(ifp_selection), protocol.train_start_season)
    if preds.height == 0:
        return {"status": "insufficient_data", "positions": {}}
    preds = preds.with_columns(pl.coalesce([pl.when(pl.col("position") == p).then(pl.col(f"p_{selection['positions'][p]['lambda']:g}"))
                                            for p in POSITIONS]).alias("p_model"))
    report = {"participation_version": PARTICIPATION_VERSION, "heldout_seasons": list(protocol.heldout_seasons),
              "heldout_weeks": len(weeks), "positions": {}, "status": {}, "criteria": PARTICIPATION_CRITERIA}
    for pos in POSITIONS:
        rows = preds.filter((pl.col("position") == pos) & pl.col("p_model").is_not_null() & pl.col("p_status").is_not_null()
                            & pl.col("cond").is_not_null())
        rep = position_report(rows, protocol.seed)
        report["positions"][pos] = rep
        report["status"][pos] = status_for(rep)
    report["all_positions"] = position_report(
        preds.filter(pl.col("p_model").is_not_null() & pl.col("p_status").is_not_null() & pl.col("cond").is_not_null()),
        protocol.seed, detail=False)
    return report


def position_report(rows: pl.DataFrame, seed: int, detail: bool = True) -> dict:
    c = PARTICIPATION_CRITERIA
    if rows.height == 0:
        return {"n": 0}
    y = rows["played"].cast(pl.Float64).to_numpy()
    probs = {"always_plays": np.ones(len(y)), "designation_rate": rows["p_status"].to_numpy(),
             "trailing_appearance": rows["p_trail"].to_numpy(), "model": rows["p_model"].to_numpy()}
    pm = {k: prob_metrics(y, v) for k, v in probs.items()}
    best_base = min(("designation_rate", "trailing_appearance"), key=lambda k: pm[k]["brier"])
    base_col = {"designation_rate": "p_status", "trailing_appearance": "p_trail"}[best_base]
    d = rows.with_columns(((pl.col("p_model") - pl.col("played")) ** 2).alias("_bm"),
                          ((pl.col(base_col) - pl.col("played")) ** 2).alias("_bb"))
    boot = week_bootstrap(d, "_bm", "_bb", c["bootstrap_resamples"], seed)
    # unconditional expected points: label = points if appeared else 0
    actual = rows["label"].to_numpy()
    cond = rows["cond"].to_numpy()
    out_rule = rows["inj_out"].fill_null(0).to_numpy() == 1
    u = {"conditional_as_is": cond, "out_rule": np.where(out_rule, 0.0, cond),
         "best_baseline_x_conditional": rows[base_col].to_numpy() * cond, "expected_points": probs["model"] * cond}
    um = {k: point_metrics(actual, v) for k, v in u.items()}
    d = rows.with_columns(pl.Series("_ue", np.abs(actual - u["expected_points"])),
                          pl.Series("_uc", np.abs(actual - cond)), pl.Series("_ur", np.abs(actual - u["out_rule"])))
    rel = lambda a, b: round((um[b]["mae"] - um[a]["mae"]) / um[b]["mae"], 4) if um[b]["mae"] else None  # noqa: E731
    rep = {"n": rows.height, "played_rate": round(float(y.mean()), 4), "probability": pm, "best_baseline": best_base,
           "model_minus_best_baseline_brier": boot,
           "reliability": reliability(y, probs["model"]),
           "unconditional_points": um,
           "expected_minus_conditional_mae": week_bootstrap(d, "_ue", "_uc", c["bootstrap_resamples"], seed),
           "expected_minus_out_rule_mae": week_bootstrap(d, "_ue", "_ur", c["bootstrap_resamples"], seed),
           "relative_mae_improvement_vs_conditional": rel("expected_points", "conditional_as_is"),
           "relative_mae_improvement_vs_out_rule": rel("expected_points", "out_rule")}
    if detail:
        rep["by_season"] = {int(s): {"n": g.height, "model": prob_metrics(g["played"].cast(pl.Float64).to_numpy(), g["p_model"].to_numpy()),
                                     "expected_points": point_metrics(g["label"].to_numpy(), (g["p_model"] * g["cond"]).to_numpy()),
                                     "conditional_as_is": point_metrics(g["label"].to_numpy(), g["cond"].to_numpy())}
                            for (s,), g in rows.group_by(["season"], maintain_order=True)}
        rep["cohorts"] = {name: cohort(rows.filter(expr)) for name, expr in COHORTS.items()}
    return rep


COHORTS = {
    "designated_out_or_doubtful": (pl.col("inj_out") == 1) | (pl.col("inj_doubtful") == 1),
    "designated_questionable": pl.col("inj_questionable") == 1,
    "no_designation": (pl.col("inj_out") == 0) & (pl.col("inj_doubtful") == 0) & (pl.col("inj_questionable") == 0),
    "depth_1": (pl.col("depth_2") == 0) & (pl.col("depth_3plus") == 0),
    "depth_2_plus": (pl.col("depth_2") == 1) | (pl.col("depth_3plus") == 1),
    "depth_2_plus_teammate_ahead_out": ((pl.col("depth_2") == 1) | (pl.col("depth_3plus") == 1)) & (pl.col("ahead_unavailable") == 1),
    "injury_exit_last_game": pl.col("injury_exit_last") == 1,
    "returning_from_reserve": pl.col("reserve_since_last_game") == 1,
}


def cohort(g: pl.DataFrame) -> dict:
    if g.height == 0:
        return {"n": 0}
    y = g["played"].cast(pl.Float64).to_numpy()
    return {"n": g.height, "played_rate": round(float(y.mean()), 4),
            "model": prob_metrics(y, g["p_model"].to_numpy()),
            "designation_rate": prob_metrics(y, g["p_status"].to_numpy()),
            "trailing_appearance": prob_metrics(y, g["p_trail"].to_numpy()),
            "expected_points": point_metrics(g["label"].to_numpy(), (g["p_model"] * g["cond"]).to_numpy()),
            "conditional_as_is": point_metrics(g["label"].to_numpy(), g["cond"].to_numpy())}


def status_for(rep: dict) -> dict:
    c = PARTICIPATION_CRITERIA
    if rep.get("n", 0) < c["heldout_min_observations_per_position"]:
        return {"participation": "insufficient_data", "expected_points": "insufficient_data",
                "reasons": [f"{rep.get('n', 0)} held-out observations < {c['heldout_min_observations_per_position']}"]}
    reasons = []
    pm = rep["probability"]
    if not pm["model"]["brier"] < pm[rep["best_baseline"]]["brier"]:
        reasons.append(f"Brier {pm['model']['brier']} not below best baseline ({rep['best_baseline']}) "
                       f"{pm[rep['best_baseline']]['brier']}")
    ci = rep["model_minus_best_baseline_brier"]["ci95"]
    if not (ci and ci[1] < 0):
        reasons.append(f"bootstrap 95% CI of the Brier difference {ci} does not exclude 0")
    rel = rep["reliability"]
    if rel["ece"] is None or rel["ece"] > c["max_expected_calibration_error"]:
        reasons.append(f"ECE {rel['ece']} > {c['max_expected_calibration_error']}")
    if rel["max_gap_bins_n_ge_min"] > c["max_bin_gap"]:
        reasons.append(f"a bin with n >= {c['bin_min_n']} is off by {rel['max_gap_bins_n_ge_min']} > {c['max_bin_gap']}")
    participation = "validated" if not reasons else "experimental"
    ureasons = []
    imp = rep["relative_mae_improvement_vs_conditional"]
    if imp is None or imp < c["unconditional_min_relative_mae_improvement"]:
        ureasons.append(f"unconditional MAE improvement vs the conditional estimate {imp} < "
                        f"{c['unconditional_min_relative_mae_improvement']}")
    uci = rep["expected_minus_conditional_mae"]["ci95"]
    if not (uci and uci[1] < 0):
        ureasons.append(f"bootstrap 95% CI of the unconditional MAE difference {uci} does not exclude 0")
    expected = "validated" if participation == "validated" and not ureasons else "experimental"
    return {"participation": participation, "expected_points": expected, "reasons": reasons + ureasons,
            "validated_for": ("held-out calibration and Brier vs simple pregame baselines; expected points "
                              "validated vs the conditional estimate on the roster population; NOT vs ESPN")
            if expected == "validated" else None}


# ----------------------------------------------------------------------------- snap-drop diagnostic


def snap_drop_diagnostic(store: pl.DataFrame) -> dict:
    """Does injury_exit_last separate injury exits from role demotions? For last-game snap drops,
    compare the next game: appearance rate and, when he appears, snap share relative to the
    pre-drop level (last snap share minus snap_trend). Outcome columns are used only here."""
    d = store.filter(pl.col("injury_exit_last").is_not_null() & (pl.col("snap_trend") <= -SNAP_DROP))
    out = {}
    for name, g in (("injury_exit", d.filter(pl.col("injury_exit_last") == 1)),
                    ("role_drop", d.filter(pl.col("injury_exit_last") == 0))):
        played = g.filter((pl.col("played") == 1) & pl.col("actual_snap").is_not_null())
        pre = played["snap_last"] - played["snap_trend"]
        out[name] = {"n": g.height, "played_next_rate": round(float(g["played"].mean()), 4) if g.height else None,
                     "next_snap_minus_last": round(float((played["actual_snap"] - played["snap_last"]).mean()), 4)
                     if played.height else None,
                     "next_snap_minus_pre_drop_mean": round(float((played["actual_snap"] - pre).mean()), 4)
                     if played.height else None,
                     "next_snap_recovered_to_within_0.10_of_pre_drop": round(float(((played["actual_snap"] - pre) >= -0.10).mean()), 4)
                     if played.height else None}
    return out


# ----------------------------------------------------------------------------- final fit / inference


@dataclass
class ParticipationArtifact:
    version: str
    created_at: str
    config: dict
    models: dict[str, LogitModel]
    status: dict[str, dict]
    evaluation: dict
    schema: str = PARTICIPATION_SCHEMA

    def to_dict(self) -> dict:
        d = asdict(self)
        d["models"] = {p: m.to_dict() for p, m in self.models.items()}
        return d

    @staticmethod
    def from_dict(d: dict) -> "ParticipationArtifact":
        if d.get("schema") != PARTICIPATION_SCHEMA:
            raise ValueError(f"unsupported participation artifact schema {d.get('schema')!r}")
        d = dict(d)
        d["models"] = {p: LogitModel.from_dict(m) for p, m in d["models"].items()}
        return ParticipationArtifact(**d)


def fit_final(store: pl.DataFrame, selection: dict, evaluation: dict, created_at: str,
              train_start: int) -> ParticipationArtifact:
    """Each position's logistic model on every labeled row with the FROZEN penalty; status from
    the held-out evaluation. Nothing is re-tuned."""
    models = {}
    for pos in POSITIONS:
        rows = store.filter((pl.col("position") == pos) & (pl.col("season") >= train_start))
        if rows.height < MIN_LOGIT_ROWS:
            continue
        lam = selection["positions"][pos]["lambda"]
        models[pos] = fit_logit(rows, pos, [lam])[lam]
    status = {p: evaluation.get("status", {}).get(p) or {"participation": "insufficient_data",
                                                         "expected_points": "insufficient_data", "reasons": ["not evaluated"]}
              for p in POSITIONS}
    keep = ("n", "played_rate", "probability", "best_baseline", "model_minus_best_baseline_brier",
            "unconditional_points", "relative_mae_improvement_vs_conditional", "relative_mae_improvement_vs_out_rule")
    summary = {p: {k: v for k, v in rep.items() if k in keep} for p, rep in evaluation.get("positions", {}).items()}
    for p, rep in evaluation.get("positions", {}).items():
        if "reliability" in rep:
            summary[p]["ece"] = rep["reliability"]["ece"]
    summary["heldout_seasons"] = evaluation.get("heldout_seasons")
    return ParticipationArtifact(PARTICIPATION_VERSION, created_at,
                                 {"lambda": {p: s["lambda"] for p, s in selection["positions"].items()},
                                  "conditional_model": selection["conditional_model"], "train_start_season": train_start},
                                 models, status, summary)


PARTICIPATION_LABELS = {
    "inj_out": "designated Out", "inj_doubtful": "designated Doubtful", "inj_questionable": "designated Questionable",
    "prac_dnp": "did not practice (final report)", "prac_limited": "limited in practice (final report)",
    "inj_out_prev": "Out the previous week", "played_last_team_game": "played in the team's last game",
    "appear_rate_team": "share of recent team games played", "appear_rate_prev": "share of last season's games played",
    "prev_ina": "inactive last week", "prev_res": "on a reserve list last week", "prev_other": "off the 53 last week",
    "reserve_since_last_game": "returning from a reserve list", "depth_2": "second on the depth order",
    "depth_3plus": "third or lower on the depth order", "ahead_unavailable": "a teammate ahead is Out/Doubtful",
    "ahead_questionable": "a teammate ahead is Questionable", "snap_pct": "recent snap share",
    "snap_last": "last-game snap share", "snap_trend_role": "role change in snap share last game",
    "injury_exit_last": "left the last game injured", "games_recent": "games in recent window",
    "games_season": "games this season", "weeks_since_last": "weeks since last game",
    "team_changed": "changed NFL team", "xfp_recent": "expected fantasy points per game",
}


def explain(model: LogitModel, row: pl.DataFrame, top: int = 3) -> list[dict]:
    """Largest logit contributions for one row, relative to the training-average player."""
    contrib = model.contributions(row)[0]
    items = []
    for i, col in enumerate(model.columns):
        c = float(contrib[i])
        if abs(c) < 0.1:
            continue
        base = col[: -len("__missing")] if col.endswith("__missing") else col
        label = PARTICIPATION_LABELS.get(base, base)
        val = row[base][0] if base in row.columns else None
        text = f"{label} unknown" if col.endswith("__missing") else f"{label}: {val if val is not None else 'unknown'}"
        items.append({"feature": col, "logit_contribution": round(c, 2), "text": text,
                      "direction": "raises" if c > 0 else "lowers"})
    items.sort(key=lambda d: (-abs(d["logit_contribution"]), d["feature"]))
    return items[:top]


# ----------------------------------------------------------------------------- live week


def live_population(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Live counterpart of roster_population: each player's LATEST weekly roster row of the season
    with week <= W (the week-W roster may not be posted yet), status ACT/INA, team playing in W."""
    if sources.rosters is None:
        return pl.DataFrame()
    r = (sources.rosters.filter((pl.col("season") == season) & (pl.col("week") <= week))
         .sort("week", descending=True).group_by("gsis_id", maintain_order=True).first()
         .filter(pl.col("status").is_in(ACTIVE_ROSTER) & pl.col("position").is_in(POSITIONS)))
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week)).select(
        "team", "opponent", "is_home")
    return r.select("gsis_id", "position", "team").join(games, on="team", how="inner").sort("gsis_id")


def predict_week(sources: Sources, artifact: ParticipationArtifact, season: int, week: int,
                 fallback_teams: frozenset[str] | set[str] = frozenset()) -> dict[str, dict]:
    """P(appears) for every live-population player with a lookback game. Never trains.
    `fallback_teams`: teams whose week-W designations in `sources.injuries` come from a live
    fallback source rather than the nflverse report (flagged on every affected row)."""
    pop = live_population(sources, season, week)
    if pop.height == 0:
        return {}
    base = features_for_week(sources, season, week, pop).filter(pl.col("games_recent") > 0)
    if base.height == 0:
        return {}
    feats = participation_features(sources, season, week, base)
    published: set[str] = set()
    if sources.injuries is not None:
        published = set(sources.injuries.filter((pl.col("season") == season) & (pl.col("week") == week)
                                                & pl.col("report_status").is_not_null())["team"].unique().to_list())
        published -= set(fallback_teams)
    out = {}
    for pos in POSITIONS:
        model = artifact.models.get(pos)
        rows = feats.filter(pl.col("position") == pos)
        if model is None or rows.height == 0:
            continue
        probs = model.predict(rows)
        st = artifact.status.get(pos, {})
        for i, rec in enumerate(rows.iter_rows(named=True)):
            flags = []
            if rec["team"] in fallback_teams:
                flags.append(f"week-{week} designations for {rec['team']} come from ESPN injury status (the nflverse "
                             "report the model was trained on is not synced yet); practice status unknown")
            elif rec["team"] not in published:
                flags.append(f"no week-{week} game-status designations synced for {rec['team']} (not yet published, "
                             "or none issued): P(plays) treats him as undesignated")
            out[rec["gsis_id"]] = {
                "p_play": round(float(probs[i]), 4), "model_version": artifact.version,
                "status": st.get("participation", "insufficient_data"),
                "expected_points_status": st.get("expected_points", "insufficient_data"),
                "depth_rank": int(rec["depth_rank"]), "explanation": explain(model, rows.slice(i, 1)), "flags": flags}
    return out

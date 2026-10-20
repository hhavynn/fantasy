"""Kicker weekly fantasy-point model (kfp), trained on nflverse kicking and drive data only.

ESPN projections are never an input. The production model is an OPPORTUNITY x CONVERSION model:

    E[points | kicks] = lambda_FG * sum_d P(d) * E[pts | FG at d]  +  lambda_PAT * E[pts | PAT]

  lambda_FG, lambda_PAT   expected FG and PAT tries of the kicker's team in this game: ridge-penalized Poisson
                          regressions (log link) on point-in-time team offense, opponent defense, venue and game
                          environment features (feature groups below).
  P(d)                    distance distribution of the team's FG tries: the league's per-yard histogram split
                          into <50 and 50+ yards, mixed with the KICKER's shrunk share of 50+ tries (coaches try
                          long kicks with kickers who can make them).
  p(d)                    make probability: a league logistic curve in distance (quadratic in yards, so smooth
                          across the 39/40 and 49/50 scoring cutoffs) plus venue/weather terms, plus the kicker's
                          empirical-Bayes skill: a log-odds shift estimated from all of his earlier FG and PAT tries
                          (misses and blocks included), recency-weighted by season and shrunk toward 0 (league
                          average) with prior variance tau^2. 2-of-2 from 50+ moves him far less than 30-of-40.
  E[pts | kick]           the league's own per-kick points (app.analytics.kicker_scoring), with a league block
                          rate splitting non-makes into blocked and missed.

Feature groups (ablations): offense, defense, stadium, weather (RETROSPECTIVE: nflverse's observed game-time
wind/temperature, never a pregame forecast), script (team strength, 4th-down and 2-pt tendencies).

Comparators: A1 season-to-date points per game, A2 last-4 games, A3 "ability + team opportunity" (team FG/PAT
rates x kicker conversion, no regression), D a direct ridge on points.

Point-in-time contract (tested): a row for (season S, week W) uses only team games and kicks with
(season, week) < (S, W); the target game contributes only its schedule (teams, venue, kickoff) and, in the
retrospective weather experiment only, its observed weather. League curves and regressions used for a target
season are fitted on earlier seasons.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import polars as pl

from .kicker_scoring import KickerScoring
from .kicker_venues import registry, resolve

MODEL_VERSION = "kfp-1.1.0"
ARTIFACT_SCHEMA = "kicker-projections-artifact/v1"
GROUPS = ("offense", "defense", "stadium", "weather", "script", "line")
MIN_DIST, MAX_DIST = 18, 70
COLD_F = 40.0
# Unknown wind at an open-air venue is imputed with this prior (the approximate mean of nflverse's observed outdoor
# game wind, 2019-2025: 8.4 mph) and flagged; a separate "missing" coefficient is NOT fitted, because missingness is
# rare in some seasons and common in others (2022: 30% of outdoor FG tries), so its coefficient is unidentified.
OUTDOOR_WIND_FILL = 8.5

TEAM_STATS = ("fg_att", "pat_att", "fg_range_drives", "rz_drives", "rz_td_drives", "td_drives", "drives", "points",
              "points_allowed", "two_pt_att", "fourth_go_in_range", "fourth_in_range", "fg_att_50")

LAMBDA_FEATURES = {
    "offense": ["o_fg_att", "o_pat_att", "o_fg_range", "o_rz_td_rate", "o_points", "o_drives", "o_fg_per_range"],
    "defense": ["d_fg_att", "d_pat_att", "d_fg_range", "d_rz_td_rate", "d_points"],
    "stadium": ["is_home", "venue_fixed", "venue_retractable", "elev_km"],
    "weather": ["wx_wind", "wx_cold"],
    "script": ["o_net", "d_net", "o_fourth_go_rate", "o_two_pt_rate"],
    "line": ["o_implied"],
}
CONVERSION_FEATURES = {      # added to the league FG curve
    "stadium": ["fixed", "retractable", "elev_x_dist"],
    "weather": ["wind_x_dist", "cold_x_dist"],
}
DIRECT_KICKER = ["k_pts_season", "k_pts_prev", "k_pts_l4", "k_skill", "k_long_share", "k_games"]
FEATURE_LABELS = {
    "o_fg_att": "team FG tries per game", "o_pat_att": "team PAT tries per game (touchdowns)",
    "o_fg_range": "team drives reaching the opponent 35 per game", "o_rz_td_rate": "team red-zone TD rate",
    "o_points": "team points per game", "o_drives": "team drives per game", "o_fg_per_range": "team FG tries per scoring-range drive",
    "d_fg_att": "opponent FG tries allowed per game", "d_pat_att": "opponent PATs allowed per game",
    "d_fg_range": "opponent scoring-range drives allowed per game", "d_rz_td_rate": "opponent red-zone TD rate allowed",
    "d_points": "opponent points allowed per game", "is_home": "home game", "venue_fixed": "indoor (fixed roof)",
    "venue_retractable": "retractable roof (position unknown)", "elev_km": "venue elevation (km)",
    "wx_wind": "wind (mph, outdoor)", "wx_cold": "cold (< 40F, outdoor)", "wx_missing": "outdoor weather unknown",
    "o_net": "team point differential per game", "d_net": "opponent point differential per game",
    "o_fourth_go_rate": "team 4th-down go rate in FG range", "o_two_pt_rate": "team 2-pt tries per TD",
    "o_implied": "team implied points from the betting line",
}


@dataclass(frozen=True)
class KConfig:
    groups: tuple[str, ...] = ("offense", "defense", "stadium", "script")
    team_k: float = 4.0          # current-season games of shrinkage toward the team prior
    prior_k: float = 8.0         # previous-season games of shrinkage toward the league mean
    tau2: float = 0.15           # kicker skill prior variance (log-odds^2)
    decay: float = 0.7           # weight of a kick per season of age
    long_k: float = 25.0         # FG tries of shrinkage for the kicker's 50+ share
    ridge_lambda: float = 1.0    # Poisson ridge (per row, standardized features)
    ridge_conv: float = 1.0      # league conversion logistic ridge
    ridge_direct: float = 10.0
    blend: float = 1.0           # weight on the structural model vs the direct ridge
    fg_groups: tuple[str, ...] | None = None    # lambda_FG feature groups (None = `groups`)
    train_decay: float = 1.0     # weight of a training season per season of age (league trends: longer, better kicking)

    def with_groups(self, groups, fg_groups=None) -> "KConfig":
        return replace(self, groups=tuple(groups), fg_groups=None if fg_groups is None else tuple(fg_groups))

    @property
    def lambda_fg_groups(self) -> tuple[str, ...]:
        return self.groups if self.fg_groups is None else self.fg_groups


def key(season, week):
    return season * 100 + week


# ----------------------------------------------------------------------------- loading


def load_frames(con, seasons: list[int] | None = None) -> dict[str, pl.DataFrame]:
    """Kicks, team games and the REG schedule from one database (history or live)."""
    from ..db.connection import table_exists

    where = f"AND season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'nfl_schedules'").fetchall()}
    # the betting line (kfp-1.1 "line" group); an older or synthetic schedule without it gets NULLs
    line_cols = ", ".join(f"{c}::DOUBLE AS {c}" if c in cols else f"NULL::DOUBLE AS {c}"
                          for c in ("spread_line", "total_line"))
    out: dict[str, pl.DataFrame] = {}
    for name, sql in (("kicks", f"SELECT * FROM nfl_kick_attempts WHERE true {where}"),
                      ("teams", f"SELECT * FROM nfl_team_game_drives WHERE true {where}"),
                      ("schedule", f"""SELECT game_id, season::INT AS season, week::INT AS week, gameday, gametime,
                                       home_team, away_team, home_score, away_score, location, roof, surface,
                                       temp::DOUBLE AS temp, wind::DOUBLE AS wind, stadium_id, stadium,
                                       {line_cols}
                                       FROM nfl_schedules WHERE game_type = 'REG' {where}""")):
        table = {"kicks": "nfl_kick_attempts", "teams": "nfl_team_game_drives", "schedule": "nfl_schedules"}[name]
        out[name] = pl.from_arrow(con.execute(sql).arrow()) if table_exists(con, table) else pl.DataFrame()
    return out


def merge_frames(*frames: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """Union history + live frames; a (season) present in a later frame replaces the earlier one."""
    out: dict[str, pl.DataFrame] = {}
    for name in ("kicks", "teams", "schedule"):
        parts = [f[name] for f in frames if f.get(name) is not None and f[name].height]
        if not parts:
            out[name] = pl.DataFrame()
            continue
        acc = parts[0]
        for p in parts[1:]:
            seasons = p["season"].unique().to_list()
            acc = pl.concat([acc.filter(~pl.col("season").is_in(seasons)), p], how="diagonal_relaxed")
        out[name] = acc
    return out


# ----------------------------------------------------------------------------- base table


def kick_points(kicks: pl.DataFrame, scoring: KickerScoring) -> pl.DataFrame:
    if kicks.height == 0:
        return kicks.with_columns(pl.lit(None, dtype=pl.Float64).alias("pts"))
    pts = [scoring.fg_points(int(d), r) if t == "FG" else
           (scoring.pat_points(r) if r in ("made", "missed", "blocked") else scoring.pat_points("missed"))
           for t, d, r in zip(kicks["kick_type"], kicks["kick_distance"].fill_null(33), kicks["result"])]
    return kicks.with_columns(pl.Series("pts", pts, dtype=pl.Float64))


def venue_table(schedule: pl.DataFrame) -> pl.DataFrame:
    """Per game: resolved venue (registry/override), roof capability, elevation, observed weather (retrospective,
    nulled when the venue was corrected and nflverse's roof disagrees, or the venue is not open-air)."""
    reg = registry()
    rows = []
    for g in schedule.iter_rows(named=True):
        v = resolve(g, reg)
        outdoor = v["outdoor"]
        corrected = v["source"] == "override"
        wind = g.get("wind") if outdoor and not corrected else None
        temp = g.get("temp") if outdoor and not corrected else None
        rows.append({"game_id": g["game_id"], "venue_id": v["venue_id"], "venue_name": v["name"],
                     "venue_source": v["source"], "roof_class": v["roof_class"],
                     "venue_fixed": float(v["roof_class"] == "fixed") if v["roof_class"] else None,
                     "venue_retractable": float(v["roof_class"] == "retractable") if v["roof_class"] else None,
                     "venue_outdoor": None if outdoor is None else float(outdoor),
                     "elev_km": None if v["elevation_m"] is None else v["elevation_m"] / 1000.0,
                     "obs_wind": wind, "obs_temp": temp, "venue_country": v["country"],
                     "venue_notes": "; ".join(v["notes"]) or None})
    schema = {"game_id": pl.String, "venue_id": pl.String, "venue_name": pl.String, "venue_source": pl.String,
              "roof_class": pl.String, "venue_fixed": pl.Float64, "venue_retractable": pl.Float64,
              "venue_outdoor": pl.Float64, "elev_km": pl.Float64, "obs_wind": pl.Float64, "obs_temp": pl.Float64,
              "venue_country": pl.String, "venue_notes": pl.String}
    return pl.DataFrame(rows, schema=schema)


def team_games(frames: dict[str, pl.DataFrame], scoring: KickerScoring) -> pl.DataFrame:
    """One row per (game, team) for COMPLETED games: opportunity counts, the game's kicker and his league points."""
    teams = frames["teams"]
    if teams.height == 0:
        return pl.DataFrame()
    opp_pts = teams.select("game_id", pl.col("team").alias("opponent"), pl.col("points").alias("points_allowed"))
    kp = kick_points(frames["kicks"], scoring)
    kicker_pts = kp.group_by(["game_id", "posteam", "kicker_player_id"]).agg(
        pl.col("pts").sum().alias("label"), (pl.col("kick_type") == "FG").sum().alias("k_fg_att"),
        (pl.col("kick_type") == "PAT").sum().alias("k_pat_att")).rename(
        {"posteam": "team", "kicker_player_id": "game_kicker_id"})
    out = (teams.join(opp_pts, on=["game_id", "opponent"], how="left")
           .join(kicker_pts, on=["game_id", "team", "game_kicker_id"], how="left")
           .with_columns(pl.col("label").fill_null(0.0), pl.col("k_fg_att").fill_null(0), pl.col("k_pat_att").fill_null(0),
                         pl.lit(True).alias("completed")))
    return out


def line_table(schedule: pl.DataFrame) -> pl.DataFrame:
    """(game_id, team, o_implied): each side's implied points from the stored line. nflverse's spread_line is positive
    when the home team is favored, so home = (total + spread) / 2 and away = (total - spread) / 2. Games without a line
    are left out (the GLM fills a missing value with its training mean, a league-average total)."""
    schema = {"game_id": pl.String, "team": pl.String, "o_implied": pl.Float64}
    if schedule.height == 0 or "spread_line" not in schedule.columns or "total_line" not in schedule.columns:
        return pl.DataFrame(schema=schema)
    g = schedule.filter(pl.col("spread_line").is_not_null() & pl.col("total_line").is_not_null())
    tot, spr = pl.col("total_line").cast(pl.Float64), pl.col("spread_line").cast(pl.Float64)
    return pl.concat([g.select("game_id", pl.col("home_team").alias("team"), ((tot + spr) / 2).alias("o_implied")),
                      g.select("game_id", pl.col("away_team").alias("team"), ((tot - spr) / 2).alias("o_implied"))]
                     ).unique(["game_id", "team"], keep="last").cast(schema)


def target_rows(schedule: pl.DataFrame, season: int, weeks: list[int]) -> pl.DataFrame:
    """Unplayed (or to-be-forecast) team-games from the schedule: one row per team per game."""
    s = schedule.filter((pl.col("season") == season) & pl.col("week").is_in(weeks))
    home = s.select("game_id", "season", "week", pl.col("home_team").alias("team"), pl.col("away_team").alias("opponent"),
                    pl.lit(True).alias("is_home"))
    away = s.select("game_id", "season", "week", pl.col("away_team").alias("team"), pl.col("home_team").alias("opponent"),
                    pl.lit(False).alias("is_home"))
    return pl.concat([home, away]).with_columns(pl.lit(False).alias("completed"))


# ----------------------------------------------------------------------------- point-in-time team features


def _cum_prior(df: pl.DataFrame, by: str, prefix: str, cols: tuple[str, ...]) -> pl.DataFrame:
    """For each row: sums over the SAME team's earlier games this season (strictly before the row's week) and the
    team's full previous-season totals. Target rows (completed False) contribute nothing."""
    d = df.sort([by, "season", "week"])
    w = pl.col("completed").cast(pl.Float64)
    exprs = [((pl.col(c).fill_null(0).cast(pl.Float64) * w).cum_sum().shift(1).over([by, "season"]).fill_null(0)
              .alias(f"{prefix}cur_{c}")) for c in cols]
    exprs.append(w.cum_sum().shift(1).over([by, "season"]).fill_null(0).alias(f"{prefix}cur_n"))
    d = d.with_columns(exprs)
    prev = (df.filter(pl.col("completed")).group_by([by, "season"])
            .agg([pl.col(c).fill_null(0).cast(pl.Float64).sum().alias(f"{prefix}prev_{c}") for c in cols]
                 + [pl.len().cast(pl.Float64).alias(f"{prefix}prev_n")])
            .with_columns((pl.col("season") + 1).alias("season")))
    return d.join(prev, on=[by, "season"], how="left")


def league_means(tg: pl.DataFrame) -> dict[int, dict[str, float]]:
    """Per-game league means of each team stat, by season (used as the prior for the NEXT season)."""
    out = {}
    done = tg.filter(pl.col("completed"))
    for (s,), g in done.group_by(["season"]):
        out[int(s)] = {c: float(g[c].cast(pl.Float64).mean()) for c in TEAM_STATS if c in g.columns}
    return out


def team_features(tg: pl.DataFrame, cfg: KConfig) -> pl.DataFrame:
    """Adds shrunk per-game offense (o_*) and opponent-defense (d_*) features to every row, point-in-time.
    Prior for a team-season = previous-season team rate shrunk toward the previous-season league mean
    (prior_k games); current-season rate shrinks toward that prior (team_k games). No previous season: league."""
    lm = league_means(tg)
    fallback = next(iter(lm.values())) if lm else {c: 0.0 for c in TEAM_STATS}

    def league(season_col: pl.Expr, c: str) -> pl.Expr:
        mapping = {s + 1: v.get(c, fallback.get(c, 0.0)) for s, v in lm.items()}
        return season_col.replace_strict(mapping, default=None, return_dtype=pl.Float64).fill_null(
            float(np.mean([v.get(c, 0.0) for v in lm.values()])) if lm else 0.0)

    base = tg.with_columns(pl.col("is_home").cast(pl.Boolean))
    off = _cum_prior(base, "team", "o_", TEAM_STATS)
    # defense: the opponent's view of the same row (what `opponent` allowed in its earlier games)
    dfr = base.select("game_id", "season", "week", "completed", pl.col("opponent").alias("defteam"),
                      *[pl.col(c) for c in TEAM_STATS])
    dfe = _cum_prior(dfr, "defteam", "d_", TEAM_STATS).drop(list(TEAM_STATS) + ["completed"])
    out = off.join(dfe.rename({"defteam": "opponent"}), on=["game_id", "season", "week", "opponent"], how="left")

    def shrunk(prefix: str, c: str) -> pl.Expr:
        lg = league(pl.col("season"), c)
        pn, ps = pl.col(f"{prefix}prev_n").fill_null(0), pl.col(f"{prefix}prev_{c}").fill_null(0)
        prior = (ps + cfg.prior_k * lg) / (pn + cfg.prior_k)
        return (pl.col(f"{prefix}cur_{c}") + cfg.team_k * prior) / (pl.col(f"{prefix}cur_n") + cfg.team_k)

    out = out.with_columns(
        shrunk("o_", "fg_att").alias("o_fg_att"), shrunk("o_", "pat_att").alias("o_pat_att"),
        shrunk("o_", "fg_range_drives").alias("o_fg_range"), shrunk("o_", "rz_drives").alias("_o_rz"),
        shrunk("o_", "rz_td_drives").alias("_o_rztd"), shrunk("o_", "points").alias("o_points"),
        shrunk("o_", "points_allowed").alias("_o_pa"), shrunk("o_", "drives").alias("o_drives"),
        shrunk("o_", "td_drives").alias("_o_td"), shrunk("o_", "two_pt_att").alias("_o_2pt"),
        shrunk("o_", "fourth_go_in_range").alias("_o_go"), shrunk("o_", "fourth_in_range").alias("_o_4th"),
        shrunk("d_", "fg_att").alias("d_fg_att"), shrunk("d_", "pat_att").alias("d_pat_att"),
        shrunk("d_", "fg_range_drives").alias("d_fg_range"), shrunk("d_", "rz_drives").alias("_d_rz"),
        shrunk("d_", "rz_td_drives").alias("_d_rztd"), shrunk("d_", "points").alias("d_points"),
        shrunk("d_", "points_allowed").alias("_d_pa"),
        (pl.col("o_cur_n")).alias("o_games"), (pl.col("d_cur_n")).alias("d_games"),
    ).with_columns(
        (pl.col("_o_rztd") / pl.col("_o_rz")).alias("o_rz_td_rate"),
        (pl.col("_d_rztd") / pl.col("_d_rz")).alias("d_rz_td_rate"),
        (pl.col("o_fg_att") / pl.col("o_fg_range")).alias("o_fg_per_range"),
        (pl.col("o_points") - pl.col("_o_pa")).alias("o_net"),
        # the defense row's "points" are what the opponent's opponents scored on it; its own offense is not here,
        # so the opponent's net = (its points scored) - (its points allowed) comes from its offense rows below
        (pl.col("_o_go") / pl.col("_o_4th")).alias("o_fourth_go_rate"),
        (pl.col("_o_2pt") / pl.col("_o_td")).alias("o_two_pt_rate"),
    )
    # opponent's own net differential: its offense row of the same game
    opp_net = out.select("game_id", pl.col("team").alias("opponent"), pl.col("o_net").alias("d_net"))
    out = out.join(opp_net, on=["game_id", "opponent"], how="left")
    keep_drop = [c for c in out.columns if c.startswith(("o_cur_", "o_prev_", "d_cur_", "d_prev_", "_o_", "_d_"))]
    return out.drop(keep_drop)


def add_context(df: pl.DataFrame, venues: pl.DataFrame, weather: dict | None = None) -> pl.DataFrame:
    """Venue and weather inputs. `weather` (production only): game_id -> {wind, temp} forecast values; without
    it, weather features come from nflverse's OBSERVED conditions (retrospective)."""
    out = df.join(venues, on="game_id", how="left")
    if weather is not None:
        w = pl.DataFrame([{"game_id": g, "fc_wind": v.get("wind"), "fc_temp": v.get("temp")} for g, v in weather.items()],
                         schema={"game_id": pl.String, "fc_wind": pl.Float64, "fc_temp": pl.Float64})
        out = out.join(w, on="game_id", how="left").with_columns(pl.col("fc_wind").alias("obs_wind"),
                                                                  pl.col("fc_temp").alias("obs_temp")).drop("fc_wind", "fc_temp")
    outdoor = pl.col("venue_outdoor").fill_null(0.0)
    return out.with_columns(
        pl.col("is_home").cast(pl.Float64),
        (outdoor * pl.col("obs_wind").fill_null(OUTDOOR_WIND_FILL)).alias("wx_wind"),
        (outdoor * (pl.col("obs_temp") < COLD_F).cast(pl.Float64).fill_null(0.0)).alias("wx_cold"),
        (outdoor * pl.col("obs_wind").is_null().cast(pl.Float64)).alias("wx_missing"),
    )


# ----------------------------------------------------------------------------- GLMs


def _standardize(x: np.ndarray, means=None, stds=None):
    means = x.mean(axis=0) if means is None else np.asarray(means)
    stds = x.std(axis=0) if stds is None else np.asarray(stds)
    stds = np.where(stds < 1e-9, 1.0, stds)
    return (x - means) / stds, means, stds


def _matrix(df: pl.DataFrame, cols: list[str], means: list[float] | None = None) -> np.ndarray:
    if not cols:
        return np.zeros((df.height, 0))
    arrs = []
    for i, c in enumerate(cols):
        a = df[c].cast(pl.Float64).to_numpy().astype(float) if c in df.columns else np.full(df.height, np.nan)
        fill = means[i] if means is not None else (np.nanmean(a) if np.isfinite(a).any() else 0.0)
        a = np.where(np.isfinite(a), a, fill)
        arrs.append(a)
    return np.column_stack(arrs)


@dataclass
class GLM:
    family: str                 # poisson | logistic | gaussian
    columns: list[str]
    means: list[float]
    stds: list[float]
    coef: list[float]
    intercept: float
    ridge: float
    n: int

    def linear(self, df_or_x) -> np.ndarray:
        x = df_or_x if isinstance(df_or_x, np.ndarray) else _matrix(df_or_x, self.columns, self.means)
        if not self.columns:
            return np.full(x.shape[0], self.intercept)
        z = (x - np.array(self.means)) / np.array(self.stds)
        return z @ np.array(self.coef) + self.intercept

    def predict(self, df_or_x) -> np.ndarray:
        eta = self.linear(df_or_x)
        if self.family == "poisson":
            return np.exp(np.clip(eta, -10, 5))
        if self.family == "logistic":
            return 1.0 / (1.0 + np.exp(-eta))
        return eta

    def contributions(self, df: pl.DataFrame) -> np.ndarray:
        """Per-row, per-column contribution on the linear-predictor scale (log for Poisson)."""
        x = _matrix(df, self.columns, self.means)
        z = (x - np.array(self.means)) / np.array(self.stds)
        return z * np.array(self.coef)

    def to_dict(self) -> dict:
        return asdict(self)


def fit_glm(df: pl.DataFrame, cols: list[str], y: np.ndarray, family: str, ridge: float,
            weights: np.ndarray | None = None, iters: int = 30) -> GLM:
    """IRLS with a ridge penalty on standardized coefficients (intercept unpenalized); ridge scales with n."""
    x = _matrix(df, cols)
    means = x.mean(axis=0) if cols else np.zeros(0)
    z, means, stds = _standardize(x, means) if cols else (x, means, np.ones(0))
    n = len(y)
    w0 = np.ones(n) if weights is None else weights
    xd = np.column_stack([np.ones(n), z])
    pen = np.eye(xd.shape[1]) * ridge * w0.sum()
    pen[0, 0] = 0.0
    beta = np.zeros(xd.shape[1])
    if family == "poisson":
        beta[0] = math.log(max((w0 * y).sum() / w0.sum(), 1e-6))
    elif family == "logistic":
        m = min(max((w0 * y).sum() / w0.sum(), 1e-4), 1 - 1e-4)
        beta[0] = math.log(m / (1 - m))
    for _ in range(iters if family != "gaussian" else 1):
        eta = xd @ beta
        if family == "poisson":
            mu = np.exp(np.clip(eta, -10, 5))
            wt, zr = w0 * mu, eta + (y - mu) / np.maximum(mu, 1e-9)
        elif family == "logistic":
            mu = 1 / (1 + np.exp(-eta))
            v = np.maximum(mu * (1 - mu), 1e-9)
            wt, zr = w0 * v, eta + (y - mu) / v
        else:
            wt, zr = w0, y
        new = np.linalg.solve(xd.T @ (xd * wt[:, None]) + pen, xd.T @ (wt * zr))
        if np.max(np.abs(new - beta)) < 1e-8:
            beta = new
            break
        beta = new
    return GLM(family, list(cols), [float(v) for v in means], [float(v) for v in stds],
               [float(v) for v in beta[1:]], float(beta[0]), float(ridge), int(n))


# ----------------------------------------------------------------------------- conversion


def fg_design(dist: np.ndarray) -> np.ndarray:
    dc = (dist - 35.0) / 10.0
    return np.column_stack([dc, dc * dc])


def conversion_frame(kicks: pl.DataFrame, venues: pl.DataFrame) -> pl.DataFrame:
    """FG/PAT tries with venue features for the conversion model ('aborted' PAT snaps are excluded: no kick)."""
    k = kicks.filter(pl.col("result").is_in(["made", "missed", "blocked"]))
    k = k.join(venues.select("game_id", "venue_fixed", "venue_retractable", "venue_outdoor", "elev_km", "obs_wind",
                             "obs_temp"), on="game_id", how="left")
    d = pl.col("kick_distance").fill_null(33.0)
    long_part = ((d - 20.0).clip(lower_bound=0.0) / 10.0)
    outdoor = pl.col("venue_outdoor").fill_null(0.0)
    return k.with_columns(
        (pl.col("result") == "made").cast(pl.Float64).alias("y"),
        pl.col("venue_fixed").fill_null(0.0).alias("fixed"), pl.col("venue_retractable").fill_null(0.0).alias("retractable"),
        (pl.col("elev_km").fill_null(0.2) * long_part).alias("elev_x_dist"),
        (outdoor * pl.col("obs_wind").fill_null(OUTDOOR_WIND_FILL) * long_part).alias("wind_x_dist"),
        (outdoor * (pl.col("obs_temp") < COLD_F).cast(pl.Float64).fill_null(0.0) * long_part).alias("cold_x_dist"),
        (outdoor * pl.col("obs_wind").is_null().cast(pl.Float64)).alias("wx_missing"),
        (pl.col("season") * 100 + pl.col("week")).alias("k"),
    )


@dataclass
class Conversion:
    fg: GLM                       # columns: dc, dc2, + env columns (raw, not standardized: see fit_conversion)
    pat: GLM
    env_columns: list[str]
    block_rate_fg: float
    block_rate_pat: float
    dist_short: list[float]       # P(d | d < 50), d = MIN_DIST..49
    dist_long: list[float]        # P(d | d >= 50), d = 50..MAX_DIST
    league_long_share: float

    def to_dict(self) -> dict:
        return {**asdict(self), "fg": self.fg.to_dict(), "pat": self.pat.to_dict()}

    @staticmethod
    def from_dict(d: dict) -> "Conversion":
        return Conversion(**{**d, "fg": GLM(**d["fg"]), "pat": GLM(**d["pat"])})

    def p_make(self, dist: np.ndarray, env: dict[str, float], skill: float) -> np.ndarray:
        dist = np.asarray(dist, dtype=float)
        base = fg_design(dist)
        long_part = np.clip(dist - 20.0, 0, None) / 10.0
        cols = [base[:, 0], base[:, 1]]
        for c in self.env_columns:
            v = env.get(c.replace("_x_dist", ""), 0.0) or 0.0     # env keys: fixed retractable elev wind cold
            cols.append(v * long_part if c.endswith("_x_dist") else np.full(len(dist), v))
        x = np.column_stack(cols)
        eta = self.fg.linear(x) + skill
        return 1 / (1 + np.exp(-eta))

    def p_pat(self, skill: float) -> float:
        return float(1 / (1 + np.exp(-(self.pat.intercept + skill))))


def season_weights(seasons: np.ndarray, decay: float) -> np.ndarray:
    """Training weight decay^(latest season - season): 1.0 keeps every season equal."""
    if len(seasons) == 0 or decay >= 1.0:
        return np.ones(len(seasons))
    return np.power(decay, (seasons.max() - seasons).astype(float))


def fit_conversion(conv: pl.DataFrame, cfg: KConfig) -> Conversion:
    env = [c for g in cfg.groups for c in CONVERSION_FEATURES.get(g, [])]
    fg = conv.filter(pl.col("kick_type") == "FG")
    dist = fg["kick_distance"].to_numpy().astype(float)
    wfg = season_weights(fg["season"].to_numpy(), cfg.train_decay)
    base = fg_design(dist)
    cols = ["dc", "dc2", *env]
    fgx = fg.with_columns(pl.Series("dc", base[:, 0]), pl.Series("dc2", base[:, 1]))
    glm = fit_glm(fgx, cols, fgx["y"].to_numpy(), "logistic", cfg.ridge_conv / 1000.0, weights=wfg)
    pat = conv.filter(pl.col("kick_type") == "PAT")
    wpat = season_weights(pat["season"].to_numpy(), cfg.train_decay)
    patm = fit_glm(pat, [], pat["y"].to_numpy(), "logistic", 0.0, weights=wpat)
    blk_fg = float(np.average(fg["result"] == "blocked", weights=wfg)) if fg.height else 0.015
    blk_pat = float(np.average(pat["result"] == "blocked", weights=wpat)) if pat.height else 0.007
    d = np.clip(np.rint(dist), MIN_DIST, MAX_DIST).astype(int)
    hist = np.bincount(d - MIN_DIST, weights=wfg, minlength=MAX_DIST - MIN_DIST + 1).astype(float) + 0.5   # add-half smoothing
    short, long_ = hist[: 50 - MIN_DIST], hist[50 - MIN_DIST:]
    # environment columns are kept raw (standardization folded into the GLM's means/stds)
    return Conversion(glm, patm, env, blk_fg, blk_pat, (short / short.sum()).tolist(), (long_ / long_.sum()).tolist(),
                      float(np.average(dist >= 50, weights=wfg)) if len(dist) else 0.2)


def kicker_skill(conv: pl.DataFrame, model: Conversion, target_season: int, cfg: KConfig) -> pl.DataFrame:
    """Per (kicker, week of target_season): empirical-Bayes log-odds skill and 50+ try share from every kick before
    that week (earlier seasons weighted decay^age). Returns kicker_player_id, week, k_skill, k_long_share,
    k_kicks (effective FG+PAT tries) for weeks 1..22 (a kicker with no history gets no row: skill 0 = league)."""
    past = conv.filter(pl.col("season") <= target_season)
    if past.height == 0:
        return pl.DataFrame(schema={"kicker_player_id": pl.String, "week": pl.Int32, "k_skill": pl.Float64,
                                    "k_long_share": pl.Float64, "k_kicks": pl.Float64})
    p = np.empty(past.height)
    is_fg = (past["kick_type"] == "FG").to_numpy()
    dist = past["kick_distance"].fill_null(33.0).to_numpy().astype(float)
    base = fg_design(dist)
    cols = [base[:, 0], base[:, 1]]
    for c in model.env_columns:
        cols.append(past[c].fill_null(0.0).to_numpy().astype(float))
    xfg = np.column_stack(cols)
    p[is_fg] = model.fg.predict(xfg[is_fg])
    p[~is_fg] = model.p_pat(0.0)
    age = (target_season - past["season"].to_numpy()).astype(float)
    w = np.power(cfg.decay, age)
    y = past["y"].to_numpy()
    frame = past.select("kicker_player_id", "season", "week").with_columns(
        pl.Series("r", w * (y - p)), pl.Series("i", w * p * (1 - p)), pl.Series("wk", w),
        pl.Series("long", w * ((dist >= 50) & is_fg)), pl.Series("fgw", w * is_fg))
    prior = frame.filter(pl.col("season") < target_season).group_by("kicker_player_id").agg(
        pl.col("r").sum().alias("r0"), pl.col("i").sum().alias("i0"), pl.col("long").sum().alias("l0"),
        pl.col("fgw").sum().alias("f0"), pl.col("wk").sum().alias("n0"))
    cur = frame.filter(pl.col("season") == target_season).group_by(["kicker_player_id", "week"]).agg(
        pl.col("r").sum(), pl.col("i").sum(), pl.col("long").sum(), pl.col("fgw").sum(), pl.col("wk").sum())
    kickers = pl.concat([prior.select("kicker_player_id"), cur.select("kicker_player_id")]).unique()
    grid = kickers.join(pl.DataFrame({"week": list(range(1, 23))}, schema={"week": pl.Int32}), how="cross")
    g = (grid.join(cur.with_columns(pl.col("week").cast(pl.Int32)), on=["kicker_player_id", "week"], how="left")
         .with_columns([pl.col(c).fill_null(0.0) for c in ("r", "i", "long", "fgw", "wk")])
         .sort(["kicker_player_id", "week"])
         .with_columns([pl.col(c).cum_sum().shift(1).over("kicker_player_id").fill_null(0.0).alias(f"c_{c}")
                        for c in ("r", "i", "long", "fgw", "wk")])
         .join(prior, on="kicker_player_id", how="left")
         .with_columns([pl.col(c).fill_null(0.0) for c in ("r0", "i0", "l0", "f0", "n0")]))
    # one Newton step of the penalized (Gaussian-prior) log-likelihood from skill 0: the empirical-Bayes posterior
    # mode to first order; shrinkage is the prior precision 1/tau2 against the kicker's Fisher information
    return g.select(
        "kicker_player_id", "week",
        ((pl.col("r0") + pl.col("c_r")) / (pl.col("i0") + pl.col("c_i") + 1.0 / cfg.tau2)).alias("k_skill"),
        ((pl.col("l0") + pl.col("c_long") + cfg.long_k * model.league_long_share)
         / (pl.col("f0") + pl.col("c_fgw") + cfg.long_k)).alias("k_long_share"),
        (pl.col("n0") + pl.col("c_wk")).alias("k_kicks"))


# ----------------------------------------------------------------------------- structural expectation


_PTS_CACHE: dict[str, tuple] = {}
DISTS = np.arange(MIN_DIST, MAX_DIST + 1)
BUCKET_MASKS = {"0_39": DISTS < 40, "40_49": (DISTS >= 40) & (DISTS < 50), "50_59": (DISTS >= 50) & (DISTS < 60),
                "60_plus": DISTS >= 60}


def _pts_arrays(scoring: KickerScoring) -> tuple:
    fp = scoring.fingerprint
    if fp not in _PTS_CACHE:
        _PTS_CACHE[fp] = (np.array([scoring.fg_points(int(d), "made") for d in DISTS]),
                          np.array([scoring.fg_points(int(d), "missed") for d in DISTS]),
                          np.array([scoring.fg_points(int(d), "blocked") for d in DISTS]),
                          scoring.pat_points("made"), scoring.pat_points("missed"), scoring.pat_points("blocked"))
    return _PTS_CACHE[fp]


def env_of(row: dict, groups) -> dict[str, float]:
    """Conversion-model environment inputs for one row (only the groups the model uses)."""
    env: dict[str, float] = {}
    if "stadium" in groups:
        env.update(fixed=row.get("venue_fixed") or 0.0, retractable=row.get("venue_retractable") or 0.0,
                   elev=0.2 if row.get("elev_km") is None else row["elev_km"])
    if "weather" in groups:
        env.update(wind=row.get("wx_wind") or 0.0, cold=row.get("wx_cold") or 0.0)
    return env


def expected_kicking(conv: Conversion, scoring: KickerScoring, lam_fg: float, lam_pat: float, skill: float,
                     long_share: float, env: dict[str, float]) -> dict:
    """Expected points and stat line for one kicker-game (conditional on him kicking)."""
    made_pts, miss_pts, blk_pts, pat_m, pat_x, pat_b = _pts_arrays(scoring)
    pd_ = np.concatenate([(1 - long_share) * np.array(conv.dist_short), long_share * np.array(conv.dist_long)])
    pm = conv.p_make(DISTS, env, skill)
    pb = np.minimum(conv.block_rate_fg, 1 - pm)
    pmiss = np.clip(1 - pm - pb, 0, None)
    per_fg = float(np.sum(pd_ * (pm * made_pts + pmiss * miss_pts + pb * blk_pts)))
    pp = conv.p_pat(skill)
    pbp = min(conv.block_rate_pat, 1 - pp)
    per_pat = pp * pat_m + max(1 - pp - pbp, 0) * pat_x + pbp * pat_b
    line = {"fg_att": round(lam_fg, 3), "fg_made": round(lam_fg * float(np.sum(pd_ * pm)), 3),
            "pat_att": round(lam_pat, 3), "pat_made": round(lam_pat * pp, 3),
            "fg_att_by_distance": {b: round(lam_fg * float(np.sum(pd_[m])), 3) for b, m in BUCKET_MASKS.items()},
            "fg_made_by_distance": {b: round(lam_fg * float(np.sum(pd_[m] * pm[m])), 3) for b, m in BUCKET_MASKS.items()},
            "p_make_by_distance": {b: round(float(np.sum(pd_[m] * pm[m]) / max(np.sum(pd_[m]), 1e-12)), 3)
                                   for b, m in BUCKET_MASKS.items()},
            "p_pat": round(pp, 4), "points_per_fg_try": round(per_fg, 3), "points_per_pat_try": round(per_pat, 3)}
    return {"points": lam_fg * per_fg + lam_pat * per_pat, "stat_line": line}


# ----------------------------------------------------------------------------- kicker history (baselines, direct)


def kicker_history_features(tg: pl.DataFrame) -> pl.DataFrame:
    """Per completed team-game row keyed by its game kicker: kicker points history BEFORE the game (season-to-date
    mean, previous-season mean, last-4 mean across seasons, games). Joined to targets by (kicker, season, week)."""
    k = (tg.filter(pl.col("completed") & pl.col("game_kicker_id").is_not_null())
         .select(pl.col("game_kicker_id").alias("kicker_player_id"), "season", "week", "label")
         .sort(["kicker_player_id", "season", "week"]))
    return k


def kicker_points_asof(khist: pl.DataFrame, rows: pl.DataFrame) -> pl.DataFrame:
    """For each (kicker_player_id, season, week) in rows: his points history strictly before that week."""
    keys = rows.select("kicker_player_id", "season", "week").unique()
    h = khist.with_columns((pl.col("season") * 100 + pl.col("week")).alias("hk"))
    j = keys.join(h, on="kicker_player_id", how="left", suffix="_h").filter(
        pl.col("hk") < pl.col("season") * 100 + pl.col("week"))
    agg = j.sort("hk").group_by(["kicker_player_id", "season", "week"]).agg(
        pl.col("label").filter(pl.col("season_h") == pl.col("season")).mean().alias("k_pts_season"),
        pl.col("label").filter(pl.col("season_h") == pl.col("season") - 1).mean().alias("k_pts_prev"),
        pl.col("label").tail(4).mean().alias("k_pts_l4"),
        (pl.col("season_h") == pl.col("season")).sum().cast(pl.Float64).alias("k_games"),
        pl.len().cast(pl.Float64).alias("k_games_all"))
    return rows.join(agg, on=["kicker_player_id", "season", "week"], how="left").with_columns(
        pl.col("k_games").fill_null(0.0), pl.col("k_games_all").fill_null(0.0))


# ----------------------------------------------------------------------------- artifact


@dataclass
class KickerArtifact:
    version: str
    created_at: str
    scoring_fingerprint: str
    scoring: dict
    config: dict
    lambda_fg: GLM
    lambda_pat: GLM
    conversion: Conversion
    direct: GLM | None
    intervals: dict                  # {bands: [{low, high, q_low, q_high}]} from validation residuals
    participation: dict              # empirical P(kicks | designation) table
    status: dict
    evaluation: dict
    train_window: list[int] = field(default_factory=list)
    data_cutoff_key: int | None = None
    schema: str = ARTIFACT_SCHEMA

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lambda_fg"], d["lambda_pat"] = self.lambda_fg.to_dict(), self.lambda_pat.to_dict()
        d["conversion"] = self.conversion.to_dict()
        d["direct"] = self.direct.to_dict() if self.direct else None
        return d

    @staticmethod
    def from_dict(d: dict) -> "KickerArtifact":
        if d.get("schema") != ARTIFACT_SCHEMA:
            raise ValueError(f"unsupported kicker artifact schema {d.get('schema')!r}")
        d = dict(d)
        d["lambda_fg"], d["lambda_pat"] = GLM(**d["lambda_fg"]), GLM(**d["lambda_pat"])
        d["conversion"] = Conversion.from_dict(d["conversion"])
        d["direct"] = GLM(**d["direct"]) if d.get("direct") else None
        return KickerArtifact(**d)

    @property
    def cfg(self) -> KConfig:
        c = dict(self.config["kconfig"])
        c["groups"] = tuple(c["groups"])
        if c.get("fg_groups") is not None:
            c["fg_groups"] = tuple(c["fg_groups"])
        return KConfig(**c)


def interval_for(intervals: dict, pred: float) -> tuple[float, float] | None:
    for band in (intervals or {}).get("bands") or []:
        if (band["low"] is None or pred >= band["low"]) and (band["high"] is None or pred < band["high"]):
            return pred + band["q_low"], pred + band["q_high"]
    return None


def residual_bands(pred: np.ndarray, actual: np.ndarray, n_bands: int = 3, coverage: float = 0.8) -> dict:
    qs = np.quantile(pred, np.linspace(0, 1, n_bands + 1)[1:-1]) if len(pred) else []
    edges = [None, *[float(q) for q in qs], None]
    bands = []
    lo_q, hi_q = (1 - coverage) / 2, 1 - (1 - coverage) / 2
    for i in range(n_bands):
        lo, hi = edges[i], edges[i + 1]
        m = np.ones(len(pred), dtype=bool)
        if lo is not None:
            m &= pred >= lo
        if hi is not None:
            m &= pred < hi
        r = actual[m] - pred[m]
        if len(r) < 30:
            continue
        bands.append({"low": lo, "high": hi, "q_low": float(np.quantile(r, lo_q)), "q_high": float(np.quantile(r, hi_q)),
                      "n": int(m.sum())})
    return {"nominal_coverage": coverage, "bands": bands}

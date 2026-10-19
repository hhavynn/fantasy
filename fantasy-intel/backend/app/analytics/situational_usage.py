"""Research only: situational usage splits and RB workload components (ifp-su) on top of the frozen ifp-2 spec.

Nothing in the app imports this module (tests/test_situational_usage.py). ifp-1.0.0, ifp-2.0.0, the model
registry, production runs, ledgers, Start/Sit, trades and waivers are untouched.
See docs/research/situational_usage.md.

Question: do situational usage splits (goal-line share, down-and-distance, two-minute, game-script-conditioned
usage, opportunity per snap) predict next-week workload and points better than the total-volume features ifp-2
already has (carries/targets per game, rush/target share, snap share, red-zone and inside-5 counts)?

Everything is derived from nflverse play-by-play (`pbp`, the same release `app.nfl.sync` ingests), the existing
panel (snap share, team plays) and the schedule. No NFL Savant data is read: the metrics are Savant-STYLE,
re-derived here. Point-in-time: a feature for (season S, week W) uses only games strictly before (S, W), from S
and S-1 (the player's last <= 5 games played, the same window as forecast_features), plus this season's final
scores before W for the expected game script. The target week contributes only team and opponent.

Two success rates are kept apart on purpose, under different names:
  su_conv_success  conventional down-and-distance success (Savant / Football Outsiders style): a gain of >= 40% of
                   the yards to go on 1st down, >= 60% on 2nd, 100% on 3rd/4th (or a first down / touchdown)
  su_epa_success   nflverse `success`, i.e. EPA > 0 (what the rest of this repository calls "success")
They measure different things and are never merged under one name.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, fit_linear, recency_weights, train_rows
from .forecast_features import POSITIONS, PRIOR_SEASONS, RECENT_GAMES, key
from .independent_projections import MIN_TRAIN_ROWS, RidgeModel

SU_VERSION = "ifp-su-0.1.0"
SU_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}
SEED = 20261018

# ----------------------------------------------------------------------------- situation definitions (fixed)

LEAD_MARGIN = 7          # posteam leads by >= 7 at the snap: "leading"; trails by >= 7: "trailing"; else neutral
TWO_MINUTE_SECONDS = 120  # last two minutes of either half
SHARE_PSEUDO = 5.0       # a split share shrinks toward the player's overall share by this many team opportunities
SUCCESS_PSEUDO = 10.0    # a player's success rate shrinks toward the position rate by this many opportunities
SCRIPT_SHRINK_GAMES = 3.0  # a team's points margin per game shrinks toward 0 by this many games

PBP_COLUMNS = ("game_id", "play_id", "season", "week", "season_type", "posteam", "defteam", "down", "ydstogo",
               "yardline_100", "score_differential", "half_seconds_remaining", "play_type", "pass_attempt",
               "rusher_player_id", "receiver_player_id", "qb_kneel", "qb_scramble", "two_point_attempt", "sack",
               "qb_dropback", "qb_hit", "epa", "success", "yards_gained", "first_down", "touchdown")

SPLIT_COUNTS = ("car", "car_i5", "car_i10", "car_early", "car_late", "car_lead", "car_trail", "car_2min",
                "tgt", "tgt_i10", "tgt_early", "tgt_late", "tgt_pd", "tgt_lead", "tgt_trail", "tgt_2min")
SUCCESS_COUNTS = ("opp_down", "conv_succ", "opp_epa", "epa_succ")

# Feature docs: every feature below is computed by situational_features()
FEATURE_DOCS = {
    "su_car_i5_share": "share of team designed carries inside the 5, last <= 5 games (shrunk toward overall carry share)",
    "su_opp_i10_share": "share of team designed carries + targets inside the 10 (shrunk toward overall opportunity share)",
    "su_car_early_share": "share of team designed carries on 1st/2nd down outside the two-minute drill",
    "su_car_late_share": "share of team designed carries on 3rd/4th down",
    "su_tgt_i10_share": "share of team targets inside the 10",
    "su_tgt_early_share": "share of team targets on 1st/2nd down outside the two-minute drill",
    "su_tgt_late_share": "share of team targets on 3rd/4th down",
    "su_tgt_pd_share": "share of team targets on passing downs (2nd and 8+, 3rd/4th and 5+)",
    "su_tgt_2min_share": "share of team targets in the last two minutes of a half",
    "su_opp_2min_share": "share of team designed carries + targets in the last two minutes of a half",
    "su_tgt_mix": "targets / (designed carries + targets): receiving profile of the player's own work",
    "su_opp_per_snap": "(carries + targets) per offensive snap (snap share x team plays), last <= 5 games",
    "su_car_tilt": "carry share when leading by 7+ minus when trailing by 7+ (positive = early-down / script back)",
    "su_tgt_tilt": "target share when trailing by 7+ minus when leading by 7+ (positive = catch-up / passing-down role)",
    "su_exp_margin": "expected game script: half of (team points margin/game - opponent's), this season before W, shrunk",
    "su_script_car": "su_exp_margin x su_car_early_share (an early-down back with an expected lead)",
    "su_script_tgt": "su_exp_margin x su_tgt_pd_share (a passing-down role with an expected deficit)",
    "su_conv_success": "conventional down-and-distance success rate of the player's carries + targets (shrunk)",
    "su_epa_success": "EPA > 0 success rate of the player's carries + targets (nflverse `success`, shrunk)",
}

# Candidate feature groups (fixed before any backtest; docs/research/situational_usage.md, section 4)
RB_FULL = ["su_car_i5_share", "su_opp_i10_share", "su_car_early_share", "su_car_late_share", "su_tgt_pd_share",
           "su_tgt_late_share", "su_opp_2min_share", "su_tgt_mix", "su_opp_per_snap", "su_car_tilt", "su_tgt_tilt",
           "su_exp_margin", "su_script_car", "su_script_tgt", "su_conv_success", "su_epa_success"]
REC_FULL = ["su_tgt_i10_share", "su_tgt_early_share", "su_tgt_late_share", "su_tgt_pd_share", "su_tgt_2min_share",
            "su_opp_per_snap", "su_tgt_tilt", "su_exp_margin", "su_script_tgt", "su_conv_success", "su_epa_success"]
RB_SCRIPT = ["su_exp_margin", "su_script_car", "su_script_tgt", "su_car_tilt", "su_tgt_tilt"]
REC_SCRIPT = ["su_exp_margin", "su_script_tgt", "su_tgt_tilt"]
GROUPS = {
    "split_add": {"QB": [], "RB": RB_FULL, "WR": REC_FULL, "TE": REC_FULL},
    "script_only": {"QB": [], "RB": RB_SCRIPT, "WR": REC_SCRIPT, "TE": REC_SCRIPT},
}
CANDIDATES = ("split_add", "script_only", "rb_components")
for _g in GROUPS.values():
    for _fs in _g.values():
        assert all(f in FEATURE_DOCS for f in _fs), _fs

# rb_components: next-game components (labels from the target game's own box score / play-by-play)
COMPONENTS = ("y_car", "y_gl", "y_tgt")       # carries (box), designed carries inside the 5, targets
PRICE_LAM = 1e-6                              # recombination prices: (essentially) unpenalised least squares


# ----------------------------------------------------------------------------- play-by-play splits


def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


def classify_situations(pbp: pl.DataFrame) -> pl.DataFrame:
    """Regular-season plays with the repository's play classes (app.nfl.sync.classify_plays: designed rush,
    target) plus fixed situation flags and the two success definitions."""
    from ..nfl.sync import classify_plays

    p = pbp.filter(pl.col("season_type") == "REG") if "season_type" in pbp.columns else pbp
    for c in ("qb_hit",):
        if c not in p.columns:
            p = p.with_columns(pl.lit(0.0).alias(c))
    p = classify_plays(p)
    down, togo, gain = pl.col("down").cast(pl.Int64), pl.col("ydstogo"), pl.col("yards_gained").fill_null(0)
    sd = pl.col("score_differential")
    converted = _flag("first_down") | _flag("touchdown")
    conv = (pl.when(down == 1).then(gain >= 0.4 * togo).when(down == 2).then(gain >= 0.6 * togo)
            .when(down.is_in([3, 4])).then(gain >= togo).otherwise(None)) | converted
    return p.with_columns(
        (down.is_in([1, 2]) & ~(pl.col("half_seconds_remaining") <= TWO_MINUTE_SECONDS)).fill_null(False).alias("early"),
        down.is_in([3, 4]).fill_null(False).alias("late"),
        (((down == 2) & (togo >= 8)) | (down.is_in([3, 4]) & (togo >= 5))).fill_null(False).alias("pass_down"),
        (pl.col("half_seconds_remaining") <= TWO_MINUTE_SECONDS).fill_null(False).alias("two_min"),
        (pl.col("yardline_100") <= 5).fill_null(False).alias("i5"),
        (pl.col("yardline_100") <= 10).fill_null(False).alias("i10"),
        (sd >= LEAD_MARGIN).fill_null(False).alias("leading"),
        (sd <= -LEAD_MARGIN).fill_null(False).alias("trailing"),
        pl.when(down.is_null() | togo.is_null()).then(None).otherwise(conv).alias("conv_success"),
        pl.when(pl.col("epa").is_null()).then(None).otherwise(_flag("success")).alias("epa_success"),
    )


def _counts(p: pl.DataFrame, opp: pl.Expr) -> list[pl.Expr]:
    dr, tg = pl.col("designed_rush"), pl.col("target")
    return [
        dr.sum().alias("car"), (dr & pl.col("i5")).sum().alias("car_i5"), (dr & pl.col("i10")).sum().alias("car_i10"),
        (dr & pl.col("early")).sum().alias("car_early"), (dr & pl.col("late")).sum().alias("car_late"),
        (dr & pl.col("leading")).sum().alias("car_lead"), (dr & pl.col("trailing")).sum().alias("car_trail"),
        (dr & pl.col("two_min")).sum().alias("car_2min"),
        tg.sum().alias("tgt"), (tg & pl.col("i10")).sum().alias("tgt_i10"),
        (tg & pl.col("early")).sum().alias("tgt_early"), (tg & pl.col("late")).sum().alias("tgt_late"),
        (tg & pl.col("pass_down")).sum().alias("tgt_pd"), (tg & pl.col("leading")).sum().alias("tgt_lead"),
        (tg & pl.col("trailing")).sum().alias("tgt_trail"), (tg & pl.col("two_min")).sum().alias("tgt_2min"),
        (opp & pl.col("conv_success").is_not_null()).sum().alias("opp_down"),
        (opp & pl.col("conv_success").fill_null(False)).sum().alias("conv_succ"),
        (opp & pl.col("epa_success").is_not_null()).sum().alias("opp_epa"),
        (opp & pl.col("epa_success").fill_null(False)).sum().alias("epa_succ"),
    ]


def week_splits(pbp: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(player, team) split counts per regular-season game week.

    player: season, week, team, gsis_id, SPLIT_COUNTS, SUCCESS_COUNTS (a player's carries are his designed rushes,
            his targets the passes thrown to him; success counts cover both)
    team:   season, week, team, tm_<SPLIT_COUNTS> (the team's designed rushes and targets in each situation)
    """
    p = classify_situations(pbp)
    opp = pl.col("designed_rush") | pl.col("target")
    work = p.filter(opp).with_columns(
        pl.when(pl.col("designed_rush")).then(pl.col("rusher_player_id")).otherwise(pl.col("receiver_player_id"))
        .alias("gsis_id"))
    player = (work.filter(pl.col("gsis_id").is_not_null())
              .group_by([pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), pl.col("posteam").alias("team"),
                         "gsis_id"])
              .agg(_counts(work, opp)))
    team = (p.filter(pl.col("scrimmage"))
            .group_by([pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), pl.col("posteam").alias("team")])
            .agg([e.alias(f"tm_{n}") for e, n in zip(_counts(p, opp)[:len(SPLIT_COUNTS)], SPLIT_COUNTS)]))
    cast = {c: pl.Int64 for c in [*SPLIT_COUNTS, *SUCCESS_COUNTS]}
    return player.with_columns([pl.col(c).cast(t) for c, t in cast.items()]), \
        team.with_columns([pl.col(f"tm_{c}").cast(pl.Int64) for c in SPLIT_COUNTS])


# ----------------------------------------------------------------------------- point-in-time features


def _shrunk_share(num: str, den: str, prior: pl.Expr) -> pl.Expr:
    """(player count + k * prior) / (team count + k): a split share that departs from the player's overall share
    only with evidence. Null when the overall prior is null (no team totals in the window)."""
    return pl.when(prior.is_not_null()).then(
        (pl.col(num).fill_null(0) + SHARE_PSEUDO * prior) / (pl.col(den).fill_null(0) + SHARE_PSEUDO)).otherwise(None)


def team_margins(games: pl.DataFrame, season: int, week: int) -> pl.DataFrame:
    """team -> points margin per game this season before W, shrunk toward 0 (SCRIPT_SHRINK_GAMES)."""
    g = games.filter((pl.col("season") == season) & (pl.col("week") < week) & pl.col("points_for").is_not_null())
    return g.group_by("team").agg(
        ((pl.col("points_for") - pl.col("points_against")).sum() / (pl.len() + SCRIPT_SHRINK_GAMES)).alias("margin_pg"))


def situational_features(panel: pl.DataFrame, games: pl.DataFrame, player_splits: pl.DataFrame,
                         team_splits: pl.DataFrame, season: int, week: int, targets: pl.DataFrame) -> pl.DataFrame:
    """FEATURE_DOCS columns for `targets` (gsis_id, position, team, opponent) in week (season, week).

    Window: the player's last <= RECENT_GAMES panel appearances strictly before (season, week), seasons
    season - PRIOR_SEASONS .. season (forecast_features' window). A window game without a play-by-play row for the
    player counts as zero situational opportunities when the team's game is covered; a game whose team has no
    play-by-play row is left out of every share (its team totals are unknown)."""
    cutoff = key(season, week)
    win = (panel.filter((pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - PRIOR_SEASONS))
           .sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
           .select("season", "week", "gsis_id", "position", "team", "carries", "targets", "offense_pct", "team_plays"))
    win = (win.join(team_splits, on=["season", "week", "team"], how="left")
           .join(player_splits.drop("team"), on=["season", "week", "gsis_id"], how="left"))
    covered = pl.col("tm_car").is_not_null()
    win = win.with_columns([pl.when(covered).then(pl.col(c).fill_null(0)).otherwise(None).alias(c)
                            for c in [*SPLIT_COUNTS, *SUCCESS_COUNTS]])
    snaps = pl.col("offense_pct") * pl.col("team_plays")
    agg = win.group_by("gsis_id").agg(
        *[pl.col(c).sum() for c in [*SPLIT_COUNTS, *SUCCESS_COUNTS]],
        *[pl.col(f"tm_{c}").sum() for c in SPLIT_COUNTS],
        covered.sum().alias("su_games"),
        (pl.col("carries") + pl.col("targets")).filter(snaps.is_not_null() & (snaps > 0)).sum().alias("_opp_snap"),
        snaps.filter(snaps.is_not_null() & (snaps > 0)).sum().alias("_snaps"),
    )
    # success-rate priors: position means over every window row (pre-cutoff data only)
    pos_rates = win.group_by("position").agg(
        (pl.col("conv_succ").sum() / pl.col("opp_down").sum()).alias("_conv_prior"),
        (pl.col("epa_succ").sum() / pl.col("opp_epa").sum()).alias("_epa_prior"))
    m = team_margins(games, season, week)
    out = (targets.select("gsis_id", "position", "team", "opponent").join(agg, on="gsis_id", how="left")
           .join(pos_rates, on="position", how="left")
           .join(m.rename({"margin_pg": "_tm_margin"}), on="team", how="left")
           .join(m.rename({"team": "opponent", "margin_pg": "_op_margin"}), on="opponent", how="left"))

    def ratio(a, b):
        return pl.when(pl.col(b) > 0).then(pl.col(a) / pl.col(b)).otherwise(None)

    car_all = ratio("car", "tm_car")
    tgt_all = pl.when(pl.col("tm_tgt") > 0).then(pl.col("tgt") / pl.col("tm_tgt")).otherwise(None)
    opp_all = pl.when((pl.col("tm_car") + pl.col("tm_tgt")) > 0).then(
        (pl.col("car") + pl.col("tgt")) / (pl.col("tm_car") + pl.col("tm_tgt"))).otherwise(None)
    out = out.with_columns(car_all.alias("_car_all"), tgt_all.alias("_tgt_all"), opp_all.alias("_opp_all"),
                           (pl.col("car_i10") + pl.col("tgt_i10")).alias("_opp_i10"),
                           (pl.col("tm_car_i10") + pl.col("tm_tgt_i10")).alias("_tm_opp_i10"),
                           (pl.col("car_2min") + pl.col("tgt_2min")).alias("_opp_2min"),
                           (pl.col("tm_car_2min") + pl.col("tm_tgt_2min")).alias("_tm_opp_2min"))
    ca, ta, oa = pl.col("_car_all"), pl.col("_tgt_all"), pl.col("_opp_all")
    out = out.with_columns(
        _shrunk_share("car_i5", "tm_car_i5", ca).alias("su_car_i5_share"),
        _shrunk_share("_opp_i10", "_tm_opp_i10", oa).alias("su_opp_i10_share"),
        _shrunk_share("car_early", "tm_car_early", ca).alias("su_car_early_share"),
        _shrunk_share("car_late", "tm_car_late", ca).alias("su_car_late_share"),
        _shrunk_share("tgt_i10", "tm_tgt_i10", ta).alias("su_tgt_i10_share"),
        _shrunk_share("tgt_early", "tm_tgt_early", ta).alias("su_tgt_early_share"),
        _shrunk_share("tgt_late", "tm_tgt_late", ta).alias("su_tgt_late_share"),
        _shrunk_share("tgt_pd", "tm_tgt_pd", ta).alias("su_tgt_pd_share"),
        _shrunk_share("tgt_2min", "tm_tgt_2min", ta).alias("su_tgt_2min_share"),
        _shrunk_share("_opp_2min", "_tm_opp_2min", oa).alias("su_opp_2min_share"),
        (_shrunk_share("car_lead", "tm_car_lead", ca) - _shrunk_share("car_trail", "tm_car_trail", ca)).alias("su_car_tilt"),
        (_shrunk_share("tgt_trail", "tm_tgt_trail", ta) - _shrunk_share("tgt_lead", "tm_tgt_lead", ta)).alias("su_tgt_tilt"),
        pl.when((pl.col("car") + pl.col("tgt")) > 0).then(pl.col("tgt") / (pl.col("car") + pl.col("tgt")))
          .otherwise(None).alias("su_tgt_mix"),
        ratio("_opp_snap", "_snaps").alias("su_opp_per_snap"),
        pl.when(pl.col("su_games") > 0).then(
            (pl.col("conv_succ") + SUCCESS_PSEUDO * pl.col("_conv_prior")) / (pl.col("opp_down") + SUCCESS_PSEUDO))
          .otherwise(None).alias("su_conv_success"),
        pl.when(pl.col("su_games") > 0).then(
            (pl.col("epa_succ") + SUCCESS_PSEUDO * pl.col("_epa_prior")) / (pl.col("opp_epa") + SUCCESS_PSEUDO))
          .otherwise(None).alias("su_epa_success"),
        (0.5 * (pl.col("_tm_margin").fill_null(0.0) - pl.col("_op_margin").fill_null(0.0))).alias("su_exp_margin"),
    ).with_columns(
        (pl.col("su_exp_margin") * pl.col("su_car_early_share")).alias("su_script_car"),
        (pl.col("su_exp_margin") * pl.col("su_tgt_pd_share")).alias("su_script_tgt"),
    )
    out = out.select("gsis_id", pl.col("su_games").fill_null(0), *FEATURE_DOCS)
    return out.with_columns([pl.col(c).round(10) for c, t in out.schema.items() if t == pl.Float64])


def component_labels(panel: pl.DataFrame) -> pl.DataFrame:
    """Next-game component targets per (season, week, gsis_id): carries (box score), designed carries inside the
    5 (null when the game's play-by-play is missing) and targets."""
    return panel.select("season", "week", "gsis_id", pl.col("carries").cast(pl.Float64).alias("y_car"),
                        pl.when(pl.col("rz_targets").is_not_null()).then(pl.col("gl_carries")).otherwise(None)
                        .cast(pl.Float64).alias("y_gl"),
                        pl.col("targets").cast(pl.Float64).alias("y_tgt"))


def attach(store: pl.DataFrame, panel: pl.DataFrame, games: pl.DataFrame, player_splits: pl.DataFrame,
           team_splits: pl.DataFrame) -> pl.DataFrame:
    """Store (ifp-2 features, one row per appearance) + situational features + component labels."""
    parts = []
    for season, week in sorted({(int(s), int(w)) for s, w in store.select("season", "week").unique().iter_rows()}):
        rows = store.filter((pl.col("season") == season) & (pl.col("week") == week))
        parts.append(situational_features(panel, games, player_splits, team_splits, season, week,
                                          rows.select("gsis_id", "position", "team", "opponent"))
                     .with_columns(pl.lit(key(season, week)).alias("key")))
    feats = pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()
    return (store.join(feats, on=["key", "gsis_id"], how="left")
            .join(component_labels(panel), on=["season", "week", "gsis_id"], how="left"))


# ----------------------------------------------------------------------------- candidates


def candidate_features(cfg: ChallengerConfig, position: str, group: str) -> list[str]:
    feats = list(cfg.features(position))
    return feats + [f for f in GROUPS[group][position] if f not in feats]


def _fit(train, cfg, position, feats, k, label="label", name="ifp2") -> RidgeModel:
    spec = cfg.positions[position]
    return fit_linear(train, position, feats, spec["lam"], half_life_weeks=spec.get("half_life_weeks"),
                      target_key=k, label=label, name=name)


def fit_prices(train: pl.DataFrame, half_life_weeks: float | None, k: int) -> np.ndarray:
    """Recombination prices from realized components on training rows: label ~ b0 + b1 (carries - inside-5 carries)
    + b2 inside-5 carries + b3 targets (recency-weighted least squares). Returns [b0, b1, b2, b3]."""
    t = train.filter(pl.all_horizontal([pl.col(c).is_not_null() for c in COMPONENTS]) & pl.col("label").is_not_null())
    x = np.column_stack([np.ones(t.height), (t["y_car"] - t["y_gl"]).to_numpy(), t["y_gl"].to_numpy(),
                         t["y_tgt"].to_numpy()])
    y = t["label"].to_numpy()
    keys = (t["season"] * 100 + t["week"]).to_numpy()
    w = recency_weights(keys, k, half_life_weeks)
    xw = x * w[:, None]
    reg = PRICE_LAM * w.sum() * np.diag([0.0, 1.0, 1.0, 1.0])
    return np.linalg.solve(x.T @ xw + reg, xw.T @ y)


def recombine(prices: np.ndarray, car: np.ndarray, gl: np.ndarray, tgt: np.ndarray) -> np.ndarray:
    """Points from component forecasts: components floored at 0, inside-5 carries capped at carries."""
    car, tgt = np.maximum(car, 0.0), np.maximum(tgt, 0.0)
    gl = np.minimum(np.maximum(gl, 0.0), car)
    return np.maximum(prices[0] + prices[1] * (car - gl) + prices[2] * gl + prices[3] * tgt, 0.0)


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                 adopted: dict[str, dict[str, bool]] | None = None, components: bool = True) -> pl.DataFrame:
    """Per store row of `weeks`: pred_ifp2, pred_<candidate> (null = abstention), and, for RB, the stage-1
    workload forecasts of rb_components and of its volume-only twin (wl_<component>_su / wl_<component>_vol).

    `adopted[candidate][position]` makes pred_<candidate> the frozen composite: a position that did not adopt keeps
    ifp2's prediction. None = every position runs the candidate. Training rows are strictly earlier weeks
    (asserted); each fit uses ifp-2's frozen penalty and recency half-life for the position."""
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            nan = np.full(rows.height, np.nan)
            cols: dict[str, np.ndarray] = {}
            if train.height < MIN_TRAIN_ROWS:
                cols["pred_ifp2"] = nan
                for c in CANDIDATES:
                    cols[f"pred_{c}"] = nan
            else:
                base = _fit(train, cfg, pos, list(cfg.features(pos)), k).predict(rows)
                cols["pred_ifp2"] = base
                for c in ("split_add", "script_only"):
                    use = bool(GROUPS[c][pos]) and (adopted is None or adopted.get(c, {}).get(pos, False))
                    cols[f"pred_{c}"] = (_fit(train, cfg, pos, candidate_features(cfg, pos, c), k, name=c).predict(rows)
                                         if use else base)
                cols["pred_rb_components"] = base
                if pos == "RB" and components:
                    hl = cfg.positions[pos].get("half_life_weeks")
                    fc = {}
                    for tag, feats in (("su", candidate_features(cfg, pos, "split_add")), ("vol", list(cfg.features(pos)))):
                        for comp in COMPONENTS:
                            tr = train.filter(pl.col(comp).is_not_null())
                            fc[(tag, comp)] = (_fit(tr, cfg, pos, feats, k, label=comp, name=f"wl_{comp}").predict(rows)
                                               if tr.height >= MIN_TRAIN_ROWS else nan)
                            cols[f"wl_{comp}_{tag}"] = fc[(tag, comp)]
                    use = adopted is None or adopted.get("rb_components", {}).get(pos, False)
                    if use and all(np.isfinite(fc[("su", c)]).all() for c in COMPONENTS):
                        prices = fit_prices(train, hl, k)
                        cols["pred_rb_components"] = recombine(prices, fc[("su", "y_car")], fc[("su", "y_gl")],
                                                               fc[("su", "y_tgt")])
                elif pos in ("WR", "TE") and components:
                    tr = train.filter(pl.col("y_tgt").is_not_null())
                    for tag, feats in (("su", candidate_features(cfg, pos, "split_add")), ("vol", list(cfg.features(pos)))):
                        cols[f"wl_y_tgt_{tag}"] = _fit(tr, cfg, pos, feats, k, label="y_tgt", name="wl_y_tgt").predict(rows)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, np.where(ok, v, np.nan)).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- metrics


def week_bootstrap(df: pl.DataFrame, a: str, b: str, *, loss: str = "absolute", label: str = "label",
                   resamples: int = 1000, seed: int = SEED, alpha: float = 0.05) -> dict:
    """CI of mean loss(a) - mean loss(b) on rows where both and the label are present, resampling whole NFL weeks
    (`key`). loss: absolute | squared. `alpha` sets the two-sided interval (0.05 / m for Bonferroni)."""
    d = df.filter(pl.col(a).is_not_null() & pl.col(b).is_not_null() & pl.col(label).is_not_null())
    if d.height == 0:
        return {"diff": None, "ci": None, "n": 0, "weeks": 0}
    f = (lambda e: e.abs()) if loss == "absolute" else (lambda e: e ** 2)
    d = d.with_columns(f(pl.col(label) - pl.col(a)).alias("_la"), f(pl.col(label) - pl.col(b)).alias("_lb"))
    by = d.group_by("key").agg(pl.col("_la").sum(), pl.col("_lb").sum(), pl.len().alias("n")).sort("key")
    la, lb, n = by["_la"].to_numpy(), by["_lb"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (la[idx].sum(1) - lb[idx].sum(1)) / n[idx].sum(1)
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"diff": float((la.sum() - lb.sum()) / n.sum()), "ci": [float(lo), float(hi)], "n": int(n.sum()),
            "weeks": int(len(n))}


def point_metrics(df: pl.DataFrame, col: str, label: str = "label") -> dict:
    d = df.filter(pl.col(col).is_not_null() & pl.col(label).is_not_null())
    if d.height == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    e = (d[label] - d[col]).to_numpy()
    return {"n": int(len(e)), "mae": float(np.abs(e).mean()), "rmse": float(np.sqrt((e ** 2).mean())),
            "bias": float(e.mean())}


# ----------------------------------------------------------------------------- cohorts (pregame quantities only)

# Archetype thresholds, fixed from feature distributions on the development window (no outcomes; section 3)
EARLY_DOWN_SHARE = 0.40      # >= 40% of the team's early-down designed carries
PASS_DOWN_SHARE = 0.08       # >= 8% of the team's passing-down targets (about the 70th RB percentile)
SCRIPT_EDGE = 1.5            # expected margin of at least +1.5 (positive script) / at most -1.5 (negative)
GOAL_LINE_SHARE = 0.50       # >= 50% of the team's inside-5 designed carries


def cohorts() -> dict[str, pl.Expr]:
    """Pregame cohort definitions. The handoff columns (ho_*, tl_*) come from app.analytics.injury_workload,
    the same definitions as the injury-handoff study, so its failed returning-player check is re-asked here."""
    from .forecast_backtest import starters_pool_expr

    rb, wr, te = (pl.col("position") == p for p in ("RB", "WR", "TE"))
    early = (pl.col("su_car_early_share") >= EARLY_DOWN_SHARE) & (pl.col("su_tgt_pd_share") < PASS_DOWN_SHARE)
    pdb = (pl.col("su_tgt_pd_share") >= PASS_DOWN_SHARE) & (pl.col("su_car_early_share") < EARLY_DOWN_SHARE)
    pos_script, neg_script = pl.col("su_exp_margin") >= SCRIPT_EDGE, pl.col("su_exp_margin") <= -SCRIPT_EDGE
    lead_out = (pl.col("ho_lead_out") == 1) & (pl.col("ho_is_lead") == 0)
    lead_back = (pl.col("ho_lead_returning") == 1) & (pl.col("ho_is_lead") == 0)
    self_ret = pl.col("tl_status_class") == "available_returning"
    opp = pl.col("carries_pg").fill_null(0) + pl.col("targets_pg").fill_null(0)
    return {
        "rb_early_down_back": rb & early,
        "rb_early_down_pos_script": rb & early & pos_script,
        "rb_early_down_neg_script": rb & early & neg_script,
        "rb_passing_down_back": rb & pdb,
        "rb_passing_down_neg_script": rb & pdb & neg_script,
        "rb_passing_down_pos_script": rb & pdb & pos_script,
        "rb_goal_line_back": rb & (pl.col("su_car_i5_share") >= GOAL_LINE_SHARE),
        "rb_lead_out_teammates": rb & lead_out,
        "rb_backup_takeover": rb & lead_out & (pl.col("ho_car_share") >= 0.5),
        "rb_backup_after_lead_returns": rb & lead_back,
        "rb_lead_returning": rb & (pl.col("ho_is_lead") == 1) & self_ret,
        "self_returning_any": self_ret,
        "wr1_out_teammates": wr & lead_out,
        "te1_out_teammates": te & lead_out,
        "rec_passing_down_neg_script": (wr | te) & (pl.col("su_tgt_pd_share") >= 0.20) & neg_script,
        "high_volume": opp >= 15,
        "low_volume": opp < 4,
        "starters_pool": starters_pool_expr(),
    }


# Cohorts the hypotheses name (mechanism checks) and cohorts that must not get worse (guards)
MECHANISM = {"split_add": ("rb_early_down_pos_script", "rb_passing_down_neg_script", "rb_goal_line_back"),
             "script_only": ("rb_early_down_pos_script", "rb_passing_down_neg_script"),
             "rb_components": ("rb_goal_line_back", "rb_passing_down_back", "rb_early_down_back")}
GUARDS = ("rb_lead_out_teammates", "rb_backup_takeover", "self_returning_any", "rb_lead_returning",
          "high_volume", "low_volume", "starters_pool")
MECHANISM_MIN_N = 100

# ----------------------------------------------------------------------------- decision rules (pre-registered)

DEV_MAX_MSE_DIFF = 0.0
BIAS_TOLERANCE = 0.15
CONF_MAE_FLOOR = -0.010       # the confirmation gain must be at least 0.01 points of MAE
STARTERS_TOLERANCE = 0.005    # starters-pool MAE not worse by > 0.5%
PAIRS_TOLERANCE = 0.005       # Start/Sit pair accuracy not lower by > 0.5 pp

DECISION_RULES = {
    "development": "2021-2022 only. For each (candidate, position) with a feature group (split_add, script_only: "
                   "RB/WR/TE; rb_components: RB), versus ifp2 on identical rows: adopt if (a) MAE diff < 0 and its "
                   "week-clustered 95% CI upper bound < 0, (b) MSE diff <= 0, (c) |bias| not worse by > 0.15. "
                   "No hyperparameter is searched: every fit uses ifp-2's frozen per-position penalty and half-life.",
    "confirmation": "2023-2024, run once, frozen adoption, per adopted (candidate, position) vs ifp2 on identical "
                    "rows. Bonferroni over the number of adopted pairs m (alpha = 0.05 / m). PASS only if ALL: "
                    "(c1) MAE diff <= -0.010 with the adjusted CI upper bound < 0; (c2) MAE diff < 0 in 2023 and in "
                    "2024 separately; (c3) MSE diff <= 0; (c4) |bias| not worse by > 0.15; (c5) starters-pool MAE "
                    "not worse by > 0.5%; (c6) Start/Sit pair accuracy not lower by > 0.5 pp; (c7) no guard cohort "
                    "(RB lead out -> teammates, RB backup takeover, returning player himself, RB lead returning, "
                    "high volume, low volume, starters pool; for the candidate's position) with n >= 100 whose MAE "
                    "diff has an adjusted CI lower bound > 0, or whose |bias| grows by > 0.15; (c8) mechanism: every "
                    "named cohort of the candidate with n >= 100 has MSE diff < 0.",
    "outcome": "a passing (candidate, position) is 'supported_for_shadow_tracking' (a recommendation to record frozen "
               "pregame forecasts; nothing more); otherwise 'not_supported'. Nothing adopted -> 'not_supported'.",
    "workload": "secondary, reported for every RB row with labels: out-of-sample MSE of next-game carries, inside-5 "
                "carries and targets, situational stage 1 (ifp-2 features + split_add group) vs the volume-only "
                "stage 1 (ifp-2 features), week-clustered 95% CI; development and confirmation. Answers 'do splits "
                "predict workload better than total volume' independent of the points verdict; it decides nothing.",
    "exploratory": "2025-2026 weeks 1-5 replay reported only (earlier studies inspected those weeks).",
    "never": "no promotion, no registry change, no change to ifp-2.0.0; historical replays never establish promotion "
             "readiness; ESPN is never an input",
}


def development_adoption(p: pl.DataFrame) -> dict[str, dict[str, dict]]:
    """DECISION_RULES['development'] on development predictions `p` (pred_ifp2, pred_<candidate>, label)."""
    out: dict[str, dict[str, dict]] = {}
    for c in CANDIDATES:
        out[c] = {}
        positions = ["RB"] if c == "rb_components" else [q for q in POSITIONS if GROUPS[c][q]]
        for pos in positions:
            g = p.filter((pl.col("position") == pos) & pl.col("label").is_not_null()
                         & pl.col(f"pred_{c}").is_not_null() & pl.col("pred_ifp2").is_not_null())
            mae = week_bootstrap(g, f"pred_{c}", "pred_ifp2")
            mse = week_bootstrap(g, f"pred_{c}", "pred_ifp2", loss="squared")
            b, a = point_metrics(g, "pred_ifp2"), point_metrics(g, f"pred_{c}")
            checks = {"a_mae": mae["diff"] is not None and mae["diff"] < 0 and mae["ci"][1] < 0,
                      "b_mse": mse["diff"] is not None and mse["diff"] <= DEV_MAX_MSE_DIFF,
                      "c_bias": a["n"] > 0 and abs(a["bias"]) <= abs(b["bias"]) + BIAS_TOLERANCE}
            out[c][pos] = {"mae": mae, "mse": mse, "base": b, "cand": a, "checks": checks,
                           "adopt": all(checks.values())}
    return out


def verdict(p: pl.DataFrame, adoption: dict[str, dict[str, bool]]) -> dict:
    """DECISION_RULES['confirmation'] on confirmation predictions `p` (identical rows, cohort columns present)."""
    from .forecast_backtest import pair_decisions

    pairs_ = [(c, pos) for c, d in adoption.items() for pos, v in d.items() if v]
    if not pairs_:
        return {"adopted": [], "outcome": "not_supported", "reason": "nothing adopted on development"}
    alpha = 0.05 / len(pairs_)
    flt = cohorts()
    res = {}
    for c, pos in pairs_:
        col = f"pred_{c}"
        g = p.filter((pl.col("position") == pos) & pl.col("label").is_not_null() & pl.col(col).is_not_null()
                     & pl.col("pred_ifp2").is_not_null())
        mae = week_bootstrap(g, col, "pred_ifp2", alpha=alpha)
        mse = week_bootstrap(g, col, "pred_ifp2", loss="squared")
        by_season = {int(s): week_bootstrap(g.filter(pl.col("season") == s), col, "pred_ifp2")["diff"]
                     for s in sorted(g["season"].unique().to_list())}
        b, a = point_metrics(g, "pred_ifp2"), point_metrics(g, col)
        sp = g.filter(flt["starters_pool"])
        sb, sa = point_metrics(sp, "pred_ifp2"), point_metrics(sp, col)
        pr = pair_decisions(g, [col, "pred_ifp2"], exact=True)["all"]
        guards, mech = {}, {}
        for name in GUARDS:
            cg = g.filter(flt[name].fill_null(False))
            if cg.height >= MECHANISM_MIN_N:
                d = week_bootstrap(cg, col, "pred_ifp2", alpha=alpha)
                cb, ca = point_metrics(cg, "pred_ifp2"), point_metrics(cg, col)
                guards[name] = {"n": cg.height, "mae_diff": d, "bias": [cb["bias"], ca["bias"]],
                                "pass": d["ci"][0] <= 0 and abs(ca["bias"]) <= abs(cb["bias"]) + BIAS_TOLERANCE}
        for name in MECHANISM[c]:
            cg = g.filter(flt[name].fill_null(False))
            if cg.height >= MECHANISM_MIN_N:
                d = week_bootstrap(cg, col, "pred_ifp2", loss="squared")
                mech[name] = {"n": cg.height, "mse_diff": d, "pass": d["diff"] < 0}
        checks = {
            "c1_mae": mae["diff"] <= CONF_MAE_FLOOR and mae["ci"][1] < 0,
            "c2_both_seasons": len(by_season) >= 2 and all(v is not None and v < 0 for v in by_season.values()),
            "c3_mse": mse["diff"] <= 0,
            "c4_bias": abs(a["bias"]) <= abs(b["bias"]) + BIAS_TOLERANCE,
            "c5_starters": sb["n"] == 0 or sa["mae"] <= sb["mae"] * (1 + STARTERS_TOLERANCE),
            "c6_pairs": pr[col]["accuracy"] >= pr["pred_ifp2"]["accuracy"] - PAIRS_TOLERANCE,
            "c7_guards": all(v["pass"] for v in guards.values()),
            "c8_mechanism": all(v["pass"] for v in mech.values()),
        }
        res[f"{c}:{pos}"] = {"n": g.height, "alpha": alpha, "mae": mae, "mse": mse, "by_season_mae_diff": by_season,
                             "base": b, "cand": a, "starters": [sb, sa],
                             "pairs": {"cand": pr[col], "ifp2": pr["pred_ifp2"]}, "guards": guards,
                             "mechanism": mech, "checks": checks,
                             "outcome": "supported_for_shadow_tracking" if all(checks.values()) else "not_supported"}
    return {"adopted": [f"{c}:{pos}" for c, pos in pairs_], "results": res,
            "outcome": {k: v["outcome"] for k, v in res.items()}}


def workload_report(p: pl.DataFrame) -> dict:
    """DECISION_RULES['workload']: stage-1 workload forecasts, situational vs volume-only (RB: carries, inside-5
    carries, targets; WR/TE: targets)."""
    out = {}
    for pos, comp in [("RB", c) for c in COMPONENTS] + [("WR", "y_tgt"), ("TE", "y_tgt")]:
        g = p.filter(pl.col("position") == pos)
        su, vol = f"wl_{comp}_su", f"wl_{comp}_vol"
        if su not in g.columns or g.filter(pl.col(su).is_not_null()).height == 0:
            continue
        r = {"su": point_metrics(g, su, label=comp), "vol": point_metrics(g, vol, label=comp),
             "mse_diff": week_bootstrap(g, su, vol, loss="squared", label=comp),
             "mae_diff": week_bootstrap(g, su, vol, label=comp)}
        flt = cohorts()
        r["cohorts"] = {}
        for name in ("rb_early_down_back", "rb_passing_down_back", "rb_goal_line_back", "rb_lead_out_teammates",
                     "rb_early_down_pos_script", "rb_passing_down_neg_script", "wr1_out_teammates",
                     "te1_out_teammates", "rec_passing_down_neg_script", "self_returning_any"):
            cg = g.filter(flt[name].fill_null(False))
            if cg.height >= 30:
                r["cohorts"][name] = {"n": cg.height, "mse_diff": week_bootstrap(cg, su, vol, loss="squared", label=comp),
                                      "bias_su": point_metrics(cg, su, label=comp)["bias"],
                                      "bias_vol": point_metrics(cg, vol, label=comp)["bias"]}
        out[f"{pos}:{comp}"] = r
    return out


__all__ = ["SU_VERSION", "SU_STATUS", "FEATURE_DOCS", "GROUPS", "CANDIDATES", "DECISION_RULES", "classify_situations",
           "week_splits", "situational_features", "attach", "walk_forward", "fit_prices", "recombine", "cohorts",
           "development_adoption", "verdict", "workload_report", "week_bootstrap", "point_metrics"]

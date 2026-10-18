"""Research only: receiver role x expected-QB passing quality (ifp-qbr) on top of ifp-2 and ifp-ctx A.

Nothing in the app imports this module (tests/test_qb_receiver_compat.py). It extends the ifp-ctx
QB-context candidate A (context_challenger.GROUPS["qb"]) with role-aware interactions. It never
changes ifp-ctx, ifp-ctx-p1 or its write-once ledger. ifp-1.0.0, ifp-2.0.0, the model registry,
production runs, Start/Sit, trades and waivers are untouched. See docs/research/qb_receiver_compatibility.md.

Why. ifp-ctx found that a changed expected QB moves WR projections only a little (changed-QB WR
cohort -0.03 to -0.10 MAE), and candidate A was flat for WR/TE pooled. A's QB deltas are role-blind:
every receiver on a team gets the same delta. The hypothesis tested here: a deep-threat receiver
(high deep-target share, high air-yards share) loses more with a weaker or shorter-throwing backup
than a short-route receiver on the same team, so a role x QB-delta interaction should capture more.

Definitions (fixed before the first points backtest; constants are never tuned):
  * Source: nflverse play-by-play (`load_pbp`, the same free CC-BY feed sync.py already derives
    nfl_pbp_player_week from), REG season, pass plays only (`pass_game_tables`). Only plays of games
    strictly before (S, W) are used for a week-W row.
  * QB passing profile (`qb_pass_profiles`), current + QB_LOOKBACK_SEASONS previous seasons before W:
    CPOE (attempt-weighted), deep-ball CPOE (air_yards >= DEEP_AIR_YARDS), deep attempt rate, aDOT,
    sack rate and EPA per dropback. Each is shrunk toward the replacement-level prior (pooled rates of
    completed QB-seasons with < LOW_VOLUME_QB_SEASON dropbacks, as roster_context.qb_profiles), so a
    backup with 15 attempts is pulled toward backups. The shrinkage constants are the samples at which
    within-QB play noise equals the between-QB spread (reliability 0.5), measured once on 2018-2020
    play-by-play (no points labels, before the development window) and then frozen.
  * Expected starter: ifp-ctx's pregame identification (roster_context.expected_starters, column
    expected_qb_id of the ctx store). Recent QBs: the starters of the player's last <= 5 games, the
    same definition as roster_context._recent_qb_env. Deltas are expected minus the recent mean, both
    as of W, so the same QB in every recent game gives a delta of exactly 0.
  * Receiver role (`receiver_roles`), the player's last <= REC_WINDOW games with a target before W
    (current + previous season, any team): aDOT, deep-target share, EPA per target (each shrunk toward
    the league rate of his position as of W), air-yards share and target share of his team's pass
    attempts in those games (unshrunk shares).
  * Interactions (`GROUP`, WR and TE rows): `qbr_deep_x` = deep-target share x expected-QB deep-CPOE
    delta; `qbr_vol_x` = deep-target share x deep-attempt-rate delta; `qbr_epa_x` = air-yards share x
    ifp-ctx's qb_epa_delta. With A's main effects present, b1 * delta + b2 * role * delta is a QB
    effect whose slope depends on role, which is the hypothesis.
  * Unknown stays null: no expected QB, no recent starter or no target history gives null, and the
    ridge imputes the training mean with a __missing indicator. A known QB with no attempts gets the
    prior (the n = 0 shrinkage estimate).

No fantasy-point adjustment is made here: any effect on points is a ridge coefficient fitted
walk-forward on ifp-2's own spec (features, frozen penalty, recency weights).
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, fit_linear, train_rows
from .context_challenger import candidate_features as ctx_candidate_features
from .forecast_features import key
from .independent_projections import MIN_TRAIN_ROWS, RidgeModel
from .roster_context import LOW_VOLUME_QB_SEASON, QB_LOOKBACK_SEASONS, shrink

QBR_VERSION = "qbr-0.1.0"                 # features
QBR_CHALLENGER_VERSION = "ifp-qbr-0.1.0"
QBR_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}

DEEP_AIR_YARDS = 20.0     # a deep attempt / target: air_yards >= 20 (the brief's definition)
RECENT_GAMES = 5          # recent QBs: the starters of the player's last <= 5 games (as roster_context)
REC_WINDOW = 10           # receiver role: his last <= 10 games with a target, current + previous season
# Reliability-0.5 samples, measured once on 2018-2020 play-by-play (within-QB variance / between-QB variance of
# QB-seasons, method of moments; docs section 2) and rounded. Never tuned on points.
QB_SHRINK = {"cpoe": 250.0,        # attempts with a cpoe value (measured 237)
             "deep_cpoe": 250.0,   # deep attempts with a cpoe value (measured 264): deep CPOE is barely QB-specific
             "deep_rate": 250.0,   # attempts with air yards (measured 242)
             "adot": 150.0,        # attempts with air yards (measured 147)
             "sack_rate": 200.0,   # dropbacks (measured 198)
             "epa": 200.0}         # dropbacks (measured 205; roster_context uses 200)
REC_SHRINK = {"adot": 5.0,         # targets with air yards (measured 4.7)
              "deep_share": 20.0,  # targets with air yards (measured 17)
              "epa_tgt": 120.0}    # targets with an EPA value (measured 121)

QBR_DOCS: dict[str, str] = {
    "exp_qb_deep_cpoe": "Expected starter's CPOE on deep attempts (air_yards >= 20), shrunk toward the replacement prior "
                        "by 250 deep attempts",
    "exp_qb_deep_rate": "Expected starter's share of attempts with air_yards >= 20, shrunk by 250 attempts",
    "exp_qb_adot": "Expected starter's air yards per attempt, shrunk by 150 attempts",
    "exp_qb_sack_rate": "Expected starter's sacks per dropback, shrunk by 200 dropbacks",
    "exp_qb_pbp_attempts": "Attempts behind the expected starter's profile (0 = known QB with no history: the prior)",
    "qb_deep_cpoe_delta": "Expected starter's deep CPOE minus the mean of the starters of the player's last <= 5 games",
    "qb_deep_rate_delta": "Deep attempt rate delta, same definition",
    "qb_adot_delta": "aDOT delta, same definition",
    "qb_sack_delta": "Sack rate delta, same definition",
    "qb_pbp_cpoe_delta": "Play-by-play CPOE delta (shrunk by 250 attempts; cross-check of ifp-ctx's qb_cpoe_delta)",
    "rec_adot": "Player's air yards per target over his last <= 10 games with a target, shrunk toward his position's "
                "league rate by 5 targets",
    "rec_deep_share": "Player's share of targets with air_yards >= 20, same window, shrunk toward his position by 20 targets",
    "rec_ay_share": "Player's share of his team's air yards in those games (unshrunk)",
    "rec_tgt_share": "Player's share of his team's targets in those games (unshrunk)",
    "rec_epa_tgt": "Player's EPA per target, same window, shrunk toward his position by 120 targets",
    "rec_targets": "Targets in the role window",
    "qbr_deep_x": "rec_deep_share x qb_deep_cpoe_delta (feature)",
    "qbr_vol_x": "rec_deep_share x qb_deep_rate_delta (feature)",
    "qbr_epa_x": "rec_ay_share x qb_epa_delta (ifp-ctx's EPA/dropback delta) (feature)",
}
QB_FIELDS = ("cpoe", "deep_cpoe", "deep_rate", "adot", "sack_rate", "epa")
QBR_COLUMNS = ("exp_qb_deep_cpoe", "exp_qb_deep_rate", "exp_qb_adot", "exp_qb_sack_rate", "exp_qb_pbp_attempts",
               "qb_deep_cpoe_delta", "qb_deep_rate_delta", "qb_adot_delta", "qb_sack_delta", "qb_pbp_cpoe_delta",
               "rec_adot", "rec_deep_share", "rec_ay_share", "rec_tgt_share", "rec_epa_tgt", "rec_targets",
               "qbr_deep_x", "qbr_vol_x", "qbr_epa_x")
FEATURE_COLUMNS = ("qbr_deep_x", "qbr_vol_x", "qbr_epa_x")
assert set(QBR_COLUMNS) == set(QBR_DOCS)

# The interaction group (fixed before any backtest). WR and TE only: the hypothesis is about receivers;
# QB and RB rows are out of scope by pre-registration and keep ifp2.
POSITIONS = ("WR", "TE")
GROUP: dict[str, list[str]] = {"WR": list(FEATURE_COLUMNS), "TE": list(FEATURE_COLUMNS)}
# Q: ifp-ctx A (role-blind QB context) + the interactions. CQ: ifp-ctx C (A + teammates, ifp-ctx-p1's WR spec)
# + the interactions, descriptive only. A and C are fitted on the same rows as comparators.
CANDIDATES = {"A": ("A", False), "Q": ("A", True), "C": ("C", False), "CQ": ("C", True)}
CANDIDATE = "Q"

# Role cohorts: fixed thresholds on the shrunk deep-target share, the 75th and 25th percentiles of WR rows
# 2020-2026 (build diagnostics, no points labels; docs section 2). TE rows are mostly "short route".
DEEP_THREAT = 0.20
SHORT_ROUTE = 0.13
DOWNGRADE = -0.03        # qb_epa_delta below this: a materially weaker expected QB (EPA/dropback)

# Pre-registered (committed with the code, before the first development run). Thresholds are ifp-ctx's.
DECISION_RULES = {
    "selection": "development 2021-2022 only, per position (WR, TE): Q is adopted if MAE(Q) - MAE(ifp2) <= -0.010 with "
                 "a week-clustered bootstrap 95% CI upper bound < 0, |bias| not worse than ifp2's by more than 0.15, AND "
                 "MAE(Q) - MAE(A) < 0 with CI upper < 0 (the gain must come from role-awareness, not from A's "
                 "role-blind QB context); otherwise the position keeps ifp2. The composite is 'qbr'.",
    "confirmation": "2023-2024, run once, frozen composite vs ifp2 on identical rows, adopted positions only: (1) pooled "
                    "MAE diff < 0 with CI upper < 0; (2) no adopted position significantly worse (CI lower > 0); "
                    "(3) pooled RMSE not worse by > 0.5%; (4) |bias| not worse by > 0.15; (5) starters-pool MAE not "
                    "worse by > 1%; (6) Start/Sit pair accuracy not lower by > 0.5 pp; (7) mechanism: on each adopted "
                    "position's changed-expected-QB rows, MAE(qbr) < MAE(ifp2) AND MAE(qbr) < MAE(A).",
    "outcome": "all pass -> 'prospective_shadow' (a recommendation to pre-register a NEW frozen pregame candidate; it "
               "changes nothing by itself); any fail, or nothing adopted -> 'retain_baseline'.",
    "exploratory": "2025-2026 replay is reported only: already inspected by earlier work, and 2025-26 injury reports "
                   "carry no modification timestamp (retrospective).",
    "descriptive": "A, C and CQ are fitted on the same rows and reported; the changed-QB cohort is reported by role "
                   "(deep threat >= 0.20 deep-target share, short route <= 0.13) and for QB downgrades, with bias. "
                   "None of these select anything.",
    "never": "no promotion, no registry change, no change to ifp-ctx-p1 or its ledger; historical replays never "
             "establish promotion readiness",
}
SELECTION_MAX_DIFF = -0.010
BIAS_TOLERANCE = 0.15
MECHANISM_COHORT = "changed_qb"


# ----------------------------------------------------------------------------- play-by-play tables


PBP_COLUMNS = ("season", "week", "season_type", "game_id", "play_id", "posteam", "play_type", "qb_dropback",
               "pass_attempt", "sack", "qb_scramble", "two_point_attempt", "air_yards", "cpoe", "epa", "qb_epa",
               "passer_player_id", "rusher_player_id", "receiver_player_id")


def _f(c: str) -> pl.Expr:
    return pl.col(c).fill_null(0) == 1


def pass_game_tables(pbp: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Per QB-game and per receiver-game passing aggregates from nflverse play-by-play (REG only,
    two-point tries excluded). Dropbacks include sacks and scrambles (a scramble is charged to the
    scrambling QB); attempts exclude sacks; air yards / CPOE sums count only plays with a value."""
    p = pbp.filter((pl.col("season_type") == "REG") & ~_f("two_point_attempt"))
    deep = pl.col("air_yards") >= DEEP_AIR_YARDS
    att = _f("pass_attempt") & ~_f("sack") & pl.col("passer_player_id").is_not_null()
    db = (p.filter(_f("qb_dropback"))
          .with_columns(pl.when(_f("qb_scramble")).then(pl.coalesce("passer_player_id", "rusher_player_id"))
                        .otherwise(pl.col("passer_player_id")).alias("qb"))
          .filter(pl.col("qb").is_not_null())
          .group_by("season", "week", "posteam", "qb").agg(
              pl.len().alias("dropbacks"), _f("sack").sum().alias("sacks"),
              pl.col("qb_epa").fill_null(0).sum().alias("epa_sum")))
    a = p.filter(att).group_by("season", "week", "posteam", pl.col("passer_player_id").alias("qb")).agg(
        pl.len().alias("attempts"),
        pl.col("cpoe").is_not_null().sum().alias("cpoe_n"), pl.col("cpoe").drop_nulls().sum().alias("cpoe_sum"),
        pl.col("air_yards").is_not_null().sum().alias("ay_n"), pl.col("air_yards").drop_nulls().sum().alias("ay_sum"),
        deep.fill_null(False).sum().alias("deep_att"),
        (deep & pl.col("cpoe").is_not_null()).fill_null(False).sum().alias("deep_cpoe_n"),
        pl.col("cpoe").filter(deep.fill_null(False) & pl.col("cpoe").is_not_null()).sum().alias("deep_cpoe_sum"))
    qb = (db.join(a, on=["season", "week", "posteam", "qb"], how="full", coalesce=True)
          .rename({"posteam": "team", "qb": "gsis_id"}).fill_null(0))
    t = p.filter(att & pl.col("receiver_player_id").is_not_null())
    team = t.group_by("season", "week", "posteam").agg(pl.len().alias("team_targets"),
                                                       pl.col("air_yards").drop_nulls().sum().alias("team_ay"))
    rec = (t.group_by("season", "week", "posteam", pl.col("receiver_player_id").alias("gsis_id")).agg(
        pl.len().alias("targets"),
        pl.col("air_yards").is_not_null().sum().alias("ay_n"), pl.col("air_yards").drop_nulls().sum().alias("ay_sum"),
        deep.fill_null(False).sum().alias("deep_tgt"),
        pl.col("epa").is_not_null().sum().alias("epa_n"), pl.col("epa").drop_nulls().sum().alias("epa_sum"))
        .join(team, on=["season", "week", "posteam"], how="left").rename({"posteam": "team"}))
    cast = {c: pl.Float64 for c in (*qb.columns, *rec.columns) if c not in ("season", "week", "team", "gsis_id")}
    return (qb.with_columns([pl.col(c).cast(pl.Float64) for c in qb.columns if c in cast]).sort("season", "week"),
            rec.with_columns([pl.col(c).cast(pl.Float64) for c in rec.columns if c in cast]).sort("season", "week"))


def _before(df: pl.DataFrame, season: int, week: int, lookback: int) -> pl.DataFrame:
    return df.filter((pl.col("season") * 100 + pl.col("week") < key(season, week))
                     & (pl.col("season") >= season - lookback))


# ----------------------------------------------------------------------------- QB profiles


_QB_RATES = {  # field -> (numerator, denominator)
    "cpoe": ("cpoe_sum", "cpoe_n"), "deep_cpoe": ("deep_cpoe_sum", "deep_cpoe_n"), "deep_rate": ("deep_att", "ay_n"),
    "adot": ("ay_sum", "ay_n"), "sack_rate": ("sacks", "dropbacks"), "epa": ("epa_sum", "dropbacks")}


def qb_pass_profiles(qb_games: pl.DataFrame, season: int, week: int) -> tuple[dict[str, dict], dict]:
    """Shrunk passing profile per QB as of week W ({gsis: {field: rate, 'attempts': n}}) and the
    replacement-level prior: pooled rates of completed QB-seasons in the window with fewer than
    LOW_VOLUME_QB_SEASON dropbacks (roster_context.qb_profiles' rule)."""
    rows = _before(qb_games, season, week, QB_LOOKBACK_SEASONS)
    sums = [pl.col(c).sum() for c in ("dropbacks", "sacks", "epa_sum", "attempts", "cpoe_n", "cpoe_sum", "ay_n",
                                      "ay_sum", "deep_att", "deep_cpoe_n", "deep_cpoe_sum")]
    per_qb = rows.group_by("gsis_id").agg(sums)
    seasons = rows.group_by("gsis_id", "season").agg(sums)
    done = seasons.filter(pl.col("season") < season)
    low = done.filter(pl.col("dropbacks") < LOW_VOLUME_QB_SEASON)
    base = low if low.height and low["attempts"].sum() else (done if done.height else seasons)

    def rate(df, num, den):
        d = float(df[den].sum()) if df.height else 0.0
        return float(df[num].sum()) / d if d else 0.0

    prior = {f: rate(base, n, d) for f, (n, d) in _QB_RATES.items()}
    out = {}
    for r in per_qb.iter_rows(named=True):
        out[r["gsis_id"]] = {f: shrink(r[n], r[d], prior[f], QB_SHRINK[f]) for f, (n, d) in _QB_RATES.items()}
        out[r["gsis_id"]]["attempts"] = r["attempts"]
    return out, prior


def pass_profile_of(profiles: dict, prior: dict, qb: str | None) -> dict | None:
    """Known QB -> profile (prior with 0 attempts if he has none); unknown (None) -> None."""
    if qb is None:
        return None
    return profiles.get(qb) or {**prior, "attempts": 0.0}


# ----------------------------------------------------------------------------- receiver roles


def receiver_roles(rec_games: pl.DataFrame, positions: dict[str, str], season: int, week: int) -> dict[str, dict]:
    """Role per receiver as of week W over his last <= REC_WINDOW games with a target (current and
    previous season, any team). Rates shrink toward the league rate of his position in the same
    lookback (games before W); shares are unshrunk."""
    rows = _before(rec_games, season, week, 1)
    if rows.height == 0:
        return {}
    pos = pl.DataFrame({"gsis_id": list(positions), "pos": list(positions.values())},
                       schema={"gsis_id": pl.String, "pos": pl.String})
    rows = rows.join(pos, on="gsis_id", how="left")
    lg = {r.pop("pos"): r for r in rows.group_by("pos").agg(
        (pl.col("ay_sum").sum() / pl.col("ay_n").sum()).alias("adot"),
        (pl.col("deep_tgt").sum() / pl.col("ay_n").sum()).alias("deep_share"),
        (pl.col("epa_sum").sum() / pl.col("epa_n").sum()).alias("epa_tgt")).iter_rows(named=True)}
    win = (rows.sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(REC_WINDOW)
           .group_by("gsis_id").agg(pl.col("pos").first(), *[pl.col(c).sum() for c in (
               "targets", "ay_n", "ay_sum", "deep_tgt", "epa_n", "epa_sum", "team_targets", "team_ay")]))
    out = {}
    for r in win.iter_rows(named=True):
        p = lg.get(r["pos"])
        if p is None:
            continue
        out[r["gsis_id"]] = {
            "rec_adot": shrink(r["ay_sum"], r["ay_n"], p["adot"] or 0.0, REC_SHRINK["adot"]),
            "rec_deep_share": shrink(r["deep_tgt"], r["ay_n"], p["deep_share"] or 0.0, REC_SHRINK["deep_share"]),
            "rec_epa_tgt": shrink(r["epa_sum"], r["epa_n"], p["epa_tgt"] or 0.0, REC_SHRINK["epa_tgt"]),
            "rec_ay_share": r["ay_sum"] / r["team_ay"] if r["team_ay"] else None,
            "rec_tgt_share": r["targets"] / r["team_targets"] if r["team_targets"] else None,
            "rec_targets": r["targets"]}
    return out


# ----------------------------------------------------------------------------- features


def recent_starters(panel: pl.DataFrame, starters: pl.DataFrame, season: int, week: int) -> dict[str, list[str]]:
    """Per player: the known starting QBs of his last <= RECENT_GAMES games before W (any team, the
    current and previous season), most recent first. Same rule as roster_context._recent_qb_env."""
    from .forecast_features import PRIOR_SEASONS

    p = (panel.filter((pl.col("season") * 100 + pl.col("week") < key(season, week))
                      & (pl.col("season") >= season - PRIOR_SEASONS))
         .sort(["season", "week"], descending=True).group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
         .join(starters, on=["season", "week", "team"], how="left"))
    return {g[0]: [q for q in grp["qb_id"].to_list() if q is not None]
            for g, grp in p.group_by("gsis_id", maintain_order=True)}


def _schema() -> dict:
    return {"gsis_id": pl.String, **{c: pl.Float64 for c in QBR_COLUMNS}}


def qbr_for_week(qb_games: pl.DataFrame, rec_games: pl.DataFrame, panel: pl.DataFrame, starters: pl.DataFrame,
                 positions: dict[str, str], season: int, week: int, targets: pl.DataFrame) -> pl.DataFrame:
    """Role x QB features for `targets` (gsis_id, position, expected_qb_id, qb_epa_delta: the ifp-ctx
    store columns) in week (S, W). Every input is from games strictly before (S, W) except the
    pregame expected starter, which ifp-ctx identified."""
    schema = _schema()
    if targets.height == 0:
        return pl.DataFrame(schema=schema)
    profiles, prior = qb_pass_profiles(qb_games, season, week)
    roles = receiver_roles(rec_games, positions, season, week)
    recent = recent_starters(panel, starters, season, week)
    rows = []
    for g, pos, exp_id, epa_delta in targets.select("gsis_id", "position", "expected_qb_id",
                                                    "qb_epa_delta").iter_rows():
        r = {"gsis_id": g, **{c: None for c in QBR_COLUMNS}}
        r.update(roles.get(g, {}))
        exp = pass_profile_of(profiles, prior, exp_id)
        if exp is not None:
            r.update(exp_qb_deep_cpoe=exp["deep_cpoe"], exp_qb_deep_rate=exp["deep_rate"], exp_qb_adot=exp["adot"],
                     exp_qb_sack_rate=exp["sack_rate"], exp_qb_pbp_attempts=exp["attempts"])
            qs = recent.get(g, [])
            if qs:
                rec = [pass_profile_of(profiles, prior, q) for q in qs]
                mean = {f: sum(x[f] for x in rec) / len(rec) for f in QB_FIELDS}
                r.update(qb_deep_cpoe_delta=exp["deep_cpoe"] - mean["deep_cpoe"],
                         qb_deep_rate_delta=exp["deep_rate"] - mean["deep_rate"],
                         qb_adot_delta=exp["adot"] - mean["adot"], qb_sack_delta=exp["sack_rate"] - mean["sack_rate"],
                         qb_pbp_cpoe_delta=exp["cpoe"] - mean["cpoe"])
        ds = r["rec_deep_share"]
        if pos in POSITIONS and ds is not None:
            if r["qb_deep_cpoe_delta"] is not None:
                r["qbr_deep_x"] = ds * r["qb_deep_cpoe_delta"]
                r["qbr_vol_x"] = ds * r["qb_deep_rate_delta"]
            if r["rec_ay_share"] is not None and epa_delta is not None:
                r["qbr_epa_x"] = r["rec_ay_share"] * epa_delta
        rows.append(r)
    return pl.DataFrame(rows, schema=schema, orient="row")


# ----------------------------------------------------------------------------- challenger


def candidate_features(cfg: ChallengerConfig, position: str, candidate: str | None) -> list[str]:
    """ifp-2's features (None / 'ifp2'), ifp-ctx A or C, optionally plus the interaction group."""
    if candidate in (None, "ifp2"):
        return list(cfg.features(position))
    base, inter = CANDIDATES[candidate]
    feats = ctx_candidate_features(cfg, position, base)
    if inter:
        feats += [f for f in GROUP[position] if f not in feats]
    return feats


def fit_candidate(train: pl.DataFrame, cfg: ChallengerConfig, position: str, candidate: str | None,
                  target_key: int) -> RidgeModel:
    spec = cfg.positions[position]
    return fit_linear(train, position, candidate_features(cfg, position, candidate), spec["lam"],
                      half_life_weeks=spec.get("half_life_weeks"), target_key=target_key,
                      name=f"qbr_{candidate or 'ifp2'}")


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                 candidates: tuple[str, ...] = tuple(CANDIDATES),
                 per_position: dict[str, str] | None = None) -> pl.DataFrame:
    """pred_ifp2 and pred_<candidate> for the WR/TE store rows of `weeks` (null = abstention: no recent
    game or fewer than MIN_TRAIN_ROWS training rows). `per_position` adds pred_qbr (the composite).
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
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            fitted: dict[str, np.ndarray] = {}
            for tag in ("ifp2", *candidates):
                fitted[tag] = (np.full(rows.height, np.nan) if train.height < MIN_TRAIN_ROWS else
                               np.where(ok, fit_candidate(train, cfg, pos, None if tag == "ifp2" else tag, k)
                                        .predict(rows), np.nan))
            cols = {f"pred_{t}": v for t, v in fitted.items()}
            if per_position is not None:
                tag = per_position.get(pos, "ifp2")
                cols["pred_qbr"] = fitted[tag] if tag in fitted else np.where(
                    ok, fit_candidate(train, cfg, pos, None if tag == "ifp2" else tag, k).predict(rows), np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


def cohorts() -> dict[str, pl.Expr]:
    """Pregame cohorts (the two eval_* use the actual week-W starter, a postgame fact)."""
    from .forecast_backtest import starters_pool_expr

    changed = pl.col("qb_changed") == 1
    down = changed & (pl.col("qb_epa_delta") < DOWNGRADE)
    deep = pl.col("rec_deep_share") >= DEEP_THREAT
    short = pl.col("rec_deep_share") <= SHORT_ROUTE
    deep_down = pl.col("qb_deep_cpoe_delta") < 0
    return {
        "changed_qb": changed,
        "same_qb": pl.col("qb_changed") == 0,
        "changed_qb_deep_threat": changed & deep,
        "changed_qb_short_route": changed & short,
        "qb_downgrade": down,
        "qb_downgrade_deep_threat": down & deep,
        "qb_downgrade_short_route": down & short,
        "qb_upgrade": changed & (pl.col("qb_epa_delta") > -DOWNGRADE),
        "changed_qb_deep_cpoe_down_deep_threat": changed & deep_down & deep,
        "backup_qb_expected": pl.col("prev_starter_status").is_in(["out", "doubtful", "gone", "inactive"])
                              & pl.col("expected_qb_id").is_not_null(),
        "deep_threat": deep,
        "short_route": short,
        "starters_pool": starters_pool_expr(),
        "eval_actual_qb_differs_from_recent": pl.col("actual_qb_id").is_not_null() & pl.col("recent_qb_id").is_not_null()
                                              & (pl.col("actual_qb_id") != pl.col("recent_qb_id")),
        "eval_expected_qb_wrong": pl.col("actual_qb_id").is_not_null() & pl.col("expected_qb_id").is_not_null()
                                  & (pl.col("actual_qb_id") != pl.col("expected_qb_id")),
    }


__all__ = ["QBR_VERSION", "QBR_CHALLENGER_VERSION", "QBR_STATUS", "QBR_DOCS", "GROUP", "CANDIDATES", "DECISION_RULES",
           "pass_game_tables", "qb_pass_profiles", "receiver_roles", "recent_starters", "qbr_for_week",
           "candidate_features", "fit_candidate", "walk_forward", "cohorts"]

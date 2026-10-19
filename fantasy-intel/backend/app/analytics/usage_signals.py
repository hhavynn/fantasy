"""Research only: FTN read-progression targets and timestamped depth-chart signals
(usage-signals research, PR 9). See docs/research/first_read_depth_chart_signals.md.

Nothing here changes ifp-1.0.0 or ifp-2.0.0. Nothing in the app imports this module
(tests/test_usage_signals.py::test_research_module_is_never_imported_by_the_app); only
scripts/usage_signals_research.py does. Outputs live in the git-ignored model directory.

FTN `read_thrown` semantics are DISPUTED (app/nfl/ftn.py, nflverse/nflreadr#325). Two readings
are kept side by side and never mixed:
  dictionary  the nflreadr dictionary (PR #319): "0" first read, "1" second, "2" third or later,
              2022 primary reads coded NA (so in 2022 an NA target is "primary or missing":
              first reads are UNAVAILABLE that season, never zero).
  empirical   what the data supports (measured in the coverage audit): "0" appears from 2023 on
              runs, kicks, penalties and ~1% of targets (a "no read / not applicable" code that
              replaced 2022's NA), while "1" is the dominant read on ~55% of targets in EVERY season
              2022-2026 (completion ~65%, aDOT ~10.5). Here "1" = first read, "2" = a later read
              (no third-or-later code exists in any season), "0" = no progression read.
Whatever the reading: CHK (checkdown), DES (designed read: screens, many RPOs) and SD (scramble
drill) are separate classes, never first reads; NA on a target in a charted game is `missing`;
a target in an uncharted game is `not_charted`; both stay null in features (never zero).

Depth charts: two schemas that are never mixed.
  modern (2025+)  nflverse snapshots with an ISO-8601 load timestamp `dt` (one or more per day).
                  A forecast for a team's game uses the latest snapshot with dt strictly before
                  that team's kickoff minus `cutoff_hours` (never a later snapshot), and only if
                  it is at most MAX_STALE_DAYS old.
  legacy (<=2024) one weekly chart per (team, week) with no publication time: whether it existed
                  before kickoff cannot be verified. Used only in a labeled timing diagnostic,
                  never to train or evaluate an adoptable signal, and never backfilled from modern
                  snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import polars as pl

from .forecast_features import POSITIONS, RECENT_GAMES, key

SIGNALS_VERSION = "usage-signals-0.1.0"
SIGNALS_STATUS = "research_only"

# ----------------------------------------------------------------------------- FTN read classes

FIRST = "first_read"
SECOND = "second_read"
THIRD_PLUS = "third_plus_read"
LATER = "later_read"                 # empirical "2": second or later (no third+ code exists)
CHECKDOWN = "checkdown"
DESIGNED = "designed_read"
SCRAMBLE = "scramble_drill"
NO_READ = "no_read"                  # empirical "0": not a progression throw / not applicable
MISSING = "missing"                  # charted game, target without a code (or no FTN row)
PRIMARY_UNAVAILABLE = "primary_unavailable"   # dictionary reading, 2022 NA: primary OR missing
UNKNOWN = "unknown_code"
NOT_CHARTED = "not_charted"          # the game has no FTN charting at all
DUPLICATE = "duplicate_play_id"      # FTN lists the play id more than once: excluded
ID_MISMATCH = "id_mismatch"          # FTN season/week disagree with play-by-play for that play id

PROGRESSION = (FIRST, SECOND, THIRD_PLUS, LATER)        # throws inside a progression
READ_CLASSES = (FIRST, SECOND, THIRD_PLUS, LATER, CHECKDOWN, DESIGNED, SCRAMBLE, NO_READ, MISSING,
                PRIMARY_UNAVAILABLE, UNKNOWN, NOT_CHARTED, DUPLICATE, ID_MISMATCH)
SEMANTICS = {
    "dictionary": {"0": FIRST, "1": SECOND, "2": THIRD_PLUS, "CHK": CHECKDOWN, "DES": DESIGNED, "SD": SCRAMBLE},
    "empirical": {"1": FIRST, "2": LATER, "0": NO_READ, "CHK": CHECKDOWN, "DES": DESIGNED, "SD": SCRAMBLE},
}
DICTIONARY_PRIMARY_FROM = 2023       # dictionary: "0" coded from 2023; 2022 primary reads are NA


def classify_read(code: str | None, season: int, semantics: str) -> str:
    """One FTN read_thrown value -> read class (scalar reference implementation of read_class_expr)."""
    mapping = SEMANTICS[semantics]
    c = code.strip() if isinstance(code, str) else None
    if not c:
        return PRIMARY_UNAVAILABLE if semantics == "dictionary" and season < DICTIONARY_PRIMARY_FROM else MISSING
    return mapping.get(c, UNKNOWN)


def read_class_expr(semantics: str, code: str = "read_thrown", season: str = "season") -> pl.Expr:
    mapping = SEMANTICS[semantics]
    c = pl.col(code).str.strip_chars()
    empty = c.is_null() | (c == "")
    na = (pl.when(pl.col(season) < DICTIONARY_PRIMARY_FROM).then(pl.lit(PRIMARY_UNAVAILABLE)).otherwise(pl.lit(MISSING))
          if semantics == "dictionary" else pl.lit(MISSING))
    return pl.when(empty).then(na).otherwise(c.replace_strict(mapping, default=UNKNOWN, return_dtype=pl.String))


def target_plays(pbp: pl.DataFrame) -> pl.DataFrame:
    """REG-season targets, the same definition as app.nfl.sync.classify_plays `target`."""
    f = lambda c: pl.col(c).fill_null(0) == 1  # noqa: E731
    return (pbp.filter((pl.col("season_type") == "REG") & (pl.col("play_type") == "pass") & f("pass_attempt")
                       & ~f("sack") & ~f("two_point_attempt") & pl.col("receiver_player_id").is_not_null())
            .select(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), "game_id",
                    pl.col("play_id").cast(pl.Int64), pl.col("posteam").alias("team"),
                    pl.col("receiver_player_id").alias("gsis_id"), pl.col("air_yards").cast(pl.Float64)))


def classify_targets(pbp: pl.DataFrame, ftn: pl.DataFrame, semantics: str) -> tuple[pl.DataFrame, dict]:
    """Every REG target with its read class. FTN rows join on (nflverse_game_id, nflverse_play_id);
    a key FTN lists twice is `duplicate_play_id` (excluded, never guessed), a key whose FTN
    season/week disagree with play-by-play is `id_mismatch`. Returns (targets, audit counts)."""
    t = target_plays(pbp)
    f = ftn.select(pl.col("nflverse_game_id").alias("game_id"), pl.col("nflverse_play_id").cast(pl.Int64).alias("play_id"),
                   pl.col("season").cast(pl.Int64).alias("_f_season"), pl.col("week").cast(pl.Int64).alias("_f_week"),
                   "read_thrown", pl.col("date_pulled"))
    dup = f.group_by("game_id", "play_id").len().filter(pl.col("len") > 1).select("game_id", "play_id")
    f1 = f.join(dup, on=["game_id", "play_id"], how="anti")
    charted = f.select("game_id").unique().with_columns(pl.lit(True).alias("_charted"))
    j = (t.join(f1, on=["game_id", "play_id"], how="left")
          .join(charted, on="game_id", how="left")
          .join(dup.with_columns(pl.lit(True).alias("_dup")), on=["game_id", "play_id"], how="left"))
    mismatch = pl.col("_f_season").is_not_null() & ((pl.col("_f_season") != pl.col("season"))
                                                    | (pl.col("_f_week") != pl.col("week")))
    j = j.with_columns(
        pl.when(pl.col("_charted").is_null()).then(pl.lit(NOT_CHARTED))
          .when(pl.col("_dup").fill_null(False)).then(pl.lit(DUPLICATE))
          .when(mismatch).then(pl.lit(ID_MISMATCH))
          .otherwise(read_class_expr(semantics)).alias("read_class"),
        pl.col("_charted").fill_null(False).alias("charted"))
    plays = pbp.select(pl.col("game_id"), pl.col("play_id").cast(pl.Int64)).unique()
    unmatched = f.join(plays, on=["game_id", "play_id"], how="anti")
    audit = {"targets": t.height, "ftn_rows": ftn.height, "duplicate_keys": dup.height,
             "ftn_rows_without_pbp_play": unmatched.height,
             "targets_in_charted_games": int(j["charted"].sum()),
             "classes": dict(sorted(j.group_by("read_class").len().iter_rows()))}
    return j.drop("_f_season", "_f_week", "_charted", "_dup"), audit


def restrict_to_available(ftn: pl.DataFrame, as_of: datetime) -> pl.DataFrame:
    """FTN rows of games whose charting nflverse had pulled strictly before `as_of` (a game is
    available only once ALL its rows are). A game charted later is then `not_charted` for that
    forecast: unknown, never zero. Only meaningful for prospective use: nflverse rewrites
    `date_pulled` when it re-pulls a season, so historical pull times are upper bounds."""
    late = ftn.group_by("nflverse_game_id").agg(pl.col("date_pulled").max().alias("_last"))
    ok = late.filter(pl.col("_last") < as_of).select("nflverse_game_id")
    return ftn.join(ok, on="nflverse_game_id", how="semi")


def game_reads(targets: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(player-game, team-game) read-class counts. Uncharted games keep their target counts but
    null read counts; a charted target without a code counts in `missing`, never in a read."""
    classes = [FIRST, SECOND, THIRD_PLUS, LATER, CHECKDOWN, DESIGNED, SCRAMBLE, NO_READ, MISSING, PRIMARY_UNAVAILABLE]
    rc = pl.col("read_class")
    aggs = [pl.len().alias("targets"), pl.col("charted").all().alias("charted"),
            pl.col("air_yards").fill_null(0).sum().alias("air_yards"),
            *[(rc == c).sum().alias(f"n_{c}") for c in classes],
            rc.is_in(list(PROGRESSION)).sum().alias("n_progression")]
    keys = ["season", "week", "game_id", "team"]
    player = targets.group_by(*keys, "gsis_id").agg(aggs)
    team = targets.group_by(*keys).agg(aggs)
    null_if_uncharted = [pl.when(pl.col("charted")).then(pl.col(c)).otherwise(None).alias(c)
                         for c in [*(f"n_{c}" for c in classes), "n_progression"]]
    return player.with_columns(null_if_uncharted), team.with_columns(null_if_uncharted)


def ftn_pull_lag(ftn: pl.DataFrame, games: pl.DataFrame) -> list[dict]:
    """Per (season, week): hours from the week's last kickoff to the earliest and latest
    `date_pulled` of its charted plays. `date_pulled` is overwritten when nflverse re-pulls a season,
    so for re-pulled seasons this bounds availability from above only (it is reported, not used)."""
    ko = games.select("season", "week", "game_id", "kickoff").unique("game_id")
    f = ftn.select(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), pl.col("nflverse_game_id").alias("game_id"),
                   "date_pulled").group_by("season", "week", "game_id").agg(pl.col("date_pulled").min().alias("pulled"))
    j = f.join(ko.drop("season", "week"), on="game_id", how="left")
    return (j.group_by("season", "week").agg(
        pl.len().alias("games"), pl.col("kickoff").max().alias("last_kickoff"),
        ((pl.col("pulled").min() - pl.col("kickoff").max()).dt.total_minutes() / 60).round(1).alias("first_pull_h"),
        ((pl.col("pulled").max() - pl.col("kickoff").max()).dt.total_minutes() / 60).round(1).alias("last_pull_h"))
        .sort("season", "week").with_columns(pl.col("last_kickoff").cast(pl.String)).to_dicts())


# ----------------------------------------------------------------------------- FTN features

FTN_PRIOR_GAMES = 2.0         # per-game rates shrink toward the position mean by this many games
FTN_PRIOR_TEAM_FIRST = 10.0   # team-share shrinks by this many team first-read targets
FTN_PRIOR_OWN_TARGETS = 5.0   # own-share shrinks by this many of the player's charted targets

FTN_FEATURE_DOCS = {
    "fr_tpg": "first-read targets per charted game, last <=5 appearances with charting (shrunk to position mean)",
    "fr_team_share": "player first-read targets / team first-read targets in those games (shrunk to position mean)",
    "fr_own_share": "share of the player's charted targets that were first reads (shrunk to position mean)",
    "des_tpg": "designed-read targets (screens, many RPOs) per charted game",
    "chk_tpg": "checkdown targets per charted game",
    "fr_share_trend": "last charted game's first-read team share minus the earlier games' mean",
    "fr_minus_ts": "fr_team_share minus the player's ordinary target share in the same charted games",
    "ftn_games": "charted games in the window (0 = no charting: every FTN feature is null)",
}
FTN_FEATURES = [f for f in FTN_FEATURE_DOCS if f != "ftn_games"]


def appearance_reads(panel: pl.DataFrame, player_reads: pl.DataFrame, team_reads: pl.DataFrame) -> pl.DataFrame:
    """Every appearance (panel row) with that game's player and team read counts. A player who
    appeared in a charted game without a target has measured zeros; an uncharted game is null."""
    pr = player_reads.select("season", "week", "team", "gsis_id", pl.col("targets").alias("p_targets"),
                             pl.col(f"n_{FIRST}").alias("p_first"), pl.col(f"n_{DESIGNED}").alias("p_des"),
                             pl.col(f"n_{CHECKDOWN}").alias("p_chk"), pl.col(f"n_{PRIMARY_UNAVAILABLE}").alias("p_unav"),
                             pl.col(f"n_{MISSING}").alias("p_missing"))
    tr = team_reads.select("season", "week", "team", pl.col("charted").alias("t_charted"),
                           pl.col("targets").alias("t_targets"), pl.col(f"n_{FIRST}").alias("t_first"),
                           pl.col(f"n_{PRIMARY_UNAVAILABLE}").alias("t_unav"))
    a = (panel.select("season", "week", "gsis_id", "position", "team")
         .join(tr, on=["season", "week", "team"], how="left")
         .join(pr, on=["season", "week", "team", "gsis_id"], how="left"))
    charted = pl.col("t_charted").fill_null(False)
    # first reads exist only where the team's game has no "primary or missing" ambiguity
    usable = charted & (pl.col("t_unav").fill_null(0) == 0)
    z = lambda c: pl.when(usable).then(pl.col(c).fill_null(0)).otherwise(None).alias(c)  # noqa: E731
    return a.with_columns(usable.alias("ftn_usable"), z("p_targets"), z("p_first"), z("p_des"), z("p_chk"),
                          z("t_targets"), z("t_first"))


def ftn_features(appearances: pl.DataFrame, season: int, week: int, targets: pl.DataFrame,
                 prior_seasons: int = 1) -> pl.DataFrame:
    """FTN features for week (season, week) from appearances strictly before it (filtered here),
    current season plus `prior_seasons` before it: the player's last <=5 appearances in usable
    charted games. Same-game charting is never an input."""
    cut = key(season, week)
    a = appearances.filter((pl.col("season") * 100 + pl.col("week") < cut) & (pl.col("season") >= season - prior_seasons)
                           & pl.col("ftn_usable"))
    pos = (a.group_by("position").agg((pl.col("p_first").sum() / pl.len()).alias("_m_fr"),
                                      (pl.col("p_first").sum() / pl.col("t_first").sum()).alias("_m_share"),
                                      (pl.col("p_first").sum() / pl.col("p_targets").sum()).alias("_m_own"))
           if a.height else pl.DataFrame(schema={"position": pl.String, "_m_fr": pl.Float64, "_m_share": pl.Float64,
                                                 "_m_own": pl.Float64}))
    w = (a.sort("season", "week", descending=True).group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
         .with_columns(pl.int_range(pl.len()).over("gsis_id").alias("_r"),
                       (pl.col("p_first") / pl.col("t_first")).alias("_g_share")))
    agg = w.group_by("gsis_id").agg(
        pl.len().alias("ftn_games"), pl.col("p_first").sum().alias("_fr"), pl.col("t_first").sum().alias("_tfr"),
        pl.col("p_targets").sum().alias("_tg"), pl.col("t_targets").sum().alias("_ttg"),
        pl.col("p_des").sum().alias("_des"), pl.col("p_chk").sum().alias("_chk"),
        pl.col("_g_share").filter(pl.col("_r") == 0).first().alias("_share_last"),
        pl.col("_g_share").filter(pl.col("_r") > 0).mean().alias("_share_prev"))
    out = (targets.select("gsis_id", "position").join(agg, on="gsis_id", how="left").join(pos, on="position", how="left"))
    n = pl.col("ftn_games").fill_null(0)
    has = n > 0
    share = (pl.col("_fr") + pl.col("_m_share").fill_null(0) * FTN_PRIOR_TEAM_FIRST) / (pl.col("_tfr") + FTN_PRIOR_TEAM_FIRST)
    out = out.with_columns(
        n.alias("ftn_games"),
        pl.when(has).then((pl.col("_fr") + pl.col("_m_fr").fill_null(0) * FTN_PRIOR_GAMES) / (n + FTN_PRIOR_GAMES)).alias("fr_tpg"),
        pl.when(has).then(share).alias("fr_team_share"),
        pl.when(has).then((pl.col("_fr") + pl.col("_m_own").fill_null(0) * FTN_PRIOR_OWN_TARGETS)
                          / (pl.col("_tg") + FTN_PRIOR_OWN_TARGETS)).alias("fr_own_share"),
        pl.when(has).then(pl.col("_des") / n).alias("des_tpg"),
        pl.when(has).then(pl.col("_chk") / n).alias("chk_tpg"),
        pl.when(has).then(pl.col("_share_last") - pl.col("_share_prev")).alias("fr_share_trend"),
        pl.when(has & (pl.col("_ttg") > 0)).then(share - pl.col("_tg") / pl.col("_ttg")).alias("fr_minus_ts"),
    )
    cols = ["gsis_id", *FTN_FEATURE_DOCS]
    return out.select(cols).with_columns([pl.col(c).cast(pl.Float64).round(10) for c in FTN_FEATURES])


# ----------------------------------------------------------------------------- depth charts

MODERN_FROM = 2025
MAX_STALE_DAYS = 8
STARTERS = {"QB": 1, "RB": 1, "WR": 3, "TE": 1}
SNAP_STARTER = {"QB": 0.8, "RB": 0.45, "WR": 0.6, "TE": 0.5}   # recent snap share "playing like a starter"
OFFENSE_GROUPS = ("3WR 1TE",)

DEPTH_FEATURE_DOCS = {
    "dc_tier": "pregame depth tier at his position: 1 starter (QB1/RB1/TE1/WR1-3), 2 next tier, 3 lower",
    "dc_off_chart": "1 if his team's pregame snapshot exists but does not list him at his position",
    "dc_tier_change": "dc_tier now minus at his team's previous game's pregame snapshot (negative = promoted)",
    "dc_promoted": "1 if a starter now but not at the previous game's snapshot",
    "dc_demoted": "1 if a starter at the previous game's snapshot but not now",
    "dc_vacancy": "teammates ranked ahead of him at the previous snapshot who are now off the chart or behind him",
    "dc_snap_gap": "dc starter (0/1) minus 'recent snap share at a starter level' (0/1): +1 chart ahead of usage",
    "dc_age_hours": "age of the snapshot at the forecast cutoff",
}
DEPTH_FEATURES = ["dc_tier", "dc_off_chart", "dc_tier_change", "dc_promoted", "dc_demoted", "dc_vacancy", "dc_snap_gap"]


def normalize_modern(depth: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Modern snapshots -> one row per (dt, team, position, gsis_id) with an ordinal rank at the
    position (WR ranks span the three WR slots). Rows without a gsis id cannot be joined to a player
    and are dropped (counted); a player listed twice at a position keeps his best rank."""
    off = depth.filter(pl.col("pos_grp").is_in(list(OFFENSE_GROUPS)) & pl.col("pos_abb").is_in(list(POSITIONS)))
    audit = {"rows": depth.height, "skill_rows": off.height, "skill_rows_without_gsis": int(off["gsis_id"].null_count()),
             "snapshots": int(depth["dt"].n_unique())}
    d = (off.filter(pl.col("gsis_id").is_not_null())
         .with_columns(pl.col("dt").str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC").alias("dt"))
         .group_by("dt", "team", pl.col("pos_abb").alias("position"), "gsis_id").agg(pl.col("pos_rank").min().alias("_r"))
         .with_columns(pl.col("_r").rank("ordinal").over("dt", "team", "position").cast(pl.Int64).alias("rank")))
    return d.drop("_r").sort("team", "dt"), audit


def tier_expr(position: str = "position", rank: str = "rank") -> pl.Expr:
    s = pl.col(position).replace_strict(STARTERS, default=1, return_dtype=pl.Int64)
    r = pl.col(rank)
    return pl.when(r <= s).then(1).when(r <= 2 * s).then(2).otherwise(3).cast(pl.Int64)


def snapshot_for(snapshots: pl.DataFrame, cutoffs: pl.DataFrame) -> pl.DataFrame:
    """For each (team, cutoff) the latest snapshot time strictly BEFORE the cutoff (as-of join,
    exact matches excluded) and at most MAX_STALE_DAYS old; null when none qualifies."""
    times = snapshots.select("team", pl.col("dt").alias("snap_dt")).unique().sort("team", "snap_dt")
    c = cutoffs.sort("team", "cutoff")
    j = c.join_asof(times, left_on="cutoff", right_on="snap_dt", by="team", strategy="backward", check_sortedness=False,
                    allow_exact_matches=False)
    fresh = (pl.col("cutoff") - pl.col("snap_dt")) <= pl.duration(days=MAX_STALE_DAYS)
    return j.with_columns(pl.when(fresh).then(pl.col("snap_dt")).otherwise(None).alias("snap_dt"))


def team_cutoffs(games: pl.DataFrame, cutoff_hours: float) -> pl.DataFrame:
    """(season, week, team, kickoff, cutoff) with cutoff = kickoff - cutoff_hours, plus the team's
    previous game of the same season (for change features)."""
    g = (games.filter(pl.col("kickoff").is_not_null())
         .select("season", "week", "team", "kickoff").unique(["season", "week", "team"]).sort("team", "season", "week"))
    g = g.with_columns((pl.col("kickoff") - pl.duration(minutes=int(round(cutoff_hours * 60)))).alias("cutoff"))
    return g.with_columns(pl.col("cutoff").shift(1).over("team", "season").alias("prev_cutoff"))


def depth_features(snapshots: pl.DataFrame, games: pl.DataFrame, targets: pl.DataFrame, cutoff_hours: float,
                   snap_pct: pl.DataFrame | None = None) -> tuple[pl.DataFrame, dict]:
    """Depth features for `targets` (season, week, gsis_id, position, team). Each player is looked
    up on HIS forecast team's chart: a player listed only by another team (a trade or a stale chart)
    is off his team's chart, and a chart position different from his own is an identity conflict
    (features null). `snap_pct` (season, week, gsis_id, snap_pct) is the pregame recent snap share."""
    cut = team_cutoffs(games, cutoff_hours)
    now = snapshot_for(snapshots, cut.select("season", "week", "team", "cutoff"))
    prev = snapshot_for(snapshots, cut.filter(pl.col("prev_cutoff").is_not_null()).select(
        "season", "week", "team", pl.col("prev_cutoff").alias("cutoff"))).rename({"snap_dt": "prev_dt", "cutoff": "prev_cutoff"})
    tw = (cut.join(now.select("season", "week", "team", "snap_dt"), on=["season", "week", "team"], how="left")
          .join(prev.select("season", "week", "team", "prev_dt"), on=["season", "week", "team"], how="left"))
    snaps = snapshots.with_columns(tier_expr().alias("tier"))
    any_pos = snaps.select("dt", "team", "gsis_id", pl.col("position").alias("chart_pos")).unique(["dt", "team", "gsis_id"])
    t = targets.select("season", "week", "gsis_id", "position", "team").join(tw, on=["season", "week", "team"], how="left")
    cur = snaps.select(pl.col("dt").alias("snap_dt"), "team", "position", "gsis_id", "rank", "tier")
    t = (t.join(cur, on=["snap_dt", "team", "position", "gsis_id"], how="left")
          .join(cur.select(pl.col("snap_dt").alias("prev_dt"), "team", "position", "gsis_id",
                           pl.col("rank").alias("prev_rank"), pl.col("tier").alias("prev_tier")),
                on=["prev_dt", "team", "position", "gsis_id"], how="left")
          .join(any_pos.rename({"dt": "snap_dt"}), on=["snap_dt", "team", "gsis_id"], how="left"))
    # vacancy: teammates ahead of him at the previous snapshot now off the chart or behind him
    pairs = (t.filter(pl.col("prev_rank").is_not_null())
             .select("season", "week", "team", "position", "gsis_id", "prev_dt", "snap_dt", "prev_rank", "rank")
             .join(cur.select(pl.col("snap_dt").alias("prev_dt"), "team", "position", pl.col("gsis_id").alias("mate"),
                              pl.col("rank").alias("mate_prev_rank")), on=["prev_dt", "team", "position"])
             .filter(pl.col("mate_prev_rank") < pl.col("prev_rank"))
             .join(cur.select("snap_dt", "team", "position", pl.col("gsis_id").alias("mate"), pl.col("rank").alias("mate_rank")),
                   on=["snap_dt", "team", "position", "mate"], how="left")
             .group_by("season", "week", "gsis_id").agg(
                 (pl.col("mate_rank").is_null() | (pl.col("rank").is_not_null() & (pl.col("mate_rank") > pl.col("rank"))))
                 .sum().alias("dc_vacancy")))
    t = t.join(pairs, on=["season", "week", "gsis_id"], how="left")
    if snap_pct is not None:
        t = t.join(snap_pct.select("season", "week", "gsis_id", "snap_pct"), on=["season", "week", "gsis_id"], how="left")
    else:
        t = t.with_columns(pl.lit(None, dtype=pl.Float64).alias("snap_pct"))
    has = pl.col("snap_dt").is_not_null()
    conflict = has & pl.col("rank").is_null() & pl.col("chart_pos").is_not_null() & (pl.col("chart_pos") != pl.col("position"))
    ok = has & ~conflict
    starter = (pl.col("tier") == 1).cast(pl.Int64)
    prev_starter = (pl.col("prev_tier") == 1).cast(pl.Int64)
    both = ok & pl.col("tier").is_not_null() & pl.col("prev_tier").is_not_null()
    snap_starter = (pl.col("snap_pct") >= pl.col("position").replace_strict(SNAP_STARTER, default=1.0,
                                                                            return_dtype=pl.Float64)).cast(pl.Int64)
    t = t.with_columns(
        pl.when(ok).then(pl.col("tier").fill_null(3)).alias("dc_tier"),
        pl.when(ok).then(pl.col("rank").is_null().cast(pl.Int64)).alias("dc_off_chart"),
        pl.when(both).then(pl.col("tier") - pl.col("prev_tier")).alias("dc_tier_change"),
        pl.when(both).then(((starter == 1) & (prev_starter == 0)).cast(pl.Int64)).alias("dc_promoted"),
        pl.when(both).then(((starter == 0) & (prev_starter == 1)).cast(pl.Int64)).alias("dc_demoted"),
        pl.when(ok & pl.col("prev_dt").is_not_null() & pl.col("prev_rank").is_not_null())
          .then(pl.col("dc_vacancy").fill_null(0)).alias("dc_vacancy"),
        pl.when(ok & pl.col("snap_pct").is_not_null()).then(starter.fill_null(0) - snap_starter).alias("dc_snap_gap"),
        pl.when(has).then((pl.col("cutoff") - pl.col("snap_dt")).dt.total_minutes() / 60).alias("dc_age_hours"),
        conflict.alias("dc_identity_conflict"),
    )
    other = snapshots.select(pl.col("dt").alias("snap_dt"), pl.col("team").alias("_other"), "gsis_id").unique()
    oth = (t.filter(ok & pl.col("rank").is_null()).select("season", "week", "gsis_id", "team", "snap_dt")
           .join(other, on=["snap_dt", "gsis_id"]).filter(pl.col("_other") != pl.col("team")))
    audit = {"rows": t.height, "with_snapshot": int(t["snap_dt"].is_not_null().sum()),
             "on_team_chart": int(t["rank"].is_not_null().sum()), "identity_conflicts": int(t["dc_identity_conflict"].sum()),
             "listed_only_by_other_team": oth.select("season", "week", "gsis_id").unique().height,
             "median_snapshot_age_hours": None if t["dc_age_hours"].drop_nulls().len() == 0
             else round(float(t["dc_age_hours"].median()), 1)}
    out = t.select("season", "week", "gsis_id", *DEPTH_FEATURES, "dc_age_hours", "snap_dt",
                   pl.col("cutoff").alias("dc_cutoff"))
    return out.with_columns([pl.col(c).cast(pl.Float64) for c in [*DEPTH_FEATURES, "dc_age_hours"]]), audit


def normalize_legacy(depth: pl.DataFrame) -> pl.DataFrame:
    """Legacy weekly charts (<= 2024): REG offense, FB folded into RB. No publication time."""
    return (depth.filter((pl.col("formation") == "Offense") & (pl.col("game_type") == "REG") & pl.col("gsis_id").is_not_null())
            .with_columns(pl.when(pl.col("position") == "FB").then(pl.lit("RB")).otherwise(pl.col("position")).alias("position"))
            .filter(pl.col("position").is_in(list(POSITIONS)))
            .group_by(pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64), pl.col("club_code").alias("team"),
                      "position", "gsis_id")
            .agg(pl.col("depth_team").cast(pl.Int64, strict=False).min().alias("depth_team")))


def starter_agreement(chart_starters: pl.DataFrame, panel: pl.DataFrame) -> dict:
    """Timing diagnostic: how often a chart's starter is the snap leader at his position in the SAME
    game vs the team's PREVIOUS game. A pregame chart should not agree with the same game much more
    than a timestamped pregame snapshot does. `chart_starters`: (season, week, team, position, gsis_id)."""
    lead = (panel.filter(pl.col("offense_pct").is_not_null()).sort("offense_pct", descending=True)
            .group_by("season", "week", "team", "position", maintain_order=True).head(1)
            .select("season", "week", "team", "position", pl.col("gsis_id").alias("leader")))
    lead = lead.sort("team", "position", "season", "week").with_columns(
        pl.col("leader").shift(1).over("team", "position", "season").alias("prev_leader"))
    j = chart_starters.join(lead, on=["season", "week", "team", "position"], how="inner")
    j = j.filter(pl.col("prev_leader").is_not_null())
    return {"n": j.height, "same_game": round(float((j["gsis_id"] == j["leader"]).mean()), 4) if j.height else None,
            "previous_game": round(float((j["gsis_id"] == j["prev_leader"]).mean()), 4) if j.height else None,
            "same_not_previous": round(float(((j["gsis_id"] == j["leader"]) & (j["gsis_id"] != j["prev_leader"])).mean()), 4)
            if j.height else None}


# ----------------------------------------------------------------------------- labels


ROLE_DELTA = 0.15


def decision(dev: dict, replay: dict, group: str) -> dict:
    """Apply DECISION_RULES. `dev` / `replay`: {label: compare_regression report} on the pooled
    population of the group's positions."""
    def d(rep, lab):
        r = rep.get(lab, {}).get("all", {}).get("minus_baseline_mae", {}).get(group)
        return r if r and r.get("diff") is not None else None
    checks = {}
    opp_gain = [lab for lab in ("targets", "opp") if d(dev, lab) and d(dev, lab)["ci95"][1] < 0]
    checks["development_opportunity_gain"] = bool(opp_gain)
    checks["development_points_not_worse"] = bool(d(dev, "points")) and d(dev, "points")["diff"] <= 0
    checks["replay_points_not_worse"] = bool(d(replay, "points")) and d(replay, "points")["diff"] <= 0.005
    checks["replay_opp_not_worse"] = bool(d(replay, "opp")) and d(replay, "opp")["diff"] <= 0.005
    return {"checks": checks, "opportunity_labels_improved": opp_gain,
            "outcome": "continue_to_prospective_shadow" if all(checks.values()) else "reject"}


def outcome_labels(store: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """Outcome-side labels for each forecast row (known only after the game): targets, realized
    expected league points (`y_opp`, measured zero for a zero-opportunity appearance in a covered
    team-week, else null when nflverse has no expectation), the snap share and role-change flags vs
    the player's reference (mean of his previous <= 4 appearances, >= 2 required)."""
    keys = ["season", "week", "gsis_id"]
    covered = (panel.filter(pl.col("xfpl").is_not_null()).select("season", "week", "team").unique()
               .with_columns(pl.lit(True).alias("_cov")))
    opps = pl.col("carries").fill_null(0) + pl.col("targets").fill_null(0) + pl.col("pass_attempts").fill_null(0)
    p = (panel.sort("gsis_id", "season", "week")
         .with_columns(pl.col("offense_pct").shift(1).rolling_mean(4, min_samples=2).over("gsis_id").alias("ref_snap"))
         .join(covered, on=["season", "week", "team"], how="left")
         .select(*keys, pl.col("targets").alias("y_targets"), pl.col("offense_pct").alias("y_snap"), "ref_snap",
                 pl.when(pl.col("xfpl").is_not_null()).then(pl.col("xfpl"))
                   .when((opps == 0) & pl.col("_cov").fill_null(False)).then(0.0).otherwise(None).alias("y_opp"))
         .unique(keys, keep="first"))
    ok = pl.col("y_snap").is_not_null() & pl.col("ref_snap").is_not_null()
    p = p.with_columns(
        pl.when(ok).then((pl.col("y_snap") <= pl.col("ref_snap") - ROLE_DELTA).cast(pl.Float64)).alias("y_role_down"),
        pl.when(ok).then((pl.col("y_snap") >= pl.col("ref_snap") + ROLE_DELTA).cast(pl.Float64)).alias("y_role_up"))
    return store.join(p.drop("y_snap"), on=keys, how="left")


# ----------------------------------------------------------------------------- experiments

LABELS = {"points": "label", "targets": "y_targets", "opp": "y_opp"}
ROLE_LABELS = ("y_role_down", "y_role_up")

# Pre-registered before any experiment result (conservative: few features, no search).
GROUPS = {
    "A": {"features": ["fr_team_share", "fr_own_share"], "positions": ("RB", "WR", "TE"),
          "source": "ftn", "label": "first-read only"},
    "B": {"features": ["dc_tier", "dc_tier_change", "dc_vacancy", "dc_snap_gap"], "positions": POSITIONS,
          "source": "depth_modern", "label": "depth-chart change only"},
    "C": {"features": ["fr_team_share", "fr_own_share", "dc_tier", "dc_tier_change", "dc_vacancy", "dc_snap_gap"],
          "positions": ("RB", "WR", "TE"), "source": "both", "label": "both"},
}
# Development-only exploration of the remaining FTN features (reported; cannot change a decision).
EXPLORATORY_GROUPS = {"A_all": {"features": FTN_FEATURES, "positions": ("RB", "WR", "TE"), "source": "ftn",
                                "label": "every FTN feature (exploratory)"}}

WINDOWS = {
    # FTN exists 2022+; features need prior charted games, so 2022 week 1 has none.
    "ftn": {"development": (2022, 2023), "inspected_replay": (2024, 2025, 2026)},
    # modern timestamped depth charts exist only 2025+: no untouched data at all.
    "depth_modern": {"development": (2025,), "inspected_replay": (2026,)},
    "both": {"development": (2025,), "inspected_replay": (2026,)},
}

DECISION_RULES = {
    "continue_if": "development (seasons the group's source exists, chosen before results): candidate - ifp2 MAE "
                   "week-clustered 95% CI upper bound < 0 for next-week targets OR next-week expected league points, "
                   "AND next-week points MAE not worse (point estimate <= 0); AND on the already-inspected replay "
                   "neither points MAE worse by > 0.005 nor expected-points MAE worse by > 0.005",
    "reject_otherwise": "documented negative result; no artifact, no integration",
    "adopt": "not possible from this PR: adoption needs prospective frozen pregame evidence (follow-up plan)",
    "never": "no change to ifp-1.0.0 / ifp-2.0.0, the weekly ledgers, the ESPN/Ours switch, Start/Sit, trades, "
             "waivers or the API",
}


# Separately pre-registered PROSPECTIVE candidates (the follow-up plan of
# docs/research/first_read_depth_chart_signals.md, written before any prospective week was forecast). The
# historical windows have no untouched holdout; only frozen pregame forecasts from 2026 week 7 on can
# confirm them (scripts/signal_shadows.py, docs/research/signal_shadows.md). Depth-change features
# (dc_tier_change, dc_vacancy, dc_snap_gap) were rejected and are not in any candidate.
PROSPECTIVE = {
    "ifp-fr-p1": {"features": ["fr_team_share", "fr_own_share"], "positions": ("RB", "WR"), "source": "ftn",
                  "semantics": "empirical", "label": "ifp-2 + FTN first-read team share and own share"},
    "ifp-dct-p1": {"features": ["dc_tier"], "positions": ("QB", "RB", "WR"), "source": "depth_modern",
                   "cutoff_hours": 1.5, "label": "ifp-2 + static pregame depth-chart tier"},
}
PROSPECTIVE_RULES = {
    "model": "the frozen ifp-2 spec per position (features, penalty, recency half-life; no search) plus the "
             "candidate's features, missing source data through ifp's automatic missing indicator; positions "
             "outside the candidate are not forecast by it",
    "ftn_capture": "FTN rows pulled by nflverse strictly before the forecast time only "
                   "(restrict_to_available(as_of=forecast time)); a prior game not charted yet contributes nothing "
                   "(never zero, never backfilled after the freeze); empirical read semantics ('1' = first read)",
    "depth_capture": "the latest modern snapshot strictly before the team's kickoff minus cutoff_hours, at most "
                     "MAX_STALE_DAYS old; the snapshot file sha and each team's snapshot time are frozen with the row",
    "evidence": "write-once pregame ledger rows only, scored after the week is fully final; ifp2 and every candidate "
                "fitted in the same run on the same rows",
    "first_week": (2026, 7),
    "gate": "per candidate position: challenger.SHADOW_GATE via weekly_learning.shadow_criteria (>= 8 fully final "
            "weeks, >= 400 matched appearances, MAE CI upper < 0, bias, starters pool, 80% interval coverage with "
            "ifp-2's frozen bands)",
    "before_consumer_use": "FTN code semantics confirmed upstream (nflverse/nflreadr#325), a CC-BY-SA 4.0 review of "
                           "any published FTN derivative, and the depth-chart upstream terms",
    "never": "passing the gate is evidence for an owner review only; no automatic promotion or registry change",
}
FTN_LICENSE = ("FTN Data via nflverse, CC-BY-SA 4.0: internal research input only; any published derivative must "
               "carry the attribution and the same license (review required before publishing)")


def live_ftn_features(pbp: pl.DataFrame, ftn: pl.DataFrame, panel: pl.DataFrame, season: int, week: int,
                      targets: pl.DataFrame, as_of: datetime, prior_seasons: int = 1) -> tuple[pl.DataFrame, dict]:
    """Prospective FTN features for week (season, week) as of `as_of`: only FTN games nflverse had fully
    pulled strictly before `as_of`, empirical semantics, the same ftn_features as the research. Returns
    (features, provenance) with every earlier REG game of the window that is not available yet."""
    lo = season - prior_seasons
    p = pbp.filter((pl.col("season") >= lo) & ((pl.col("season") < season) | (pl.col("week") < week)))
    f = ftn.filter(pl.col("season").cast(pl.Int64) >= lo)
    avail = restrict_to_available(f, as_of)
    tg, audit = classify_targets(p, avail, "empirical")
    pr, tr = game_reads(tg)
    feats = ftn_features(appearance_reads(panel, pr, tr), season, week, targets, prior_seasons)
    games = p.filter(pl.col("season_type") == "REG").select("season", "week", "game_id").unique()
    have = set(avail["nflverse_game_id"].unique().to_list()) if avail.height else set()
    late = games.filter(~pl.col("game_id").is_in(list(have))).sort("season", "week", "game_id")
    prev = late.filter((pl.col("season") == season) & (pl.col("week") == week - 1))
    pulled = avail["date_pulled"].max() if avail.height else None
    return feats, {"as_of": as_of.isoformat(), "semantics": "empirical", "ftn_rows_available": avail.height,
                   "ftn_rows_excluded_not_yet_pulled": f.height - avail.height,
                   "latest_date_pulled": None if pulled is None else str(pulled),
                   "pbp_games_in_window": games.height, "games_not_available": late.height,
                   "previous_week_games_not_available": prev["game_id"].to_list(), "target_audit": audit}


def live_depth_features(depth: pl.DataFrame, games: pl.DataFrame, targets: pl.DataFrame, cutoff_hours: float,
                        as_of: datetime) -> tuple[pl.DataFrame, dict]:
    """Prospective depth features: modern snapshots with dt strictly before `as_of` (anything later cannot
    exist at the freeze; the filter guards a clock skew), then depth_features' point-in-time rule per team.
    `targets`: (season, week, gsis_id, position, team); `games`: (season, week, team, kickoff)."""
    snaps, norm = normalize_modern(depth)
    snaps = snaps.filter(pl.col("dt") < as_of)
    dc, audit = depth_features(snaps, games, targets, cutoff_hours)
    return dc, {"as_of": as_of.isoformat(), "cutoff_hours": cutoff_hours, "normalize": norm, "join": audit,
                "latest_snapshot": None if snaps.height == 0 else str(snaps["dt"].max())}


def fit_logistic(z: np.ndarray, y: np.ndarray, w: np.ndarray, lam: float, iters: int = 50) -> tuple[np.ndarray, float]:
    """L2-penalized (lam * sum of weights, intercept free) weighted logistic regression by IRLS on
    standardized inputs. Deterministic."""
    beta, b0 = np.zeros(z.shape[1]), float(np.log((np.average(y, weights=w) + 1e-6) / (1 - np.average(y, weights=w) + 1e-6)))
    sw = w.sum()
    for _ in range(iters):
        eta = np.clip(z @ beta + b0, -30, 30)
        p = 1 / (1 + np.exp(-eta))
        s = np.maximum(p * (1 - p), 1e-6) * w
        x1 = np.column_stack([np.ones(len(y)), z])
        h = x1.T @ (s[:, None] * x1)
        h[1:, 1:] += lam * sw * np.eye(z.shape[1])
        g = x1.T @ (w * (y - p))
        g[1:] -= lam * sw * beta
        step = np.linalg.solve(h, g)
        b0, beta = b0 + step[0], beta + step[1:]
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta, b0


def classification_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    ok = ~(np.isnan(y) | np.isnan(p))
    y, p = y[ok], np.clip(p[ok], 1e-6, 1 - 1e-6)
    if len(y) == 0:
        return {"n": 0}
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    # average ranks over ties
    _, inv, cnt = np.unique(p, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / cnt)[inv]
    npos = y.sum()
    auc = (ranks[y == 1].sum() - npos * (npos + 1) / 2) / (npos * (len(y) - npos)) if 0 < npos < len(y) else None
    return {"n": int(len(y)), "rate": round(float(y.mean()), 4),
            "log_loss": round(float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))), 5),
            "brier": round(float(np.mean((y - p) ** 2)), 5), "auc": None if auc is None else round(float(auc), 4)}


@dataclass(frozen=True)
class FitSpec:
    features: tuple
    lam: float
    half_life_weeks: float | None


TRAIN_START = 2020          # as ifp-2 (challenger.TRAIN_START)
ROLE_LAMBDA = 0.01          # logistic penalty for the role-change classifiers (fixed, not tuned)


def _train(store: pl.DataFrame, k: int, label: str) -> pl.DataFrame:
    return store.filter((pl.col("key") < k) & (pl.col("season") >= TRAIN_START) & (pl.col("games_recent") > 0)
                        & pl.col(label).is_not_null())


def walk(store: pl.DataFrame, weeks: list[tuple[int, int]], position: str, spec: FitSpec, label: str,
         kind: str = "linear", col: str = "pred") -> pl.DataFrame:
    """Walk-forward predictions for one position: for each target week, fit on strictly earlier
    weeks (asserted) with ifp-2's estimator (challenger.fit_linear: weighted ridge, its penalty and
    recency half-life) or a logistic analogue, predict the week's rows. Rows without any recent game
    abstain (null), exactly as ifp-2 does."""
    from .challenger import fit_linear, recency_weights
    from .independent_projections import MIN_TRAIN_ROWS, _design_columns, design_matrix, training_means

    st = store.filter(pl.col("position") == position)
    out = []
    for season, week in weeks:
        k = key(season, week)
        rows = st.filter(pl.col("key") == k)
        if rows.height == 0:
            continue
        train = _train(st, k, label)
        assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
        pred = np.full(rows.height, np.nan)
        if train.height >= MIN_TRAIN_ROWS:
            if kind == "linear":
                m = fit_linear(train, position, list(spec.features), spec.lam, half_life_weeks=spec.half_life_weeks,
                               target_key=k, label=label)
                pred = m.predict(rows)
            else:
                cols = _design_columns(train, list(spec.features))
                means = training_means(train, cols)
                x, _ = design_matrix(cols, means, train)
                sd = x.std(axis=0)
                sd[sd < 1e-9] = 1.0
                w = recency_weights(train["key"].to_numpy(), k, spec.half_life_weeks)
                beta, b0 = fit_logistic((x - np.array(means)) / sd, train[label].to_numpy().astype(float), w, spec.lam)
                xr, _ = design_matrix(cols, means, rows)
                pred = 1 / (1 + np.exp(-np.clip(((xr - np.array(means)) / sd) @ beta + b0, -30, 30)))
        pred = np.where(rows["games_recent"].to_numpy() > 0, pred, np.nan)
        out.append(rows.select("key", "gsis_id").with_columns(pl.Series(col, pred).fill_nan(None)))
    return pl.concat(out) if out else pl.DataFrame(schema={"key": pl.Int64, "gsis_id": pl.String, col: pl.Float64})


def compare_regression(df: pl.DataFrame, label: str, models: dict[str, str], baseline: str, covered: pl.Expr | None,
                       resamples: int = 1000, seed: int = 20261016) -> dict:
    """Metrics on ONE shared population: rows with the label and a prediction from every model.
    Also the covered / uncovered split of that same population (source present or not) and the
    week-clustered bootstrap CI of each model's MAE minus the baseline's."""
    from .forecast_backtest import _arr, metrics, week_bootstrap_mae_diff

    d = df.filter(pl.col(label).is_not_null() & pl.all_horizontal([pl.col(c).is_not_null() for c in models.values()]))
    d = d.with_columns(pl.col(label).alias("label")) if label != "label" else d
    out = {"population": d.height, "abstained_or_unlabeled": df.height - d.height}
    parts = {"all": d}
    if covered is not None:
        parts["covered"] = d.filter(covered.fill_null(False))
        parts["uncovered"] = d.filter(~covered.fill_null(False))
    for name, g in parts.items():
        rep = {"n": g.height}
        if g.height:
            a = _arr(g, "label")
            rep["metrics"] = {m: metrics(a, _arr(g, c)) for m, c in models.items()}
            rep["minus_baseline_mae"] = {m: week_bootstrap_mae_diff(g, c, models[baseline], resamples, seed)
                                         for m, c in models.items() if m != baseline}
        out[name] = rep
    return out


def frame_hash(df: pl.DataFrame | None) -> str | None:
    """Order-independent content hash of a frame."""
    if df is None:
        return None
    if df.height == 0:
        return hashlib.sha256(json.dumps(sorted(df.columns)).encode()).hexdigest()[:16]
    return hashlib.sha256(np.sort(df.hash_rows(seed=0).to_numpy()).tobytes()
                          + json.dumps(df.columns).encode()).hexdigest()[:16]


def fingerprint(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def signal_cache_fingerprint(store_fp: str, raw_fp: str, semantics: str, cutoff_hours: float) -> str:
    """Everything the signal cache depends on: the feature store's fingerprint (its source frames,
    weeks, scoring, polars version and builder code), the raw FTN / depth / play-by-play snapshots,
    the read semantics, the depth cutoff, this module's code and the polars version."""
    return fingerprint({"store": store_fp, "raw": raw_fp, "semantics": semantics, "cutoff_hours": cutoff_hours,
                        "code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16], "polars": pl.__version__})


def check_selection(selection: dict, scoring_fingerprint: str) -> dict:
    """Refuse a frozen ifp-1.0.0 selection made under different league scoring."""
    if selection.get("scoring_fingerprint") != scoring_fingerprint:
        raise ValueError(f"selection.json was made under different league scoring "
                         f"({selection.get('scoring_fingerprint')} != {scoring_fingerprint})")
    return selection


# ----------------------------------------------------------------------------- raw source snapshots


def write_snapshot(root: Path, family: str, season: int, df: pl.DataFrame, retrieved_at: datetime | None = None) -> dict:
    """Content-addressed raw source snapshot: <root>/<family>_<season>-<sha>.parquet plus a manifest
    entry. A corrected upstream file produces a NEW snapshot; earlier ones are never overwritten
    (the manifest keeps their history) and every downstream cache fingerprint changes."""
    root.mkdir(parents=True, exist_ok=True)
    sha = frame_hash(df)
    path = root / f"{family}_{season}-{sha}.parquet"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(path)
    manifest = load_manifest(root)
    entry = {"family": family, "season": season, "sha": sha, "file": path.name, "rows": df.height,
             "retrieved_at": (retrieved_at or datetime.now(timezone.utc)).isoformat()}
    hist = manifest.setdefault("history", [])
    current = manifest.setdefault("current", {})
    k = f"{family}_{season}"
    if current.get(k, {}).get("sha") != sha:
        if k in current:
            hist.append(current[k])
        current[k] = entry
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return current[k]


def load_manifest(root: Path) -> dict:
    p = root / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {"current": {}, "history": []}


def read_snapshot(root: Path, family: str, season: int) -> pl.DataFrame | None:
    entry = load_manifest(root)["current"].get(f"{family}_{season}")
    if entry is None:
        return None
    df = pl.read_parquet(root / entry["file"])
    if frame_hash(df) != entry["sha"]:
        raise ValueError(f"snapshot {entry['file']} does not match its content hash")
    return df


def manifest_fingerprint(root: Path, families: tuple[str, ...]) -> str:
    cur = load_manifest(root)["current"]
    return fingerprint({k: v["sha"] for k, v in sorted(cur.items()) if v["family"] in families})


__all__ = ["SIGNALS_VERSION", "classify_read", "classify_targets", "game_reads", "appearance_reads", "ftn_features",
           "normalize_modern", "snapshot_for", "depth_features", "outcome_labels", "GROUPS", "WINDOWS",
           "DECISION_RULES", "write_snapshot", "read_snapshot", "PROSPECTIVE", "PROSPECTIVE_RULES",
           "live_ftn_features", "live_depth_features"]

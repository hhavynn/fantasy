"""Coverage audit for the NFL Savant-style research: which nflverse fields exist, per season and week.

    cd backend && uv run python ../scripts/savant_data_coverage.py --seasons 2016-2026
    cd backend && uv run python ../scripts/savant_data_coverage.py --seasons 2016-2026 --out-dir /tmp/coverage

Read-only research tooling: downloads play-by-play, FTN charting, NGS (passing/rushing/receiving),
ff_opportunity and participation straight from nflverse (via nflreadpy) and reports, for every
regular-season week, how many games each source covers and the null rate of each field on the plays where
that field should be populated. Nothing is written to any database.

Outputs (default docs/research/):
  savant_data_coverage_weekly.csv   one row per (source, metric, season, week)
  savant_data_coverage_season.csv   the same rolled up to (source, metric, season)

Denominators are deliberate: a null `cpoe` on a sack or a run is not missing data, so each metric is
measured only on the plays where nflverse defines it (see METRIC_SCOPES). Freshness (when a week becomes
available relative to its games) is not derivable from the files themselves; see
docs/research/savant_data_coverage.md for the sync_runs / release-timestamp evidence.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

import nflreadpy as nfl
import polars as pl

from app.config import REPO_ROOT

log = logging.getLogger("savant_coverage")

# Play subsets used as denominators. Built from nflverse flags; regular season, no two-point tries.
def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


PASS_ATTEMPT = _flag("pass_attempt") & ~_flag("sack") & pl.col("play_type").eq("pass")
COMPLETION = PASS_ATTEMPT & _flag("complete_pass")
SCRIMMAGE = pl.col("play_type").is_in(["run", "pass"])
DROPBACK = _flag("qb_dropback")
PENALTY = _flag("penalty")
ANY_PLAY = pl.col("play_type").is_not_null()   # excludes timeout / end-of-quarter rows

# metric -> (column, scope). A metric is "missing" when the column is null on a play inside its scope.
PBP_METRICS: dict[str, tuple[str, pl.Expr]] = {
    "cpoe": ("cpoe", PASS_ATTEMPT),
    "cp": ("cp", PASS_ATTEMPT),
    "air_yards": ("air_yards", PASS_ATTEMPT),
    "yards_after_catch": ("yards_after_catch", COMPLETION),
    "xyac_mean_yardage": ("xyac_mean_yardage", PASS_ATTEMPT),
    "xpass": ("xpass", SCRIMMAGE),
    "pass_oe": ("pass_oe", SCRIMMAGE),
    "down": ("down", SCRIMMAGE),
    "ydstogo": ("ydstogo", SCRIMMAGE),
    "yardline_100": ("yardline_100", SCRIMMAGE),
    "epa": ("epa", SCRIMMAGE),
    "success": ("success", SCRIMMAGE),
    "wp": ("wp", SCRIMMAGE),
    "qb_hit": ("qb_hit", DROPBACK),
    "sack": ("sack", DROPBACK),
    "qb_scramble": ("qb_scramble", DROPBACK),
    "penalty_flag": ("penalty", ANY_PLAY),
    "penalty_type": ("penalty_type", PENALTY),
    "penalty_yards": ("penalty_yards", PENALTY),
    "penalty_team": ("penalty_team", PENALTY),
    "penalty_player_id": ("penalty_player_id", PENALTY),
}

# FTN charts every snap; pass-only fields are measured on pass attempts, rush counts on dropbacks.
FTN_METRICS: dict[str, pl.Expr | None] = {
    "is_play_action": SCRIMMAGE, "is_rpo": SCRIMMAGE, "is_motion": SCRIMMAGE, "is_screen_pass": PASS_ATTEMPT,
    "is_no_huddle": SCRIMMAGE, "n_defense_box": SCRIMMAGE, "n_pass_rushers": DROPBACK, "n_blitzers": DROPBACK,
    "is_qb_out_of_pocket": DROPBACK, "read_thrown": PASS_ATTEMPT, "is_catchable_ball": PASS_ATTEMPT,
    "is_contested_ball": PASS_ATTEMPT, "is_drop": PASS_ATTEMPT, "is_interception_worthy": PASS_ATTEMPT,
    "is_created_reception": PASS_ATTEMPT,
}

NGS_METRICS = {
    "passing": ["avg_time_to_throw", "aggressiveness", "avg_intended_air_yards", "avg_completed_air_yards",
                "avg_air_yards_differential", "avg_air_yards_to_sticks", "expected_completion_percentage",
                "completion_percentage_above_expectation", "avg_air_distance", "max_air_distance"],
    "rushing": ["efficiency", "percent_attempts_gte_eight_defenders", "avg_time_to_los",
                "expected_rush_yards", "rush_yards_over_expected", "rush_yards_over_expected_per_att",
                "rush_pct_over_expected"],
    "receiving": ["avg_cushion", "avg_separation", "avg_intended_air_yards", "percent_share_of_intended_air_yards",
                  "avg_yac", "avg_expected_yac", "avg_yac_above_expectation"],
}

FFO_METRICS = ["total_fantasy_points_exp", "rec_fantasy_points_exp", "rush_fantasy_points_exp",
               "pass_fantasy_points_exp", "rec_yards_gained_exp", "rush_yards_gained_exp", "receptions_exp"]

# metric -> (column, scope) on plays joined to pbp. `route` is the targeted receiver's route, so it exists only on
# pass attempts. Pressure and coverage are reported twice: over all dropbacks, and over pass attempts only. NGS-era
# files (2016-2022) code them on attempts but leave sacks and scrambles null.
PARTICIPATION_METRICS: dict[str, tuple[str, pl.Expr]] = {
    "was_pressure": ("was_pressure", DROPBACK),
    "was_pressure_pass_attempts": ("was_pressure", PASS_ATTEMPT),
    "time_to_throw": ("time_to_throw", PASS_ATTEMPT),
    "number_of_pass_rushers": ("number_of_pass_rushers", DROPBACK),
    "defenders_in_box": ("defenders_in_box", SCRIMMAGE),
    "offense_formation": ("offense_formation", SCRIMMAGE),
    "offense_personnel": ("offense_personnel", SCRIMMAGE),
    "defense_coverage_type": ("defense_coverage_type", DROPBACK),
    "defense_coverage_type_pass_attempts": ("defense_coverage_type", PASS_ATTEMPT),
    "defense_man_zone_type": ("defense_man_zone_type", DROPBACK),
    "defense_man_zone_type_pass_attempts": ("defense_man_zone_type", PASS_ATTEMPT),
    "route": ("route", PASS_ATTEMPT),
}


def _missing(frame: pl.DataFrame, col: str) -> pl.Expr:
    """Null, or a blank string: FTN-era participation (2023+) writes "" where NGS-era files wrote null."""
    if frame.schema[col] == pl.String:
        return pl.col(col).is_null() | (pl.col(col).str.strip_chars() == "")
    return pl.col(col).is_null()


def _null_rows(frame: pl.DataFrame, source: str, season: int, games: pl.DataFrame,
               metrics: dict[str, tuple[str, pl.Expr | None]]) -> list[dict]:
    """Per-week coverage rows for frame (which carries week and game_id) over the scheduled games."""
    out = []
    for metric, (col, scope) in metrics.items():
        sub = frame if scope is None else frame.filter(scope)
        if col not in frame.columns:
            agg = sub.group_by("week").agg(pl.col("game_id").n_unique().alias("games_with_rows"),
                                           pl.len().alias("rows"), pl.len().alias("nulls"))
        else:
            agg = sub.group_by("week").agg(pl.col("game_id").n_unique().alias("games_with_rows"),
                                           pl.len().alias("rows"), _missing(frame, col).sum().alias("nulls"))
        agg = games.join(agg, on="week", how="left").with_columns(
            pl.col("games_with_rows").fill_null(0), pl.col("rows").fill_null(0), pl.col("nulls").fill_null(0))
        for r in agg.iter_rows(named=True):
            out.append({"source": source, "metric": metric, "season": season, "week": r["week"],
                        "games_scheduled": r["games_scheduled"], "games_played": r["games_played"],
                        "games_with_rows": r["games_with_rows"], "rows": r["rows"],
                        "null_rate": (r["nulls"] / r["rows"]) if r["rows"] else None,
                        "column_present": col in frame.columns})
    return out


def _games(schedules: pl.DataFrame, season: int) -> pl.DataFrame:
    reg = schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    return reg.group_by("week").agg(pl.len().alias("games_scheduled"),
                                    pl.col("result").is_not_null().sum().alias("games_played")).sort("week")


def _ngs_rows(stat_type: str, ngs: pl.DataFrame, season: int, games: pl.DataFrame) -> list[dict]:
    """NGS rows are player-weeks (only players over NGS's attempt threshold), not plays: `rows` is
    qualifying players, `games_with_rows` is teams represented (no game_id in the file)."""
    reg = ngs.filter((pl.col("season") == season) & (pl.col("season_type") == "REG") & (pl.col("week") > 0))
    out = []
    for metric in NGS_METRICS[stat_type]:
        present = metric in reg.columns
        agg = reg.group_by("week").agg(pl.col("team_abbr").n_unique().alias("teams"), pl.len().alias("rows"),
                                       (pl.col(metric).null_count() if present else pl.len()).alias("nulls"))
        agg = games.join(agg, on="week", how="left").with_columns(pl.col("teams").fill_null(0),
                                                                   pl.col("rows").fill_null(0),
                                                                   pl.col("nulls").fill_null(0))
        for r in agg.iter_rows(named=True):
            out.append({"source": f"ngs_{stat_type}", "metric": metric, "season": season, "week": r["week"],
                        "games_scheduled": r["games_scheduled"], "games_played": r["games_played"],
                        "games_with_rows": None, "teams_with_rows": r["teams"], "rows": r["rows"],
                        "null_rate": (r["nulls"] / r["rows"]) if r["rows"] else None, "column_present": present})
    return out


def audit_season(season: int, schedules: pl.DataFrame, ngs: dict[str, pl.DataFrame],
                 ffo_all: pl.DataFrame | None) -> list[dict]:
    games = _games(schedules, season)
    rows: list[dict] = []
    pbp_cols = sorted({c for c, _ in PBP_METRICS.values()} | {
        "game_id", "play_id", "week", "season_type", "play_type", "pass_attempt", "complete_pass", "sack",
        "qb_dropback", "two_point_attempt", "pass", "penalty"})
    log.info("pbp %s", season)
    raw = nfl.load_pbp([season])
    pbp = (raw.select([c for c in pbp_cols if c in raw.columns])
              .filter((pl.col("season_type") == "REG") & ~_flag("two_point_attempt")))
    del raw
    rows += _null_rows(pbp, "pbp", season, games, PBP_METRICS)

    if season >= 2022:
        log.info("ftn %s", season)
        ftn = nfl.load_ftn_charting([season]).rename({"nflverse_game_id": "game_id"})
        ftn = ftn.drop("week").join(pbp.select("game_id", "play_id", "week", "play_type", "pass_attempt", "sack",
                                  "qb_dropback").rename({"week": "pbp_week"}),
                       left_on=["game_id", pl.col("nflverse_play_id").cast(pl.Float64)],
                       right_on=["game_id", "play_id"], how="inner").rename({"pbp_week": "week"})
        rows += _null_rows(ftn, "ftn", season, games, {m: (m, sc) for m, sc in FTN_METRICS.items()})

    for stat_type, frame in ngs.items():
        rows += _ngs_rows(stat_type, frame, season, games)

    if ffo_all is not None:
        ffo = ffo_all.filter(pl.col("season").cast(pl.Int32) == season).with_columns(pl.col("week").cast(pl.Int32))
        rows += _null_rows(ffo, "ff_opportunity", season, games, {m: (m, None) for m in FFO_METRICS})

    if season < 2026:
        try:
            part = nfl.load_participation([season])
        except Exception as err:  # participation for a season is published after it ends
            log.warning("participation %s unavailable: %s", season, err)
            part = None
        if part is not None:
            log.info("participation %s", season)
            part = part.rename({"nflverse_game_id": "game_id"}).join(
                pbp, left_on=["game_id", pl.col("play_id").cast(pl.Float64)], right_on=["game_id", "play_id"],
                how="inner")
            rows += _null_rows(part, "participation", season, games, PARTICIPATION_METRICS)
    return rows


def season_rollup(weekly: pl.DataFrame) -> pl.DataFrame:
    return (weekly.group_by(["source", "metric", "season"]).agg(
        pl.col("week").filter(pl.col("rows") > 0).min().alias("first_week"),
        pl.col("week").filter(pl.col("rows") > 0).max().alias("last_week"),
        (pl.col("rows") > 0).sum().alias("weeks_with_rows"),
        (pl.col("games_played") > 0).sum().alias("weeks_played"),
        pl.col("games_played").sum().alias("games_played"),
        pl.col("games_with_rows").sum().alias("games_with_rows"),
        pl.col("rows").sum().alias("rows"),
        (pl.col("null_rate").fill_null(0) * pl.col("rows")).sum().alias("_nulls"),
        pl.col("column_present").all().alias("column_present"),
    ).with_columns(pl.when(pl.col("rows") > 0).then(pl.col("_nulls") / pl.col("rows")).alias("null_rate"))
     .drop("_nulls").sort(["source", "metric", "season"]))


def parse_seasons(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", default="2016-2026")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs" / "research")
    args = ap.parse_args(argv)
    seasons = parse_seasons(args.seasons)
    schedules = nfl.load_schedules(seasons)
    ngs = {t: nfl.load_nextgen_stats(seasons, stat_type=t) for t in NGS_METRICS}
    ffo = nfl.load_ff_opportunity(seasons)
    rows: list[dict] = []
    for s in seasons:
        rows += audit_season(s, schedules, ngs, ffo)
    weekly = pl.DataFrame(rows, infer_schema_length=None).sort(["source", "metric", "season", "week"])
    args.out_dir.mkdir(parents=True, exist_ok=True)
    weekly.write_csv(args.out_dir / "savant_data_coverage_weekly.csv", float_precision=4)
    season_rollup(weekly).write_csv(args.out_dir / "savant_data_coverage_season.csv", float_precision=4)
    print(f"wrote {weekly.height} weekly rows -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

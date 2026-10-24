"""Source matrix for defensive-matchup research (issue #198, step 1): which coverage / deployment fields exist in the
free nflverse sources, on which plays, for which seasons, and how often they are missing.

    cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026
    cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026 --out /tmp/matchup.csv

Read-only research tooling: downloads play-by-play, participation, FTN charting, NGS receiving and PFR advanced
defense straight from nflverse (via nflreadpy) and writes one CSV row per (source, metric, season). Nothing is written
to any database and no app code reads the output.

Denominators are deliberate (every row names its scope): a credited pass defender is expected on an incomplete pass,
a tackler on a completion, a pass rusher count on a dropback. A metric is "missing" when the field is null (or a blank
string, which FTN-era participation files write) on a play inside its scope. Columns that do not exist are reported
as absent and seasons a source does not publish as unavailable, with the library's own message; never as 0%.
Interpretation and licensing are in docs/research/defensive_matchup_sources.md.
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

log = logging.getLogger("matchup_sources")


def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


REG = pl.col("season_type") == "REG"
PASS_ATTEMPT = _flag("pass_attempt") & ~_flag("sack") & pl.col("play_type").eq("pass")
INCOMPLETE = PASS_ATTEMPT & ~_flag("complete_pass") & ~_flag("interception")
COMPLETION = PASS_ATTEMPT & _flag("complete_pass")
INTERCEPTION = PASS_ATTEMPT & _flag("interception")
DROPBACK = _flag("qb_dropback")
SCRIMMAGE = pl.col("play_type").is_in(["run", "pass"])

SCOPE_EXPR = {"pass_attempt": PASS_ATTEMPT, "incomplete": INCOMPLETE, "completion": COMPLETION,
              "interception": INTERCEPTION, "dropback": DROPBACK, "scrimmage": SCRIMMAGE}


def _present(col: str) -> pl.Expr:
    c = pl.col(col)
    return c.is_not_null() & (c.cast(pl.Utf8).str.strip_chars() != "")


def _tackler() -> pl.Expr:
    cols = ["solo_tackle_1_player_id", "assist_tackle_1_player_id", "tackle_with_assist_1_player_id"]
    return pl.any_horizontal([_present(c) for c in cols])


# metric -> (expression that is True when the value is PRESENT, scope name)
PBP_METRICS: dict[str, tuple[pl.Expr, str]] = {
    "receiver_player_id (targeted receiver)": (_present("receiver_player_id"), "pass_attempt"),
    "pass_location (left/middle/right)": (_present("pass_location"), "pass_attempt"),
    "pass_length (short/deep)": (_present("pass_length"), "pass_attempt"),
    "pass_defense_1_player_id (credited defender)": (_present("pass_defense_1_player_id"), "incomplete"),
    "pass_defense_1_player_id on completions": (_present("pass_defense_1_player_id"), "completion"),
    "pass_defense_1_player_id on interceptions": (_present("pass_defense_1_player_id"), "interception"),
    "interception_player_id": (_present("interception_player_id"), "interception"),
    "tackler id (solo, assist or with assist)": (_tackler(), "completion"),
}

PARTICIPATION_METRICS: dict[str, tuple[str, str]] = {   # metric -> (column, scope name)
    "route (targeted receiver only)": ("route", "pass_attempt"),
    "defense_coverage_type": ("defense_coverage_type", "pass_attempt"),
    "defense_man_zone_type": ("defense_man_zone_type", "pass_attempt"),
    "defense_players (gsis ids on field)": ("defense_players", "pass_attempt"),
    "defense_positions (roster-position labels on field)": ("defense_positions", "pass_attempt"),
    "number_of_pass_rushers": ("number_of_pass_rushers", "dropback"),
    "was_pressure": ("was_pressure", "dropback"),
    "defenders_in_box": ("defenders_in_box", "scrimmage"),
    "defense_personnel": ("defense_personnel", "scrimmage"),
}

FTN_METRICS: dict[str, str] = {      # column -> scope name
    "n_defense_box": "scrimmage", "n_blitzers": "dropback", "n_pass_rushers": "dropback",
    "is_contested_ball": "pass_attempt", "starting_hash": "scrimmage",
}

NGS_METRICS = ("avg_cushion", "avg_separation")

# PFR advanced defense: per defender per week, charted by PFR. Measured on rows with at least one target.
PFR_METRICS = ("def_targets", "def_completions_allowed", "def_yards_allowed", "def_receiving_td_allowed",
               "def_passer_rating_allowed", "def_adot")

# Column-name fragments that would suggest a defender-to-receiver pairing, checked in every source and season. PFR's
# def_targets / *_allowed are per-defender totals (recorded in the doc), not a pairing, so they are not hints.
ASSIGNMENT_HINTS = ("assign", "shadow", "covering", "coverage_player", "alignment", "matchup_defender",
                    "defender_id", "covered_by", "receiver_defender")


def _row(source: str, metric: str, season: int, scope: str, n: int, present: int | None, note: str = "") -> dict:
    return {"source": source, "metric": metric, "season": season, "scope": scope, "plays_in_scope": n,
            "present": present, "missing_rate": None if present is None or n == 0 else round(1 - present / n, 4),
            "note": note}


def _unavailable(source: str, season: int, err: Exception) -> dict:
    return _row(source, "season file", season, "regular season", 0, None, f"unavailable: {err}")


def _games(df: pl.DataFrame) -> int:
    return df.select(pl.col("game_id").n_unique()).item() if df.height else 0


def _schema_row(source: str, season: int, columns: list[str]) -> dict:
    hits = [c for c in columns if any(h in c.lower() for h in ASSIGNMENT_HINTS)]
    return _row(source, "assignment-like columns", season, "column names", len(columns), None,
                ";".join(hits) if hits else "none")


def pbp_rows(season: int) -> tuple[list[dict], pl.DataFrame]:
    pbp = nfl.load_pbp([season]).filter(REG & pl.col("two_point_attempt").fill_null(0).eq(0))
    rows = [_row("pbp", "games", season, "regular season", _games(pbp), None),
            _schema_row("pbp", season, pbp.columns)]
    for metric, (present, scope) in PBP_METRICS.items():
        sub = pbp.filter(SCOPE_EXPR[scope])
        rows.append(_row("pbp", metric, season, scope, sub.height, sub.filter(present).height))
    plays = pbp.select("game_id", pl.col("play_id").cast(pl.Int64), "play_type", "pass_attempt", "sack",
                       "qb_dropback", "complete_pass", "interception")
    return rows, plays


def participation_rows(season: int, plays: pl.DataFrame) -> list[dict]:
    try:
        part = nfl.load_participation([season])
    except ValueError as err:          # nflreadpy refuses seasons it has no file for
        return [_unavailable("participation", season, err)]
    part = part.rename({"nflverse_game_id": "game_id"}).with_columns(pl.col("play_id").cast(pl.Int64))
    joined = plays.join(part, on=["game_id", "play_id"], how="left")
    rows = [_row("participation", "games", season, "regular season",
                 _games(part.filter(pl.col("game_id").is_in(plays["game_id"].unique().implode()))), None),
            _schema_row("participation", season, part.columns)]
    for metric, (col, scope) in PARTICIPATION_METRICS.items():
        sub = joined.filter(SCOPE_EXPR[scope])
        if col not in part.columns:
            rows.append(_row("participation", metric, season, scope, sub.height, None, "column absent"))
            continue
        rows.append(_row("participation", metric, season, scope, sub.height, sub.filter(_present(col)).height))
    if "defense_positions" in joined.columns:
        pa = joined.filter(PASS_ATTEMPT & _present("defense_positions"))
        pos = pa["defense_positions"]
        n_cb = pos.str.count_matches(r"\bCB\b")
        n_db = pos.str.count_matches(r"\bDB\b")
        is_sorted = pos.map_elements(lambda s: s.split(";") == sorted(s.split(";")), return_dtype=pl.Boolean)
        rows.append(_row("participation", "CB roster labels on field", season, "pass_attempt", pa.height, None,
                         f"mean {n_cb.mean():.2f}; share with 3+ CB {float((n_cb >= 3).mean()):.3f}; "
                         f"plus {int(n_db.sum())} generic DB labels"))
        rows.append(_row("participation", "defense_positions in sorted order", season, "pass_attempt", pa.height,
                         int(is_sorted.sum()), "sorted lists encode no alignment"))
    return rows


def ftn_rows(season: int, plays: pl.DataFrame) -> list[dict]:
    try:
        ftn = nfl.load_ftn_charting([season])
    except ValueError as err:
        return [_unavailable("ftn", season, err)]
    ftn = ftn.rename({"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"}) \
        .with_columns(pl.col("play_id").cast(pl.Int64))
    joined = plays.join(ftn.drop([c for c in ("season", "week") if c in ftn.columns]), on=["game_id", "play_id"],
                        how="left")
    rows = [_row("ftn", "games", season, "regular season",
                 _games(ftn.filter(pl.col("game_id").is_in(plays["game_id"].unique().implode()))), None),
            _schema_row("ftn", season, ftn.columns)]
    for col, scope in FTN_METRICS.items():
        sub = joined.filter(SCOPE_EXPR[scope])
        if col not in ftn.columns:
            rows.append(_row("ftn", col, season, scope, sub.height, None, "column absent"))
            continue
        rows.append(_row("ftn", col, season, scope, sub.height, sub.filter(_present(col)).height))
    return rows


def ngs_rows(season: int) -> list[dict]:
    ngs = nfl.load_nextgen_stats(stat_type="receiving", seasons=[season])
    rows = [_schema_row("ngs_receiving", season, ngs.columns)]
    ngs = ngs.filter((pl.col("season_type") == "REG") & (pl.col("week") >= 1))
    weeks = ngs.select(pl.col("week").n_unique()).item() if ngs.height else 0
    rows.append(_row("ngs_receiving", "player-weeks", season, "player-week, week >= 1", ngs.height, None,
                     f"{weeks} weeks; about {ngs.height / weeks:.0f} receivers a week (NGS threshold)" if weeks else ""))
    for col in NGS_METRICS:
        rows.append(_row("ngs_receiving", col, season, "player-week, week >= 1", ngs.height,
                         ngs.filter(_present(col)).height, "receiver-level average, no defender identity"))
    return rows


def pfr_rows(season: int) -> list[dict]:
    try:
        pfr = nfl.load_pfr_advstats(stat_type="def", summary_level="week", seasons=[season])
    except ValueError as err:
        return [_unavailable("pfr_def", season, err)]
    rows = [_schema_row("pfr_def", season, pfr.columns)]
    pfr = pfr.filter(pl.col("game_type") == "REG")
    weeks = pfr.select(pl.col("week").n_unique()).item() if pfr.height else 0
    rows.append(_row("pfr_def", "defender-weeks", season, "defender-week", pfr.height, None,
                     f"{weeks} weeks; {pfr.select(pl.col('pfr_player_id').n_unique()).item()} defenders; "
                     "per-defender totals, no receiver field"))
    targeted = pfr.filter(pl.col("def_targets").fill_null(0) > 0)
    for col in PFR_METRICS:
        scope = "defender-week" if col == "def_targets" else "defender-week with a target"
        base = pfr if col == "def_targets" else targeted
        rows.append(_row("pfr_def", col, season, scope, base.height, base.filter(_present(col)).height))
    return rows


def parse_seasons(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s) for s in text.split(",")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", default="2022-2026")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "research" / "defensive_matchup_sources.csv")
    args = ap.parse_args()
    rows: list[dict] = []
    for season in parse_seasons(args.seasons):
        log.info("season %s", season)
        pbp, plays = pbp_rows(season)
        rows += pbp + participation_rows(season, plays) + ftn_rows(season, plays) + ngs_rows(season) + pfr_rows(season)
    out = pl.DataFrame(rows, schema={"source": pl.Utf8, "metric": pl.Utf8, "season": pl.Int64, "scope": pl.Utf8,
                                     "plays_in_scope": pl.Int64, "present": pl.Int64, "missing_rate": pl.Float64,
                                     "note": pl.Utf8})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)
    hits = out.filter((pl.col("metric") == "assignment-like columns") & (pl.col("note") != "none"))
    log.info("assignment-like columns: %s", "none" if hits.is_empty() else hits.select("source", "season", "note").rows())
    log.info("wrote %s rows to %s", out.height, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

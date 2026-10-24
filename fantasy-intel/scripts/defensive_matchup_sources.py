"""Source matrix for defensive-matchup research (issue #198, step 1): which coverage / deployment fields exist in the
free nflverse sources, on which plays, for which seasons, and how often they are missing.

    cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026
    cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026 --out /tmp/matchup.csv

Read-only research tooling: downloads play-by-play, participation, FTN charting and NGS receiving straight from
nflverse (via nflreadpy) and writes one CSV row per (source, metric, season). Nothing is written to any database and
no app code reads the output.

Denominators are deliberate (see SCOPES): a credited pass defender is only expected on an incomplete pass, a tackler
only on a completion, a pass rusher count only on a dropback. A metric is "missing" when the field is null (or a blank
string, which FTN-era participation files write) on a play inside its scope. Columns that do not exist are reported as
such, never as 0% coverage. Interpretation and licensing are in docs/research/defensive_matchup_sources.md.
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

PARTICIPATION_LAST = 2025      # nflreadpy 0.1.5 refuses later seasons: no in-season participation file


def _flag(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0) == 1


REG = pl.col("season_type") == "REG"
PASS_ATTEMPT = _flag("pass_attempt") & ~_flag("sack") & pl.col("play_type").eq("pass")
INCOMPLETE = PASS_ATTEMPT & ~_flag("complete_pass") & ~_flag("interception")
COMPLETION = PASS_ATTEMPT & _flag("complete_pass")
INTERCEPTION = PASS_ATTEMPT & _flag("interception")
DROPBACK = _flag("qb_dropback")
SCRIMMAGE = pl.col("play_type").is_in(["run", "pass"])
RUN = pl.col("play_type").eq("run")

# metric -> (expression that is True when the value is PRESENT, scope)
def _present(col: str) -> pl.Expr:
    c = pl.col(col)
    return c.is_not_null() & (c.cast(pl.Utf8).str.strip_chars() != "")


def _tackler() -> pl.Expr:
    cols = ["solo_tackle_1_player_id", "assist_tackle_1_player_id", "tackle_with_assist_1_player_id"]
    return pl.any_horizontal([_present(c) for c in cols])


PBP_METRICS: dict[str, tuple[pl.Expr, pl.Expr]] = {
    "receiver_player_id (targeted receiver)": (_present("receiver_player_id"), PASS_ATTEMPT),
    "pass_location (left/middle/right)": (_present("pass_location"), PASS_ATTEMPT),
    "pass_length (short/deep)": (_present("pass_length"), PASS_ATTEMPT),
    "pass_defense_1_player_id (credited defender, incompletions)": (_present("pass_defense_1_player_id"), INCOMPLETE),
    "interception_player_id (interceptions)": (_present("interception_player_id"), INTERCEPTION),
    "tackler id (completions)": (_tackler(), COMPLETION),
}

PARTICIPATION_METRICS: dict[str, tuple[str, str]] = {   # metric -> (column, scope name)
    "route (targeted receiver only)": ("route", "pass_attempt"),
    "defense_coverage_type": ("defense_coverage_type", "pass_attempt"),
    "defense_man_zone_type": ("defense_man_zone_type", "pass_attempt"),
    "defense_players (gsis ids on field)": ("defense_players", "pass_attempt"),
    "defense_positions (positions on field)": ("defense_positions", "pass_attempt"),
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

SCOPE_EXPR = {"pass_attempt": PASS_ATTEMPT, "dropback": DROPBACK, "scrimmage": SCRIMMAGE, "run": RUN}

# Fields that would be needed for a CB shadow / coverage-assignment claim. Checked by name against every source's
# columns; none exists in nflreadpy 0.1.5 (the doc records the column lists).
ASSIGNMENT_HINTS = ("assign", "shadow", "covering", "coverage_player", "alignment", "matchup_defender")


def _row(source: str, metric: str, season: int, scope: str, n: int, present: int | None, note: str = "") -> dict:
    return {"source": source, "metric": metric, "season": season, "scope": scope, "plays_in_scope": n,
            "present": present, "missing_rate": None if present is None or n == 0 else round(1 - present / n, 4),
            "note": note}


def _games(df: pl.DataFrame, col: str = "game_id") -> int:
    return df.select(pl.col(col).n_unique()).item() if col in df.columns and df.height else 0


def pbp_rows(season: int) -> tuple[list[dict], pl.DataFrame]:
    pbp = nfl.load_pbp([season]).filter(REG & pl.col("two_point_attempt").fill_null(0).eq(0))
    rows = [_row("pbp", "games", season, "regular season", _games(pbp), None)]
    for metric, (present, scope) in PBP_METRICS.items():
        sub = pbp.filter(scope)
        rows.append(_row("pbp", metric, season, "see doc", sub.height, sub.filter(present).height))
    plays = pbp.select("game_id", pl.col("play_id").cast(pl.Int64), "play_type", "pass_attempt", "sack",
                       "qb_dropback", "complete_pass", "interception")
    return rows, plays


def participation_rows(season: int, plays: pl.DataFrame) -> list[dict]:
    if season > PARTICIPATION_LAST:
        return [_row("participation", m, season, s, 0, None, "no file for this season (published after the season)")
                for m, (_, s) in PARTICIPATION_METRICS.items()]
    part = nfl.load_participation([season]).rename({"nflverse_game_id": "game_id"}).with_columns(
        pl.col("play_id").cast(pl.Int64))
    joined = plays.join(part, on=["game_id", "play_id"], how="left")
    rows = [_row("participation", "games", season, "regular season", _games(part.filter(
        pl.col("game_id").is_in(plays["game_id"].unique().implode()))), None)]
    for metric, (col, scope) in PARTICIPATION_METRICS.items():
        sub = joined.filter(SCOPE_EXPR[scope])
        if col not in part.columns:
            rows.append(_row("participation", metric, season, scope, sub.height, None, "column absent"))
            continue
        rows.append(_row("participation", metric, season, scope, sub.height, sub.filter(_present(col)).height))
    pa = joined.filter(PASS_ATTEMPT & _present("defense_positions")) if "defense_positions" in joined.columns \
        else joined.head(0)
    if pa.height:
        n_cb = pa.select(pl.col("defense_positions").str.count_matches(r"\bCB\b").alias("n")).to_series()
        rows.append(_row("participation", "CBs on field (mean, pass attempts)", season, "pass_attempt", pa.height, None,
                         f"mean {n_cb.mean():.2f}; share with 3+ CB {float((n_cb >= 3).mean()):.3f}"))
    return rows


def ftn_rows(season: int, plays: pl.DataFrame) -> list[dict]:
    if season < 2022:
        return []
    ftn = nfl.load_ftn_charting([season]).rename({"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"}) \
        .with_columns(pl.col("play_id").cast(pl.Int64))
    joined = plays.join(ftn.drop([c for c in ("season", "week") if c in ftn.columns]), on=["game_id", "play_id"],
                        how="left")
    rows = [_row("ftn", "games", season, "regular season", _games(ftn.filter(
        pl.col("game_id").is_in(plays["game_id"].unique().implode()))), None)]
    for col, scope in FTN_METRICS.items():
        sub = joined.filter(SCOPE_EXPR[scope])
        if col not in ftn.columns:
            rows.append(_row("ftn", col, season, scope, sub.height, None, "column absent"))
            continue
        rows.append(_row("ftn", col, season, scope, sub.height, sub.filter(_present(col)).height))
    return rows


def ngs_rows(season: int) -> list[dict]:
    ngs = nfl.load_nextgen_stats(stat_type="receiving", seasons=[season]).filter(
        (pl.col("season_type") == "REG") & (pl.col("week") >= 1))
    weeks = ngs.select(pl.col("week").n_unique()).item() if ngs.height else 0
    rows = [_row("ngs_receiving", "player-weeks", season, "week >= 1", ngs.height, None,
                 f"{weeks} weeks; about {ngs.height / weeks:.0f} receivers a week (NGS threshold)" if weeks else "")]
    for col in NGS_METRICS:
        rows.append(_row("ngs_receiving", col, season, "player-week", ngs.height, ngs.filter(_present(col)).height,
                         "receiver-level average, no defender identity"))
    return rows


def assignment_columns() -> dict[str, list[str]]:
    """Columns in each source whose name suggests a defender assignment (expected: none)."""
    out = {}
    for name, cols in (("pbp", nfl.load_pbp([2024]).columns),
                       ("participation", nfl.load_participation([2024]).columns),
                       ("ftn", nfl.load_ftn_charting([2024]).columns),
                       ("ngs_receiving", nfl.load_nextgen_stats(stat_type="receiving", seasons=[2024]).columns)):
        out[name] = [c for c in cols if any(h in c.lower() for h in ASSIGNMENT_HINTS)]
    return out


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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rows: list[dict] = []
    for season in parse_seasons(args.seasons):
        log.info("season %s", season)
        pbp, plays = pbp_rows(season)
        rows += pbp + participation_rows(season, plays) + ftn_rows(season, plays) + ngs_rows(season)
    out = pl.DataFrame(rows, schema={"source": pl.Utf8, "metric": pl.Utf8, "season": pl.Int64, "scope": pl.Utf8,
                                     "plays_in_scope": pl.Int64, "present": pl.Int64, "missing_rate": pl.Float64,
                                     "note": pl.Utf8})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)
    log.info("assignment-like columns: %s", assignment_columns())
    log.info("wrote %s rows to %s", out.height, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

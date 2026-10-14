"""nflverse sync via nflreadpy (current season only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import nflreadpy as nfl
import polars as pl

from ..db.connection import SyncRun, replace_frame_partition, replace_where, upsert_rows

log = logging.getLogger("nfl.sync")

# table name -> (loader, season column or None for non-seasonal tables)
RAW_DATASETS: dict[str, tuple[Callable[[int], pl.DataFrame], str | None]] = {
    "nfl_weekly_stats": (lambda s: nfl.load_player_stats([s]), "season"),
    "nfl_snap_counts": (lambda s: nfl.load_snap_counts([s]), "season"),
    "nfl_injuries": (lambda s: nfl.load_injuries([s]), "season"),
    "nfl_schedules": (lambda s: nfl.load_schedules([s]), "season"),
    "nfl_rosters_weekly": (lambda s: nfl.load_rosters_weekly([s]), "season"),
    "nfl_ff_opportunity": (
        lambda s: nfl.load_ff_opportunity([s]).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)),
        "season",
    ),
    "nfl_depth_charts": (
        lambda s: nfl.load_depth_charts([s]).with_columns(pl.lit(s, dtype=pl.Int32).alias("season")),
        "season",
    ),
    "nfl_ff_playerids": (lambda s: nfl.load_ff_playerids(), None),
}


@dataclass
class NflPayloads:
    season: int
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    players: pl.DataFrame | None = None
    pbp: pl.DataFrame | None = None
    warnings: list[str] = field(default_factory=list)


def _try(warnings: list[str], label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as err:
        msg = f"{label} failed: {type(err).__name__}: {err}"
        log.warning(msg)
        warnings.append(msg)
        return None


def fetch_all(season: int) -> NflPayloads:
    p = NflPayloads(season=season)
    for table, (loader, _) in RAW_DATASETS.items():
        log.info("loading %s", table)
        df = _try(p.warnings, table, lambda: loader(season))
        if df is not None:
            p.frames[table] = df
    p.players = _try(p.warnings, "players", nfl.load_players)
    log.info("loading play-by-play")
    p.pbp = _try(p.warnings, "pbp", lambda: nfl.load_pbp([season]))
    return p


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def curate_players(players: pl.DataFrame) -> list[dict]:
    rows = []
    for r in players.iter_rows(named=True):
        if not r.get("gsis_id"):
            continue
        rows.append({
            "gsis_id": r["gsis_id"],
            "display_name": r.get("display_name"),
            "first_name": r.get("first_name"),
            "last_name": r.get("last_name"),
            "position": r.get("position"),
            "position_group": r.get("position_group"),
            "latest_team": r.get("latest_team"),
            "status": r.get("status"),
            "espn_id": _int(r.get("espn_id")),
            "pfr_id": r.get("pfr_id"),
            "sleeper_id": None,
            "birth_date": r.get("birth_date") or None,
            "rookie_season": _int(r.get("rookie_season")),
            "years_of_experience": _int(r.get("years_of_experience")),
            "draft_year": _int(r.get("draft_year")),
            "draft_round": _int(r.get("draft_round")),
            "draft_pick": _int(r.get("draft_pick")),
        })
    return rows


def derive_redzone(pbp: pl.DataFrame, season: int) -> tuple[list[dict], list[dict]]:
    """Per-player red-zone usage and per-team opportunity totals from play-by-play.

    Carries = run plays with a rusher (incl. scrambles), excluding kneels and
    two-point tries. Targets = pass attempts with a receiver, excluding sacks.
    """
    plays = pbp.filter(
        pl.col("play_type").is_in(["run", "pass"])
        & (pl.col("two_point_attempt").fill_null(0) == 0)
        & (pl.col("season_type") == "REG")
    )
    carries = plays.filter((pl.col("rush_attempt") == 1) & pl.col("rusher_player_id").is_not_null()).select(
        pl.col("week"), pl.col("posteam").alias("team"), pl.col("rusher_player_id").alias("player_id"),
        pl.col("yardline_100"), pl.lit("carry").alias("kind"))
    targets = plays.filter(
        (pl.col("pass_attempt") == 1) & (pl.col("sack").fill_null(0) == 0)
        & pl.col("receiver_player_id").is_not_null()
    ).select(
        pl.col("week"), pl.col("posteam").alias("team"), pl.col("receiver_player_id").alias("player_id"),
        pl.col("yardline_100"), pl.lit("target").alias("kind"))
    opp = pl.concat([carries, targets])
    rz = pl.col("yardline_100") <= 20
    i10 = pl.col("yardline_100") <= 10
    gl = pl.col("yardline_100") <= 5
    is_c, is_t = pl.col("kind") == "carry", pl.col("kind") == "target"

    player = opp.group_by(["week", "player_id", "team"]).agg(
        (is_t & rz).sum().alias("rz_targets"),
        (is_c & rz).sum().alias("rz_carries"),
        (is_t & i10).sum().alias("inside10_targets"),
        (is_c & i10).sum().alias("inside10_carries"),
        (is_c & gl).sum().alias("gl_carries"),
    )
    team = opp.group_by(["week", "team"]).agg(
        is_c.sum().alias("team_carries"),
        is_t.sum().alias("team_targets"),
        (is_c & rz).sum().alias("team_rz_carries"),
        (is_t & rz).sum().alias("team_rz_targets"),
    )
    player_rows = [{**r, "season": season} for r in player.iter_rows(named=True)]
    team_rows = [{**r, "season": season, "team_offense_snaps": None} for r in team.iter_rows(named=True)]
    return player_rows, team_rows


def write_all(con, p: NflPayloads) -> dict:
    stats: dict[str, Any] = {"warnings": list(p.warnings), "season": p.season}
    con.execute("BEGIN TRANSACTION")
    try:
        for table, df in p.frames.items():
            season_col = RAW_DATASETS[table][1]
            stats[table] = replace_frame_partition(con, table, df, season_col, p.season)
            if "week" in df.columns and season_col:
                stats[f"{table}_max_week"] = df["week"].max()
        if p.players is not None:
            stats["nfl_players"] = upsert_rows(con, "nfl_players", curate_players(p.players), ["gsis_id"])
        if p.pbp is not None:
            player_rows, team_rows = derive_redzone(p.pbp, p.season)
            stats["nfl_redzone_usage"] = replace_where(con, "nfl_redzone_usage", player_rows, "season = ?", [p.season])
            stats["nfl_team_week"] = replace_where(con, "nfl_team_week", team_rows, "season = ?", [p.season])
            stats["pbp_max_week"] = p.pbp["week"].max()
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return stats


def run_nfl_sync(con_factory, season: int) -> dict:
    payloads = fetch_all(season)
    with con_factory() as con:
        run = SyncRun(con, "nfl")
        try:
            stats = write_all(con, payloads)
        except Exception as err:
            run.finish("error", f"{type(err).__name__}: {err}")
            raise
        run.finish("ok", "warnings: %d" % len(stats["warnings"]), stats)
    return stats

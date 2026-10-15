"""Optional historical nflverse ingestion for forecast research (finished seasons only).

Kept apart from the current-season sync (app/nfl/sync.py):
  * writes to its own DuckDB file (default data/nfl_history.duckdb), never the live database;
  * accepts only seasons before the configured live NFL season;
  * each run rebuilds every table for exactly the requested seasons in one transaction, so the
    result depends only on the season list and the nflverse release contents at fetch time
    (recorded in sync_runs with the nflreadpy version);
  * reuses the live sync's loaders and play-by-play derivations, so historical and live
    tables have identical definitions.

Only datasets the forecasting features use are fetched (no depth charts, PFR, NGS or FTN).
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

import polars as pl

from ..db.connection import SyncRun, connect, init_db, replace_where, table_exists, upsert_rows
from .sync import curate_players, derive_pbp, player_positions

log = logging.getLogger("nfl.history")

HISTORY_ROLE = "nfl_history"


def _loaders() -> dict[str, Callable[[list[int]], pl.DataFrame]]:
    import nflreadpy as nfl

    return {
        "nfl_weekly_stats": lambda s: nfl.load_player_stats(s),
        "nfl_snap_counts": lambda s: nfl.load_snap_counts(s),
        "nfl_injuries": lambda s: nfl.load_injuries(s),
        "nfl_schedules": lambda s: nfl.load_schedules(s),
        "nfl_rosters_weekly": lambda s: nfl.load_rosters_weekly(s),
        "nfl_ff_opportunity": lambda s: nfl.load_ff_opportunity(s).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)),
    }


def validate_request(seasons: list[int], live_season: int, history_path: Path, live_path: Path) -> list[int]:
    seasons = sorted(set(int(s) for s in seasons))
    if not seasons:
        raise ValueError("no seasons requested")
    if seasons[-1] >= live_season:
        raise ValueError(f"historical ingestion accepts finished seasons only (< {live_season}); "
                         f"got {seasons[-1]}. The live season comes from the regular NFL sync.")
    if seasons[0] < 2016:
        raise ValueError("seasons before 2016 are not supported (snap counts / ff_opportunity coverage)")
    if Path(history_path).resolve() == Path(live_path).resolve():
        raise ValueError("refusing to write historical data into the live database")
    return seasons


def _write_table(con, table: str, frame: pl.DataFrame) -> int:
    con.register("_hist_frame", frame.to_arrow())
    try:
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _hist_frame")
    finally:
        con.unregister("_hist_frame")
    return frame.height


def _completed(schedules: pl.DataFrame, season: int) -> bool:
    reg = schedules.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    return reg.height > 0 and reg["result"].null_count() == 0


def fetch(seasons: list[int], loaders: dict[str, Callable] | None = None,
          players_loader: Callable[[], pl.DataFrame] | None = None,
          pbp_loader: Callable[[int], pl.DataFrame] | None = None) -> dict[str, Any]:
    """Download every dataset for all seasons. Raises on any failure: a partial history would
    silently change the training population."""
    import nflreadpy as nfl

    loaders = loaders or _loaders()
    frames = {t: fn(seasons) for t, fn in loaders.items()}
    players = (players_loader or nfl.load_players)()
    pbp = {s: (pbp_loader or (lambda y: nfl.load_pbp([y])))(s) for s in seasons}
    incomplete = [s for s in seasons if not _completed(frames["nfl_schedules"], s)]
    if incomplete:
        raise ValueError(f"seasons {incomplete} have unfinished regular-season games")
    return {"frames": frames, "players": players, "pbp": pbp}


def write(con, seasons: list[int], payload: dict[str, Any]) -> dict:
    stats: dict[str, Any] = {"seasons": seasons}
    con.execute("BEGIN TRANSACTION")
    try:
        for table, frame in payload["frames"].items():
            stats[table] = _write_table(con, table, frame)
        players = payload["players"]
        con.execute("DELETE FROM nfl_players")
        stats["nfl_players"] = upsert_rows(con, "nfl_players", curate_players(players), ["gsis_id"])
        for t in ("nfl_pbp_player_week", "nfl_team_week", "nfl_defense_week"):
            con.execute(f"DELETE FROM {t}")
        rosters = payload["frames"].get("nfl_rosters_weekly")
        for season in seasons:
            season_rosters = rosters.filter(pl.col("season") == season) if rosters is not None else None
            positions = player_positions(season_rosters, players)
            player_rows, team_rows, defense_rows = derive_pbp(payload["pbp"][season], season, positions)
            stats[f"pbp_{season}"] = {
                "player": replace_where(con, "nfl_pbp_player_week", player_rows, "season = ?", [season]),
                "team": replace_where(con, "nfl_team_week", team_rows, "season = ?", [season]),
                "defense": replace_where(con, "nfl_defense_week", defense_rows, "season = ?", [season])}
        con.execute("DELETE FROM schema_meta WHERE key IN ('database_role', 'history_seasons')")
        con.execute("INSERT INTO schema_meta VALUES ('database_role', ?), ('history_seasons', ?)",
                    [HISTORY_ROLE, ",".join(map(str, seasons))])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return stats


def is_history_db(con) -> bool:
    if not table_exists(con, "schema_meta"):
        return False
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'database_role'").fetchone()
    return bool(row) and row[0] == HISTORY_ROLE


def run_history_sync(history_path: Path, seasons: list[int], live_season: int, live_path: Path,
                     fetcher: Callable[[list[int]], dict] | None = None) -> dict:
    seasons = validate_request(seasons, live_season, history_path, live_path)
    payload = (fetcher or fetch)(seasons)
    con = connect(history_path)
    try:
        init_db(con)
        run = SyncRun(con, "nfl_history")
        try:
            stats = write(con, seasons, payload)
        except Exception as err:
            run.finish("error", f"{type(err).__name__}: {err}")
            raise
        try:
            stats["nflreadpy_version"] = version("nflreadpy")
        except PackageNotFoundError:
            stats["nflreadpy_version"] = None
        run.finish("ok", f"seasons {seasons[0]}-{seasons[-1]}", stats)
    finally:
        con.close()
    return stats

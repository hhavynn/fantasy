"""Split the analytics database into one league file and one global file (multi-league Phase 2).

The hosted API serves a league by attaching its league file and the global file it was built against, read-only,
with `search_path = league, global` (app/db/league_conn.py). This module decides which table lives where:

  * LEAGUE_TABLES hold one league's data, or values derived from its ESPN account or its scoring rules
    (ADR-0001 "Audit": fantasy_players availability, ESPN points and projections, Ours runs keyed by a scoring
    fingerprint, owner research built on them). A league file must never carry another league's rows.
  * GLOBAL_TABLES hold league-independent facts: nflverse statistics, schedules, contexts, external usage, weather,
    the model registry. Every league reads the same global file.
  * BOTH_TABLES exist in both files (`schema_meta`: the league file's copy wins through search_path).

A table missing from all three sets fails tests/test_league_split.py: a new table must be classified on purpose,
and when in doubt it is a league table (a misplaced league table in the global file would leak to every league).

Versions are `<UTC stamp>-<sha256[:24]>` of the written file, so a download can be verified against its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from .r2 import file_sha256

LEAGUE_TABLES = frozenset({
    # one league's provider data
    "leagues", "league_members", "fantasy_teams", "fantasy_players", "matchups", "draft_picks", "lineup_snapshots",
    "roster_history", "transactions", "transaction_items", "activity_messages",
    # ESPN values in the league's scoring / from its ESPN account
    "espn_player_weekly_points", "espn_projection_history", "player_ros_projections", "player_ownership_history",
    "player_id_map", "player_scores", "injury_retrievals",
    # analytics computed for the league
    "waiver_recommendations", "team_waiver_recommendations", "team_position_needs", "team_week_lineup_projections",
    "team_ros_lineup_projections", "manager_metrics", "manager_classifications",
    # Ours, kicker and D/ST runs carry a scoring fingerprint: league scoring
    "independent_projection_runs", "independent_projection_rows", "independent_player_projections",
    "kicker_projection_runs", "kicker_projection_rows", "dst_projection_runs", "dst_projection_rows",
    # owner research and run bookkeeping of this database
    "projection_performance_reports", "model_replays", "sync_runs", "validation_issues",
})

GLOBAL_TABLES = frozenset({
    "nfl_players", "nfl_schedules", "nfl_defense_week", "nfl_kick_attempts", "nfl_pbp_player_week",
    "nfl_team_game_drives", "nfl_team_week", "player_week_usage", "player_context", "defense_context", "team_context",
    "team_game_lines", "weather_forecasts", "external_player_map", "external_player_week_usage",
    "external_player_week_usage_revisions", "external_usage_imports", "external_usage_issues", "approved_artifacts",
})

BOTH_TABLES = frozenset({"schema_meta"})


@dataclass(frozen=True)
class SplitResult:
    league_path: Path
    global_path: Path
    league_version: str
    global_version: str
    league_id: int
    season: int
    schema_version: int | None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def version_for(path: Path, stamp: str) -> str:
    compact = stamp.replace("-", "").replace(":", "").split(".")[0]
    return f"{compact}Z-{file_sha256(path)[:24]}"


def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'src' AND schema_name = 'main' "
        "AND table_name = ?", [table]).fetchall()}


def split_database(src: Path, league_out: Path, global_out: Path, *, stamp: str,
                   league: tuple[int, int] | None = None) -> SplitResult:
    """Write `league_out` (the league's tables) and `global_out` from `src`, both stamped
    `schema_meta.published_at = stamp`. The source must hold exactly one league; `league` = (league_id, season), when
    given, asserts which one. Rows of tables with a league column are filtered to it as defence in depth. Existing
    output files are replaced."""
    src, league_out, global_out = Path(src), Path(league_out), Path(global_out)
    for out in (league_out, global_out):
        out.unlink(missing_ok=True)
        Path(str(out) + ".wal").unlink(missing_ok=True)
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_q(src)}' AS src (READ_ONLY)")
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' AND schema_name = 'main'").fetchall()}
        unclassified = known - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES
        if unclassified:
            raise ValueError(f"unclassified tables (add them to app/db/league_split.py): {sorted(unclassified)}")
        leagues = con.execute("SELECT DISTINCT league_id, season FROM src.main.leagues ORDER BY 1, 2").fetchall()
        if len(leagues) != 1:
            # Several league tables (fantasy_players, ESPN points and projections) have no league column, so a
            # database holding two leagues cannot be split without mixing them. Each league is synced on its own.
            raise ValueError(f"{src} holds more than one league ({len(leagues)}); league tables without a league "
                             "column cannot be split safely")
        if league is not None and tuple(league) != tuple(leagues[0]):
            raise ValueError(f"league {league} is not in {src}")
        league = leagues[0]
        league_id, season = int(league[0]), int(league[1])
        schema = con.execute("SELECT value FROM src.main.schema_meta WHERE key = 'schema_version'").fetchone()

        for alias, out, tables in (("lg", league_out, LEAGUE_TABLES), ("gl", global_out, GLOBAL_TABLES)):
            con.execute(f"ATTACH '{_q(out)}' AS {alias}")
            con.execute(f"COPY FROM DATABASE src TO {alias} (SCHEMA)")      # tables with their exact types
            for t in sorted(known):
                if t in tables or t in BOTH_TABLES:
                    cols = _columns(con, t)
                    where, params = "", []
                    if alias == "lg" and {"league_id", "season"} <= cols:
                        where, params = " WHERE league_id = ? AND season = ?", [league_id, season]
                    elif alias == "lg" and "league_id" in cols:
                        where, params = " WHERE league_id = ?", [league_id]
                    con.execute(f"INSERT INTO {alias}.main.{_ident(t)} SELECT * FROM src.main.{_ident(t)}{where}",
                                params)
                else:
                    con.execute(f"DROP TABLE {alias}.main.{_ident(t)}")
            con.execute(f"DELETE FROM {alias}.main.schema_meta WHERE key = 'published_at'")
            con.execute(f"INSERT INTO {alias}.main.schema_meta VALUES ('published_at', ?)", [stamp])
            con.execute(f"CHECKPOINT {alias}")
            con.execute(f"DETACH {alias}")
    finally:
        con.close()
    return SplitResult(league_out, global_out, version_for(league_out, stamp), version_for(global_out, stamp),
                       league_id, season, int(schema[0]) if schema else None)

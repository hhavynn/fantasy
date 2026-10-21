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

Versions are `<UTC stamp>Z-<sha256[:24]>-<bytes>` of the written file, so a download can be verified against its
name and fetched with parallel ranged GETs.
"""

from __future__ import annotations

import hashlib
import re
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
    # owner research and run bookkeeping of this database; projection ranges carry a scoring fingerprint
    "projection_performance_reports", "model_replays", "sync_runs", "validation_issues", "projection_range_quantiles",
})

GLOBAL_TABLES = frozenset({
    "nfl_players", "nfl_schedules", "nfl_defense_week", "nfl_kick_attempts", "nfl_pbp_player_week",
    "nfl_team_game_drives", "nfl_team_week",
    # raw nflverse datasets the NFL sync creates on the fly (app/nfl/sync.py RAW_DATASETS)
    "nfl_weekly_stats", "nfl_snap_counts", "nfl_injuries", "nfl_rosters_weekly", "nfl_ff_opportunity", "nfl_depth_charts",
    "nfl_ff_playerids", "nfl_pfr_rush", "nfl_pfr_rec", "nfl_pfr_pass", "nfl_ngs_rushing", "nfl_ngs_receiving",
    "nfl_ngs_passing",
    "player_week_usage", "player_context", "defense_context", "team_context",
    "team_game_lines", "weather_forecasts", "external_player_map", "external_player_week_usage",
    "external_player_week_usage_revisions", "external_usage_imports", "external_usage_issues", "approved_artifacts",
})

BOTH_TABLES = frozenset({"schema_meta"})

# Model outputs in the reference scoring (the laptop's league), shipped in the global file to every league build
# (multi-league Phase 4): the job copies or re-scores them into the league's own tables (app/analytics/rescore.py).
# They are league-independent: no rosters, teams or members (tests/test_league_split.py checks the columns).
# `ref_league_scoring` holds the reference league's scoring items, which say what the runs were scored in.
REFERENCE_TABLES = {t: "ref_" + t for t in (
    "independent_projection_runs", "independent_projection_rows", "kicker_projection_runs", "kicker_projection_rows",
    "dst_projection_runs", "dst_projection_rows", "projection_range_quantiles", "projection_performance_reports",
    "model_replays")}
REFERENCE_SCORING = "ref_league_scoring"
GLOBAL_TABLES = GLOBAL_TABLES | frozenset(REFERENCE_TABLES.values()) | {REFERENCE_SCORING}


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


VERSION_RE = re.compile(r"(?P<stamp>\d{8}T\d{6}Z)-(?P<sha>[0-9a-f]{24})-(?P<bytes>[1-9][0-9]{0,11})")


def version_for(path: Path, stamp: str) -> str:
    """`<UTC stamp>Z-<sha256[:24]>-<bytes>`: enough to verify a download and to fetch it with parallel ranges."""
    compact = stamp.replace("-", "").replace(":", "").split(".")[0]
    return f"{compact}Z-{file_sha256(path)[:24]}-{Path(path).stat().st_size}"


def content_hash(path: Path) -> str:
    """sha256[:24] of every table's name, columns and rows (order-independent), leaving out schema_meta's
    `published_at`. DuckDB does not write identical bytes for identical content, so the publisher recognises an
    unchanged global file by this hash, not by the file's own."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_q(Path(path))}' AS db (READ_ONLY)")
        h = hashlib.sha256()
        for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'db' AND schema_name = 'main' "
                                "ORDER BY table_name").fetchall():
            cols = con.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = 'db' "
                               "AND schema_name = 'main' AND table_name = ? ORDER BY column_index", [t]).fetchall()
            where = " WHERE x.key <> 'published_at'" if t == "schema_meta" else ""
            n, total = con.execute(f"SELECT count(*), coalesce(sum(hash(x)::HUGEINT), 0) FROM db.main.{_ident(t)} AS x"
                                   f"{where}").fetchone()
            h.update(repr((t, cols, n, int(total))).encode())
        return h.hexdigest()[:24]
    finally:
        con.close()


def _columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'src' AND schema_name = 'main' "
        "AND table_name = ?", [table]).fetchall()}


def split_database(src: Path, league_out: Path, global_out: Path, *, stamp: str,
                   league: tuple[int, int] | None = None, global_stamp: str | None = None) -> SplitResult:
    """Write `league_out` (the league's tables) and `global_out` from `src`, both stamped
    `schema_meta.published_at = stamp`. The source must hold exactly one league; `league` = (league_id, season), when
    given, asserts which one. Rows of tables with a league column are filtered to it as defence in depth. Existing
    output files are replaced. `global_stamp` (when given) is written into the global file instead of `stamp`: the
    global file is then byte-identical for identical content, so a publisher can reuse an object already uploaded
    (nothing reads the global file's stamp; the league file's `schema_meta` wins through search_path)."""
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
            # A job's working copy already holds the references it read from the global file (copied above as global
            # tables); only the laptop's own database makes new ones.
            if alias == "gl" and REFERENCE_SCORING not in known:
                for source, ref in REFERENCE_TABLES.items():
                    if source in known:
                        con.execute(f"CREATE TABLE gl.main.{_ident(ref)} AS SELECT * FROM src.main.{_ident(source)}")
                con.execute(f"CREATE TABLE gl.main.{REFERENCE_SCORING} AS SELECT season, scoring_items "
                            "FROM src.main.leagues")
            con.execute(f"DELETE FROM {alias}.main.schema_meta WHERE key = 'published_at'")
            con.execute(f"INSERT INTO {alias}.main.schema_meta VALUES ('published_at', ?)",
                        [stamp if alias == "lg" or global_stamp is None else global_stamp])
            con.execute(f"CHECKPOINT {alias}")
            con.execute(f"DETACH {alias}")
    finally:
        con.close()
    return SplitResult(league_out, global_out, version_for(league_out, stamp), version_for(global_out, stamp),
                       league_id, season, int(schema[0]) if schema else None)

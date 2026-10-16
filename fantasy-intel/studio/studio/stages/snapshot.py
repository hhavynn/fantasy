"""Stage 1: a read-only, allow-listed snapshot of NFL data for one episode.

Only nflverse-derived NFL tables are read. ESPN league, manager, roster and projection tables are never
touched, so no private league data and no fantasy points or projections can reach a video. The episode
for week N uses games from weeks < N only.
"""

from __future__ import annotations

import json
import time

import duckdb

from ..core import Episode, canonical_json, read_json, sha256_bytes, write_json

POSITIONS = ("RB", "WR", "TE")

USAGE_COLUMNS = (
    "week", "gsis_id", "team", "opponent", "position", "offense_snaps", "team_offense_snaps",
    "offense_pct", "targets", "receptions", "target_share", "air_yards", "air_yards_share", "adot", "carries",
    "designed_rushes", "team_rb_carries", "backfield_rush_share", "team_rush_share", "rz_targets", "rz_carries", "gl_carries",
)
SCHEDULE_COLUMNS = (
    "game_id", "week", "game_type", "gameday", "gametime", "away_team", "home_team", "away_score", "home_score",
    "away_qb_name", "home_qb_name", "away_coach", "home_coach",
)
# Quarterbacks' passing per game, for the flame / praise Shorts (stages/flame.py). Allow-listed columns only: the
# source table also carries fantasy points, which never enter a snapshot.
PASSING_COLUMNS = (
    "week", "player_id", "team", "opponent_team", "attempts", "completions", "passing_yards", "passing_air_yards",
    "passing_yards_after_catch", "passing_epa", "sacks_suffered", "passing_tds", "passing_interceptions",
)
# Everyone's box score per game (catches, yards, touchdowns), for the flame / praise series' football lines.
BOX_COLUMNS = (
    "week", "player_id", "team", "position", "receptions", "receiving_yards", "receiving_tds", "carries",
    "rushing_yards", "rushing_tds",
)
TEAM_CONTEXT_COLUMNS = ("team", "through_week", "games", "proe", "neutral_pass_rate", "dropbacks")
DEFENSE_CONTEXT_COLUMNS = (
    "defteam", "through_week", "games", "pass_plays", "pass_epa_allowed", "rush_plays", "rush_epa_allowed",
    "targets", "rb_target_share_allowed", "te_target_share_allowed", "ranks",
)

# Allow-list: the snapshot may only read these tables.
TABLES = ("player_week_usage", "nfl_players", "nfl_schedules", "team_context", "defense_context",
          "nfl_injuries", "nfl_rosters_weekly", "nfl_team_week", "nfl_weekly_stats", "sync_runs")


class StaleDataError(RuntimeError):
    pass


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    for table in TABLES:
        sql = sql.replace(f"{{{table}}}", table)
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _round(rows: list[dict]) -> list[dict]:
    # Stable floats so the snapshot hash does not depend on float printing noise.
    return [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def build_snapshot(con: duckdb.DuckDBPyConnection, season: int, week: int) -> dict:
    usage = _round(_rows(con, f"""
        select {", ".join(USAGE_COLUMNS)} from {{player_week_usage}}
        where season = ? and week < ? and position in ('RB', 'WR', 'TE')
        order by week, gsis_id""", [season, week]))
    if not usage:
        raise StaleDataError(f"no usage rows for {season} before week {week}")
    data_week = max(r["week"] for r in usage)
    if data_week < week - 1:
        raise StaleDataError(
            f"usage data runs through week {data_week}, but a week {week} episode needs week {week - 1}. "
            "Run `uv run python ../scripts/sync_nfl.py` in fantasy-intel/backend first.")
    passing = _round(_rows(con, f"""
        select {", ".join(PASSING_COLUMNS)} from {{nfl_weekly_stats}}
        where season = ? and week < ? and season_type = 'REG' and position = 'QB' and attempts > 0
        order by week, player_id""", [season, week]))
    passing = [{("gsis_id" if k == "player_id" else "opponent" if k == "opponent_team" else k): v
                for k, v in r.items()} for r in passing]
    box = _round(_rows(con, f"""
        select {", ".join(BOX_COLUMNS)} from {{nfl_weekly_stats}}
        where season = ? and week < ? and season_type = 'REG' and position in ('QB', 'RB', 'WR', 'TE')
        order by week, player_id""", [season, week]))
    box = [{("gsis_id" if k == "player_id" else k): v for k, v in r.items()} for r in box]
    ids = sorted({r["gsis_id"] for r in usage} | {r["gsis_id"] for r in passing})
    players = _rows(con, """select gsis_id, display_name, position, espn_id, draft_round, years_of_experience
                            from {nfl_players}
                            where gsis_id in (select unnest(?)) order by gsis_id""", [ids])
    schedules = _round(_rows(con, f"""select {", ".join(SCHEDULE_COLUMNS)} from {{nfl_schedules}}
                                    where season = ? and game_type = 'REG' order by week, game_id""", [season]))
    team_ctx = _round(_rows(con, f"""select {", ".join(TEAM_CONTEXT_COLUMNS)} from {{team_context}}
                                   where season = ? and through_week = ? order by team""", [season, data_week]))
    defense_ctx = _round(_rows(con, f"""select {", ".join(DEFENSE_CONTEXT_COLUMNS)} from {{defense_context}}
                                      where season = ? and through_week = ? order by defteam""", [season, data_week]))
    for row in defense_ctx:
        row["ranks"] = json.loads(row["ranks"]) if isinstance(row["ranks"], str) else row["ranks"]
    if not team_ctx or not defense_ctx:
        raise StaleDataError(f"team/defense context is not computed through week {data_week}; "
                             "run scripts/compute_analytics.py")
    # The whole season to date (and the episode week's report so far): who missed games, why, and where a
    # returning player stands this week.
    injuries = _rows(con, """select gsis_id, team, week, report_status, report_primary_injury, practice_status
                             from {nfl_injuries}
                             where season = ? and week <= ? and gsis_id in (select unnest(?))
                             order by gsis_id, week""", [season, week, ids])
    team_weeks = _rows(con, """select team, week, team_targets from {nfl_team_week}
                               where season = ? and week < ? order by team, week""", [season, week])
    jerseys = _rows(con, """select gsis_id, arg_max(jersey_number, week) as jersey_number from {nfl_rosters_weekly}
                            where season = ? and week <= ? and gsis_id in (select unnest(?)) group by gsis_id
                            order by gsis_id""", [season, week, ids])
    rosters = _rows(con, """select gsis_id, team, week, status from {nfl_rosters_weekly}
                            where season = ? and week <= ? and status <> 'ACT' and gsis_id in (select unnest(?))
                            order by gsis_id, week""", [season, week, ids])
    sync = _rows(con, """select run_id, finished_at from {sync_runs} where source = 'nfl' and status = 'ok'
                         order by finished_at desc limit 1""", [])
    return {
        "season": season, "week": week, "data_through_week": data_week,
        "source_sync": {k: str(v) for k, v in sync[0].items()} if sync else None,
        "tables": {
            "player_week_usage": usage, "nfl_players": players, "nfl_schedules": schedules,
            "team_context": team_ctx, "defense_context": defense_ctx, "nfl_injuries": injuries,
            "nfl_rosters_weekly": rosters, "jerseys": jerseys, "nfl_team_week": team_weeks,
            "qb_week_passing": passing, "player_week_box": box,
        },
    }


def connect(path, attempts: int = 20) -> duckdb.DuckDBPyConnection:
    """Read-only connect, waiting out a sync or analytics run that briefly holds the write lock."""
    for i in range(attempts):
        try:
            return duckdb.connect(str(path), read_only=True)
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower() or i == attempts - 1:
                raise
            time.sleep(min(2 * (i + 1), 10))
    raise AssertionError("unreachable")


def run(ep: Episode) -> None:
    con = connect(ep.cfg.db_path)
    try:
        snap = build_snapshot(con, ep.season, ep.week)
    finally:
        con.close()
    # The content hash covers the tables only. An unchanged dataset with the same sync record is skipped; a new
    # sync of the same data still rewrites the file, because "as of" claims (and the timeliness gate) read the
    # sync's timestamp, and a fresh sync must be able to clear a stale one.
    digest = sha256_bytes(canonical_json(snap["tables"]).encode())
    snap["content_sha256"] = digest
    path = ep.path("snapshot.json")
    if path.exists() and "snapshot" not in ep.force:
        prev = read_json(path)
        if prev.get("content_sha256") == digest and prev.get("source_sync") == snap.get("source_sync"):
            ep.log(f"  snapshot: unchanged ({digest[:12]}), skipped")
            return
        if prev.get("content_sha256") == digest:
            ep.log(f"  snapshot: same data ({digest[:12]}), newer sync record: refreshed")
    write_json(path, snap)
    ep.log(f"  snapshot: wrote {len(snap['tables']['player_week_usage'])} usage rows "
           f"through week {snap['data_through_week']} ({digest[:12]})")

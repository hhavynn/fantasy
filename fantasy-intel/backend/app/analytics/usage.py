"""Builds `player_week_usage`: one row per skill player per game played."""

from __future__ import annotations

from ..db.connection import fetch_dicts, replace_where, table_exists

SKILL_POSITIONS = ("QB", "RB", "WR", "TE", "FB")

BASE_SQL = """
WITH stats AS (
    SELECT player_id AS gsis_id, week, team, opponent_team AS opponent, position,
           targets, receptions, receiving_yards, receiving_tds, receiving_air_yards AS air_yards,
           target_share, air_yards_share, wopr, carries, rushing_yards, rushing_tds, fantasy_points_ppr
    FROM nfl_weekly_stats WHERE season = $season AND season_type = 'REG'
),
snaps AS (
    SELECT np.gsis_id, s.week, s.team, s.opponent, s.position,
           s.offense_snaps, s.offense_pct
    FROM nfl_snap_counts s JOIN nfl_players np ON np.pfr_id = s.pfr_player_id
    WHERE s.season = $season AND s.game_type = 'REG'
),
team_snaps AS (
    SELECT team, week, max(offense_snaps) AS team_offense_snaps
    FROM nfl_snap_counts WHERE season = $season AND game_type = 'REG' GROUP BY 1, 2
),
base AS (
    SELECT coalesce(st.gsis_id, sn.gsis_id) AS gsis_id,
           coalesce(st.week, sn.week) AS week,
           coalesce(st.team, sn.team) AS team,
           coalesce(st.opponent, sn.opponent) AS opponent,
           coalesce(st.position, np.position, sn.position) AS position,
           st.gsis_id IS NOT NULL AS has_stats,
           sn.gsis_id IS NOT NULL AS has_snaps,
           st.* EXCLUDE (gsis_id, week, team, opponent, position),
           sn.offense_snaps, sn.offense_pct
    FROM stats st
    FULL OUTER JOIN snaps sn ON sn.gsis_id = st.gsis_id AND sn.week = st.week
    LEFT JOIN nfl_players np ON np.gsis_id = coalesce(st.gsis_id, sn.gsis_id)
)
SELECT b.*, ts.team_offense_snaps, tw.team_carries, tw.team_targets,
       rz.rz_targets, rz.rz_carries, rz.gl_carries,
       fo.total_fantasy_points_exp AS xfp,
       lp.actual_points AS league_points
FROM base b
LEFT JOIN team_snaps ts ON ts.team = b.team AND ts.week = b.week
LEFT JOIN nfl_team_week tw ON tw.season = $season AND tw.team = b.team AND tw.week = b.week
LEFT JOIN nfl_redzone_usage rz ON rz.season = $season AND rz.player_id = b.gsis_id AND rz.week = b.week
LEFT JOIN nfl_ff_opportunity fo ON fo.season = $season AND fo.player_id = b.gsis_id AND fo.week = b.week
LEFT JOIN (SELECT gsis_id, min(espn_player_id) AS espn_player_id FROM player_id_map
           WHERE gsis_id IS NOT NULL GROUP BY 1) m ON m.gsis_id = b.gsis_id
LEFT JOIN espn_player_weekly_points lp ON lp.season = $season AND lp.espn_player_id = m.espn_player_id
     AND lp.scoring_period = b.week
WHERE coalesce(b.position, '') IN ('QB','RB','WR','TE','FB')
"""


def _ratio(num, den):
    if num is None or not den:
        return None
    return num / den


def derive_usage_row(r: dict, season: int) -> dict:
    """Derived per-game metrics. Missing source data stays None; a player who
    logged snaps but no stats is a measured zero for counting stats."""
    has_stats, has_snaps = r.get("has_stats"), r.get("has_snaps")
    zero_if_played = 0 if has_snaps and not has_stats else None

    def count(key):
        v = r.get(key)
        return v if v is not None else zero_if_played

    targets, carries = count("targets"), count("carries")
    air = r.get("air_yards") if has_stats else (0.0 if has_snaps else None)
    target_share = r.get("target_share")
    if target_share is None and targets is not None and r.get("team_targets"):
        target_share = targets / r["team_targets"]
    rz_t, rz_c, gl_c = r.get("rz_targets"), r.get("rz_carries"), r.get("gl_carries")
    opportunities = (targets or 0) + (carries or 0) if (targets is not None or carries is not None) else None
    xfp = r.get("xfp")
    if r.get("team_carries") is not None:  # pbp available for this game: absence means zero
        rz_t, rz_c, gl_c = rz_t or 0, rz_c or 0, gl_c or 0
        if xfp is None and opportunities == 0:
            xfp = 0.0  # played, no targets/carries -> zero expected points (measured, not missing)
    return {
        "season": season,
        "week": r["week"],
        "gsis_id": r["gsis_id"],
        "team": r.get("team"),
        "opponent": r.get("opponent"),
        "position": "RB" if r.get("position") == "FB" else r.get("position"),
        "offense_snaps": r.get("offense_snaps"),
        "offense_pct": r.get("offense_pct"),
        "team_offense_snaps": r.get("team_offense_snaps"),
        "targets": targets,
        "receptions": count("receptions"),
        "receiving_yards": count("receiving_yards"),
        "receiving_tds": count("receiving_tds"),
        "air_yards": air,
        "target_share": target_share if target_share is not None else (0.0 if zero_if_played == 0 else None),
        "air_yards_share": r.get("air_yards_share") if has_stats else (0.0 if has_snaps else None),
        "wopr": r.get("wopr") if has_stats else (0.0 if has_snaps else None),
        "adot": _ratio(air, targets),
        "carries": carries,
        "rushing_yards": count("rushing_yards"),
        "rushing_tds": count("rushing_tds"),
        "team_carries": r.get("team_carries"),
        "rush_share": _ratio(carries, r.get("team_carries")) if carries is not None else None,
        "yards_per_carry": _ratio(count("rushing_yards"), carries),
        "rz_targets": rz_t,
        "rz_carries": rz_c,
        "gl_carries": gl_c,
        "opportunities": opportunities,
        "fantasy_points_ppr": count("fantasy_points_ppr"),
        "league_points": r.get("league_points"),
        "xfp": xfp,
        "stats_source": "stats+snaps" if has_stats and has_snaps else "stats_only" if has_stats else "snaps_only",
    }


def build_player_week_usage(con, season: int) -> dict:
    needed = ["nfl_weekly_stats", "nfl_snap_counts", "nfl_ff_opportunity"]
    missing = [t for t in needed if not table_exists(con, t)]
    if missing:
        return {"skipped": f"missing tables: {missing}"}
    rows = [derive_usage_row(r, season) for r in fetch_dicts(con, BASE_SQL.replace("$season", str(int(season))))]
    replace_where(con, "player_week_usage", rows, "season = ?", [season])
    unmatched_snaps = con.execute("""
        SELECT count(DISTINCT pfr_player_id) FROM nfl_snap_counts s
        WHERE season = ? AND position IN ('QB','RB','WR','TE','FB')
          AND NOT EXISTS (SELECT 1 FROM nfl_players p WHERE p.pfr_id = s.pfr_player_id)
    """, [season]).fetchone()[0]
    return {"rows": len(rows), "snap_players_without_gsis": unmatched_snaps}

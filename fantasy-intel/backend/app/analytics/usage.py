"""Builds `player_week_usage`: one row per skill player per game played.

Definitions live in docs/nfl_metrics_verified_contract.md. Key rules:
  * missing source data stays NULL; a player with snaps but no box-score line is
    a measured zero for counting stats
  * team_rush_share   = PBP rush attempts / team PBP rush attempts
                        (scrambles included, kneels and 2-pt excluded)
  * backfield_rush_share (RBs only) = designed rushes / team RB designed rushes
  * NGS columns are LEFT JOINed and never coalesced (NULL = not qualified)
"""

from __future__ import annotations

from statistics import median

from ..db.connection import fetch_dicts, replace_where, table_exists

SKILL_POSITIONS = ("QB", "RB", "WR", "TE", "FB")


def _optional(con, table: str, sql: str, fallback: str) -> str:
    return sql if table_exists(con, table) else fallback


def base_sql(con, season: int) -> str:
    s = str(int(season))
    pfr_rush = _optional(con, "nfl_pfr_rush", f"""
        SELECT np.gsis_id, r.week, sum(r.carries) AS pfr_carries,
               sum(r.rushing_yards_before_contact) AS pfr_ybc, sum(r.rushing_yards_after_contact) AS pfr_yac,
               sum(r.rushing_broken_tackles) AS pfr_rush_bt
        FROM nfl_pfr_rush r JOIN nfl_players np ON np.pfr_id = r.pfr_player_id
        WHERE r.season = {s} AND r.game_type = 'REG' GROUP BY 1, 2""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT week, NULL::DOUBLE pfr_carries, NULL::DOUBLE pfr_ybc, "
        "NULL::DOUBLE pfr_yac, NULL::DOUBLE pfr_rush_bt WHERE false")
    pfr_rec = _optional(con, "nfl_pfr_rec", f"""
        SELECT np.gsis_id, r.week, sum(r.receiving_broken_tackles) AS pfr_rec_bt
        FROM nfl_pfr_rec r JOIN nfl_players np ON np.pfr_id = r.pfr_player_id
        WHERE r.season = {s} AND r.game_type = 'REG' GROUP BY 1, 2""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT week, NULL::DOUBLE pfr_rec_bt WHERE false")
    ngs_rush = _optional(con, "nfl_ngs_rushing", f"""
        SELECT player_gsis_id AS gsis_id, week, any_value(rush_yards_over_expected_per_att) AS ngs_rush_yoe_per_att
        FROM nfl_ngs_rushing WHERE season = {s} AND season_type = 'REG' AND week >= 1 GROUP BY 1, 2""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT week, NULL::DOUBLE ngs_rush_yoe_per_att WHERE false")
    ngs_rec = _optional(con, "nfl_ngs_receiving", f"""
        SELECT player_gsis_id AS gsis_id, week, any_value(avg_separation) AS ngs_avg_separation
        FROM nfl_ngs_receiving WHERE season = {s} AND season_type = 'REG' AND week >= 1 GROUP BY 1, 2""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT week, NULL::DOUBLE ngs_avg_separation WHERE false")
    return f"""
WITH stats AS (
    SELECT player_id AS gsis_id, week, team, opponent_team AS opponent, position,
           targets, receptions, receiving_yards, receiving_tds, receiving_air_yards AS air_yards,
           target_share, air_yards_share, wopr, carries, rushing_yards, rushing_tds, fantasy_points_ppr
    FROM nfl_weekly_stats WHERE season = {s} AND season_type = 'REG'
),
snaps AS (
    SELECT np.gsis_id, s.week, s.team, s.opponent, s.position, s.offense_snaps, s.offense_pct
    FROM nfl_snap_counts s JOIN nfl_players np ON np.pfr_id = s.pfr_player_id
    WHERE s.season = {s} AND s.game_type = 'REG'
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
),
pfr_rush AS ({pfr_rush}), pfr_rec AS ({pfr_rec}), ngs_rush AS ({ngs_rush}), ngs_rec AS ({ngs_rec})
SELECT b.*, tw.team_offense_snaps, tw.team_targets, tw.team_rush_attempts, tw.team_rb_carries,
       tw.team_first_read_targets, tw.team_targets IS NOT NULL AS has_pbp,
       pb.rush_attempts AS pbp_rush_attempts, pb.designed_rushes, pb.designed_rush_success, pb.explosive_runs,
       pb.rz_targets, pb.rz_carries, pb.gl_carries, pb.targets AS pbp_targets,
       pb.first_read_targets, pb.designed_targets, pb.checkdown_targets,
       pr.pfr_carries, pr.pfr_ybc, pr.pfr_yac, pr.pfr_rush_bt, prc.pfr_rec_bt,
       nr.ngs_rush_yoe_per_att, nc.ngs_avg_separation,
       fo.total_fantasy_points_exp AS xfp,
       lp.actual_points AS league_points
FROM base b
LEFT JOIN nfl_team_week tw ON tw.season = {s} AND tw.team = b.team AND tw.week = b.week
LEFT JOIN nfl_pbp_player_week pb ON pb.season = {s} AND pb.player_id = b.gsis_id AND pb.week = b.week
LEFT JOIN pfr_rush pr ON pr.gsis_id = b.gsis_id AND pr.week = b.week
LEFT JOIN pfr_rec prc ON prc.gsis_id = b.gsis_id AND prc.week = b.week
LEFT JOIN ngs_rush nr ON nr.gsis_id = b.gsis_id AND nr.week = b.week
LEFT JOIN ngs_rec nc ON nc.gsis_id = b.gsis_id AND nc.week = b.week
LEFT JOIN nfl_ff_opportunity fo ON fo.season = {s} AND fo.player_id = b.gsis_id AND fo.week = b.week
LEFT JOIN (SELECT gsis_id, min(espn_player_id) AS espn_player_id FROM player_id_map
           WHERE gsis_id IS NOT NULL GROUP BY 1) m ON m.gsis_id = b.gsis_id
LEFT JOIN espn_player_weekly_points lp ON lp.season = {s} AND lp.espn_player_id = m.espn_player_id
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
    has_stats, has_snaps, has_pbp = r.get("has_stats"), r.get("has_snaps"), r.get("has_pbp")
    zero_if_played = 0 if has_snaps and not has_stats else None
    position = "RB" if r.get("position") == "FB" else r.get("position")

    def count(key):
        v = r.get(key)
        return v if v is not None else zero_if_played

    def pbp_count(key):  # PBP covered the game: absence from the player table is a zero
        v = r.get(key)
        return v if v is not None else (0 if has_pbp else None)

    targets, carries = count("targets"), count("carries")
    air = r.get("air_yards") if has_stats else (0.0 if has_snaps else None)
    target_share = r.get("target_share")
    if target_share is None and targets is not None and r.get("team_targets"):
        target_share = targets / r["team_targets"]   # nflverse definition: team targets, not attempts
    opportunities = (targets or 0) + (carries or 0) if (targets is not None or carries is not None) else None
    xfp = r.get("xfp")
    if has_pbp and xfp is None and opportunities == 0:
        xfp = 0.0  # played, no targets/carries -> zero expected points (measured, not missing)
    rush_attempts, designed = pbp_count("pbp_rush_attempts"), pbp_count("designed_rushes")
    success, explosive = pbp_count("designed_rush_success"), pbp_count("explosive_runs")
    fr = r.get("first_read_targets")
    if fr is None and r.get("team_first_read_targets") is not None:
        fr = 0  # team game was FTN-charted; player had no first-read targets
    bt_parts = [r.get("pfr_rush_bt"), r.get("pfr_rec_bt")]
    return {
        "season": season,
        "week": r["week"],
        "gsis_id": r["gsis_id"],
        "team": r.get("team"),
        "opponent": r.get("opponent"),
        "position": position,
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
        "rush_attempts": rush_attempts,
        "designed_rushes": designed,
        "team_rush_attempts": r.get("team_rush_attempts"),
        "team_rush_share": _ratio(rush_attempts, r.get("team_rush_attempts")) if rush_attempts is not None else None,
        "team_rb_carries": r.get("team_rb_carries"),
        "backfield_rush_share": (_ratio(designed, r.get("team_rb_carries"))
                                 if position == "RB" and designed is not None else None),
        "yards_per_carry": _ratio(count("rushing_yards"), carries),
        "rushing_success_rate": _ratio(success, designed),
        "explosive_runs": explosive,
        "explosive_run_rate": _ratio(explosive, designed),
        "ybc_per_att": _ratio(r.get("pfr_ybc"), r.get("pfr_carries")),
        "yac_per_att": _ratio(r.get("pfr_yac"), r.get("pfr_carries")),
        "broken_tackles": (int(sum(x for x in bt_parts if x is not None))
                           if any(x is not None for x in bt_parts) else None),
        "rz_targets": pbp_count("rz_targets"),
        "rz_carries": pbp_count("rz_carries"),
        "gl_carries": pbp_count("gl_carries"),
        "first_read_targets": fr,
        "first_read_target_share": _ratio(fr, r.get("team_first_read_targets")) if fr is not None else None,
        "designed_targets": r.get("designed_targets") if r.get("designed_targets") is not None else (
            0 if r.get("team_first_read_targets") is not None else None),
        "checkdown_targets": r.get("checkdown_targets") if r.get("checkdown_targets") is not None else (
            0 if r.get("team_first_read_targets") is not None else None),
        "ngs_rush_yoe_per_att": r.get("ngs_rush_yoe_per_att"),
        "ngs_avg_separation": r.get("ngs_avg_separation"),
        "opportunities": opportunities,
        "fantasy_points_ppr": count("fantasy_points_ppr"),
        "league_points": r.get("league_points"),
        "xfp": xfp,
        "stats_source": "stats+snaps" if has_stats and has_snaps else "stats_only" if has_stats else "snaps_only",
    }


def estimate_team_snaps(player_snaps: list[tuple[int, float]]) -> tuple[int | None, str | None]:
    """Team offensive snaps from PFR (offense_snaps, offense_pct) rows of one team-game.

    offense_pct is rounded to 2 decimals, so each player implies snaps / pct
    within rounding error. We use players on the field for >= 50% of snaps
    (smallest relative rounding error), take the median, and validate:
      * every implied estimate is within 2 snaps of the median
      * no player has more snaps than the estimate (+1 for rounding)
    Returns (estimate or None, warning or None)."""
    implied = [s / p for s, p in player_snaps if p and p >= 0.5 and s]
    if not implied:
        return None, "no player with >= 50% snap share"
    est = round(median(implied))
    if max(implied) - min(implied) > 4 or any(abs(x - est) > 2 for x in implied):
        return None, f"inconsistent implied totals {min(implied):.1f}-{max(implied):.1f}"
    if max(s for s, _ in player_snaps) > est + 1:
        return None, f"a player logged more snaps ({max(s for s, _ in player_snaps)}) than the estimate ({est})"
    return est, None


def update_team_snaps(con, season: int) -> list[str]:
    if not table_exists(con, "nfl_snap_counts"):
        return []
    by_tw: dict[tuple, list] = {}
    for r in fetch_dicts(con, """SELECT team, week, offense_snaps, offense_pct FROM nfl_snap_counts
                                 WHERE season = ? AND game_type = 'REG' AND offense_snaps > 0""", [season]):
        by_tw.setdefault((r["team"], r["week"]), []).append((r["offense_snaps"], r["offense_pct"]))
    warnings = []
    for (team, week), rows in by_tw.items():
        est, warn = estimate_team_snaps(rows)
        if warn:
            warnings.append(f"{team} wk{week}: {warn}")
        con.execute("UPDATE nfl_team_week SET team_offense_snaps = ? WHERE season = ? AND team = ? AND week = ?",
                    [est, season, team, week])
    return warnings


def build_player_week_usage(con, season: int) -> dict:
    needed = ["nfl_weekly_stats", "nfl_snap_counts", "nfl_ff_opportunity"]
    missing = [t for t in needed if not table_exists(con, t)]
    if missing:
        return {"skipped": f"missing tables: {missing}"}
    snap_warnings = update_team_snaps(con, season)
    rows = [derive_usage_row(r, season) for r in fetch_dicts(con, base_sql(con, season))]
    replace_where(con, "player_week_usage", rows, "season = ?", [season])
    unmatched_snaps = con.execute("""
        SELECT count(DISTINCT pfr_player_id) FROM nfl_snap_counts s
        WHERE season = ? AND position IN ('QB','RB','WR','TE','FB')
          AND NOT EXISTS (SELECT 1 FROM nfl_players p WHERE p.pfr_id = s.pfr_player_id)
    """, [season]).fetchone()[0]
    return {"rows": len(rows), "snap_players_without_gsis": unmatched_snaps, "team_snap_warnings": snap_warnings}

"""Point-in-time feature pipeline for independent weekly projections (QB/RB/WR/TE).

Leakage contract (tested in tests/test_forecast_features.py):
  * A forecast for (season S, NFL week W) may use only games with (season, week) < (S, W).
    `features_for_week` re-filters every input frame to that cutoff itself, so a caller
    that passes the full panel cannot leak the target week or any later week.
  * Opponent and team context use only that team's games completed before week W of the
    same season (never a defense's full-season numbers).
  * The target week contributes only pregame facts: the player's team, the opponent and
    home/away from the schedule. Its box score, snaps, xFP and points are never inputs.
  * Betting lines are NOT used: nflverse stores closing lines, which are set after a
    Tuesday-Thursday forecast cutoff.
  * Unknown values stay null and get a missing indicator; a measured zero stays zero.

Units: `league_points` are this league's fantasy points (app.analytics.league_scoring).
nflverse xFP (`total_fantasy_points_exp`) is an opportunity measure in nflverse's own PPR
units; it is a feature, never relabeled as an expected league score.
See docs/independent_projection_engine.md for every feature's definition.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ..db.connection import table_columns, table_exists
from .league_scoring import COMPONENT_COLUMNS, SkillScoring, points_sql

POSITIONS = ("QB", "RB", "WR", "TE")
RECENT_GAMES = 5            # player window: most recent games played
PRIOR_SEASONS = 1           # lookback: current season + this many previous seasons
SHRINK_GAMES = 2.0          # player per-game rates shrink toward the position mean by this many games
TEAM_SHRINK_GAMES = 3.0     # team offense per-game rates
OPP_SHRINK_GAMES = 4.0      # opponent points allowed to a position
OPP_SHRINK_PLAYS = 150.0    # opponent EPA per play
ABSENCE_WEEKS = 3           # weeks since last game at or above which a player is "returning"

PANEL_COLUMNS = (
    "season", "week", "gsis_id", "position", "team", "opponent", "has_stats", "has_snaps",
    "league_points", "pass_attempts", "carries", "targets", "receptions", "air_yards",
    "passing_yards", "rushing_yards", "receiving_yards", "offense_pct", "xfp",
    "rz_targets", "rz_carries", "gl_carries", "designed_rushes",
    "team_targets", "team_rush_attempts", "team_rb_carries",
)


def key(season, week):
    return season * 100 + week


# ----------------------------------------------------------------------------- loading


def _optional_sql(con, table: str, cols: set[str], sql: str, empty: str) -> str:
    return sql if table_exists(con, table) and cols <= set(table_columns(con, table)) else empty


def load_panel(con, scoring: SkillScoring, seasons: list[int] | None = None) -> pl.DataFrame:
    """One row per (season, week, player) for REG games a QB/RB/WR/TE played: an nflverse
    box-score row, or offensive snaps > 0. A snap-only appearance is a measured zero for
    counting stats and points (as in player_week_usage). Absent sources stay null."""
    scoring.require_supported()
    if not table_exists(con, "nfl_weekly_stats"):
        return pl.DataFrame(schema=_panel_schema())
    stat_cols = set(table_columns(con, "nfl_weekly_stats"))
    missing = [c for c in scoring.weights if c not in stat_cols]
    if missing:
        raise ValueError(f"nfl_weekly_stats lacks scoring columns {missing}")
    where = f"AND season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    snaps = _optional_sql(con, "nfl_snap_counts", {"pfr_player_id", "offense_snaps", "offense_pct"}, f"""
        SELECT np.gsis_id, s.season::INT AS season, s.week::INT AS week, any_value(s.team) AS team,
               any_value(s.opponent) AS opponent, max(s.offense_snaps) AS offense_snaps,
               max(s.offense_pct) AS offense_pct
        FROM nfl_snap_counts s JOIN nfl_players np ON np.pfr_id = s.pfr_player_id
        WHERE s.game_type = 'REG' {where.replace('season', 's.season')} GROUP BY 1, 2, 3""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT season, NULL::INT week, NULL::VARCHAR team, "
        "NULL::VARCHAR opponent, NULL::DOUBLE offense_snaps, NULL::DOUBLE offense_pct WHERE false")
    xfp = _optional_sql(con, "nfl_ff_opportunity", {"player_id", "total_fantasy_points_exp"}, f"""
        SELECT player_id AS gsis_id, season::INT AS season, week::INT AS week,
               sum(total_fantasy_points_exp) AS xfp
        FROM nfl_ff_opportunity WHERE true {where} GROUP BY 1, 2, 3""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT season, NULL::INT week, NULL::DOUBLE xfp WHERE false")
    pbp = _optional_sql(con, "nfl_pbp_player_week", {"rz_targets", "rz_carries", "gl_carries"}, f"""
        SELECT player_id AS gsis_id, season, week, rz_targets, rz_carries, gl_carries, designed_rushes
        FROM nfl_pbp_player_week WHERE true {where}""",
        "SELECT NULL::VARCHAR gsis_id, NULL::INT season, NULL::INT week, NULL::INT rz_targets, "
        "NULL::INT rz_carries, NULL::INT gl_carries, NULL::INT designed_rushes WHERE false")
    team = _optional_sql(con, "nfl_team_week", {"team_targets", "team_rush_attempts", "team_rb_carries"}, f"""
        SELECT season, week, team, team_targets, team_rush_attempts, team_rb_carries
        FROM nfl_team_week WHERE true {where}""",
        "SELECT NULL::INT season, NULL::INT week, NULL::VARCHAR team, NULL::INT team_targets, "
        "NULL::INT team_rush_attempts, NULL::INT team_rb_carries WHERE false")
    pts = points_sql(scoring, "w")
    sql = f"""
    WITH stats AS (
        SELECT w.player_id AS gsis_id, w.season::INT AS season, w.week::INT AS week, w.team,
               w.opponent_team AS opponent, CASE WHEN w.position = 'FB' THEN 'RB' ELSE w.position END AS position,
               {pts} AS league_points, w.attempts AS pass_attempts, w.carries, w.targets, w.receptions,
               w.receiving_air_yards AS air_yards, w.passing_yards, w.rushing_yards, w.receiving_yards
        FROM nfl_weekly_stats w WHERE w.season_type = 'REG' {where}
    ),
    snaps AS ({snaps}), xfp AS ({xfp}), pbp AS ({pbp}), tw AS ({team}),
    base AS (
        SELECT coalesce(st.gsis_id, sn.gsis_id) AS gsis_id, coalesce(st.season, sn.season) AS season,
               coalesce(st.week, sn.week) AS week, coalesce(st.team, sn.team) AS team,
               coalesce(st.opponent, sn.opponent) AS opponent,
               coalesce(st.position, CASE WHEN np.position = 'FB' THEN 'RB' ELSE np.position END) AS position,
               st.gsis_id IS NOT NULL AS has_stats, sn.gsis_id IS NOT NULL AS has_snaps,
               CASE WHEN st.gsis_id IS NULL THEN 0.0 ELSE st.league_points END AS league_points,
               coalesce(st.pass_attempts, 0) AS pass_attempts, coalesce(st.carries, 0) AS carries,
               coalesce(st.targets, 0) AS targets, coalesce(st.receptions, 0) AS receptions,
               coalesce(st.air_yards, 0) AS air_yards, coalesce(st.passing_yards, 0) AS passing_yards,
               coalesce(st.rushing_yards, 0) AS rushing_yards, coalesce(st.receiving_yards, 0) AS receiving_yards,
               sn.offense_pct, sn.offense_snaps
        FROM stats st
        FULL OUTER JOIN snaps sn ON sn.gsis_id = st.gsis_id AND sn.season = st.season AND sn.week = st.week
        LEFT JOIN nfl_players np ON np.gsis_id = coalesce(st.gsis_id, sn.gsis_id)
    )
    SELECT b.season, b.week, b.gsis_id, b.position, b.team, b.opponent, b.has_stats, b.has_snaps,
           b.league_points, b.pass_attempts, b.carries, b.targets, b.receptions, b.air_yards,
           b.passing_yards, b.rushing_yards, b.receiving_yards, b.offense_pct, x.xfp,
           -- No play-by-play row while the team's game has play-by-play = no target/rush: a measured 0.
           CASE WHEN p.gsis_id IS NULL AND t.team IS NOT NULL THEN 0 ELSE p.rz_targets END AS rz_targets,
           CASE WHEN p.gsis_id IS NULL AND t.team IS NOT NULL THEN 0 ELSE p.rz_carries END AS rz_carries,
           CASE WHEN p.gsis_id IS NULL AND t.team IS NOT NULL THEN 0 ELSE p.gl_carries END AS gl_carries,
           CASE WHEN p.gsis_id IS NULL AND t.team IS NOT NULL THEN 0 ELSE p.designed_rushes END AS designed_rushes,
           t.team_targets, t.team_rush_attempts, t.team_rb_carries
    FROM base b
    LEFT JOIN xfp x ON x.gsis_id = b.gsis_id AND x.season = b.season AND x.week = b.week
    LEFT JOIN pbp p ON p.gsis_id = b.gsis_id AND p.season = b.season AND p.week = b.week
    LEFT JOIN tw t ON t.team = b.team AND t.season = b.season AND t.week = b.week
    WHERE b.gsis_id IS NOT NULL AND b.position IN ('QB', 'RB', 'WR', 'TE')
      AND (b.has_stats OR coalesce(b.offense_snaps, 0) > 0)
    ORDER BY b.season, b.week, b.gsis_id"""
    df = pl.from_arrow(con.execute(sql).arrow())
    if df.height == 0:
        return pl.DataFrame(schema=_panel_schema())
    return df.cast(_panel_schema(), strict=False).select(PANEL_COLUMNS)


def _panel_schema() -> dict:
    f, i, s, b = pl.Float64, pl.Int64, pl.String, pl.Boolean
    return {"season": i, "week": i, "gsis_id": s, "position": s, "team": s, "opponent": s, "has_stats": b,
            "has_snaps": b, "league_points": f, "pass_attempts": f, "carries": f, "targets": f,
            "receptions": f, "air_yards": f, "passing_yards": f, "rushing_yards": f, "receiving_yards": f,
            "offense_pct": f, "xfp": f, "rz_targets": f, "rz_carries": f, "gl_carries": f,
            "designed_rushes": f, "team_targets": f, "team_rush_attempts": f, "team_rb_carries": f}


def load_games(con, seasons: list[int] | None = None) -> pl.DataFrame:
    """Team-game rows (two per REG game) from nfl_schedules with scores when final."""
    schema = {"season": pl.Int64, "week": pl.Int64, "game_id": pl.String, "team": pl.String,
              "opponent": pl.String, "is_home": pl.Boolean, "gameday": pl.String, "gametime": pl.String,
              "points_for": pl.Float64, "points_against": pl.Float64}
    if not table_exists(con, "nfl_schedules"):
        return pl.DataFrame(schema=schema)
    where = f"AND season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    sql = f"""
        SELECT season, week, game_id, home_team AS team, away_team AS opponent, true AS is_home,
               gameday, gametime, home_score AS points_for, away_score AS points_against
        FROM nfl_schedules WHERE game_type = 'REG' {where}
        UNION ALL
        SELECT season, week, game_id, away_team, home_team, false, gameday, gametime, away_score, home_score
        FROM nfl_schedules WHERE game_type = 'REG' {where}"""
    df = pl.from_arrow(con.execute(sql).arrow())
    return df.cast(schema, strict=False).select(list(schema)) if df.height else pl.DataFrame(schema=schema)


def load_defense(con, seasons: list[int] | None = None) -> pl.DataFrame:
    """Per defense-game EPA allowed (play-by-play derived, see nfl_metrics_verified_contract)."""
    schema = {"season": pl.Int64, "week": pl.Int64, "defteam": pl.String, "pass_plays": pl.Float64,
              "pass_epa_sum": pl.Float64, "rush_plays": pl.Float64, "rush_epa_sum": pl.Float64}
    if not table_exists(con, "nfl_defense_week"):
        return pl.DataFrame(schema=schema)
    where = f"WHERE season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    df = pl.from_arrow(con.execute(f"SELECT season, week, defteam, pass_plays, pass_epa_sum, rush_plays, "
                                   f"rush_epa_sum FROM nfl_defense_week {where}").arrow())
    return df.cast(schema, strict=False).select(list(schema)) if df.height else pl.DataFrame(schema=schema)


@dataclass
class Sources:
    panel: pl.DataFrame
    games: pl.DataFrame
    defense: pl.DataFrame

    @staticmethod
    def concat(parts: list["Sources"]) -> "Sources":
        """Combine sources (e.g. history DB + live DB). A season present in several parts is
        kept from the LAST part only, so the live database wins for the live season."""
        seen: set[int] = set()
        keep: list[Sources] = []
        for part in reversed(parts):
            seasons = set(part.panel["season"].unique().to_list()) | set(part.games["season"].unique().to_list())
            new = seasons - seen
            seen |= seasons
            flt = pl.col("season").is_in(sorted(new))
            keep.append(Sources(part.panel.filter(flt), part.games.filter(flt), part.defense.filter(flt)))
        keep.reverse()
        return Sources(pl.concat([k.panel for k in keep]).sort("season", "week", "gsis_id"),
                       pl.concat([k.games for k in keep]), pl.concat([k.defense for k in keep]))


def load_sources(con, scoring: SkillScoring, seasons: list[int] | None = None) -> Sources:
    return Sources(load_panel(con, scoring, seasons), load_games(con, seasons), load_defense(con, seasons))


# ----------------------------------------------------------------------------- features

# name -> (meaning, provenance). Every feature is computed from games strictly before the target week.
FEATURE_DOCS: dict[str, tuple[str, str]] = {
    "pts_recent_raw": ("Unshrunk league points per game over the last <=5 games (baseline A1 only)",
                       "nfl_weekly_stats via league_scoring"),
    "pts_season_raw": ("Unshrunk league points per game this season before week W (baseline A2 only)",
                       "nfl_weekly_stats via league_scoring"),
    "pts_recent": ("League points per game over the player's last <=5 games, shrunk toward the position mean "
                   f"by {SHRINK_GAMES:g} games", "nfl_weekly_stats via league_scoring"),
    "pts_l3": ("Raw league points per game over the last <=3 games", "nfl_weekly_stats via league_scoring"),
    "pts_season": ("League points per game this season before week W, shrunk toward the position mean",
                   "nfl_weekly_stats via league_scoring"),
    "pts_prev_season": ("League points per game in the previous season (null if none)", "nfl_weekly_stats"),
    "pts_std": ("Standard deviation of league points over the last <=5 games (null with <2 games)", "derived"),
    "xfp_recent": ("nflverse expected fantasy points (nflverse PPR units) per game over the last <=5 games with "
                   "xFP coverage, shrunk toward the position mean", "nfl_ff_opportunity"),
    "pts_minus_xfp": ("Recent league points per game minus recent nflverse xFP per game (efficiency; unit mix "
                      "noted)", "derived"),
    "targets_pg": ("Targets per game, last <=5 games", "nfl_weekly_stats"),
    "carries_pg": ("Carries per game, last <=5 games (nflverse official carries)", "nfl_weekly_stats"),
    "pass_att_pg": ("Pass attempts per game, last <=5 games", "nfl_weekly_stats"),
    "air_yards_pg": ("Receiving air yards per game, last <=5 games", "nfl_weekly_stats"),
    "target_share": ("Player targets / team targets over the last <=5 games (play-by-play team totals)",
                     "nfl_weekly_stats + nfl_team_week"),
    "rush_share": ("Player carries / team rush attempts over the last <=5 games", "nfl_weekly_stats + nfl_team_week"),
    "rz_opps_pg": ("Red-zone targets + red-zone designed rushes per game, last <=5 games", "nfl_pbp_player_week"),
    "gl_carries_pg": ("Designed rushes inside the 5 per game, last <=5 games", "nfl_pbp_player_week"),
    "snap_pct": ("Mean offensive snap share over the last <=5 games with a snap record", "nfl_snap_counts"),
    "snap_trend": ("Last game's snap share minus the mean of the earlier recent games (null with <2)", "nfl_snap_counts"),
    "opp_trend": ("Last game's targets+carries minus the mean of the earlier recent games (null with <2)",
                  "nfl_weekly_stats"),
    "games_recent": ("Games in the recent window (0-5)", "derived"),
    "games_season": ("Games played this season before week W", "derived"),
    "weeks_since_last": ("NFL weeks since the player's last game (season boundary counted as 18 weeks)", "derived"),
    "team_changed": ("1 if the target-week team differs from the team in the player's last game", "rosters/schedule"),
    "team_pts_pg": ("Target team's points scored per game this season before week W minus league mean, shrunk "
                    f"by {TEAM_SHRINK_GAMES:g} games", "nfl_schedules"),
    "opp_pos_allowed": ("Opponent's league points allowed per game to this position this season before week W, "
                        f"relative to league mean, shrunk by {OPP_SHRINK_GAMES:g} games", "panel by opponent"),
    "opp_pass_epa": ("Opponent's EPA per pass play allowed this season before week W relative to league, shrunk",
                     "nfl_defense_week"),
    "opp_rush_epa": ("Opponent's EPA per designed rush allowed this season before week W relative to league, shrunk",
                     "nfl_defense_week"),
    "is_home": ("1 if the player's team is at home in week W (neutral sites follow nflverse home_team)", "nfl_schedules"),
    "xfp_missing": ("1 if no recent game has nflverse xFP", "derived"),
    "snap_missing": ("1 if no recent game has a snap record", "derived"),
    "rz_missing": ("1 if no recent game has play-by-play red-zone data", "derived"),
}


def _shrink(total: pl.Expr, n: pl.Expr, prior, k: float) -> pl.Expr:
    return (total + prior * k) / (n + k)


def features_for_week(sources: Sources, season: int, week: int, targets: pl.DataFrame) -> pl.DataFrame:
    """Features for `targets` (columns gsis_id, position, team, opponent, is_home) for NFL week
    (season, week). Uses ONLY rows strictly before (season, week); the filter is applied here."""
    cutoff = key(season, week)
    panel = sources.panel.filter(
        (pl.col("season") * 100 + pl.col("week") < cutoff) & (pl.col("season") >= season - PRIOR_SEASONS))
    games = sources.games.filter(
        (pl.col("season") == season) & (pl.col("week") < week) & pl.col("points_for").is_not_null())
    defense = sources.defense.filter((pl.col("season") == season) & (pl.col("week") < week))
    season_panel = panel.filter(pl.col("season") == season)

    if targets.height == 0:
        return targets
    # --- position priors (point-in-time: from the same pre-cutoff window)
    priors = {}
    for pos in POSITIONS:
        rows = panel.filter(pl.col("position") == pos)
        priors[pos] = {
            "pts": float(rows["league_points"].mean()) if rows.height else 0.0,
            "xfp": float(rows["xfp"].drop_nulls().mean()) if rows["xfp"].drop_nulls().len() else 0.0,
        }
    prior_df = pl.DataFrame({"position": list(priors), "prior_pts": [priors[p]["pts"] for p in priors],
                             "prior_xfp": [priors[p]["xfp"] for p in priors]})

    # --- player windows
    ordered = panel.sort(["season", "week"], descending=True)
    recent = ordered.group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
    recent = recent.with_columns(pl.int_range(pl.len()).over("gsis_id").alias("rank"))
    xfp_ok, snap_ok = pl.col("xfp").is_not_null(), pl.col("offense_pct").is_not_null()
    rz_ok = pl.col("rz_targets").is_not_null()
    agg = recent.group_by("gsis_id").agg(
        pl.len().alias("games_recent"),
        pl.col("league_points").sum().alias("pts_sum"),
        pl.col("league_points").mean().alias("pts_recent_raw"),
        pl.col("league_points").filter(pl.col("rank") < 3).mean().alias("pts_l3"),
        pl.col("league_points").std().alias("pts_std"),
        pl.col("xfp").filter(xfp_ok).sum().alias("xfp_sum"),
        xfp_ok.sum().alias("xfp_games"),
        pl.col("targets").mean().alias("targets_pg"),
        pl.col("carries").mean().alias("carries_pg"),
        pl.col("pass_attempts").mean().alias("pass_att_pg"),
        pl.col("air_yards").mean().alias("air_yards_pg"),
        pl.col("targets").filter(pl.col("team_targets").is_not_null()).sum().alias("_tg"),
        pl.col("team_targets").sum().alias("_team_tg"),
        pl.col("carries").filter(pl.col("team_rush_attempts").is_not_null()).sum().alias("_ca"),
        pl.col("team_rush_attempts").sum().alias("_team_ru"),
        (pl.col("rz_targets") + pl.col("rz_carries")).filter(rz_ok).mean().alias("rz_opps_pg"),
        pl.col("gl_carries").filter(rz_ok).mean().alias("gl_carries_pg"),
        rz_ok.sum().alias("rz_games"),
        pl.col("offense_pct").filter(snap_ok).mean().alias("snap_pct"),
        snap_ok.sum().alias("snap_games"),
        pl.col("offense_pct").filter(pl.col("rank") == 0).first().alias("_snap_last"),
        pl.col("offense_pct").filter((pl.col("rank") > 0) & snap_ok).mean().alias("_snap_prev"),
        (pl.col("targets") + pl.col("carries")).filter(pl.col("rank") == 0).first().alias("_opp_last"),
        (pl.col("targets") + pl.col("carries")).filter(pl.col("rank") > 0).mean().alias("_opp_prev"),
        pl.col("team").first().alias("last_team"),
        pl.col("season").first().alias("last_season"),
        pl.col("week").first().alias("last_week"),
    )
    season_agg = season_panel.group_by("gsis_id").agg(pl.len().alias("games_season"),
                                                      pl.col("league_points").sum().alias("pts_season_sum"),
                                                      pl.col("league_points").mean().alias("pts_season_raw"))
    prev_agg = panel.filter(pl.col("season") == season - 1).group_by("gsis_id").agg(
        pl.col("league_points").mean().alias("pts_prev_season"))

    # --- team offense (target team, this season before W)
    team_pts = games.group_by("team").agg(pl.len().alias("_tg_n"), pl.col("points_for").sum().alias("_tg_pts"))
    league_pts = float(games["points_for"].mean()) if games.height else None
    # --- opponent points allowed to position (this season before W)
    allowed = season_panel.group_by(["opponent", "position", "week"]).agg(pl.col("league_points").sum().alias("pa"))
    allowed = allowed.group_by(["opponent", "position"]).agg(pl.len().alias("_oa_n"), pl.col("pa").sum().alias("_oa_sum"))
    pos_mean_allowed = (season_panel.group_by(["opponent", "position", "week"]).agg(pl.col("league_points").sum().alias("pa"))
                        .group_by("position").agg(pl.col("pa").mean().alias("_oa_league")))
    # --- opponent EPA (this season before W)
    dfn = defense.group_by("defteam").agg(pl.col("pass_plays").sum().alias("_pp"), pl.col("pass_epa_sum").sum().alias("_pe"),
                                          pl.col("rush_plays").sum().alias("_rp"), pl.col("rush_epa_sum").sum().alias("_re"))
    lg_pass = float(defense["pass_epa_sum"].sum() / defense["pass_plays"].sum()) if defense.height and defense["pass_plays"].sum() else None
    lg_rush = float(defense["rush_epa_sum"].sum() / defense["rush_plays"].sum()) if defense.height and defense["rush_plays"].sum() else None

    out = (targets.select("gsis_id", "position", "team", "opponent", "is_home")
           .join(prior_df, on="position", how="left")
           .join(agg, on="gsis_id", how="left")
           .join(season_agg, on="gsis_id", how="left")
           .join(prev_agg, on="gsis_id", how="left")
           .join(team_pts, on="team", how="left")
           .join(allowed, left_on=["opponent", "position"], right_on=["opponent", "position"], how="left")
           .join(pos_mean_allowed, on="position", how="left")
           .join(dfn, left_on="opponent", right_on="defteam", how="left"))
    n = pl.col("games_recent").fill_null(0)
    out = out.with_columns(
        n.alias("games_recent"),
        pl.col("games_season").fill_null(0),
    ).with_columns(
        _shrink(pl.col("pts_sum").fill_null(0), n, pl.col("prior_pts"), SHRINK_GAMES).alias("pts_recent"),
        pl.when(pl.col("games_season") > 0).then(
            (pl.col("pts_season_sum").fill_null(0) + pl.col("prior_pts") * SHRINK_GAMES)
            / (pl.col("games_season") + SHRINK_GAMES)).otherwise(None).alias("pts_season"),
        pl.when(pl.col("xfp_games") > 0).then(
            (pl.col("xfp_sum") + pl.col("prior_xfp") * SHRINK_GAMES) / (pl.col("xfp_games") + SHRINK_GAMES)
        ).otherwise(None).alias("xfp_recent"),
        pl.when(pl.col("_team_tg") > 0).then(pl.col("_tg") / pl.col("_team_tg")).otherwise(None).alias("target_share"),
        pl.when(pl.col("_team_ru") > 0).then(pl.col("_ca") / pl.col("_team_ru")).otherwise(None).alias("rush_share"),
        (pl.col("_snap_last") - pl.col("_snap_prev")).alias("snap_trend"),
        (pl.col("_opp_last") - pl.col("_opp_prev")).alias("opp_trend"),
        pl.when(pl.col("last_season") == season).then(week - pl.col("last_week"))
          .otherwise(week + 18 * (season - pl.col("last_season")) - pl.col("last_week")).alias("weeks_since_last"),
        (pl.col("last_team").is_not_null() & (pl.col("last_team") != pl.col("team"))).cast(pl.Int8).alias("team_changed"),
        (pl.col("xfp_games").fill_null(0) == 0).cast(pl.Int8).alias("xfp_missing"),
        (pl.col("snap_games").fill_null(0) == 0).cast(pl.Int8).alias("snap_missing"),
        (pl.col("rz_games").fill_null(0) == 0).cast(pl.Int8).alias("rz_missing"),
        pl.col("is_home").cast(pl.Int8),
        (((pl.col("_tg_pts").fill_null(0) + (league_pts or 0) * TEAM_SHRINK_GAMES)
          / (pl.col("_tg_n").fill_null(0) + TEAM_SHRINK_GAMES)) - (league_pts or 0)).alias("team_pts_pg"),
        pl.when(pl.col("_oa_league").is_not_null()).then(
            (pl.col("_oa_sum").fill_null(0) + pl.col("_oa_league") * OPP_SHRINK_GAMES)
            / (pl.col("_oa_n").fill_null(0) + OPP_SHRINK_GAMES) - pl.col("_oa_league")).otherwise(0.0)
          .alias("opp_pos_allowed"),
        (((pl.col("_pe").fill_null(0) + (lg_pass or 0) * OPP_SHRINK_PLAYS) / (pl.col("_pp").fill_null(0) + OPP_SHRINK_PLAYS))
         - (lg_pass or 0)).alias("opp_pass_epa"),
        (((pl.col("_re").fill_null(0) + (lg_rush or 0) * OPP_SHRINK_PLAYS) / (pl.col("_rp").fill_null(0) + OPP_SHRINK_PLAYS))
         - (lg_rush or 0)).alias("opp_rush_epa"),
        pl.lit(season).alias("season"), pl.lit(week).alias("week"),
    ).with_columns((pl.col("pts_recent") - pl.col("xfp_recent")).alias("pts_minus_xfp"))
    # the opponent/team context had data this season?
    out = out.with_columns(
        (pl.col("_oa_n").fill_null(0)).alias("opp_games"),
        pl.col("_tg_n").fill_null(0).alias("team_games"),
        pl.lit(len(set(games["team"].to_list())) if games.height else 0).alias("_teams_with_games"),
    )
    keep = ["season", "week", "gsis_id", "position", "team", "opponent", "last_team", "last_season", "last_week",
            "opp_games", "team_games", *FEATURE_DOCS]
    return out.select([c for c in dict.fromkeys(keep) if c in out.columns])


def played_targets(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Backtest population: players who appeared in (season, week). Their team and opponent are
    pregame facts (roster + schedule); the appearance itself is the conditioning event, so these
    are forecasts conditional on playing (availability is evaluated separately)."""
    rows = sources.panel.filter((pl.col("season") == season) & (pl.col("week") == week))
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week)).select(
        "team", "opponent", "is_home")
    return (rows.select("gsis_id", "position", "team", "opponent", "league_points")
            .join(games, on=["team", "opponent"], how="inner"))

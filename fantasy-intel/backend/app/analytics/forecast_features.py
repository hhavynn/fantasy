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
  * Injury reports (optional `Sources.injuries`) may be used up to and INCLUDING week W: the
    week-W report is published before the week-W game (rows modified at or after kickoff are
    dropped at load time). They are used only to label a last-game snap drop as an injury exit.
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
SNAP_DROP = 0.15            # last-game snap share this far below the earlier recent mean is a "drop"
EXIT_REPORT_WEEKS = 2       # an injury listing within this many weeks after the drop game marks an injury exit
BLOWOUT_MARGIN = 17         # a snap drop in a game decided by at least this many points is "game script"
REF_GAMES = 4               # a game's reference snap share: mean of the player's previous <= this many games
USAGE_PRIOR_GAMES = 3.0     # healthy current-season usage shrinks toward last season's by this many games (EB)
GAME_CLASSES = ("full", "injury_exit", "blowout_low", "listed_low", "role_low", "unknown")

PANEL_COLUMNS = (
    "season", "week", "gsis_id", "position", "team", "opponent", "has_stats", "has_snaps",
    "league_points", "pass_attempts", "carries", "targets", "receptions", "air_yards",
    "passing_yards", "rushing_yards", "receiving_yards", "offense_pct", "xfp",
    "rz_targets", "rz_carries", "gl_carries", "designed_rushes",
    "team_targets", "team_rush_attempts", "team_rb_carries",
    "td_points", "xfpl", "xfpl_td", "team_plays",
)

# League scoring column -> nflverse ff_opportunity expected column. Expected league points
# (`xfpl`) = sum(league weight * expected count): the same units as `league_points`, unlike
# nflverse's own PPR total. Fumbles and return/recovery TDs have no expected column and are left
# out (they are rare); every touchdown component is also summed separately (`xfpl_td`).
EXPECTED_COMPONENTS = {
    "receptions": "receptions_exp", "receiving_yards": "rec_yards_gained_exp",
    "rushing_yards": "rush_yards_gained_exp", "passing_yards": "pass_yards_gained_exp",
    "passing_tds": "pass_touchdown_exp", "receiving_tds": "rec_touchdown_exp", "rushing_tds": "rush_touchdown_exp",
    "passing_interceptions": "pass_interception_exp", "passing_2pt_conversions": "pass_two_point_conv_exp",
    "receiving_2pt_conversions": "rec_two_point_conv_exp", "rushing_2pt_conversions": "rush_two_point_conv_exp",
}
TD_COLUMNS = ("passing_tds", "receiving_tds", "rushing_tds", "special_teams_tds", "fumble_recovery_tds")


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
        "SELECT NULL::VARCHAR AS gsis_id, NULL::INT AS season, NULL::INT AS week, NULL::VARCHAR AS team, "
        "NULL::VARCHAR AS opponent, NULL::DOUBLE AS offense_snaps, NULL::DOUBLE AS offense_pct WHERE false")
    xfp = _optional_sql(con, "nfl_ff_opportunity", {"player_id", "total_fantasy_points_exp"}, f"""
        SELECT player_id AS gsis_id, season::INT AS season, week::INT AS week,
               sum(total_fantasy_points_exp) AS xfp
        FROM nfl_ff_opportunity WHERE true {where} GROUP BY 1, 2, 3""",
        "SELECT NULL::VARCHAR AS gsis_id, NULL::INT AS season, NULL::INT AS week, NULL::DOUBLE AS xfp WHERE false")
    pbp = _optional_sql(con, "nfl_pbp_player_week", {"rz_targets", "rz_carries", "gl_carries"}, f"""
        SELECT player_id AS gsis_id, season, week, rz_targets, rz_carries, gl_carries, designed_rushes
        FROM nfl_pbp_player_week WHERE true {where}""",
        "SELECT NULL::VARCHAR AS gsis_id, NULL::INT AS season, NULL::INT AS week, NULL::INT AS rz_targets, "
        "NULL::INT AS rz_carries, NULL::INT AS gl_carries, NULL::INT AS designed_rushes WHERE false")
    exp = {k: c for k, c in EXPECTED_COMPONENTS.items() if k in scoring.weights}
    exp_sum = lambda keys: " + ".join(f"{float(scoring.weights[k])!r} * coalesce({exp[k]}, 0)" for k in keys) or "0"  # noqa: E731
    xfpl = _optional_sql(con, "nfl_ff_opportunity", {"player_id", *exp.values()}, f"""
        SELECT player_id AS gsis_id, season::INT AS season, week::INT AS week,
               sum({exp_sum(exp)}) AS xfpl, sum({exp_sum([k for k in exp if k.endswith('_tds')])}) AS xfpl_td
        FROM nfl_ff_opportunity WHERE true {where} GROUP BY 1, 2, 3""",
        "SELECT NULL::VARCHAR AS gsis_id, NULL::INT AS season, NULL::INT AS week, NULL::DOUBLE AS xfpl, "
        "NULL::DOUBLE AS xfpl_td WHERE false")
    tw_cols = set(table_columns(con, "nfl_team_week")) if table_exists(con, "nfl_team_week") else set()
    plays = "offense_plays" if "offense_plays" in tw_cols else "NULL::INT"
    team = _optional_sql(con, "nfl_team_week", {"team_targets", "team_rush_attempts", "team_rb_carries"}, f"""
        SELECT season, week, team, team_targets, team_rush_attempts, team_rb_carries, {plays} AS team_plays
        FROM nfl_team_week WHERE true {where}""",
        "SELECT NULL::INT AS season, NULL::INT AS week, NULL::VARCHAR AS team, NULL::INT AS team_targets, "
        "NULL::INT AS team_rush_attempts, NULL::INT AS team_rb_carries, NULL::INT AS team_plays WHERE false")
    pts = points_sql(scoring, "w")
    td_pts = " + ".join(f"{float(scoring.weights[c])!r} * coalesce(w.{c}, 0)" for c in TD_COLUMNS
                        if c in scoring.weights and c in stat_cols) or "0"
    sql = f"""
    WITH stats AS (
        SELECT w.player_id AS gsis_id, w.season::INT AS season, w.week::INT AS week, w.team,
               w.opponent_team AS opponent, CASE WHEN w.position = 'FB' THEN 'RB' ELSE w.position END AS position,
               {pts} AS league_points, {td_pts} AS td_points, w.attempts AS pass_attempts, w.carries, w.targets, w.receptions,
               w.receiving_air_yards AS air_yards, w.passing_yards, w.rushing_yards, w.receiving_yards
        FROM nfl_weekly_stats w WHERE w.season_type = 'REG' {where}
    ),
    snaps AS ({snaps}), xfp AS ({xfp}), xfpl AS ({xfpl}), pbp AS ({pbp}), tw AS ({team}),
    base AS (
        SELECT coalesce(st.gsis_id, sn.gsis_id) AS gsis_id, coalesce(st.season, sn.season) AS season,
               coalesce(st.week, sn.week) AS week, coalesce(st.team, sn.team) AS team,
               coalesce(st.opponent, sn.opponent) AS opponent,
               coalesce(st.position, CASE WHEN np.position = 'FB' THEN 'RB' ELSE np.position END) AS position,
               st.gsis_id IS NOT NULL AS has_stats, sn.gsis_id IS NOT NULL AS has_snaps,
               CASE WHEN st.gsis_id IS NULL THEN 0.0 ELSE st.league_points END AS league_points,
               CASE WHEN st.gsis_id IS NULL THEN 0.0 ELSE st.td_points END AS td_points,
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
           t.team_targets, t.team_rush_attempts, t.team_rb_carries,
           b.td_points, xl.xfpl, xl.xfpl_td, t.team_plays
    FROM base b
    LEFT JOIN xfp x ON x.gsis_id = b.gsis_id AND x.season = b.season AND x.week = b.week
    LEFT JOIN xfpl xl ON xl.gsis_id = b.gsis_id AND xl.season = b.season AND xl.week = b.week
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
            "designed_rushes": f, "team_targets": f, "team_rush_attempts": f, "team_rb_carries": f,
            "td_points": f, "xfpl": f, "xfpl_td": f, "team_plays": f}


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
    rosters: pl.DataFrame | None = None     # nflverse weekly roster status (participation model)
    injuries: pl.DataFrame | None = None    # nflverse weekly injury report, pregame rows only
    exits: pl.DataFrame | None = None       # play-by-play in-game injury evidence (app.nfl.pbp_exits)

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
            keep.append(Sources(part.panel.filter(flt), part.games.filter(flt), part.defense.filter(flt),
                                None if part.rosters is None else part.rosters.filter(flt),
                                None if part.injuries is None else part.injuries.filter(flt),
                                None if part.exits is None else part.exits.filter(flt)))
        keep.reverse()

        def optional(name):
            frames = [getattr(k, name) for k in keep if getattr(k, name) is not None]
            return pl.concat(frames) if frames else None

        return Sources(pl.concat([k.panel for k in keep]).sort("season", "week", "gsis_id"),
                       pl.concat([k.games for k in keep]), pl.concat([k.defense for k in keep]),
                       optional("rosters"), optional("injuries"), optional("exits"))

    def with_exits(self, exits: pl.DataFrame | None) -> "Sources":
        """Same sources plus play-by-play in-game injury evidence (seasons it covers only)."""
        if exits is None:
            return self
        return Sources(self.panel, self.games, self.defense, self.rosters, self.injuries,
                       exits.cast(EXITS_SCHEMA, strict=False).select(list(EXITS_SCHEMA)))


EXITS_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "injured_in_game": pl.Int8,
                "injury_sec": pl.Float64, "last_touch_sec": pl.Float64, "team_last_sec": pl.Float64}
ROSTER_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                 "position": pl.String, "status": pl.String}
INJURY_SCHEMA = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                 "report_status": pl.String, "practice_status": pl.String, "injury": pl.String,
                 "not_injury": pl.Boolean}


def load_rosters(con, seasons: list[int] | None = None) -> pl.DataFrame | None:
    """One row per (season, week, player) from nflverse weekly rosters (REG). A player listed
    twice in a week (trade) keeps the ACT row, then INA, then any other."""
    need = {"season", "week", "gsis_id", "team", "position", "status"}
    if not table_exists(con, "nfl_rosters_weekly") or not need <= set(table_columns(con, "nfl_rosters_weekly")):
        return None
    where = f"AND season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    gt = "AND (game_type = 'REG' OR game_type IS NULL)" if "game_type" in table_columns(con, "nfl_rosters_weekly") else ""
    pref = "CASE status WHEN 'ACT' THEN 0 WHEN 'INA' THEN 1 ELSE 2 END"
    df = pl.from_arrow(con.execute(f"""
        SELECT season::INT AS season, week::INT AS week, gsis_id, min_by(team, {pref}) AS team,
               min_by(CASE WHEN position = 'FB' THEN 'RB' ELSE position END, {pref}) AS position,
               min_by(status, {pref}) AS status
        FROM nfl_rosters_weekly WHERE gsis_id IS NOT NULL {gt} {where} GROUP BY 1, 2, 3""").arrow())
    return df.cast(ROSTER_SCHEMA, strict=False).select(list(ROSTER_SCHEMA)) if df.height else pl.DataFrame(schema=ROSTER_SCHEMA)


def load_injuries(con, seasons: list[int] | None = None) -> pl.DataFrame | None:
    """One row per (season, week, player) from the nflverse weekly injury report (REG). When the
    table records date_modified, rows modified at or after the team's kickoff are DROPPED: a report
    is a pregame input only if it existed before the game."""
    cols = set(table_columns(con, "nfl_injuries")) if table_exists(con, "nfl_injuries") else set()
    if not {"season", "week", "gsis_id", "report_status", "practice_status"} <= cols:
        return None
    where = f"AND i.season IN ({', '.join(str(int(s)) for s in seasons)})" if seasons else ""
    gt = "AND i.game_type = 'REG'" if "game_type" in cols else ""
    pregame, kick = "", ""
    if "date_modified" in cols and table_exists(con, "nfl_schedules"):
        kick = """LEFT JOIN (
            SELECT season, week, t AS team, timezone('America/New_York', (gameday || ' ' || gametime)::TIMESTAMP) AS ko
            FROM (SELECT season, week, gameday, gametime, home_team AS t FROM nfl_schedules WHERE game_type = 'REG'
                  UNION ALL SELECT season, week, gameday, gametime, away_team FROM nfl_schedules WHERE game_type = 'REG')
            WHERE gameday IS NOT NULL AND gametime IS NOT NULL) k
          ON k.season = i.season AND k.week = i.week AND k.team = i.team"""
        pregame = "AND (i.date_modified IS NULL OR k.ko IS NULL OR i.date_modified < k.ko)"
    df = pl.from_arrow(con.execute(f"""
        SELECT i.season::INT AS season, i.week::INT AS week, i.gsis_id, any_value(i.team) AS team,
               any_value(i.report_status) AS report_status, any_value(trim(i.practice_status)) AS practice_status,
               any_value(coalesce(i.report_primary_injury, i.practice_primary_injury)) AS injury,
               bool_or(lower(coalesce(i.practice_primary_injury, '')) LIKE 'not injury related%') AS not_injury
        FROM nfl_injuries i {kick}
        WHERE i.gsis_id IS NOT NULL {gt} {where} {pregame} GROUP BY 1, 2, 3""").arrow())
    return df.cast(INJURY_SCHEMA, strict=False).select(list(INJURY_SCHEMA)) if df.height else pl.DataFrame(schema=INJURY_SCHEMA)


def load_sources(con, scoring: SkillScoring, seasons: list[int] | None = None) -> Sources:
    return Sources(load_panel(con, scoring, seasons), load_games(con, seasons), load_defense(con, seasons),
                   load_rosters(con, seasons), load_injuries(con, seasons))


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
    "snap_last": ("Snap share in the player's most recent game (null without a snap record)", "nfl_snap_counts"),
    "injury_exit_last": (f"1 if the last game was a snap drop (snap_trend <= -{SNAP_DROP:g}) AND the player is on an "
                         f"injury report (designation or injury practice listing) within {EXIT_REPORT_WEEKS} weeks after "
                         "it, up to and including the target week's pregame report; 0 otherwise; null without "
                         "snap_trend or injury reports", "nfl_snap_counts + nfl_injuries"),
    "snap_trend_role": ("snap_trend with injury exits removed (null when injury_exit_last = 1): the role-change "
                        "part of the last-game snap change", "nfl_snap_counts + nfl_injuries"),
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
    # --- added for the ifp-2 research challenger (unused by ifp-1.0.0; see docs "Weekly learning")
    "xfpl_recent": ("Expected LEAGUE points per game over the last <=5 games with xFP coverage (nflverse expected "
                    "counts x this league's weights), shrunk toward the position mean", "nfl_ff_opportunity + scoring"),
    "xfpl_last": ("Expected league points in the player's most recent game (null without coverage)",
                  "nfl_ff_opportunity + scoring"),
    "xfpl_td_recent": ("Touchdown part of expected league points per game, last <=5 covered games",
                       "nfl_ff_opportunity + scoring"),
    "yd_over_exp": ("Non-touchdown league points minus non-touchdown expected league points per game, last <=5 "
                    f"covered games, shrunk toward 0 by {SHRINK_GAMES:g} games (yardage/catch efficiency)",
                    "box score + nfl_ff_opportunity"),
    "td_over_exp": ("Touchdown league points minus expected touchdown points per game, last <=5 covered games, shrunk "
                    f"toward 0 by {SHRINK_GAMES:g} games (touchdown luck or skill)", "box score + nfl_ff_opportunity"),
    "pts_median": ("Median league points over the last <=5 games", "nfl_weekly_stats via league_scoring"),
    "opp_share": ("(Targets + carries) / (team targets + team rush attempts) over the last <=5 games",
                  "nfl_weekly_stats + nfl_team_week"),
    "opp_share_last": ("(Targets + carries) / team (targets + rush attempts) in the most recent game",
                       "nfl_weekly_stats + nfl_team_week"),
    "team_plays_pg": ("Target team's offensive plays per game this season before week W minus league mean, shrunk "
                      f"by {TEAM_SHRINK_GAMES:g} games", "nfl_team_week"),
    "last_team_plays_rel": ("Team offensive plays in the player's last game minus that team's mean over the player's "
                            "earlier recent games (low-volume game indicator)", "nfl_team_week"),
    "last_margin": ("Player's team final score margin in his last game (positive = won)", "nfl_schedules"),
    "xfpl_missing": ("1 if no recent game has expected league points", "derived"),
    # --- healthy-game usage and previous-season usage priors (ifp-2 research challenger). A game is an
    # injury exit when snap share fell >= SNAP_DROP below the player's previous <= REF_GAMES games AND
    # play-by-play says he was injured in it, or a NEW injury listing (not on that week's own report)
    # follows within EXIT_REPORT_WEEKS weeks, using reports up to the target week's pregame report only.
    "h_games_recent": ("Healthy (not injury-exit) games among the player's last <=5 healthy games in the lookback",
                       "derived"),
    "h_xfpl_pg": ("Expected league points per game over the last <=5 healthy games, shrunk toward the position mean",
                  "nfl_ff_opportunity + scoring + game classes"),
    "h_carries_pg": ("Carries per game over the last <=5 healthy games", "nfl_weekly_stats + game classes"),
    "h_targets_pg": ("Targets per game over the last <=5 healthy games", "nfl_weekly_stats + game classes"),
    "h_snap_pct": ("Mean snap share over the last <=5 healthy games with a snap record", "nfl_snap_counts + game classes"),
    "h_opp_share": ("(Targets + carries) / team (targets + rush attempts) over the last <=5 healthy games",
                    "nfl_weekly_stats + nfl_team_week + game classes"),
    "inj_exits_recent": ("Injury-exit games among the player's last <=5 appearances", "game classes"),
    "last_injury_exit": ("1 if the player's last game is classified as an injury exit", "game classes"),
    "last_role_low": ("1 if the last game was a snap drop with no injury evidence and no blowout (possible role cut)",
                      "game classes"),
    "last_blowout_low": (f"1 if the last game was a snap drop in a game decided by >= {BLOWOUT_MARGIN} points without "
                         "injury evidence", "game classes"),
    "prev_h_games": ("Healthy games in the previous season", "game classes"),
    "prev_xfpl_pg": ("Previous-season expected league points per healthy game, shrunk toward the position mean by "
                     f"{SHRINK_GAMES:g} games (null without a previous-season game)", "nfl_ff_opportunity + game classes"),
    "prev_carries_pg": ("Previous-season carries per healthy game", "nfl_weekly_stats + game classes"),
    "prev_targets_pg": ("Previous-season targets per healthy game", "nfl_weekly_stats + game classes"),
    "prev_snap_pct": ("Previous-season mean snap share over healthy games", "nfl_snap_counts + game classes"),
    "prev_team_changed": ("1 if the target-week team differs from the player's last team of the previous season",
                          "derived"),
    "cur_h_games": ("Healthy games this season before week W with expected-points coverage", "game classes"),
    "xfpl_eb": ("Empirical-Bayes established workload: this season's healthy-game expected league points per game, "
                f"shrunk toward prev_xfpl_pg by {USAGE_PRIOR_GAMES:g} games (toward the position mean when there is no "
                "previous season); null with neither", "nfl_ff_opportunity + game classes"),
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
            "xfpl": float(rows["xfpl"].drop_nulls().mean()) if rows["xfpl"].drop_nulls().len() else 0.0,
        }
    prior_df = pl.DataFrame({"position": list(priors), "prior_pts": [priors[p]["pts"] for p in priors],
                             "prior_xfp": [priors[p]["xfp"] for p in priors],
                             "prior_xfpl": [priors[p]["xfpl"] for p in priors]})

    # --- player windows
    ordered = panel.sort(["season", "week"], descending=True)
    recent = ordered.group_by("gsis_id", maintain_order=True).head(RECENT_GAMES)
    recent = recent.with_columns(pl.int_range(pl.len()).over("gsis_id").alias("rank"))
    xfp_ok, snap_ok = pl.col("xfp").is_not_null(), pl.col("offense_pct").is_not_null()
    rz_ok = pl.col("rz_targets").is_not_null()
    xfpl_ok = pl.col("xfpl").is_not_null()
    team_ok = pl.col("team_targets").is_not_null() & pl.col("team_rush_attempts").is_not_null()
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
        # challenger inputs (expected league points split into touchdown / non-touchdown parts)
        pl.col("xfpl").filter(xfpl_ok).sum().alias("_xl_sum"),
        xfpl_ok.sum().alias("_xl_n"),
        pl.col("xfpl_td").filter(xfpl_ok).sum().alias("_xl_td_sum"),
        ((pl.col("league_points") - pl.col("td_points")) - (pl.col("xfpl") - pl.col("xfpl_td")))
          .filter(xfpl_ok).sum().alias("_yd_res_sum"),
        (pl.col("td_points") - pl.col("xfpl_td")).filter(xfpl_ok).sum().alias("_td_res_sum"),
        pl.col("xfpl").filter(pl.col("rank") == 0).first().alias("xfpl_last"),
        pl.col("league_points").median().alias("pts_median"),
        (pl.col("targets") + pl.col("carries")).filter(team_ok).sum().alias("_opp_sum"),
        (pl.col("team_targets") + pl.col("team_rush_attempts")).filter(team_ok).sum().alias("_team_opp"),
        ((pl.col("targets") + pl.col("carries")) / (pl.col("team_targets") + pl.col("team_rush_attempts")))
          .filter((pl.col("rank") == 0) & team_ok).first().alias("opp_share_last"),
        pl.col("team_plays").filter(pl.col("rank") == 0).first().alias("_plays_last"),
        pl.col("team_plays").filter(pl.col("rank") > 0).mean().alias("_plays_prev"),
    )
    season_agg = season_panel.group_by("gsis_id").agg(pl.len().alias("games_season"),
                                                      pl.col("league_points").sum().alias("pts_season_sum"),
                                                      pl.col("league_points").mean().alias("pts_season_raw"))
    prev_agg = panel.filter(pl.col("season") == season - 1).group_by("gsis_id").agg(
        pl.col("league_points").mean().alias("pts_prev_season"))
    # --- team play volume this season before W, and each team-game's final margin
    team_plays = season_panel.select("week", "team", "team_plays").drop_nulls().unique(["week", "team"]).group_by(
        "team").agg(pl.len().alias("_tp_n"), pl.col("team_plays").sum().alias("_tp_sum"))
    league_plays = float(team_plays["_tp_sum"].sum() / team_plays["_tp_n"].sum()) if team_plays.height else None
    margins = sources.games.filter((pl.col("season") * 100 + pl.col("week") < cutoff)
                                   & pl.col("points_for").is_not_null()).select(
        pl.col("season").alias("last_season"), pl.col("week").alias("last_week"), pl.col("team").alias("last_team"),
        (pl.col("points_for") - pl.col("points_against")).alias("last_margin")).unique(
        ["last_season", "last_week", "last_team"])

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
           .join(dfn, left_on="opponent", right_on="defteam", how="left")
           .join(team_plays, on="team", how="left")
           .join(margins, on=["last_season", "last_week", "last_team"], how="left"))
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
    out = _challenger_features(out, league_plays)
    out = _usage_features(out, classify_games(panel, sources, season, week), season)
    # the opponent/team context had data this season?
    out = out.with_columns(
        (pl.col("_oa_n").fill_null(0)).alias("opp_games"),
        pl.col("_tg_n").fill_null(0).alias("team_games"),
        pl.lit(len(set(games["team"].to_list())) if games.height else 0).alias("_teams_with_games"),
    )
    out = _injury_exits(out, sources.injuries, season, week)
    keep = ["season", "week", "gsis_id", "position", "team", "opponent", "last_team", "last_season", "last_week",
            "opp_games", "team_games", *FEATURE_DOCS, "cur_h_xfpl_pg", "prior_xfpl"]
    out = out.select([c for c in dict.fromkeys(keep) if c in out.columns])
    # Parallel aggregation can change float summation order (~1e-15); rounding makes features bit-reproducible.
    return out.with_columns([pl.col(c).round(10) for c, t in out.schema.items() if t == pl.Float64])


def _challenger_features(out: pl.DataFrame, league_plays: float | None) -> pl.DataFrame:
    """Opportunity-first inputs for the ifp-2 research challenger. Same window and cutoff as the
    ifp-1.0.0 features; touchdown and non-touchdown efficiency are kept apart so a model can weigh
    a touchdown-driven boom differently from repeatable yardage efficiency."""
    xn = pl.col("_xl_n").fill_null(0)
    covered = xn > 0
    return out.with_columns(
        pl.when(covered).then((pl.col("_xl_sum") + pl.col("prior_xfpl") * SHRINK_GAMES) / (xn + SHRINK_GAMES))
          .otherwise(None).alias("xfpl_recent"),
        pl.when(covered).then(pl.col("_xl_td_sum") / xn).otherwise(None).alias("xfpl_td_recent"),
        pl.when(covered).then(pl.col("_yd_res_sum") / (xn + SHRINK_GAMES)).otherwise(None).alias("yd_over_exp"),
        pl.when(covered).then(pl.col("_td_res_sum") / (xn + SHRINK_GAMES)).otherwise(None).alias("td_over_exp"),
        pl.when(pl.col("_team_opp") > 0).then(pl.col("_opp_sum") / pl.col("_team_opp")).otherwise(None).alias("opp_share"),
        (((pl.col("_tp_sum").fill_null(0) + (league_plays or 0) * TEAM_SHRINK_GAMES)
          / (pl.col("_tp_n").fill_null(0) + TEAM_SHRINK_GAMES)) - (league_plays or 0)).alias("team_plays_pg"),
        (pl.col("_plays_last") - pl.col("_plays_prev")).alias("last_team_plays_rel"),
        (~covered).cast(pl.Int8).alias("xfpl_missing"),
    )


def classify_games(panel: pl.DataFrame, sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Label every pre-cutoff panel game for a forecast of (season, week). Point-in-time: the
    reference snap share uses only the player's EARLIER games, and injury listings are read only up
    to the target week's pregame report, so a recent game can be relabeled as later reports appear
    but never with information from after the forecast.

    gclass: full (no snap drop) | injury_exit (drop + play-by-play injury, or a new listing after it)
            | blowout_low (drop, no injury evidence, final margin >= BLOWOUT_MARGIN)
            | listed_low (drop, listed after it but also listed before it: chronic listing, ambiguous)
            | role_low (drop with no injury evidence: possible role cut, coaching decision, or an
              unreported injury - ambiguous by construction) | unknown (no snap share or reference)."""
    cutoff = key(season, week)
    p = panel.sort("gsis_id", "season", "week").with_columns(
        pl.col("offense_pct").shift(1).rolling_mean(REF_GAMES, min_samples=1).over("gsis_id").alias("ref_snap"))
    keys = ["season", "week", "gsis_id"]
    if sources.injuries is not None:
        inj = sources.injuries.filter((pl.col("season") * 100 + pl.col("week") <= cutoff)
                                      & ~pl.col("not_injury").fill_null(False)).select(keys).unique()
        same = inj.with_columns(pl.lit(True).alias("_listed_same"))
        after = (p.select(keys).join(inj.rename({"week": "_rw"}), on=["season", "gsis_id"])
                 .filter((pl.col("_rw") > pl.col("week")) & (pl.col("_rw") <= pl.col("week") + EXIT_REPORT_WEEKS))
                 .select(keys).unique().with_columns(pl.lit(True).alias("_listed_after")))
        p = p.join(same, on=keys, how="left").join(after, on=keys, how="left")
    else:
        p = p.with_columns(pl.lit(None, dtype=pl.Boolean).alias("_listed_same"),
                           pl.lit(None, dtype=pl.Boolean).alias("_listed_after"))
    if sources.exits is not None:
        ex = sources.exits.filter(pl.col("season") * 100 + pl.col("week") < cutoff).select(
            *keys, (pl.col("injured_in_game") == 1).alias("_pbp_injured")).unique(keys)
        p = p.join(ex, on=keys, how="left")
    else:
        p = p.with_columns(pl.lit(None, dtype=pl.Boolean).alias("_pbp_injured"))
    margin = sources.games.filter(pl.col("points_for").is_not_null()).select(
        "season", "week", "team", (pl.col("points_for") - pl.col("points_against")).alias("_margin")).unique(
        ["season", "week", "team"])
    p = p.join(margin, on=["season", "week", "team"], how="left")
    drop = (pl.col("ref_snap") - pl.col("offense_pct")) >= SNAP_DROP
    listed_after, listed_same = pl.col("_listed_after").fill_null(False), pl.col("_listed_same").fill_null(False)
    gclass = (pl.when(pl.col("offense_pct").is_null() | pl.col("ref_snap").is_null()).then(pl.lit("unknown"))
              .when(~drop).then(pl.lit("full"))
              .when(pl.col("_pbp_injured").fill_null(False) | (listed_after & ~listed_same)).then(pl.lit("injury_exit"))
              .when(pl.col("_margin").abs() >= BLOWOUT_MARGIN).then(pl.lit("blowout_low"))
              .when(listed_after).then(pl.lit("listed_low"))
              .otherwise(pl.lit("role_low")))
    return p.with_columns(gclass.alias("gclass")).with_columns((pl.col("gclass") != "injury_exit").alias("healthy")).drop(
        "_listed_same", "_listed_after", "_pbp_injured", "_margin")


def _usage_features(out: pl.DataFrame, cp: pl.DataFrame, season: int) -> pl.DataFrame:
    """Healthy-game usage, previous-season usage and the empirical-Bayes established workload.
    `cp` is classify_games output (pre-cutoff games only)."""
    ok = pl.col("xfpl").is_not_null()
    team_ok = pl.col("team_targets").is_not_null() & pl.col("team_rush_attempts").is_not_null()
    opp, team_opp = pl.col("targets") + pl.col("carries"), pl.col("team_targets") + pl.col("team_rush_attempts")
    desc = cp.sort(["season", "week"], descending=True)
    healthy = desc.filter(pl.col("healthy"))
    h = healthy.group_by("gsis_id", maintain_order=True).head(RECENT_GAMES).group_by("gsis_id").agg(
        pl.len().alias("h_games_recent"), pl.col("xfpl").filter(ok).sum().alias("_h_xl_sum"), ok.sum().alias("_h_xl_n"),
        pl.col("carries").mean().alias("h_carries_pg"), pl.col("targets").mean().alias("h_targets_pg"),
        pl.col("offense_pct").mean().alias("h_snap_pct"),
        opp.filter(team_ok).sum().alias("_h_opp"), team_opp.filter(team_ok).sum().alias("_h_team_opp"))
    last5 = desc.group_by("gsis_id", maintain_order=True).head(RECENT_GAMES).group_by("gsis_id", maintain_order=True).agg(
        (pl.col("gclass") == "injury_exit").sum().alias("inj_exits_recent"), pl.col("gclass").first().alias("_last_class"))
    prev = healthy.filter(pl.col("season") == season - 1).group_by("gsis_id").agg(
        pl.len().alias("prev_h_games"), pl.col("xfpl").filter(ok).sum().alias("_p_xl_sum"), ok.sum().alias("_p_xl_n"),
        pl.col("carries").mean().alias("prev_carries_pg"), pl.col("targets").mean().alias("prev_targets_pg"),
        pl.col("offense_pct").mean().alias("prev_snap_pct"))
    prev_team = desc.filter(pl.col("season") == season - 1).group_by("gsis_id", maintain_order=True).agg(
        pl.col("team").first().alias("_prev_team"))
    cur = healthy.filter((pl.col("season") == season) & ok).group_by("gsis_id").agg(
        pl.len().alias("cur_h_games"), pl.col("xfpl").sum().alias("_c_xl_sum"))
    out = (out.join(h, on="gsis_id", how="left").join(last5, on="gsis_id", how="left").join(prev, on="gsis_id", how="left")
           .join(prev_team, on="gsis_id", how="left").join(cur, on="gsis_id", how="left"))
    pn, cn = pl.col("_p_xl_n").fill_null(0), pl.col("cur_h_games").fill_null(0)
    hn = pl.col("_h_xl_n").fill_null(0)
    prior = pl.when(pn > 0).then((pl.col("_p_xl_sum") + pl.col("prior_xfpl") * SHRINK_GAMES) / (pn + SHRINK_GAMES))
    out = out.with_columns(
        pl.col("h_games_recent").fill_null(0), pl.col("inj_exits_recent").fill_null(0), cn.alias("cur_h_games"),
        pl.col("prev_h_games").fill_null(0),
        pl.when(hn > 0).then((pl.col("_h_xl_sum") + pl.col("prior_xfpl") * SHRINK_GAMES) / (hn + SHRINK_GAMES))
          .otherwise(None).alias("h_xfpl_pg"),
        pl.when(pl.col("_h_team_opp") > 0).then(pl.col("_h_opp") / pl.col("_h_team_opp")).otherwise(None).alias("h_opp_share"),
        prior.otherwise(None).alias("prev_xfpl_pg"),
        (pl.col("_prev_team").is_not_null() & (pl.col("_prev_team") != pl.col("team"))).cast(pl.Int8).alias("prev_team_changed"),
        (pl.col("_last_class") == "injury_exit").cast(pl.Int8).alias("last_injury_exit"),
        (pl.col("_last_class") == "role_low").cast(pl.Int8).alias("last_role_low"),
        (pl.col("_last_class") == "blowout_low").cast(pl.Int8).alias("last_blowout_low"),
        pl.col("_c_xl_sum").fill_null(0).alias("_c_xl_sum"),
    )
    eb_prior = pl.coalesce(pl.col("prev_xfpl_pg"), pl.col("prior_xfpl"))
    return out.with_columns(
        pl.when((pl.col("cur_h_games") > 0) | pl.col("prev_xfpl_pg").is_not_null())
          .then((pl.col("_c_xl_sum") + eb_prior * USAGE_PRIOR_GAMES) / (pl.col("cur_h_games") + USAGE_PRIOR_GAMES))
          .otherwise(None).alias("xfpl_eb"),
        # raw EB ingredients, kept so research can evaluate other prior strengths without refitting features
        pl.when(pl.col("cur_h_games") > 0).then(pl.col("_c_xl_sum") / pl.col("cur_h_games")).otherwise(None)
          .alias("cur_h_xfpl_pg"),
    )


def _injury_exits(out: pl.DataFrame, injuries: pl.DataFrame | None, season: int, week: int) -> pl.DataFrame:
    """Split the last-game snap change into an injury exit and a role change. A drop counts as an
    injury exit when the player appears on an injury report (any listing except 'not injury
    related') in the EXIT_REPORT_WEEKS weeks after that game, using only reports up to and
    including the target week's pregame report."""
    out = out.with_columns(pl.col("_snap_last").alias("snap_last"))
    if injuries is None:
        return out.with_columns(pl.lit(None, dtype=pl.Int8).alias("injury_exit_last"),
                                pl.col("snap_trend").alias("snap_trend_role"))
    listed = injuries.filter((pl.col("season") * 100 + pl.col("week") <= key(season, week))
                             & ~pl.col("not_injury").fill_null(False)).select("gsis_id", "season", "week")
    hit = (out.select("gsis_id", "last_season", "last_week").drop_nulls("last_season")
           .join(listed, on="gsis_id", how="inner")
           .filter((pl.col("season") == pl.col("last_season")) & (pl.col("week") > pl.col("last_week"))
                   & (pl.col("week") <= pl.col("last_week") + EXIT_REPORT_WEEKS))
           .select("gsis_id").unique().with_columns(pl.lit(True).alias("_listed_after")))
    out = out.join(hit, on="gsis_id", how="left").with_columns(
        pl.when(pl.col("snap_trend").is_null()).then(None)
          .when((pl.col("snap_trend") <= -SNAP_DROP) & pl.col("_listed_after").fill_null(False)).then(1)
          .otherwise(0).cast(pl.Int8).alias("injury_exit_last"))
    return out.with_columns(pl.when(pl.col("injury_exit_last") == 1).then(None).otherwise(pl.col("snap_trend"))
                            .alias("snap_trend_role"))


def played_targets(sources: Sources, season: int, week: int) -> pl.DataFrame:
    """Backtest population: players who appeared in (season, week). Their team and opponent are
    pregame facts (roster + schedule); the appearance itself is the conditioning event, so these
    are forecasts conditional on playing (availability is evaluated separately)."""
    rows = sources.panel.filter((pl.col("season") == season) & (pl.col("week") == week))
    games = sources.games.filter((pl.col("season") == season) & (pl.col("week") == week)).select(
        "team", "opponent", "is_home")
    return (rows.select("gsis_id", "position", "team", "opponent", "league_points")
            .join(games, on=["team", "opponent"], how="inner"))

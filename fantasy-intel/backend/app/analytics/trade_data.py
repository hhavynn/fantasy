"""Database side of the V2 lineup/trade engine: evaluation window, league state, explanatory
context, and the derived team lineup projection tables (schema v6). Reads raw ESPN/nflverse
tables; writes only team_week_lineup_projections and team_ros_lineup_projections."""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, replace_where, table_columns, table_exists, utcnow
from ..espn.constants import DEFAULT_POSITION_MAP, FANTASY_POSITIONS, IR_SLOT
from .lineup import lineup_slots
from .projections import league_window
from .team_lineups import FA_STATUSES, RosterPlayer, project_team, public_weeks
from .trades import LeagueState


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


# ------------------------------------------------------------------ evaluation window --

def evaluation_start_week(con, season: int, current: int, final: int) -> dict:
    """First fantasy week a trade made now can still change. ESPN's current_week can point at a
    scoring period whose games are all over (a trade after Monday night must not get credit for
    that week), so: if every REG game of current_week has a final score in nfl_schedules, start at
    current_week + 1; otherwise (any game unplayed, or completion unknowable) start at current_week.
    Completion is taken only from game scores, never from player stats existing (Thursday/Sunday
    partial weeks must not skip the week). Clamped to final_scoring_period."""
    out = {"espn_current_week": current, "final_scoring_period": final,
           "rule": "current_week + 1 only if every REG game of current_week has a final score; else current_week"}
    if not table_exists(con, "nfl_schedules"):
        return {**out, "evaluation_start_week": min(current, final), "current_week_completed": None,
                "reason": "nfl_schedules unavailable: completion cannot be established; using ESPN current_week"}
    cols = set(table_columns(con, "nfl_schedules"))
    if not {"home_score", "away_score"} <= cols:
        return {**out, "evaluation_start_week": min(current, final), "current_week_completed": None,
                "reason": "nfl_schedules has no score columns: completion cannot be established; using ESPN current_week"}
    games = fetch_dicts(con, """SELECT home_score, away_score FROM nfl_schedules
                                WHERE season = ? AND game_type = 'REG' AND week = ?""", [season, current])
    if not games:
        return {**out, "evaluation_start_week": min(current, final), "current_week_completed": None,
                "current_week_games": 0,
                "reason": f"no REG games for week {current} in nfl_schedules: completion unknown; using ESPN current_week"}
    done = sum(1 for g in games if g["home_score"] is not None and g["away_score"] is not None)
    out.update(current_week_games=len(games), current_week_games_final=done)
    if done < len(games):
        return {**out, "evaluation_start_week": min(current, final), "current_week_completed": False,
                "reason": f"{done} of {len(games)} REG games in week {current} have final scores; week {current} "
                          "can still change, so it is included"}
    start = min(current + 1, final)
    reason = f"all {len(games)} REG games in week {current} have final scores; evaluation starts at week {start}"
    if current >= final:
        reason = f"all games of the final scoring period ({final}) are final; clamped to week {final}"
    return {**out, "evaluation_start_week": start, "current_week_completed": True, "reason": reason}


# ------------------------------------------------------------------------ league state --

def _position_limits(settings: dict) -> dict[str, int]:
    """ESPN rosterSettings.positionLimits (keyed by defaultPositionId); values <= 0 mean no limit."""
    raw = ((settings or {}).get("rosterSettings") or {}).get("positionLimits") or {}
    out = {}
    for k, v in raw.items():
        pos = DEFAULT_POSITION_MAP.get(int(k))
        if pos in FANTASY_POSITIONS and v is not None and int(v) > 0:
            out[pos] = int(v)
    return out


def load_league_state(con, season: int | None = None) -> LeagueState | None:
    lg = fetch_dicts(con, "SELECT * FROM leagues" + (" WHERE season = ?" if season else "") +
                     " ORDER BY synced_at DESC LIMIT 1", [season] if season else [])
    if not lg:
        return None
    lg = lg[0]
    season = lg["season"]
    window = league_window(con, season)
    if window is None:
        return None
    roster_slots = _json(lg["roster_slots"]) or {}
    settings = _json(lg["settings_raw"]) or {}
    start = evaluation_start_week(con, season, window.current, window.final)
    rosters: dict[int, list[RosterPlayer]] = {}
    for row in fetch_dicts(con, """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, fp.full_name, fp.position, fp.pro_team,
               coalesce(fp.injury_status, l.injury_status) AS injury_status
        FROM lineup_snapshots l JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ?
        ORDER BY l.team_id, l.espn_player_id""", [lg["league_id"], season, window.current]):
        rosters.setdefault(row["team_id"], []).append(RosterPlayer(
            row["espn_player_id"], row["position"], row["pro_team"], row["full_name"],
            row["lineup_slot_id"] == IR_SLOT, row["injury_status"]))
    teams = fetch_dicts(con, "SELECT team_id, name FROM fantasy_teams WHERE league_id = ? AND season = ?",
                        [lg["league_id"], season])
    for t in teams:
        rosters.setdefault(t["team_id"], [])
    rostered = {p.espn_player_id for ps in rosters.values() for p in ps}
    free_agents = [RosterPlayer(f["espn_player_id"], f["position"], f["pro_team"], f["full_name"], False, f["injury_status"])
                   for f in fetch_dicts(con, f"""
                       SELECT espn_player_id, full_name, position, pro_team, injury_status FROM fantasy_players
                       WHERE availability_status IN {FA_STATUSES} ORDER BY espn_player_id""")
                   if f["espn_player_id"] not in rostered]
    proj: dict[int, dict[int, float]] = {}
    for p in fetch_dicts(con, """SELECT espn_player_id, scoring_period, projected_points FROM espn_player_weekly_points
                                 WHERE season = ? AND scoring_period BETWEEN ? AND ? AND projected_points IS NOT NULL""",
                         [season, window.current, window.final]):
        proj.setdefault(p["espn_player_id"], {})[p["scoring_period"]] = float(p["projected_points"])
    capacity = sum(int(v) for k, v in roster_slots.items() if k != "IR")
    return LeagueState(
        season=season, slots=lineup_slots(roster_slots), final_week=window.final, last_regular=window.last_regular,
        start_week=start["evaluation_start_week"], rosters=rosters, free_agents=free_agents, projections=proj,
        active_capacity=capacity, position_limits=_position_limits(settings),
        team_names={t["team_id"]: t["name"] for t in teams}, league_id=lg["league_id"], start_week_reason=start,
        undroppable_list_in_use=((settings.get("rosterSettings") or {}).get("isUsingUndroppableList")),
        first_projection_week=window.current)


# ---------------------------------------------------------------------- context (display) --

def player_intelligence(con, season: int, ids: list[int]) -> dict[int, dict]:
    """Existing intelligence for display only. HOLD is not untouchable, SELL is not must-trade,
    BUY is not accept-any-price; none of it enters the lineup math."""
    if not ids:
        return {}
    ph = ", ".join("?" * len(ids))
    out = {r["espn_player_id"]: r for r in fetch_dicts(con, f"""
        SELECT fp.espn_player_id, fp.full_name, fp.position, fp.pro_team, fp.injury_status,
               ros.ros_projected_points, ros.regular_season_projected_points, ros.playoff_projected_points,
               ros.ros_weeks_expected, ros.ros_weeks_available, ros.missing_weeks, ros.bye_weeks,
               s.opportunity_tier, s.role_trend, s.market_signal,
               s.components->>'production_sustainability' AS production_sustainability,
               pc.labels AS context_labels
        FROM fantasy_players fp
        LEFT JOIN player_ros_projections ros ON ros.espn_player_id = fp.espn_player_id AND ros.season = ?
        LEFT JOIN player_id_map m ON m.espn_player_id = fp.espn_player_id
        LEFT JOIN player_scores s ON s.gsis_id = m.gsis_id AND s.season = ?
        LEFT JOIN player_context pc ON pc.gsis_id = m.gsis_id AND pc.season = ?
        WHERE fp.espn_player_id IN ({ph})""", [season, season, season, *ids])}
    for r in out.values():
        for k in ("missing_weeks", "bye_weeks", "context_labels"):
            r[k] = _json(r[k])
        r["note"] = "Context only: HOLD is not untouchable, SELL is not must-trade, BUY is not accept-any-price."
    return out


def manager_context(con, league_id: int, season: int, team_ids: list[int]) -> dict[int, dict]:
    """Secondary context: existing team_position_needs and manager classifications."""
    out = {t: {"needs": [], "classifications": {}} for t in team_ids}
    if not team_ids:
        return out
    ph = ", ".join("?" * len(team_ids))
    for n in fetch_dicts(con, f"""SELECT team_id, position, need_score, starters_required, rostered, starter_value
                                  FROM team_position_needs WHERE league_id = ? AND season = ? AND team_id IN ({ph})
                                  ORDER BY team_id, need_score DESC""", [league_id, season, *team_ids]):
        out[n["team_id"]]["needs"].append({k: v for k, v in n.items() if k != "team_id"})
    for c in fetch_dicts(con, f"""SELECT team_id, dimension, label, confidence FROM manager_classifications
                                  WHERE league_id = ? AND season = ? AND team_id IN ({ph})""",
                         [league_id, season, *team_ids]):
        out[c["team_id"]]["classifications"][c["dimension"]] = {"label": c["label"], "confidence": c["confidence"]}
    return out


def player_names(con, ids) -> dict[int, dict]:
    ids = sorted({int(i) for i in ids if i is not None})
    if not ids:
        return {}
    ph = ", ".join("?" * len(ids))
    return {r["espn_player_id"]: r for r in fetch_dicts(
        con, f"SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players WHERE espn_player_id IN ({ph})",
        ids)}


def named_weeks(weeks: list[dict], names: dict[int, dict]) -> list[dict]:
    """Attach player names to assignment/bench rows (explainability)."""
    out = []
    for w in weeks:
        w = dict(w)
        w["assignment"] = [{**a, "name": (names.get(a["player_id"]) or {}).get("full_name")} for a in w["assignment"]]
        w["bench"] = [{**b, "name": (names.get(b["player_id"]) or {}).get("full_name")} for b in w["bench"]]
        out.append(w)
    return out


# ------------------------------------------------------------------- derived tables (v6) --

def build_team_lineup_projections(con, season: int) -> dict:
    """Rebuild team_week_lineup_projections + team_ros_lineup_projections: every team's optimal
    projected lineup for every remaining week (from the evaluation start week) on ESPN's baseline."""
    if not table_exists(con, "team_week_lineup_projections"):
        return {"skipped": "schema older than v6; run a sync to migrate"}
    state = load_league_state(con, season)
    if state is None:
        return {"skipped": "league current/final week unknown"}
    window = state.window()
    now = utcnow()
    names = player_names(con, [p.espn_player_id for ps in state.rosters.values() for p in ps])
    week_rows, team_rows = [], []
    for tid in sorted(state.rosters):
        team = project_team(state.rosters[tid], state.slots, window, state.projections)
        for w in named_weeks(public_weeks(team), names):
            week_rows.append({"league_id": state.league_id, "season": season, "team_id": tid, "week": w["week"],
                              "phase": w["phase"], "known_lineup_points": w["known_lineup_points"],
                              "projected_lineup_points": w["projected_lineup_points"],
                              "projection_complete": w["projection_complete"], "assignment": w["assignment"],
                              "bench": w["bench"], "missing_projection_ids": w["missing_projection_ids"],
                              "teamless_unprojected_ids": w["teamless_unprojected_ids"], "computed_at": now})
        s = team["summary"]
        team_rows.append({"league_id": state.league_id, "season": season, "team_id": tid,
                          "evaluation_start_week": s["evaluation_start_week"], "final_week": s["final_week"],
                          "last_regular_season_week": s["last_regular_season_week"],
                          "ros_projected_starting_points": s["ros_projected_starting_points"],
                          "ros_known_starting_points": s["ros_known_starting_points"],
                          "regular_season_projected_starting_points": s["regular_season_projected_starting_points"],
                          "playoff_projected_starting_points": s["playoff_projected_starting_points"],
                          "ros_weeks_expected": s["ros_weeks_expected"], "ros_weeks_complete": s["ros_weeks_complete"],
                          "missing_weeks": s["missing_weeks"], "missing_players": s["missing_players"],
                          "start_week_reason": state.start_week_reason.get("reason"), "computed_at": now})
    replace_where(con, "team_week_lineup_projections", week_rows, "league_id = ? AND season = ?",
                  [state.league_id, season])
    replace_where(con, "team_ros_lineup_projections", team_rows, "league_id = ? AND season = ?",
                  [state.league_id, season])
    return {"teams": len(team_rows), "weeks": [window.start, window.final],
            "evaluation_start_week": window.start, "start_week_reason": state.start_week_reason.get("reason"),
            "teams_complete": sum(1 for t in team_rows if t["ros_projected_starting_points"] is not None),
            "incomplete_team_weeks": sum(1 for w in week_rows if not w["projection_complete"])}

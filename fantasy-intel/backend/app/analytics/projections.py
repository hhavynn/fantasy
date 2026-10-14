"""ESPN forward projection baseline: per-week forward view + rest-of-season summary.

ESPN's weekly projections are the forward fantasy-point baseline. They are ESPN's numbers, not
our model, and nothing here adjusts them (no matchup, trend or schedule multipliers). Role,
sustainability, schedule and league context are evaluated separately. None of this feeds the
existing scores, market signals or waiver recommendations.

Rules (docs/espn_ros_projections.md):
  * Weeks covered: league current_week .. final_scoring_period (league settings, never NFL week 18).
  * Regular season / playoffs split at the last scoring period of matchup `reg_season_matchups`.
  * A week is "available" when ESPN supplied a projection, including a projected 0.
  * A total is NULL unless every expected week in its window is available (full coverage);
    a partial sum would look like an authoritative low projection.
  * Byes come from nfl_schedules (team has no REG game that week), never from a 0 projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..db.connection import fetch_dicts, replace_where, table_exists, utcnow

PROJECTION_SOURCE = "ESPN"
BASELINE_NOTE = ("ESPN projection is the forward fantasy-point baseline. Role, sustainability, schedule "
                 "and league context are evaluated separately.")


@dataclass(frozen=True)
class LeagueWindow:
    season: int
    current: int
    final: int
    last_regular: int | None    # last regular-season scoring period; None = can't be derived

    @property
    def weeks(self) -> list[int]:
        return list(range(self.current, self.final + 1))


def last_regular_season_week(reg_season_matchups: int | None, final: int | None,
                             schedule_settings: dict | None) -> int | None:
    """Last scoring period of the regular season. `reg_season_matchups` counts matchup periods;
    ESPN's scheduleSettings.matchupPeriods maps each to its scoring periods (multi-week matchups
    exist), so use the map when present. Without it, matchup period = scoring period only when
    matchupPeriodLength is 1 (or unreported); otherwise unknown."""
    if not reg_season_matchups:
        return None
    sched = schedule_settings or {}
    periods = (sched.get("matchupPeriods") or {}).get(str(reg_season_matchups))
    if periods:
        last = max(int(p) for p in periods)
    elif (sched.get("matchupPeriodLength") or 1) == 1:
        last = int(reg_season_matchups)
    else:
        return None
    return min(last, final) if final else last


def league_window(con, season: int) -> LeagueWindow | None:
    rows = fetch_dicts(con, """SELECT current_week, final_scoring_period, reg_season_matchups, settings_raw
                               FROM leagues WHERE season = ? ORDER BY synced_at DESC LIMIT 1""", [season])
    if not rows or not rows[0]["current_week"] or not rows[0]["final_scoring_period"]:
        return None
    r = rows[0]
    settings = r["settings_raw"]
    if isinstance(settings, str):
        settings = json.loads(settings)
    current, final = int(r["current_week"]), int(r["final_scoring_period"])
    return LeagueWindow(season, min(current, final), final,
                        last_regular_season_week(r["reg_season_matchups"], final,
                                                 (settings or {}).get("scheduleSettings")))


def load_schedule(con, season: int, weeks: list[int]) -> tuple[dict[tuple[str, int], tuple[str, bool]], set[int]]:
    """(team, week) -> (opponent, is_home) for REG games, plus the weeks the schedule covers.
    A team absent from a covered week is on bye; an uncovered week is unknown."""
    games: dict[tuple[str, int], tuple[str, bool]] = {}
    covered: set[int] = set()
    if not weeks or not table_exists(con, "nfl_schedules"):
        return games, covered
    for g in fetch_dicts(con, """SELECT week, home_team, away_team FROM nfl_schedules
                                 WHERE season = ? AND game_type = 'REG' AND week BETWEEN ? AND ?""",
                         [season, min(weeks), max(weeks)]):
        covered.add(g["week"])
        games[(g["home_team"], g["week"])] = (g["away_team"], True)
        games[(g["away_team"], g["week"])] = (g["home_team"], False)
    return games, covered


def forward_weeks(window: LeagueWindow, team: str | None, projections: dict[int, float | None],
                  games: dict, covered: set[int]) -> list[dict]:
    """One row per remaining fantasy week. `projections` maps week -> ESPN projection; a week
    absent from it (or None) has no projection. Values are ESPN's, unmodified."""
    out = []
    for w in window.weeks:
        game = games.get((team, w)) if team else None
        proj = projections.get(w)
        out.append({
            "week": w,
            "phase": None if window.last_regular is None else ("regular" if w <= window.last_regular else "playoff"),
            "opponent": game[0] if game else None,
            "is_home": game[1] if game else None,
            "on_bye": None if not team or w not in covered else game is None,   # None = unknown
            "espn_projected_points": proj,
            "projection_available": proj is not None,
        })
    return out


def _part(weeks: list[dict]) -> tuple[float | None, int, int]:
    avail = [w["espn_projected_points"] for w in weeks if w["projection_available"]]
    total = round(sum(avail), 2) if len(avail) == len(weeks) else None
    return total, len(weeks), len(avail)


def summarize(window: LeagueWindow, weeks: list[dict]) -> dict:
    ros, ros_exp, ros_av = _part(weeks)
    if window.last_regular is None:
        reg = po = (None, None, None)
    else:
        reg = _part([w for w in weeks if w["week"] <= window.last_regular])
        po = _part([w for w in weeks if w["week"] > window.last_regular])
    byes = [w["on_bye"] for w in weeks]
    current = next((w for w in weeks if w["week"] == window.current), None)
    return {
        "as_of_week": window.current, "final_week": window.final, "last_regular_season_week": window.last_regular,
        "current_week_projection": current["espn_projected_points"] if current else None,
        "ros_projected_points": ros, "ros_weeks_expected": ros_exp, "ros_weeks_available": ros_av,
        "ros_coverage": round(ros_av / ros_exp, 4) if ros_exp else None,
        "regular_season_projected_points": reg[0], "regular_season_weeks_expected": reg[1],
        "regular_season_weeks_available": reg[2],
        "playoff_projected_points": po[0], "playoff_weeks_expected": po[1], "playoff_weeks_available": po[2],
        "bye_weeks_remaining": None if any(b is None for b in byes) else sum(1 for b in byes if b),
        "bye_weeks": [w["week"] for w in weeks if w["on_bye"]],
        "missing_weeks": [w["week"] for w in weeks if not w["projection_available"]],
        "projection_source": PROJECTION_SOURCE,
    }


def _projections(con, window: LeagueWindow, pid: int | None = None) -> dict[int, dict]:
    sql = """SELECT espn_player_id, scoring_period, projected_points, synced_at FROM espn_player_weekly_points
             WHERE season = ? AND scoring_period BETWEEN ? AND ? AND projected_points IS NOT NULL"""
    params: list = [window.season, window.current, window.final]
    if pid is not None:
        sql += " AND espn_player_id = ?"
        params.append(pid)
    out: dict[int, dict] = {}
    for r in fetch_dicts(con, sql, params):
        d = out.setdefault(r["espn_player_id"], {"weeks": {}, "synced_at": None})
        d["weeks"][r["scoring_period"]] = r["projected_points"]
        if r["synced_at"] and (d["synced_at"] is None or r["synced_at"] > d["synced_at"]):
            d["synced_at"] = r["synced_at"]
    return out


def build_ros_projections(con, season: int) -> dict:
    """Rebuild player_ros_projections for the season: every rostered player plus every player
    ESPN projects in at least one remaining week."""
    if not table_exists(con, "player_ros_projections"):
        return {"skipped": "schema older than v5; run a sync to migrate"}
    window = league_window(con, season)
    if window is None:
        return {"skipped": "league current/final week unknown"}
    proj = _projections(con, window)
    teams = {r["espn_player_id"]: (r["pro_team"], r["availability_status"]) for r in fetch_dicts(
        con, "SELECT espn_player_id, pro_team, availability_status FROM fantasy_players")}
    ids = set(proj) | {pid for pid, (_, status) in teams.items() if status == "ONTEAM"}
    games, covered = load_schedule(con, season, window.weeks)
    now = utcnow()
    rows = []
    for pid in sorted(ids):
        p = proj.get(pid, {"weeks": {}, "synced_at": None})
        team = teams.get(pid, (None, None))[0]
        s = summarize(window, forward_weeks(window, team, p["weeks"], games, covered))
        rows.append({"season": season, "espn_player_id": pid, **s,
                     "projection_synced_at": p["synced_at"], "computed_at": now})
    replace_where(con, "player_ros_projections", rows, "season = ?", [season])
    rostered = {pid for pid, (_, status) in teams.items() if status == "ONTEAM"}
    return {
        "players": len(rows), "weeks": [window.current, window.final], "last_regular_season_week": window.last_regular,
        "full_coverage": sum(1 for r in rows if r["ros_projected_points"] is not None),
        "incomplete": sum(1 for r in rows if r["ros_projected_points"] is None),
        "rostered_incomplete": sum(1 for r in rows if r["ros_projected_points"] is None
                                   and r["espn_player_id"] in rostered),
        "schedule_weeks_covered": len(covered & set(window.weeks)),
    }


def player_forward(con, season: int, pid: int, team: str | None) -> dict | None:
    """The forward view for one player, computed with the same functions as the stored table."""
    window = league_window(con, season)
    if window is None:
        return None
    p = _projections(con, window, pid).get(pid, {"weeks": {}, "synced_at": None})
    games, covered = load_schedule(con, season, window.weeks)
    weeks = forward_weeks(window, team, p["weeks"], games, covered)
    return {"source": PROJECTION_SOURCE, "note": BASELINE_NOTE, "summary": summarize(window, weeks),
            "projection_synced_at": p["synced_at"], "weeks": weeks,
            "schedule_available": bool(covered)}

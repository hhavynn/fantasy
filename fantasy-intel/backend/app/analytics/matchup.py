"""Weekly head-to-head matchup: one team against its opponent for one fantasy week.

Read-only shaping of rows that already exist; nothing here computes a new analytic.

  * Pairing and scores come from `matchups`; names, records and standing from `fantasy_teams`.
  * Projected lineups come from `team_week_lineup_projections` (ESPN weekly projections run through
    the optimal-lineup solver, see analytics/team_lineups.py). That table only holds the remaining
    weeks, so past weeks have no projection. A team-week whose projection is incomplete keeps
    `projected_points` NULL and exposes only `known_points`; no NULL is ever filled with a zero.
  * The lineup a manager actually set comes from `lineup_snapshots` (`actual_lineup`/`actual_bench`),
    when a snapshot exists for that week. Its `points` are NULL until the matchup has started: a
    pre-game 0.0 is "not played yet", not a measured zero.

Matchup period and scoring period are treated as the same number (as the rest of the app does).

Status rule (`status`), from stored data only:
  * "final"       ESPN marked a winner (HOME, AWAY or TIE).
  * "in_progress" no winner yet but at least one side has scored a point (scores are as of the last sync).
  * "upcoming"    nothing has been scored and the week is the current week or later.
  * "unknown"     no matchup row for the team that week, or an undecided past week with no scores.
"""

from __future__ import annotations

import json
from typing import Any

from ..db.connection import fetch_dicts, table_exists

DECIDED = ("HOME", "AWAY", "TIE")
HEALTHY = (None, "", "ACTIVE", "NORMAL")


class LeagueNotSynced(RuntimeError):
    pass


class TeamNotFound(LookupError):
    pass


class WeekOutOfRange(ValueError):
    pass


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2) + 0.0


def _json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def matchup_status(row: dict | None, week: int, current_week: int) -> str:
    """See the module docstring."""
    if row is None:
        return "unknown"
    if row.get("winner") in DECIDED:
        return "final"
    scored = (row.get("home_points") or 0) > 0 or (row.get("away_points") or 0) > 0
    if scored:
        return "in_progress"
    return "upcoming" if week >= current_week else "unknown"


def _players(con, ids: set[int]) -> dict[int, dict]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["espn_player_id"]: r for r in fetch_dicts(
        con, f"SELECT espn_player_id, full_name, position, pro_team, injury_status "
             f"FROM fantasy_players WHERE espn_player_id IN ({marks})", sorted(ids))}


def _teams_playing(con, season: int, week: int) -> set[str]:
    if not table_exists(con, "nfl_schedules"):
        return set()
    return {r["t"] for r in fetch_dicts(con, """
        SELECT home_team t FROM nfl_schedules WHERE season = ? AND week = ? AND game_type = 'REG'
        UNION SELECT away_team FROM nfl_schedules WHERE season = ? AND week = ? AND game_type = 'REG'""",
                                        [season, week, season, week])}


def _on_bye(pro_team: str | None, playing: set[str]) -> bool | None:
    """None = schedule unknown; otherwise whether his NFL team has no game this week."""
    if not playing or not pro_team:
        return None
    return pro_team not in playing


def _projected_side(con, key: tuple, players_by_id: dict, playing: set[str]) -> dict:
    rows = fetch_dicts(con, """
        SELECT phase, known_lineup_points, projected_lineup_points, projection_complete, assignment, bench,
               missing_projection_ids FROM team_week_lineup_projections
        WHERE league_id = ? AND season = ? AND team_id = ? AND week = ?""", list(key)) \
        if table_exists(con, "team_week_lineup_projections") else []
    if not rows:
        return {"phase": None, "known_points": None, "projected_points": None, "projection_complete": False,
                "lineup": [], "bench": [], "missing_ids": []}
    r = rows[0]
    lineup = []
    for a in _json(r["assignment"], []):
        pid = a.get("player_id")
        p = players_by_id.get(pid, {})
        lineup.append({"slot": a.get("slot"), "player_id": pid, "name": a.get("name") or p.get("full_name"),
                       "position": a.get("position") or p.get("position"), "pro_team": p.get("pro_team"),
                       "points": _r(a.get("points")), "is_flex": bool(a.get("is_flex")),
                       "injury_status": p.get("injury_status"), "on_bye": _on_bye(p.get("pro_team"), playing)})
    bench = []
    for b in _json(r["bench"], []):
        pid = b.get("player_id")
        p = players_by_id.get(pid, {})
        bench.append({"player_id": pid, "name": b.get("name") or p.get("full_name"),
                      "position": b.get("position") or p.get("position"), "pro_team": p.get("pro_team"),
                      "points": _r(b.get("points")), "injury_status": p.get("injury_status"),
                      "on_bye": _on_bye(p.get("pro_team"), playing)})
    return {"phase": r["phase"], "known_points": r["known_lineup_points"],
            "projected_points": r["projected_lineup_points"] if r["projection_complete"] else None,
            "projection_complete": bool(r["projection_complete"]), "lineup": lineup, "bench": bench,
            "missing_ids": _json(r["missing_projection_ids"], [])}


def _actual_side(con, key: tuple, players_by_id: dict, playing: set[str], started: bool) -> tuple[list | None, list | None]:
    rows = fetch_dicts(con, """
        SELECT espn_player_id, lineup_slot, lineup_slot_id, is_starter, injury_status, points, projected_points
        FROM lineup_snapshots WHERE league_id = ? AND season = ? AND team_id = ? AND scoring_period = ?
        ORDER BY is_starter DESC, lineup_slot_id, espn_player_id""", list(key))
    if not rows:
        return None, None
    starters, bench = [], []
    for r in rows:
        p = players_by_id.get(r["espn_player_id"], {})
        item = {"slot": r["lineup_slot"], "player_id": r["espn_player_id"], "name": p.get("full_name"),
                "position": p.get("position"), "pro_team": p.get("pro_team"),
                "projected_points": _r(r["projected_points"]), "points": _r(r["points"]) if started else None,
                "injury_status": r["injury_status"], "on_bye": _on_bye(p.get("pro_team"), playing)}
        (starters if r["is_starter"] else bench).append(item)
    return starters, bench


def load_matchup(con, team_id: int | None = None, week: int | None = None) -> dict:
    lgs = fetch_dicts(con, "SELECT league_id, season, name, current_week, final_scoring_period, reg_season_matchups "
                           "FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lgs:
        raise LeagueNotSynced("ESPN league not synced yet. Run scripts/sync_espn.py.")
    lg = lgs[0]
    league_id, season = lg["league_id"], lg["season"]
    current = int(lg["current_week"] or 1)

    teams = fetch_dicts(con, """SELECT team_id, name, abbrev, wins, losses, ties, is_mine FROM fantasy_teams
                                WHERE league_id = ? AND season = ?
                                ORDER BY wins DESC, ties DESC, points_for DESC""", [league_id, season])
    standing = {t["team_id"]: i + 1 for i, t in enumerate(teams)}
    by_id = {t["team_id"]: t for t in teams}
    if team_id is None:
        mine = next((t for t in teams if t["is_mine"]), None)
        if mine is None:
            raise TeamNotFound("Your team was not identified. Set ESPN_MY_TEAM_ID or pass team_id.")
        team_id = mine["team_id"]
    elif team_id not in by_id:
        raise TeamNotFound(f"Team {team_id} not found in this league.")

    mine_rows = fetch_dicts(con, """
        SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points, away_points, winner, playoff_tier
        FROM matchups WHERE league_id = ? AND season = ? AND matchup_period IS NOT NULL
          AND (home_team_id = ? OR away_team_id = ?) ORDER BY matchup_period, matchup_id""",
                            [league_id, season, team_id, team_id])
    weeks_available = sorted({int(m["matchup_period"]) for m in mine_rows})
    last_week = max([int(lg["final_scoring_period"] or 0), *weeks_available])

    if week is None:
        week = current
    elif week < 1 or week > last_week:
        raise WeekOutOfRange(f"Week {week} is outside the fantasy season (1-{last_week}).")

    row = next((m for m in mine_rows if int(m["matchup_period"]) == week), None)
    status = matchup_status(row, week, current)
    started = status in ("in_progress", "final")
    notes: list[str] = []

    opp_id = None
    my_points = opp_points = None
    if row:
        is_home = row["home_team_id"] == team_id
        opp_id = row["away_team_id"] if is_home else row["home_team_id"]
        mine_pts, theirs_pts = ((row["home_points"], row["away_points"]) if is_home
                                else (row["away_points"], row["home_points"]))
        if started:
            my_points, opp_points = mine_pts, theirs_pts
        if opp_id is None or opp_id not in by_id:
            opp_id = None

    league_has_week = bool(fetch_dicts(con, """SELECT 1 FROM matchups WHERE league_id = ? AND season = ?
                                               AND matchup_period = ? LIMIT 1""", [league_id, season, week]))
    is_bye = row is None and league_has_week
    if opp_id is None:
        if is_bye:
            notes.append(f"{by_id[team_id]['name']} has a bye in week {week}: no opponent is scheduled.")
        else:
            notes.append(f"No opponent is scheduled for week {week} in the synced ESPN schedule "
                         "(playoff pairings appear after the regular season).")
    elif status == "in_progress":
        notes.append("Actual points are as of the last ESPN sync and update as games finish.")

    playing = _teams_playing(con, season, week)
    snapshot_ids = {r["espn_player_id"] for r in fetch_dicts(
        con, "SELECT espn_player_id FROM lineup_snapshots WHERE league_id = ? AND season = ? AND scoring_period = ? "
             "AND team_id IN (?, ?)", [league_id, season, week, team_id, opp_id if opp_id is not None else team_id])}
    projected_ids: set[int] = set()
    projection_rows = fetch_dicts(con, """
        SELECT assignment, bench FROM team_week_lineup_projections WHERE league_id = ? AND season = ? AND week = ?
          AND team_id IN (?, ?)""", [league_id, season, week, team_id, opp_id if opp_id is not None else team_id]) \
        if table_exists(con, "team_week_lineup_projections") else []
    for pr in projection_rows:
        projected_ids |= {x.get("player_id") for x in _json(pr["assignment"], []) + _json(pr["bench"], [])}
    players_by_id = _players(con, snapshot_ids | projected_ids)

    unprojected: list[str] = []

    def side(tid: int) -> tuple[dict, str | None]:
        t = by_id[tid]
        key = (league_id, season, tid, week)
        proj = _projected_side(con, key, players_by_id, playing)
        if not proj["lineup"] and not proj["bench"] and proj["known_points"] is None:
            unprojected.append(t["name"])
        elif not proj["projection_complete"]:
            missing = [players_by_id.get(i, {}).get("full_name") or f"player {i}" for i in proj["missing_ids"]]
            notes.append(f"{t['name']}: projection incomplete"
                         + (f" ({len(missing)} rostered without an ESPN projection: {', '.join(missing)})"
                            if missing else "")
                         + f". Only the known lineup total ({proj['known_points']}) is shown; "
                           "it is not a full-lineup projection.")
        actual, actual_bench = _actual_side(con, key, players_by_id, playing, started)
        return {"team_id": tid, "name": t["name"], "abbrev": t["abbrev"],
                "record": {"wins": t["wins"], "losses": t["losses"], "ties": t["ties"]},
                "standing": standing.get(tid),
                "actual_points": my_points if tid == team_id else opp_points,
                "projected_points": proj["projected_points"], "known_points": proj["known_points"],
                "projection_complete": proj["projection_complete"],
                "lineup": proj["lineup"], "bench": proj["bench"],
                "actual_lineup": actual, "actual_bench": actual_bench}, proj["phase"]

    me, phase = side(team_id)
    opponent = None
    if opp_id is not None:
        opponent, opp_phase = side(opp_id)
        phase = phase or opp_phase
    if unprojected:
        who = "either team" if len(unprojected) == 2 else unprojected[0]
        notes.append(f"No stored projection for {who} in week {week}"
                     + (" (projections are kept only for weeks that have not been played)." if week < current
                        else "."))
    if phase is None:
        tier = row and row.get("playoff_tier")
        reg = lg["reg_season_matchups"]
        if tier not in (None, "NONE"):
            phase = "playoff"
        elif reg:
            phase = "regular" if week <= reg else "playoff"

    return {
        "league": {"league_id": league_id, "season": season, "name": lg["name"], "current_week": current},
        "week": week, "phase": phase, "matchup_id": row["matchup_id"] if row else None,
        "is_bye_week": is_bye, "weeks_available": weeks_available,
        "me": me, "opponent": opponent, "status": status, "notes": notes,
    }

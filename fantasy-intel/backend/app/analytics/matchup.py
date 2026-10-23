"""Weekly head-to-head matchup: one team against its opponent for one fantasy week.

Read-only shaping of rows that already exist; nothing here computes a new analytic.

  * Pairing and scores come from `matchups`; names, records and standing from `fantasy_teams`.
  * Projected lineups come from `team_week_lineup_projections` (ESPN weekly projections run through
    the optimal-lineup solver, see analytics/team_lineups.py). That table only holds the remaining
    weeks, so past weeks have no projection. A team-week whose projection is incomplete keeps
    `projected_points` NULL and exposes only `known_points`; no NULL is ever filled with a zero.
  * Starting slots no engine fills (IDP, punter, head coach) are left out of every projected lineup, so in such a
    league every projected total is partial: `projection_complete` False, only `known_points`, and the slots named in
    `unsupported_slots` and a note (issue #253 G3).
  * The lineup a manager actually set comes from `lineup_snapshots` (`actual_lineup`/`actual_bench`),
    when a snapshot exists for that week. Its `points` are NULL until the matchup has started: a
    pre-game 0.0 is "not played yet", not a measured zero.

`week` is a scoring week. It is resolved to its matchup period with ESPN's scheduleSettings.matchupPeriods
(the same `period_weeks` helper the playoff race uses), so a multi-week period (common in the playoffs) finds
its pairing from either of its weeks. Projections and lineup snapshots stay keyed by scoring week. A period
that spans several weeks reports `matchup_period` and `period_weeks`; its actual points cover the whole period.
Without a usable schedule the period is assumed to equal the week, and a note says so.

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
from .lineup import unsupported_slots
from .playoff_race import period_weeks

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


def _projected_side(con, key: tuple, players_by_id: dict, playing: set[str], row: dict | None = None,
                    origin: dict[int, str] | None = None) -> dict:
    """`row` (independent source) replaces the stored ESPN lineup row; `origin` tags each player's number."""
    rows = [row] if row is not None else fetch_dicts(con, """
        SELECT phase, known_lineup_points, projected_lineup_points, projection_complete, assignment, bench,
               missing_projection_ids FROM team_week_lineup_projections
        WHERE league_id = ? AND season = ? AND team_id = ? AND week = ?""", list(key)) \
        if table_exists(con, "team_week_lineup_projections") else []
    tag = (lambda pid: {"projection_origin": origin.get(pid) if pid is not None else None}) if origin is not None \
        else (lambda pid: {})
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
                       "injury_status": p.get("injury_status"), "on_bye": _on_bye(p.get("pro_team"), playing),
                       **tag(pid)})
    bench = []
    for b in _json(r["bench"], []):
        pid = b.get("player_id")
        p = players_by_id.get(pid, {})
        bench.append({"player_id": pid, "name": b.get("name") or p.get("full_name"),
                      "position": b.get("position") or p.get("position"), "pro_team": p.get("pro_team"),
                      "points": _r(b.get("points")), "injury_status": p.get("injury_status"),
                      "on_bye": _on_bye(p.get("pro_team"), playing), **tag(pid)})
    return {"phase": r["phase"], "known_points": r["known_lineup_points"],
            "projected_points": r["projected_lineup_points"] if r["projection_complete"] else None,
            "projection_complete": bool(r["projection_complete"]), "lineup": lineup, "bench": bench,
            "missing_ids": _json(r["missing_projection_ids"], [])}


def _actual_side(con, key: tuple, players_by_id: dict, playing: set[str], started: bool,
                 src: dict | None = None) -> tuple[list | None, list | None]:
    """`src` (independent source): each set player's projection comes from it instead of the
    snapshot's ESPN projection, with its origin."""
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
        if src is not None:
            pid = r["espn_player_id"]
            item["projected_points"] = _r(src["values"].get(pid))
            item["projection_origin"] = src["origin"].get(pid) if item["projected_points"] is not None else None
        (starters if r["is_starter"] else bench).append(item)
    return starters, bench


def independent_lineups(con, season: int, week: int, team_ids: list[int]) -> tuple[dict, dict[int, dict]]:
    """Each team's optimal projected lineup for `week` from the independent source (same solver and
    rosters as the stored ESPN lineups). Returns (projection_source.weekly result, {team_id: row})."""
    from .projection_source import weekly
    from .team_lineups import project_week
    from .trade_data import load_league_state

    w = weekly(con, season, week, "independent")
    if not w["available"]:
        return w, {}
    state = load_league_state(con, season)
    if state is None:
        return {**w, "available": False, "reason": "League rosters are not synced, so ESPN projections are shown."}, {}
    proj = {pid: {week: v} for pid, v in w["values"].items() if v is not None}
    rows = {}
    for tid in team_ids:
        res = project_week(state.rosters.get(tid, []), state.slots, week, proj)
        rows[tid] = {"phase": None, **{k: res[k] for k in ("known_lineup_points", "projected_lineup_points",
                                                            "projection_complete", "assignment", "bench",
                                                            "missing_projection_ids")}}
    return w, rows


def load_matchup(con, team_id: int | None = None, week: int | None = None, source: str | None = None) -> dict:
    """`source`: "espn" (default, the stored lineups) or "independent" (analytics/projection_source.py)."""
    from .projection_source import describe, normalize, weekly

    source = normalize(source, con)
    lgs = fetch_dicts(con, "SELECT league_id, season, name, current_week, final_scoring_period, reg_season_matchups, "
                           "settings_raw, roster_slots FROM leagues ORDER BY synced_at DESC LIMIT 1")
    if not lgs:
        raise LeagueNotSynced("ESPN league not synced yet. Run scripts/sync_espn.py.")
    lg = lgs[0]
    sched = (_json(lg.pop("settings_raw"), {}) or {}).get("scheduleSettings") or {}
    unsupported = unsupported_slots(_json(lg.pop("roster_slots"), {}) or {})
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
    league_periods = {int(r["p"]) for r in fetch_dicts(
        con, "SELECT DISTINCT matchup_period p FROM matchups WHERE league_id = ? AND season = ? "
             "AND matchup_period IS NOT NULL", [league_id, season])}
    # scoring week -> matchup period, from ESPN's schedule; a period whose weeks are unknown is taken as itself
    week_to_period: dict[int, int] = {}
    period_span: dict[int, list[int]] = {}
    assumed = False
    for per in sorted(league_periods):
        ws = period_weeks(sched, per)
        if ws is None:
            ws, assumed = [per], True
        period_span[per] = ws
        for w in ws:
            week_to_period.setdefault(w, per)
    if not sched.get("matchupPeriods") and sched.get("matchupPeriodLength") is None and league_periods:
        assumed = True
    my_periods = {int(m["matchup_period"]) for m in mine_rows}
    weeks_available = sorted(w for w, per in week_to_period.items() if per in my_periods)
    last_week = max([int(lg["final_scoring_period"] or 0), *weeks_available])

    if week is None:
        week = current
    elif week < 1 or week > last_week:
        raise WeekOutOfRange(f"Week {week} is outside the fantasy season (1-{last_week}).")

    period = week_to_period.get(week)
    span = period_span.get(period, [week]) if period is not None else [week]
    row = next((m for m in mine_rows if period is not None and int(m["matchup_period"]) == period), None)
    status = matchup_status(row, max(span), current)
    started = status in ("in_progress", "final")
    snapshot_started = started and week <= current       # a snapshot's points exist only for weeks already under way
    notes: list[str] = []
    if assumed:
        notes.append("ESPN's matchup period schedule is not in the synced settings, so matchup periods are "
                     "assumed to match scoring weeks.")
    if row and len(span) > 1:
        notes.append(f"Actual points cover the whole matchup period (weeks {span[0]}–{span[-1]}); "
                     f"projections and lineups are for week {week} only.")

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

    is_bye = row is None and period is not None
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
    src, alt_rows = (independent_lineups(con, season, week, [t for t in (team_id, opp_id) if t is not None])
                     if source == "independent" else (weekly(con, season, week, "espn"), {}))
    for ar in alt_rows.values():
        projected_ids |= {x.get("player_id") for x in ar["assignment"] + ar["bench"]}
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
        stored_phase = None
        if tid in alt_rows:
            stored = _projected_side(con, key, {}, playing)
            stored_phase = stored["phase"]
        proj = _projected_side(con, key, players_by_id, playing, alt_rows.get(tid),
                               src["origin"] if tid in alt_rows else None)
        if stored_phase is not None:
            proj["phase"] = stored_phase
        if not proj["lineup"] and not proj["bench"] and proj["known_points"] is None:
            unprojected.append(t["name"])
        elif not proj["projection_complete"] and (proj["missing_ids"] or not unsupported):
            missing = [players_by_id.get(i, {}).get("full_name") or f"player {i}" for i in proj["missing_ids"]]
            notes.append(f"{t['name']}: projection incomplete"
                         + (f" ({len(missing)} rostered without {'a' if alt_rows else 'an ESPN'} projection: "
                            f"{', '.join(missing)})"
                            if missing else "")
                         + f". Only the known lineup total ({proj['known_points']}) is shown; "
                           "it is not a full-lineup projection.")
        actual, actual_bench = _actual_side(con, key, players_by_id, playing, snapshot_started,
                                            src if tid in alt_rows else None)
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
    if unsupported:
        notes.append(f"{', '.join(unsupported)}: no engine fills these slots, so projected lineups and totals cover the "
                     "other starting slots only. Each total shown is partial (known points), not a full-lineup "
                     "projection.")
    if phase is None:
        tier = row and row.get("playoff_tier")
        reg = lg["reg_season_matchups"]
        if tier not in (None, "NONE"):
            phase = "playoff"
        elif reg:
            phase = "regular" if (period if period is not None else week) <= reg else "playoff"

    return {
        "league": {"league_id": league_id, "season": season, "name": lg["name"], "current_week": current},
        "week": week, "phase": phase, "matchup_id": row["matchup_id"] if row else None,
        "matchup_period": period, "period_weeks": span if len(span) > 1 else None,
        "is_bye_week": is_bye, "weeks_available": weeks_available,
        "me": me, "opponent": opponent, "status": status, "notes": notes,
        **({"unsupported_slots": list(unsupported)} if unsupported else {}),
        "projection_source": describe({**src, "available": bool(alt_rows)} if source == "independent" else src),
    }

"""Database side of the V2 lineup/trade engine: evaluation window, league state, explanatory
context, and the derived team lineup projection tables (schema v6). Reads raw ESPN/nflverse
tables; writes only team_week_lineup_projections and team_ros_lineup_projections."""

from __future__ import annotations

import json
import zoneinfo
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from ..db.connection import fetch_dicts, replace_where, table_columns, table_exists, utcnow
from ..espn.constants import DEFAULT_POSITION_MAP, FANTASY_POSITIONS, IR_SLOT
from .lineup import lineup_slots
from .player_availability import AVAILABLE, load_availability
from .projections import league_window
from .team_lineups import FA_STATUSES, RosterPlayer, project_team, public_weeks
from .trades import LeagueState, weeks_label


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


# ------------------------------------------------------------------ evaluation window --
#
# ESPN NFL scoring periods are NFL weeks: scoringPeriodId N is NFL REG week N (a two-week fantasy
# playoff matchup spans two scoring periods, but each scoring period is still one NFL week). The
# engine already relies on this for byes (projections.load_schedule); kickoffs are read the same way.

EASTERN = "America/New_York"
WINDOW_RULE = ("first scoring period from ESPN current_week whose NFL REG games have not kicked off; a week "
               "counts as kicked off once any game's scheduled kickoff (nfl_schedules gameday + gametime, "
               "US Eastern) is at or before now, or any game has a final score")


def _eastern_to_utc(day: date, hhmm: time) -> datetime:
    """nflverse `gametime` is US Eastern wall-clock time. Uses the IANA zone when the platform has it,
    else the US DST rule in force since 2007 (2nd Sunday of March to 1st Sunday of November, 02:00)."""
    local = datetime.combine(day, hhmm)
    try:
        return local.replace(tzinfo=zoneinfo.ZoneInfo(EASTERN)).astimezone(timezone.utc)
    except zoneinfo.ZoneInfoNotFoundError:   # no tz database (e.g. Windows without tzdata)
        def nth_sunday(month: int, n: int) -> date:
            first = date(day.year, month, 1)
            return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
        dst = datetime.combine(nth_sunday(3, 2), time(2)) <= local < datetime.combine(nth_sunday(11, 1), time(2))
        return (local + timedelta(hours=4 if dst else 5)).replace(tzinfo=timezone.utc)


def kickoff_utc(gameday, gametime) -> datetime | None:
    """Scheduled kickoff as an aware UTC datetime, or None when the schedule row lacks a usable
    date or time (never guessed)."""
    try:
        if isinstance(gameday, datetime):
            day = gameday.date()
        elif isinstance(gameday, date):
            day = gameday
        elif isinstance(gameday, str) and gameday.strip():
            day = date.fromisoformat(gameday.strip()[:10])
        else:
            return None
        if isinstance(gametime, time):
            hhmm = gametime
        elif isinstance(gametime, str) and gametime.strip():
            h, m = gametime.strip().split(":")[:2]
            hhmm = time(int(h), int(m))
        else:
            return None
    except (ValueError, TypeError):
        return None
    return _eastern_to_utc(day, hhmm)


def _aware_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def week_kickoff_status(con, season: int, week: int, now: datetime, cols: set[str]) -> dict:
    """Has NFL REG week `week` kicked off as of `now`?

    status: "completed" (every game has a final score), "in_progress" (at least one game kicked off
    or final), "not_started" (every game has a scheduled kickoff and all are after now), "unknown"
    (no games, or some kickoffs missing and none known to have started). Only positive evidence
    counts: a missing score never means "not played", and a 0-0 score is not treated as final."""
    has_scores = {"home_score", "away_score"} <= cols
    has_kickoff = {"gameday", "gametime"} <= cols
    sel = ", ".join([c for c in ("game_id", "home_team", "away_team") if c in cols]
                    + (["home_score", "away_score"] if has_scores else [])
                    + (["gameday", "gametime"] if has_kickoff else [])) or "week"
    games = fetch_dicts(con, f"SELECT {sel} FROM nfl_schedules WHERE season = ? AND game_type = 'REG' AND week = ?",
                        [season, week])
    out = {"week": week, "games": len(games), "score_columns": has_scores, "kickoff_columns": has_kickoff}
    if not games:
        return {**out, "status": "unknown", "detail": f"no REG games for week {week} in nfl_schedules"}
    kicks, final, started, no_kick = [], 0, 0, 0
    for g in games:
        k = kickoff_utc(g.get("gameday"), g.get("gametime")) if has_kickoff else None
        h, a = (g.get("home_score"), g.get("away_score")) if has_scores else (None, None)
        is_final = h is not None and a is not None and not (h == 0 and a == 0)
        final += is_final
        if k is None:
            no_kick += 1
        else:
            kicks.append(k)
        started += is_final or (k is not None and k <= now)
    out.update(games_final=final, games_kicked_off=started, games_missing_kickoff=no_kick,
               first_kickoff=_iso(min(kicks) if kicks else None), last_kickoff=_iso(max(kicks) if kicks else None))
    if final == len(games):
        return {**out, "status": "completed", "detail": f"all {len(games)} REG games of week {week} are final"}
    if started:
        return {**out, "status": "in_progress",
                "detail": f"{started} of {len(games)} REG games of week {week} have kicked off "
                          f"({final} final); lineups for those games are already set"}
    if no_kick == 0:
        return {**out, "status": "not_started",
                "detail": f"no REG game of week {week} has kicked off (first kickoff {out['first_kickoff']})"}
    why = "nfl_schedules has no gameday/gametime columns" if not has_kickoff else \
        f"{no_kick} of {len(games)} REG games of week {week} have no usable kickoff time"
    return {**out, "status": "unknown", "detail": f"{why}; no game is known to have kicked off"}


def evaluation_start_week(con, season: int, current: int, final: int, now: datetime | None = None) -> dict:
    """First fantasy scoring period a trade proposed now can still change, or None when none remain.

    Walks from ESPN current_week to final_scoring_period and starts at the first week whose NFL games
    have not kicked off: once any game of a week has kicked off, some of that week's lineups are
    locked, and this engine does not simulate partial weeks, so a new hypothetical trade starts the
    following week. Kickoffs come from the NFL schedule (timezone-aware), never from whether player
    stats or scores exist. If kickoff evidence is insufficient the week is kept but flagged
    `uncertain` with a warning (its gains may not be fully achievable). Past the final period:
    `evaluation_start_week` None, `remaining_weeks` False; no week is manufactured."""
    now = _aware_utc(now)
    out = {"espn_current_week": current, "final_scoring_period": final, "rule": WINDOW_RULE,
           "evaluated_at": now.isoformat(), "uncertain": False, "warning": None, "weeks_checked": []}

    def done(start, reason, **kw):
        res = {**out, **kw, "evaluation_start_week": start, "remaining_weeks": start is not None, "reason": reason}
        if start is None:
            res["warning"] = ("No remaining fantasy scoring periods: every week through the final scoring period "
                              f"({final}) has kicked off. Trades cannot change any remaining lineup.")
        return res

    if current > final:
        return done(None, f"ESPN current_week {current} is past the final scoring period {final}",
                    current_week_status="after_final", current_week_kicked_off=True, current_week_completed=True)
    if not table_exists(con, "nfl_schedules"):
        return done(current, f"nfl_schedules unavailable: kickoff status of week {current} cannot be established; "
                             f"using ESPN current_week {current}", current_week_status="unknown",
                    current_week_kicked_off=None, current_week_completed=None, uncertain=True,
                    warning=_uncertain_warning(current))
    cols = set(table_columns(con, "nfl_schedules"))
    for week in range(current, final + 1):
        st = week_kickoff_status(con, season, week, now, cols)
        out["weeks_checked"].append(st)
        if week == current:
            out.update(current_week_status=st["status"], current_week_games=st["games"],
                       current_week_games_final=st.get("games_final"),
                       current_week_kicked_off={"completed": True, "in_progress": True,
                                                "not_started": False}.get(st["status"]),
                       current_week_completed={"completed": True, "in_progress": False,
                                               "not_started": False}.get(st["status"]))
        started = weeks_label(current, week - 1)
        if st["status"] == "not_started":
            skipped = f"; {started} already kicked off" if week > current else ""
            return done(week, f"{st['detail']}: evaluation starts at week {week}{skipped}")
        if st["status"] == "unknown":
            prefix = f"{started} already kicked off; " if week > current else ""
            return done(week, f"{prefix}{st['detail']}: kickoff status unknown, week {week} included but uncertain",
                        uncertain=True, warning=_uncertain_warning(week))
    return done(None, f"every scoring period from week {current} through the final scoring period ({final}) "
                      "has kicked off; no remaining weeks")


def _uncertain_warning(week: int) -> str:
    return (f"Kickoff status for week {week} could not be established from the NFL schedule. Week {week} is "
            f"included, but some of its games may already have started, so part of its projected gain may not be "
            f"achievable. Re-evaluate with start week {week + 1} to exclude it.")


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


def load_league_state(con, season: int | None = None, now: datetime | None = None,
                      source: str | None = None) -> LeagueState | None:
    """`now` (timezone-aware; default the current UTC time) decides which weeks have kicked off. `source`: the
    projections the state carries; None = the league's default (projection_source.default_source: ESPN's for an ESPN
    league, Ours for a league without provider projections)."""
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
    # ESPN's raw current_week (league_window clamps it to the final period; past it, the season is over).
    start = evaluation_start_week(con, season, int(lg["current_week"]), window.final, now)
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
    fa_rows = [f for f in fetch_dicts(con, f"""
                   SELECT espn_player_id, full_name, position, pro_team, injury_status, availability_status
                   FROM fantasy_players WHERE availability_status IN {FA_STATUSES} ORDER BY espn_player_id""")
               if f["espn_player_id"] not in rostered]
    free_agents = [RosterPlayer(f["espn_player_id"], f["position"], f["pro_team"], f["full_name"], False, f["injury_status"])
                   for f in fa_rows]
    from .projection_source import league_projections, normalize
    source = normalize(source, con, season)
    proj: dict[int, dict[int, float]] = {}
    if source == "espn":
        for p in fetch_dicts(con, """SELECT espn_player_id, scoring_period, projected_points FROM espn_player_weekly_points
                                     WHERE season = ? AND scoring_period BETWEEN ? AND ? AND projected_points IS NOT NULL""",
                             [season, window.current, window.final]):
            proj.setdefault(p["espn_player_id"], {})[p["scoring_period"]] = float(p["projected_points"])
    else:
        for pid, by_week in league_projections(con, season, range(window.current, window.final + 1), source).items():
            proj[pid] = {w: v for w, (v, _) in by_week.items()}
    # NFL availability (reserve-list eligibility) is applied once here, so every consumer of the
    # league state (evaluator, discovery, team value, playoff race) uses the same weeks.
    weeks = tuple(range(window.current, window.final + 1))
    everyone = [p for ps in rosters.values() for p in ps] + free_agents
    avail = load_availability(con, season, everyone, weeks, proj)

    def apply(p: RosterPlayer) -> RosterPlayer:
        rec = avail.get(p.espn_player_id)
        return replace(p, unavailable_weeks=frozenset(rec["unavailable_weeks"])) if rec and rec["unavailable_weeks"] else p
    rosters = {t: [apply(p) for p in ps] for t, ps in rosters.items()}
    free_agents = [apply(p) for p in free_agents]
    rostered_ids = {p.espn_player_id for ps in rosters.values() for p in ps}
    availability = {pid: rec for pid, rec in avail.items()
                    if pid in rostered_ids or rec["status"] != AVAILABLE or rec["nfl_designation"]}
    capacity = sum(int(v) for k, v in roster_slots.items() if k != "IR")
    return LeagueState(
        season=season, slots=lineup_slots(roster_slots), final_week=window.final, last_regular=window.last_regular,
        start_week=start["evaluation_start_week"], rosters=rosters, free_agents=free_agents, projections=proj,
        active_capacity=capacity, position_limits=_position_limits(settings),
        team_names={t["team_id"]: t["name"] for t in teams}, league_id=lg["league_id"], start_week_reason=start,
        undroppable_list_in_use=((settings.get("rosterSettings") or {}).get("isUsingUndroppableList")),
        first_projection_week=window.current, availability=availability,
        free_agent_status={f["espn_player_id"]: f["availability_status"] for f in fa_rows}, projection_source=source)


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
        w["assignment"] = [{**a, "name": a.get("name") if a.get("streamer") else (names.get(a["player_id"]) or {}).get("full_name")}
                           for a in w["assignment"]]
        w["bench"] = [{**b, "name": (names.get(b["player_id"]) or {}).get("full_name")} for b in w["bench"]]
        w["ir"] = [{**x, "name": (names.get(x["player_id"]) or {}).get("full_name")} for x in w.get("ir", [])]
        w["unavailable"] = [{**x, "name": (names.get(x["player_id"]) or {}).get("full_name")}
                            for x in w.get("unavailable", [])]
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
    if state.start_week is None:
        # Nothing left to project. Leave the previously stored rows as they are (no manufactured week).
        return {"skipped": "no remaining fantasy scoring periods", "start_week_reason": state.start_week_reason.get("reason")}
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

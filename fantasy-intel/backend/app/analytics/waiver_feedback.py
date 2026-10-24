"""Waiver feedback (issue #346, `waiver-feedback.v1`, the sibling of decision_feedback.py): how the waiver claims
ARCHIVED before a week's waiver cutoff (waiver_archive.py) would have turned out for the viewer's own team. Read-only,
SQL + stdlib + the exact lineup evaluator.

As-of-time. Nothing is rebuilt from current data:

  cutoff     for scoring week W: the earliest instant W's first waiver processing could have happened. The first
             listed processing day D (leagues.waiver_process_days) after week W-1's last kickoff, at 00:00 in UTC+14
             (the earliest moment D exists anywhere: ESPN stores processing days, not the hour), or W's first kickoff
             if that is earlier. Unknown processing days (Sleeper) or unknown kickoffs: no cutoff, the week is ungraded.
  capture    the team's latest capture of week W taken strictly before the cutoff; its CLAIM rows are the decisions.
  roster     the team's roster_history snapshot with the latest synced_at strictly before the cutoff.

A claim's value is its hindsight lineup gain: the exact optimal-lineup difference (marginal_utility.scenario) of
roster - archived drop + claimed player over the HORIZON_WEEKS weeks W..W+2, on the provider's league-scored actual
points. It assumes the claim succeeded and lineups were set optimally both ways, and is labeled so: claim order and
FAAB are not modelled. A known bye scores 0; any other missing outcome is unknown, never 0, and the claim is excluded and
counted. No probability, confidence or letter grade is produced. Only the viewer's own team is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

from ..db.connection import fetch_dicts, table_exists
from ..espn.constants import IR_SLOT
from . import production_forecasts as pf
from .decision_feedback import (
    _aware,
    _json,
    _marks,
    _nfl,
    _own_team,
    _weekly_teams,
)
from .legacy_league import latest_league
from .lineup import lineup_slots
from .marginal_utility import scenario
from .playoff_race import period_weeks
from .team_lineups import RosterPlayer, Window
from .waiver_archive import HORIZON_WEEKS, TABLE
from .weekly_recap import NotSynced, completed_periods, resolve_week

__all__ = ["NotSynced", "WaiverData", "build_waiver_feedback", "cutoff", "load_waiver_data", "week_feedback"]

SCHEMA = "waiver-feedback.v1"
RULE_VERSION = "waiver-feedback.v1"
EARLIEST_ZONE = timezone(timedelta(hours=14))     # the first time zone a calendar day starts in
LATEST_ZONE = timezone(timedelta(hours=-12))      # the last time zone it ends in
DAYS = MappingProxyType({d: i for i, d in enumerate(
    ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"))})
ADDABLE = ("FREEAGENT", "WAIVERS")

WEEK_STATUS_TEXT = MappingProxyType({
    "no_prior_week": "Week 1 has no waiver run between weeks: nothing to grade.",
    "kickoff_unknown": "The kickoff times needed to place the waiver cutoff are unknown.",
    "schedule_unknown": "The league's waiver processing days are unknown, so no recommendation can be shown to be "
                        "archived before the waiver run.",
    "no_archive": "No waiver recommendations of your team were archived before this week's waiver cutoff.",
    "no_roster": "No roster snapshot of your team was archived before this week's waiver cutoff.",
    "no_claims": "The archived recommendations before the cutoff contain no CLAIM.",
    "horizon_incomplete": "Not every week of the grading horizon is complete yet.",
})
EXCLUSION_TEXT = MappingProxyType({
    "taken_by_other_team": "Another team added the player in that waiver window: whether the claim could have "
                           "succeeded is unknown.",
    "not_eligible": "The archived availability was not free agent or waivers.",
    "drop_not_on_roster": "The archived drop was not on the roster snapshot.",
    "unknown_outcome": "A horizon week's outcome is unknown for a player the lineups need (not a known bye).",
})
GRADE_DEFINITIONS = MappingProxyType({
    "cutoff": ("For scoring week W: the first listed waiver processing day D after week W-1's last kickoff, at 00:00 "
               "UTC+14 (the earliest instant D exists anywhere), or W's first kickoff if earlier. Unknown processing "
               "days or kickoffs: no cutoff, the week is ungraded. Rule " + RULE_VERSION + "."),
    "decision_set": ("The CLAIM rows of your team's latest capture of week W archived strictly before the cutoff, each "
                     "with its archived suggested drop."),
    "roster": "Your team's roster snapshot with the latest sync time strictly before the cutoff (IR slots stay IR).",
    "horizon": f"Scoring weeks W to W+{HORIZON_WEEKS - 1} (at most the league's final scoring period).",
    "gain": ("gain(x) = sum over the horizon of [best lineup of (roster - drop + x) - best lineup of roster], on the "
             "league's actual points, rounded to 0.01. It assumes the claim succeeded and lineups were set optimally "
             "both ways: claim order and FAAB are not modelled. A known bye scores 0; any other missing outcome is "
             "unknown and the claim is excluded."),
    "verdict": "helped if gain > 0, did not help if gain < 0, push if gain = 0.",
    "regret_lower_bound": ("max(0, max over the capture's other archived candidates c of gain(c) - gain(x)), each c "
                           "with its own archived drop. Only archived candidates are compared, so it is a lower bound."),
})


def _r(x: float | None) -> float | None:
    return None if x is None else round(x + 0.0, 2)


@dataclass
class WaiverData:
    """Everything the feedback needs, loaded by load_waiver_data or built directly in tests."""
    league: dict                                     # league_id season name final_scoring_period
    team: dict | None = None                         # the viewer's own team {team_id, name}
    schedule_settings: dict = field(default_factory=dict)
    roster_slots: dict = field(default_factory=dict)
    process_days: list[int] | None = None            # weekday indexes (Monday 0); None = unknown
    matchups: list[dict] = field(default_factory=list)
    week: int | None = None                          # selected matchup period
    kickoffs: dict = field(default_factory=dict)     # (week, nfl team) -> aware kickoff
    games: set = field(default_factory=set)          # (week, nfl team) with a scheduled REG game, kickoff known or not
    weekly_team: dict = field(default_factory=dict)  # (week, espn id) -> nflverse team that week
    players: dict = field(default_factory=dict)      # espn id -> {full_name, position, pro_team}
    rosters: list[dict] = field(default_factory=list)    # roster_history rows of the team
    captures: list[dict] = field(default_factory=list)   # the team's waiver_recommendation_snapshots rows
    outcomes: dict = field(default_factory=dict)     # (week, espn id) -> actual points (None = unknown)
    adds: list[dict] = field(default_factory=list)   # executed ADDs {espn_player_id, team_id, at}

    def first_kickoff(self, week: int) -> datetime | None:
        ks = [k for (w, _), k in self.kickoffs.items() if w == week and k is not None]
        return min(ks) if ks else None

    def last_kickoff(self, week: int) -> datetime | None:
        ks = [k for (w, _), k in self.kickoffs.items() if w == week and k is not None]
        return max(ks) if ks else None

    def schedule_has(self, week: int) -> bool:
        return any(w == week for (w, _) in self.kickoffs)

    def completed_weeks(self) -> set[int]:
        return {w for p in completed_periods(self.matchups) for w in (period_weeks(self.schedule_settings, p) or [])}


def process_weekdays(raw) -> list[int] | None:
    days = _json(raw) if isinstance(raw, str) else raw
    if not isinstance(days, list):
        return None
    out = sorted({DAYS[d.upper()] for d in days if isinstance(d, str) and d.upper() in DAYS})
    return out or None


def cutoff(data: WaiverData, week: int) -> tuple[dict | None, str | None]:
    """({cutoff, processing_day, window_end, first_kickoff}, None) or (None, week status)."""
    if week <= 1:
        return None, "no_prior_week"
    first, start = data.first_kickoff(week), data.last_kickoff(week - 1)
    if first is None or start is None:
        return None, "kickoff_unknown"
    if not data.process_days:
        return None, "schedule_unknown"
    day = start.astimezone(EARLIEST_ZONE).date()
    for _ in range(8):
        begins = datetime(day.year, day.month, day.day, tzinfo=EARLIEST_ZONE)
        if day.weekday() in data.process_days and begins > start:
            break
        day += timedelta(days=1)
    ends = datetime(day.year, day.month, day.day, tzinfo=LATEST_ZONE) + timedelta(days=1)
    return {"cutoff": min(begins, first).astimezone(UTC), "processing_day": day.isoformat(),
            "window_end": ends.astimezone(UTC), "first_kickoff": first.astimezone(UTC)}, None


def eligible_capture(data: WaiverData, week: int, cut: datetime) -> list[dict]:
    rows = [r for r in data.captures if r["week"] == week and _aware(r["captured_at"]) is not None
            and _aware(r["captured_at"]) < cut]
    if not rows:
        return []
    latest = max(_aware(r["captured_at"]) for r in rows)
    return sorted((r for r in rows if _aware(r["captured_at"]) == latest), key=lambda r: r["rank"])


def roster_before(data: WaiverData, cut: datetime) -> tuple[dict | None, list[dict]]:
    stamps = {}
    for r in data.rosters:
        t = _aware(r.get("synced_at"))
        if t is not None and t < cut:
            key = (t, str(r["snapshot_date"]))
            stamps[key] = r["snapshot_date"]
    if not stamps:
        return None, []
    t, day = max(stamps)
    rows = [r for r in data.rosters if str(r["snapshot_date"]) == day and _aware(r.get("synced_at")) == t]
    return {"snapshot_id": f"roster_history:{day}", "captured_at": t.isoformat()}, rows


def _rp(data: WaiverData, pid: int, on_ir: bool = False) -> RosterPlayer:
    p = data.players.get(pid) or {}
    return RosterPlayer(pid, p.get("position"), pro_team=p.get("pro_team"), name=p.get("full_name"), on_ir=on_ir)


def known_bye(data: WaiverData, week: int, pid: int) -> bool:
    """True only when the schedule lists games for `week` and none of the player's known NFL teams (his team, his
    nflverse weekly team) plays in one. Decided from scheduled games, never from kickoff times: a game whose kickoff is
    unknown is still a game, so a missing outcome there is unknown, not a bye (Codex #353)."""
    if not any(w == week for w, _ in data.games):
        return False
    p = data.players.get(pid) or {}
    teams = {t for t in (_nfl(p.get("pro_team")), data.weekly_team.get((week, pid))) if t and t != "FA"}
    return bool(teams) and not any((week, t) in data.games for t in teams)


def _actuals(data: WaiverData, weeks: list[int], pids: set[int]) -> dict | None:
    """{pid: {week: points}} with a known bye as 0, or None when any value is unknown."""
    out: dict[int, dict[int, float]] = {}
    for pid in pids:
        for w in weeks:
            v = data.outcomes.get((w, pid))
            if v is None:
                if not known_bye(data, w, pid):
                    return None
                v = 0.0
            out.setdefault(pid, {})[w] = float(v)
    return out


def _gain(data: WaiverData, roster: list[RosterPlayer], slots: list[str], weeks: list[int], row: dict,
          on_roster: set[int]) -> tuple[float | None, str | None]:
    drop = row.get("suggested_drop_id")
    if drop is not None and drop not in on_roster:
        return None, "drop_not_on_roster"
    add = _rp(data, int(row["espn_player_id"]))
    active = {p.espn_player_id for p in roster if not p.on_ir}
    proj = _actuals(data, weeks, active | {add.espn_player_id})
    if proj is None:
        return None, "unknown_outcome"
    s = scenario(roster, slots, Window(tuple(weeks), None), proj, remove=[drop] if drop is not None else (), add=[add])
    return (None, "unknown_outcome") if s["_ros_exact"] is None else (s["_ros_exact"], None)


def _taken(data: WaiverData, row: dict, team_id: int, after: datetime, until: datetime) -> bool:
    pid = int(row["espn_player_id"])
    return any(a["espn_player_id"] == pid and a["team_id"] != team_id and _aware(a["at"]) is not None
               and after < _aware(a["at"]) <= until for a in data.adds)


def week_feedback(data: WaiverData, week: int) -> dict:
    base = {"week": week, "status": None, "message": None, "cutoff": None, "capture": None, "roster": None,
            "horizon": [], "claims": [], "excluded": {}}
    cut, why = cutoff(data, week)
    if cut is None:
        return {**base, "status": why, "message": WEEK_STATUS_TEXT[why]}
    base["cutoff"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in cut.items()}
    capture = eligible_capture(data, week, cut["cutoff"])
    if not capture:
        return {**base, "status": "no_archive", "message": WEEK_STATUS_TEXT["no_archive"]}
    c0 = capture[0]
    base["capture"] = {k: (_aware(c0[k]).isoformat() if k in ("captured_at", "roster_synced_at") and c0.get(k) else c0.get(k))
                       for k in ("snapshot_id", "captured_at", "content_sha256", "projection_source",
                                 "projection_version", "scoring_fingerprint", "rule_version", "archive_k",
                                 "horizon_weeks", "roster_synced_at")}
    claims = [r for r in capture if r["recommendation"] == "CLAIM"]
    if not claims:
        return {**base, "status": "no_claims", "message": WEEK_STATUS_TEXT["no_claims"]}
    roster_meta, roster_rows = roster_before(data, cut["cutoff"])
    if roster_meta is None:
        return {**base, "status": "no_roster", "message": WEEK_STATUS_TEXT["no_roster"]}
    base["roster"] = roster_meta
    final = data.league.get("final_scoring_period") or week + HORIZON_WEEKS - 1
    horizon = [w for w in range(week, week + HORIZON_WEEKS) if w <= final]
    base["horizon"] = horizon
    if not set(horizon) <= data.completed_weeks():
        return {**base, "status": "horizon_incomplete", "message": WEEK_STATUS_TEXT["horizon_incomplete"]}
    roster = [_rp(data, int(r["espn_player_id"]), on_ir=r.get("lineup_slot_id") == IR_SLOT) for r in roster_rows]
    on_roster = {p.espn_player_id for p in roster}
    slots = lineup_slots(data.roster_slots)
    team_id = data.team["team_id"]
    captured = _aware(c0["captured_at"])
    window_end = cut["window_end"]

    def evaluate(row):
        if row.get("availability_status") not in ADDABLE:
            return None, "not_eligible"
        if _taken(data, row, team_id, captured, window_end):
            return None, "taken_by_other_team"
        return _gain(data, roster, slots, horizon, row, on_roster)

    gains = {int(r["espn_player_id"]): evaluate(r) for r in capture}
    known = {pid: g for pid, (g, _) in gains.items() if g is not None}
    excluded: dict[str, int] = {}
    graded = []
    for r in claims:
        pid = int(r["espn_player_id"])
        g, reason = gains[pid]
        if g is None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        others = [v for o, v in known.items() if o != pid]
        best = max(others, default=None)
        gr = _r(g)
        graded.append({
            "player": {"espn_player_id": pid, **{k: (data.players.get(pid) or {}).get(k) for k in ("full_name", "position")}},
            "drop": None if r.get("suggested_drop_id") is None else {
                "espn_player_id": r["suggested_drop_id"],
                **{k: (data.players.get(r["suggested_drop_id"]) or {}).get(k) for k in ("full_name", "position")}},
            "rank": r["rank"], "gain": gr,
            "verdict": "helped" if gr > 0 else "did_not_help" if gr < 0 else "push",
            "regret_lower_bound": None if best is None else _r(max(0.0, best - g)),
            "compared_candidates": len(others), "label": "if claimed, with lineups set optimally both ways"})
    return {**base, "status": "graded" if graded else "excluded", "claims": graded, "excluded": excluded}


def _tally(weeks: list[dict]) -> dict:
    claims = [c for w in weeks for c in w["claims"]]
    excluded: dict[str, int] = {}
    for w in weeks:
        for k, n in w["excluded"].items():
            excluded[k] = excluded.get(k, 0) + n
    return {"graded": len(claims), "helped": sum(c["verdict"] == "helped" for c in claims),
            "did_not_help": sum(c["verdict"] == "did_not_help" for c in claims),
            "push": sum(c["verdict"] == "push" for c in claims),
            "total_gain": _r(sum(c["gain"] for c in claims)) if claims else None, "excluded": excluded}


def build_waiver_feedback(data: WaiverData) -> dict:
    lg = data.league
    base = {"schema": SCHEMA, "rule_version": RULE_VERSION, "season": lg.get("season"), "league_name": lg.get("name"),
            "selected_week": data.week, "available_weeks": completed_periods(data.matchups),
            "team": None if data.team is None else {"team_id": data.team["team_id"], "name": data.team.get("name")},
            "definitions": dict(GRADE_DEFINITIONS), "week_status": dict(WEEK_STATUS_TEXT),
            "exclusion_reasons": dict(EXCLUSION_TEXT), "scoring_periods": [], "weeks": [], "summary": None,
            "season_to_date": None, "message": None}
    if data.week is None:
        return {**base, "status": "no_completed_week", "message": "No completed week yet."}
    if data.team is None:
        return {**base, "status": "no_team",
                "message": "Waiver feedback covers your own team only. Choose your team for this league to see it."}
    weeks = period_weeks(data.schedule_settings, data.week)
    if not weeks:
        return {**base, "status": "weeks_unmapped",
                "message": "The scoring weeks of this matchup period are unknown, so no claim can be matched to it."}
    detail = [week_feedback(data, w) for w in weeks]
    season_weeks = sorted(w for w in data.completed_weeks() if w <= max(weeks))
    season = [week_feedback(data, w) for w in season_weeks]
    graded = any(w["status"] == "graded" for w in detail)
    return {**base, "status": "ok" if graded else "nothing_graded", "scoring_periods": weeks, "weeks": detail,
            "message": None if graded else "No claim of these weeks can be graded yet; each week says why.",
            "summary": _tally(detail), "season_to_date": {"weeks": season_weeks, **_tally(season)}}


def load_waiver_data(con, week: int | None = None) -> WaiverData:
    """Read-only. Raises NotSynced (no league) and NotFound (the week has no completed matchups)."""
    lg = latest_league(con, ["league_id", "season", "name", "roster_slots", "settings_raw", "waiver_process_days",
                             "final_scoring_period"])
    if lg is None:
        raise NotSynced("League not synced yet.")
    lid, season = lg["league_id"], lg["season"]
    data = WaiverData(league={"league_id": lid, "season": season, "name": lg.get("name"),
                              "final_scoring_period": lg.get("final_scoring_period")},
                      schedule_settings=_json(lg.get("settings_raw")).get("scheduleSettings") or {},
                      roster_slots={k: int(v) for k, v in _json(lg.get("roster_slots")).items()},
                      process_days=process_weekdays(lg.get("waiver_process_days")))
    data.matchups = fetch_dicts(con, """SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points,
                                               away_points, winner, playoff_tier FROM matchups
                                        WHERE league_id = ? AND season = ? ORDER BY matchup_period, matchup_id""",
                                [lid, season]) if table_exists(con, "matchups") else []
    data.week = resolve_week(data.matchups, week)
    data.team = _own_team(con, lid, season)
    if data.week is None or data.team is None:
        return data
    tid = data.team["team_id"]
    data.kickoffs = pf.team_kickoffs(con, season)
    if table_exists(con, "nfl_schedules"):
        data.games = {(int(g["week"]), t) for g in fetch_dicts(
            con, "SELECT week, home_team, away_team FROM nfl_schedules WHERE season = ? AND game_type = 'REG'", [season])
            for t in (g["home_team"], g["away_team"]) if t}
    if not table_exists(con, TABLE):           # a league file built before the archive: every week says no_archive
        return data
    data.captures = fetch_dicts(con, f"""SELECT * FROM {TABLE} WHERE league_id = ? AND season = ? AND team_id = ?
                                        ORDER BY week, captured_at, rank""", [lid, season, tid])
    if table_exists(con, "roster_history"):
        data.rosters = fetch_dicts(con, """SELECT snapshot_date, espn_player_id, lineup_slot_id, synced_at FROM roster_history
                                           WHERE league_id = ? AND season = ? AND team_id = ?""", [lid, season, tid])
    pids = sorted({int(r["espn_player_id"]) for r in data.rosters} | {int(r["espn_player_id"]) for r in data.captures}
                  | {int(r["suggested_drop_id"]) for r in data.captures if r.get("suggested_drop_id") is not None})
    if not pids:
        return data
    weeks = sorted({w for r in data.captures for w in range(r["week"], r["week"] + HORIZON_WEEKS)})
    data.players = {int(r["espn_player_id"]): r for r in fetch_dicts(
        con, f"""SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players
                 WHERE espn_player_id IN ({_marks(pids)})""", pids)} if table_exists(con, "fantasy_players") else {}
    if weeks:
        data.weekly_team = _weekly_teams(con, season, pids, weeks)
        if table_exists(con, "espn_player_weekly_points"):
            data.outcomes = {(int(r["scoring_period"]), int(r["espn_player_id"])): r["actual_points"] for r in fetch_dicts(
                con, f"""SELECT scoring_period, espn_player_id, actual_points FROM espn_player_weekly_points
                         WHERE season = ? AND espn_player_id IN ({_marks(pids)}) AND scoring_period IN ({_marks(weeks)})""",
                [season, *pids, *weeks])}
    if table_exists(con, "transactions") and table_exists(con, "transaction_items"):
        data.adds = fetch_dicts(con, f"""
            SELECT i.espn_player_id, i.to_team_id AS team_id, coalesce(t.process_date, t.proposed_date) AS at
            FROM transactions t JOIN transaction_items i USING (transaction_id)
            WHERE t.league_id = ? AND t.season = ? AND t.status = 'EXECUTED' AND i.item_type = 'ADD'
              AND i.espn_player_id IN ({_marks(pids)})""", [lid, season, *pids])
    return data

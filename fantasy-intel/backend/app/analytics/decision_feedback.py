"""Decision feedback beside the weekly recap (issue #194): how the start/sit calls that ARCHIVED pregame inputs implied
for the viewer's own team turned out. Read-only, SQL + stdlib (the hosted API imports it). See docs/weekly_recap.md,
"Decision feedback".

As-of-time archive. Nothing here rebuilds an input from current data; every input carries the time it was captured,
and an input captured at or after its cutoff is never used:

  roster     roster_history, the daily roster snapshots the sync keeps (a snapshot taken before a week's first kickoff
             is locked against later same-day syncs). For scoring week W: the team's snapshot with the latest
             synced_at strictly before W's first kickoff and strictly after week W-1's last kickoff. A player in the
             IR slot at that time is not an option.
  forecasts  per player, the last forecast archived strictly before HIS OWN kickoff:
               espn         espn_projection_history (daily snapshots, pregame-locked by the sync), latest synced_at
               independent  the sealed, append-only production runs read through production_forecasts.pregame_forecasts
                            (content hash verified, artifact approved, artifact fitted before the week's first
                            kickoff, run created before his kickoff); the production model, else the labeled fallback
                            for a week without a production row.
  kickoff    the EARLIEST known kickoff among his NFL teams for the week (his current team and his nflverse weekly
             roster team; for a forecast row also the team the row was made for), so a team change can only make a
             forecast less eligible. A player whose known teams all have no game that week is on a bye (no decision).
             Unknown kickoff: never pregame.

The call ("start X over Y") is a pure function of those inputs and RULE_VERSION: the lineup the archived forecasts
imply (lineup.assign_lineup over the snapshot's eligible players, the league's starting slots that the source
covers), and for each filled slot the best-forecast benched player eligible for that slot. It is labeled as such,
never as "what the Start/Sit page showed" (that page also reads live data).

Outcomes are the provider's league-scored actual points for the scoring week. A missing value is unknown, never zero:
a call whose starter or alternative has no outcome is excluded and counted. The grades (GRADE_DEFINITIONS) are counts,
point margins and point bounds; no probability, confidence or letter grade is produced.

Only the viewer's own team is read (the caller passes it; app/api/weekly_recap.py refuses any other team), and the
season-to-date numbers aggregate that team's weeks only.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import ESPN_TO_NFLVERSE_TEAM, IR_SLOT
from . import production_forecasts as pf
from . import projection_source as ps
from .legacy_league import latest_league
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, assign_lineup, eligible, lineup_slots
from .playoff_race import period_weeks
from .weekly_recap import NotFound, NotSynced, completed_periods, resolve_week

__all__ = ["FeedbackData", "NotFound", "NotSynced", "build_feedback", "load_feedback_data", "week_feedback"]

SCHEMA = "decision-feedback.v1"
RULE_VERSION = "decision-feedback.v1"
CLOSE_CALL_POINTS = 3.0          # forecast gap below which a call is a close call (a miss there is not a confident miss)
TOP_ITEMS = 5
WATCH_ITEMS = 3

SOURCE_LABEL = MappingProxyType({"espn": "ESPN", "independent": "Ours"})
SKILL = ("QB", "RB", "WR", "TE")
COVERED_POSITIONS = MappingProxyType({"espn": FIXED_SLOTS, "independent": SKILL})
OURS_ZERO = ("withheld_unavailable",)   # Ours says he cannot score this week: a real 0.0 forecast (a "sit" call)

# Why a rostered player is not part of the week's decision set (counted, never imputed).
EXCLUSION_TEXT = MappingProxyType({
    "reserve_slot": "in the IR slot at the roster snapshot (not startable)",
    "position_not_covered": "a position this source does not forecast, or no starting slot takes",
    "bye": "no game that week",
    "kickoff_unknown": "his kickoff is unknown, so no forecast can be shown to be pregame",
    "no_pregame_forecast": "no forecast archived before his kickoff",
})
CALL_EXCLUSION_TEXT = MappingProxyType({
    "outcome_missing": "the starter or the alternative has no recorded points for the week (unknown, not zero)",
})
NO_ARCHIVE = "No archived recommendations for this week."

GRADE_DEFINITIONS = MappingProxyType({
    "decision_set": ("Players on your roster snapshot taken before the week's first kickoff, not in the IR slot, whose "
                     "team plays that week and who have a forecast from the source archived before their own kickoff."),
    "archived_lineup": ("The lineup that maximises the archived forecasts over the decision set in the league's starting "
                        "slots the source covers (ties: lower player id). Rule " + RULE_VERSION + "."),
    "call": ("For each filled slot s with starter x, the alternative y is the benched decision-set player eligible for s "
             "with the highest archived forecast. The call is 'start x over y'. A slot with no eligible benched player "
             "has no call."),
    "margin": "margin = actual(x) - actual(y), in league points, rounded to 0.01.",
    "verdict": "Correct if margin > 0, missed if margin < 0, push if margin = 0.",
    "hit_rate": "correct / (correct + missed); pushes and excluded calls are left out. Null when no call is decided.",
    "forecast_gap": ("forecast(x) - forecast(y) >= 0. A call with a gap under "
                     f"{CLOSE_CALL_POINTS:g} points is a close call: a miss there is not a confident miss."),
    "opportunity_cost": ("lower = max(0, actual(y) - actual(x)): points lost against the archive's own alternative. "
                         "upper = max(0, max over every eligible benched b of actual(b) - actual(x)): points lost against "
                         "the best alternative in hindsight. lower <= upper. upper is unknown (null) when an eligible "
                         "benched player has no recorded points."),
    "lineup": ("archived = actual points of the archived lineup; best = the most points any lineup from the same decision "
               "set and slots scored; archive_shortfall = best - archived (>= 0). Only when every decision-set player has "
               "recorded points. yours = actual points of your final starters in those slots (only when all are recorded)."),
    "forecast_error": ("error = actual - forecast per decision-set player with recorded points; MAE = mean |error|, "
                       "bias = mean error. Your roster only."),
    "coverage": "decision-set players / (decision-set players + players without a pregame forecast or known kickoff).",
    "exclusions": ("Missing data is never zero: players and calls without an archived input or a recorded outcome are "
                   "excluded and counted by reason."),
})


# ----------------------------------------------------------------------------------------------------- helpers


def _aware(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(v, datetime):
        return None
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


def _iso(v: Any) -> str | None:
    t = _aware(v)
    return None if t is None else t.isoformat()


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x + 0.0, n) + 0.0


def _json(v):
    return json.loads(v) if isinstance(v, (str, bytes, bytearray)) else (v or {})


def _nfl(team: str | None) -> str | None:
    return ESPN_TO_NFLVERSE_TEAM.get(team, team) if team else None


# ------------------------------------------------------------------------------------------------------- data


@dataclass
class FeedbackData:
    """Everything the feedback needs, loaded by load_feedback_data or built directly in tests."""
    league: dict                                   # league_id season name
    source: str                                    # "espn" | "independent"
    default_source: str
    schedule_settings: dict = field(default_factory=dict)
    roster_slots: dict = field(default_factory=dict)
    team: dict | None = None                       # the viewer's own team {team_id, name}; None = no team
    matchups: list[dict] = field(default_factory=list)
    week: int | None = None                        # selected matchup period
    kickoffs: dict = field(default_factory=dict)   # (week, nfl team) -> aware kickoff
    weekly_team: dict = field(default_factory=dict)   # (week, espn id) -> nflverse team that week
    players: dict = field(default_factory=dict)    # espn id -> {full_name, position, pro_team}
    rosters: list[dict] = field(default_factory=list)  # roster_history rows of the team: snapshot_date espn_player_id lineup_slot_id synced_at
    espn_history: dict = field(default_factory=dict)   # (week, espn id) -> [{value, synced_at, snapshot_date}]
    ours: dict = field(default_factory=dict)           # (week, espn id) -> pregame_forecasts row (+ content_sha256)
    ours_versions: dict = field(default_factory=dict)  # week -> model version read for that week
    ours_unavailable: str | None = None
    outcomes: dict = field(default_factory=dict)       # (week, espn id) -> actual points (None = unknown)
    lineups: dict = field(default_factory=dict)        # week -> final lineup rows of the team {espn_player_id, lineup_slot, is_starter}

    def first_kickoff(self, week: int) -> datetime | None:
        ks = [k for (w, _), k in self.kickoffs.items() if w == week and k is not None]
        return min(ks) if ks else None

    def last_kickoff(self, week: int) -> datetime | None:
        ks = [k for (w, _), k in self.kickoffs.items() if w == week and k is not None]
        return max(ks) if ks else None

    def schedule_has(self, week: int) -> bool:
        return any(w == week for (w, _) in self.kickoffs)


# --------------------------------------------------------------------------------------- as-of-time inputs


def roster_snapshot(data: FeedbackData, week: int) -> tuple[dict | None, list[dict], str | None]:
    """(snapshot meta, rows, None) for the week's archived roster, or (None, [], reason)."""
    first = data.first_kickoff(week)
    if first is None:
        return None, [], f"The kickoff times of week {week} are unknown, so no input can be shown to be pregame."
    lower = data.last_kickoff(week - 1) if week > 1 else None
    if week > 1 and lower is None and data.schedule_has(week - 1):
        return None, [], f"The kickoff times of week {week - 1} are unknown, so the week {week} roster cannot be placed."
    by_date: dict[Any, datetime] = {}
    for r in data.rosters:
        t = _aware(r.get("synced_at"))
        if t is None or t >= first or (lower is not None and t <= lower):
            continue
        d = r["snapshot_date"]
        if d not in by_date or t > by_date[d]:
            by_date[d] = t
    if not by_date:
        return None, [], (f"No roster snapshot of your team was archived between week {week - 1}'s last kickoff and "
                          f"week {week}'s first kickoff." if week > 1 else
                          f"No roster snapshot of your team was archived before week {week}'s first kickoff.")
    day = max(by_date, key=lambda d: (by_date[d], str(d)))
    stamp = by_date[day]
    rows = [r for r in data.rosters if r["snapshot_date"] == day and _aware(r.get("synced_at")) == stamp]
    meta = {"snapshot_id": f"roster_history:{day}", "captured_at": stamp.isoformat(), "cutoff": first.isoformat(),
            "source": "roster_history"}
    return meta, rows, None


def kickoff_for(data: FeedbackData, week: int, pid: int, extra_teams: Iterable[str | None] = ()) -> tuple[datetime | None, str]:
    """(kickoff, "ok") as the earliest known kickoff of his candidate teams; (None, "bye") when every candidate team
    is known to have no game that week; (None, "unknown") otherwise."""
    p = data.players.get(pid) or {}
    teams = {t for t in (_nfl(p.get("pro_team")), data.weekly_team.get((week, pid)), *map(_nfl, extra_teams))
             if t and t != "FA"}
    if not teams:
        return None, "unknown"
    known = [data.kickoffs[(week, t)] for t in teams if data.kickoffs.get((week, t)) is not None]
    if known:
        return min(known), "ok"
    return (None, "bye") if data.schedule_has(week) else (None, "unknown")


def forecast(data: FeedbackData, week: int, pid: int) -> tuple[dict | None, str | None]:
    """(forecast, None) with the archived pregame forecast and its input record, else (None, exclusion reason)."""
    if data.source == "independent":
        row = data.ours.get((week, pid))
        ko, status = kickoff_for(data, week, pid, [row.get("nfl_team")] if row else [])
        if row is not None and row.get("kickoff") is not None:
            stored = _aware(row["kickoff"])
            ko = stored if ko is None else min(ko, stored)
            status = "ok"
        if status != "ok":
            return None, "bye" if status == "bye" else "kickoff_unknown"
        if row is None:
            return None, "no_pregame_forecast"
        created = _aware(row.get("run_created_at"))
        if created is None or created >= ko:
            return None, "no_pregame_forecast"
        cov = row.get("coverage_status")
        if cov == "bye":
            return None, "bye"
        value = 0.0 if cov in OURS_ZERO else _finite(row.get("expected_points")) if cov == "projected" else None
        if value is None:
            return None, "no_pregame_forecast"
        return {"value": value, "kickoff": ko, "input": {
            "source": SOURCE_LABEL["independent"], "snapshot_id": row.get("run_id"), "captured_at": created.isoformat(),
            "kickoff": ko.isoformat(), "model_version": row.get("model_version"),
            "scoring_version": row.get("scoring_fingerprint"), "content_sha256": row.get("content_sha256"),
            "artifact_id": row.get("artifact_id"), "coverage_status": cov}}, None
    ko, status = kickoff_for(data, week, pid)
    if status != "ok":
        return None, "bye" if status == "bye" else "kickoff_unknown"
    snaps = [s for s in data.espn_history.get((week, pid), [])
             if _aware(s.get("synced_at")) is not None and _aware(s["synced_at"]) < ko and _finite(s.get("value")) is not None]
    if not snaps:
        return None, "no_pregame_forecast"
    s = max(snaps, key=lambda s: (_aware(s["synced_at"]), str(s.get("snapshot_date"))))
    return {"value": float(s["value"]), "kickoff": ko, "input": {
        "source": SOURCE_LABEL["espn"], "snapshot_id": f"espn_projection_history:{s.get('snapshot_date')}",
        "captured_at": _aware(s["synced_at"]).isoformat(), "kickoff": ko.isoformat(), "model_version": None,
        "scoring_version": "the league's scoring as published by the provider", "content_sha256": None,
        "artifact_id": None, "coverage_status": None}}, None


def outcome(data: FeedbackData, week: int, pid: int) -> float | None:
    return _finite(data.outcomes.get((week, pid)))


def covered_slots(data: FeedbackData) -> list[str]:
    """The league's starting slots every eligible position of which the source forecasts."""
    covered = set(COVERED_POSITIONS[data.source])
    return [s for s in lineup_slots(data.roster_slots) if set(FLEX_ELIGIBILITY.get(s, (s,))) <= covered]


def decision_positions(slots: list[str]) -> set[str]:
    """Positions that can fill at least one of `slots` (a kicker in a league without a K slot is no decision)."""
    return {p for s in slots for p in FLEX_ELIGIBILITY.get(s, (s,))}


# ---------------------------------------------------------------------------------------------- one week


def _player(data: FeedbackData, pid: int) -> dict:
    p = data.players.get(pid) or {}
    return {"espn_player_id": pid, "full_name": p.get("full_name") or f"Player {pid}", "position": p.get("position"),
            "pro_team": p.get("pro_team")}


def _side(data: FeedbackData, week: int, member: dict) -> dict:
    pid = member["espn_player_id"]
    actual = outcome(data, week, pid)
    return {**_player(data, pid), "forecast": _r(member["value"]), "actual": _r(actual),
            "input": member["forecast"]["input"]}


def _grade(x: float, y: float, alts: list[float | None]) -> dict:
    margin = round(x - y, 2) + 0.0
    verdict = "correct" if margin > 0 else "missed" if margin < 0 else "push"
    upper = None if any(a is None for a in alts) else _r(max(0.0, max(alts) - x))
    return {"margin": margin, "verdict": verdict,
            "opportunity_cost": {"lower": _r(max(0.0, y - x)), "upper": upper,
                                 "upper_unknown": sum(a is None for a in alts)}}


def _tally(calls: list[dict]) -> dict:
    graded = [c for c in calls if c["verdict"] is not None]
    n = {v: sum(c["verdict"] == v for c in graded) for v in ("correct", "missed", "push")}
    decided = n["correct"] + n["missed"]
    uppers = [c["opportunity_cost"]["upper"] for c in graded]
    return {"calls": len(calls), "graded": len(graded), "excluded": len(calls) - len(graded), **n,
            "hit_rate": _r(n["correct"] / decided, 3) if decided else None,
            "close_calls_missed": sum(c["verdict"] == "missed" and c["close_call"] for c in graded),
            "opportunity_cost_lower": _r(sum(c["opportunity_cost"]["lower"] for c in graded)),
            "opportunity_cost_upper": None if any(u is None for u in uppers) else _r(sum(uppers))}


def _empty_week(week: int, reason: str, roster: dict | None = None) -> dict:
    return {"week": week, "status": "no_archive", "reason": reason, "roster_input": roster, "calls": [],
            "lineup": None, "exclusions": {}, "coverage": None, "forecast_error": None, "players": [],
            "summary": _tally([])}


def week_feedback(data: FeedbackData, week: int) -> dict:
    """The graded calls of one scoring week for data.team (module docstring), or a no_archive week with its reason."""
    meta, rows, why = roster_snapshot(data, week)
    if meta is None:
        return _empty_week(week, f"{NO_ARCHIVE} {why}")
    slots = covered_slots(data)
    covered = decision_positions(slots)
    pool, excl, seen = [], {}, set()
    for r in sorted(rows, key=lambda r: r["espn_player_id"]):
        pid = int(r["espn_player_id"])
        if pid in seen:
            continue
        seen.add(pid)
        pos = (data.players.get(pid) or {}).get("position")
        reason = None
        if r.get("lineup_slot_id") == IR_SLOT:
            reason = "reserve_slot"
        elif pos not in covered:
            reason = "position_not_covered"
        else:
            f, reason = forecast(data, week, pid)
            if f is not None:
                pool.append({"espn_player_id": pid, "position": pos, "value": f["value"], "forecast": f})
        if reason:
            excl[reason] = excl.get(reason, 0) + 1
    without = excl.get("no_pregame_forecast", 0) + excl.get("kickoff_unknown", 0)
    coverage = {"decision_set": len(pool), "without_forecast": without,
                "share": _r(len(pool) / (len(pool) + without), 3) if pool or without else None}
    if not pool:
        return {**_empty_week(week, f"{NO_ARCHIVE} No player on the archived roster has a {SOURCE_LABEL[data.source]} "
                                    "forecast archived before his kickoff.", meta), "exclusions": excl, "coverage": coverage}
    lineup = assign_lineup(pool, slots)
    by_id = {m["espn_player_id"]: m for m in pool}
    starters = {a["player_id"] for a in lineup if a["player_id"] is not None}
    calls = []
    for a in lineup:
        if a["player_id"] is None:
            continue
        x = by_id[a["player_id"]]
        alts = sorted((m for m in pool if m["espn_player_id"] not in starters and eligible(a["slot"], m["position"])),
                      key=lambda m: (-m["value"], m["espn_player_id"]))
        if not alts:
            continue
        y = alts[0]
        ax, ay = outcome(data, week, x["espn_player_id"]), outcome(data, week, y["espn_player_id"])
        gap = _r(x["value"] - y["value"])
        call = {"week": week, "slot": a["slot"], "start": _side(data, week, x), "over": _side(data, week, y),
                "forecast_gap": gap, "close_call": gap < CLOSE_CALL_POINTS, "verdict": None, "margin": None,
                "opportunity_cost": None, "excluded": None,
                "alternatives": len(alts)}
        if ax is None or ay is None:
            call["excluded"] = "outcome_missing"
        else:
            call.update(_grade(ax, ay, [outcome(data, week, m["espn_player_id"]) for m in alts]))
        calls.append(call)
    actual = {m["espn_player_id"]: outcome(data, week, m["espn_player_id"]) for m in pool}
    missing = sum(v is None for v in actual.values())
    lineup_grade: dict = {"complete": missing == 0, "missing_outcomes": missing, "archived": None, "best": None,
                          "archive_shortfall": None, "yours": None, "yours_outside_decision_set": None,
                          "archived_minus_yours": None, "slots": slots}
    if missing == 0:
        archived = sum(actual[a["player_id"]] for a in lineup if a["player_id"] is not None)
        best = assign_lineup([{**m, "value": actual[m["espn_player_id"]]} for m in pool], slots)
        best_pts = sum(actual[b["player_id"]] for b in best if b["player_id"] is not None)
        lineup_grade.update({"archived": _r(archived), "best": _r(best_pts), "archive_shortfall": _r(max(0.0, best_pts - archived))})
    mine = [r for r in data.lineups.get(week, []) if r.get("is_starter") and r.get("lineup_slot") in set(slots)]
    if mine:
        pts = [outcome(data, week, int(r["espn_player_id"])) for r in mine]
        if all(p is not None for p in pts):
            lineup_grade["yours"] = _r(sum(pts))
            lineup_grade["yours_outside_decision_set"] = sum(int(r["espn_player_id"]) not in by_id for r in mine)
            if lineup_grade["archived"] is not None:
                lineup_grade["archived_minus_yours"] = _r(lineup_grade["archived"] - lineup_grade["yours"])
    errors = [(actual[m["espn_player_id"]] - m["value"], m) for m in pool if actual[m["espn_player_id"]] is not None]
    players = [{**_side(data, week, m), "error": _r(e), "started_in_archive": m["espn_player_id"] in starters}
               for e, m in sorted(errors, key=lambda em: (-abs(em[0]), em[1]["espn_player_id"]))]
    ferr = {"n": len(errors), "mae": _r(sum(abs(e) for e, _ in errors) / len(errors)) if errors else None,
            "bias": _r(sum(e for e, _ in errors) / len(errors)) if errors else None}
    return {"week": week, "status": "graded", "reason": None, "roster_input": meta, "calls": calls,
            "lineup": lineup_grade, "exclusions": excl, "coverage": coverage, "forecast_error": ferr,
            "players": players, "summary": _tally(calls)}


# ------------------------------------------------------------------------------------------------- response


def _items(weeks: list[dict]) -> dict:
    calls = [c for w in weeks for c in w["calls"] if c["verdict"] is not None]
    worked = sorted((c for c in calls if c["verdict"] == "correct"),
                    key=lambda c: (-c["margin"], c["week"], c["start"]["espn_player_id"]))[:TOP_ITEMS]
    changed = sorted((c for c in calls if c["verdict"] == "missed"),
                     key=lambda c: (-c["opportunity_cost"]["lower"], c["week"], c["start"]["espn_player_id"]))[:TOP_ITEMS]
    players = [{**p, "week": w["week"]} for w in weeks for p in w["players"]]
    above = sorted((p for p in players if p["error"] > 0), key=lambda p: (-p["error"], p["espn_player_id"]))[:WATCH_ITEMS]
    below = sorted((p for p in players if p["error"] < 0), key=lambda p: (p["error"], p["espn_player_id"]))[:WATCH_ITEMS]
    notes = []
    excluded = sum(1 for w in weeks for c in w["calls"] if c["verdict"] is None)
    if excluded:
        notes.append(f"{excluded} call{'s' if excluded != 1 else ''} left ungraded: "
                     f"{CALL_EXCLUSION_TEXT['outcome_missing']}.")
    for reason in ("no_pregame_forecast", "kickoff_unknown"):
        n = sum(w["exclusions"].get(reason, 0) for w in weeks)
        if n:
            notes.append(f"{n} rostered player{'s' if n != 1 else ''} left out: {EXCLUSION_TEXT[reason]}.")
    return {"worked": worked, "changed": changed,
            "watch": {"above_forecast": above, "below_forecast": below, "notes": notes}}


def _season(data: FeedbackData, periods: list[int]) -> dict:
    rows = []
    for p in periods:
        for w in period_weeks(data.schedule_settings, p) or []:
            wf = week_feedback(data, w)
            rows.append({"matchup_period": p, "week": w, "status": wf["status"], **wf["summary"],
                         "coverage": (wf["coverage"] or {}).get("share"),
                         "decision_set": (wf["coverage"] or {}).get("decision_set", 0),
                         "archive_shortfall": (wf["lineup"] or {}).get("archive_shortfall"),
                         "forecast_mae": (wf["forecast_error"] or {}).get("mae"),
                         "forecast_n": (wf["forecast_error"] or {}).get("n", 0)})
    graded = [r for r in rows if r["status"] == "graded"]
    correct, missed = sum(r["correct"] for r in graded), sum(r["missed"] for r in graded)
    n_err = sum(r["forecast_n"] for r in graded)
    mae = (sum(r["forecast_mae"] * r["forecast_n"] for r in graded if r["forecast_mae"] is not None) / n_err) if n_err else None
    uppers = [r["opportunity_cost_upper"] for r in graded]
    return {"weeks": rows, "totals": {
        "weeks": len(rows), "weeks_graded": len(graded), "calls": sum(r["calls"] for r in graded),
        "graded": sum(r["graded"] for r in graded), "excluded": sum(r["excluded"] for r in graded),
        "correct": correct, "missed": missed, "push": sum(r["push"] for r in graded),
        "hit_rate": _r(correct / (correct + missed), 3) if correct + missed else None,
        "opportunity_cost_lower": _r(sum(r["opportunity_cost_lower"] or 0.0 for r in graded)) if graded else None,
        "opportunity_cost_upper": None if not graded or any(u is None for u in uppers) else _r(sum(uppers)),
        "forecast_mae": _r(mae), "forecast_n": n_err}}


def build_feedback(data: FeedbackData) -> dict:
    lg = data.league
    available = completed_periods(data.matchups)
    base = {"schema": SCHEMA, "rule_version": RULE_VERSION, "season": lg.get("season"), "league_name": lg.get("name"),
            "selected_week": data.week, "available_weeks": available,
            "team": None if data.team is None else {"team_id": data.team["team_id"], "name": data.team.get("name")},
            "source": {"key": data.source, "label": SOURCE_LABEL[data.source], "is_default": data.source == data.default_source,
                       "covered_positions": list(COVERED_POSITIONS[data.source]),
                       "unavailable_reason": data.ours_unavailable if data.source == "independent" else None,
                       "versions": {str(w): v for w, v in sorted(data.ours_versions.items())} if data.source == "independent" else {}},
            "definitions": dict(GRADE_DEFINITIONS), "exclusion_reasons": {**EXCLUSION_TEXT, **CALL_EXCLUSION_TEXT},
            "scoring_periods": [], "weeks": [], "feedback": None, "season_to_date": None, "message": None}
    if data.week is None:
        return {**base, "status": "no_completed_week", "message": "No completed week yet."}
    if data.team is None:
        return {**base, "status": "no_team",
                "message": "Decision feedback covers your own team only. Choose your team for this league to see it."}
    weeks = period_weeks(data.schedule_settings, data.week)
    if not weeks:
        return {**base, "status": "weeks_unmapped",
                "message": "The scoring weeks of this matchup period are unknown, so no decision can be matched to it."}
    detail = [week_feedback(data, w) for w in weeks]
    graded = [w for w in detail if w["status"] == "graded"]
    periods = [p for p in available if p <= data.week]
    return {**base, "status": "ok" if graded else "no_archive", "scoring_periods": weeks, "weeks": detail,
            "message": None if graded else NO_ARCHIVE, "summary": _tally([c for w in detail for c in w["calls"]]),
            "feedback": _items(graded), "season_to_date": _season(data, periods)}


# ---------------------------------------------------------------------------------------------------- loading


def _own_team(con, league_id, season) -> dict | None:
    if not table_exists(con, "fantasy_teams") or "is_mine" not in set(table_columns(con, "fantasy_teams")):
        return None
    rows = fetch_dicts(con, """SELECT team_id, name FROM fantasy_teams WHERE league_id = ? AND season = ? AND is_mine
                               ORDER BY team_id""", [league_id, season])
    return rows[0] if len(rows) == 1 else None


def _marks(xs) -> str:
    return ", ".join("?" * len(xs))


def _weekly_teams(con, season: int, pids: list[int], weeks: list[int]) -> dict:
    """(week, espn id) -> nflverse roster team that week, through a high-confidence id map (global tables)."""
    if not (pids and weeks and table_exists(con, "player_id_map") and table_exists(con, "nfl_rosters_weekly")):
        return {}
    if not {"gsis_id", "week", "team", "season"} <= set(table_columns(con, "nfl_rosters_weekly")):
        return {}
    out = {}
    for r in fetch_dicts(con, f"""SELECT m.espn_player_id, w.week, any_value(w.team) AS team
                                  FROM player_id_map m JOIN nfl_rosters_weekly w ON w.gsis_id = m.gsis_id
                                  WHERE m.match_confidence = 'high' AND w.season = ?
                                    AND m.espn_player_id IN ({_marks(pids)}) AND w.week IN ({_marks(weeks)})
                                  GROUP BY 1, 2""", [season, *pids, *weeks]):
        out[(int(r["week"]), int(r["espn_player_id"]))] = r["team"]
    return out


def _load_ours(con, data: FeedbackData, season: int, weeks: list[int], pids: set[int]) -> None:
    fp, why = ps._scoring(con, season)
    if fp is None:
        data.ours_unavailable = f"No archived Ours forecasts: {why}."
        return
    by_version = {}
    for version in ps.CONSUMER_VERSIONS:
        rows, _ = pf.pregame_forecasts(con, season, weeks, model_version=version, scoring_fingerprint=fp)
        by_version[version] = rows
    seals = {}
    run_ids = sorted({r["run_id"] for rows in by_version.values() for r in rows})
    if run_ids:
        seals = {r["run_id"]: r["content_sha256"] for r in fetch_dicts(
            con, f"SELECT run_id, content_sha256 FROM {pf.RUNS} WHERE run_id IN ({_marks(run_ids)})", run_ids)}
    for w in weeks:
        for version in ps.CONSUMER_VERSIONS:       # production first; the labeled fallback only for a week without it
            rows = [r for r in by_version[version] if r["week"] == w]
            if rows:
                data.ours_versions[w] = version
                for r in rows:
                    if r["espn_player_id"] in pids:
                        data.ours[(w, r["espn_player_id"])] = {**r, "content_sha256": seals.get(r["run_id"])}
                break


def load_feedback_data(con, week: int | None = None, source: str | None = None) -> FeedbackData:
    """Read-only. Raises NotSynced (no league), NotFound (the week has no completed matchups) and ValueError (an
    unknown source)."""
    lg = latest_league(con, ["league_id", "season", "name", "roster_slots", "settings_raw"])
    if lg is None:
        raise NotSynced("League not synced yet.")
    lid, season = lg["league_id"], lg["season"]
    default = ps.default_source(con, season)
    src = ps.normalize(source, con, season)
    data = FeedbackData(league={"league_id": lid, "season": season, "name": lg.get("name")}, source=src,
                        default_source=default,
                        schedule_settings=_json(lg.get("settings_raw")).get("scheduleSettings") or {},
                        roster_slots={k: int(v) for k, v in _json(lg.get("roster_slots")).items()})
    data.matchups = fetch_dicts(con, """SELECT matchup_id, matchup_period, home_team_id, away_team_id, home_points,
                                               away_points, winner, playoff_tier FROM matchups
                                        WHERE league_id = ? AND season = ? ORDER BY matchup_period, matchup_id""",
                                [lid, season]) if table_exists(con, "matchups") else []
    data.week = resolve_week(data.matchups, week)
    data.team = _own_team(con, lid, season)
    if data.week is None or data.team is None:
        return data
    periods = [p for p in completed_periods(data.matchups) if p <= data.week]
    weeks = sorted({w for p in periods for w in (period_weeks(data.schedule_settings, p) or [])})
    if not weeks:
        return data
    tid = data.team["team_id"]
    data.kickoffs = pf.team_kickoffs(con, season)
    if table_exists(con, "roster_history"):
        data.rosters = fetch_dicts(con, """SELECT snapshot_date, espn_player_id, lineup_slot_id, synced_at FROM roster_history
                                           WHERE league_id = ? AND season = ? AND team_id = ?""", [lid, season, tid])
    if table_exists(con, "lineup_snapshots"):
        for r in fetch_dicts(con, f"""SELECT scoring_period, espn_player_id, lineup_slot, is_starter FROM lineup_snapshots
                                      WHERE league_id = ? AND season = ? AND team_id = ?
                                        AND scoring_period IN ({_marks(weeks)})""", [lid, season, tid, *weeks]):
            data.lineups.setdefault(int(r["scoring_period"]), []).append(r)
    pids = sorted({int(r["espn_player_id"]) for r in data.rosters}
                  | {int(r["espn_player_id"]) for rows in data.lineups.values() for r in rows})
    if not pids:
        return data
    data.players = {int(r["espn_player_id"]): r for r in fetch_dicts(
        con, f"""SELECT espn_player_id, full_name, position, pro_team FROM fantasy_players
                 WHERE espn_player_id IN ({_marks(pids)})""", pids)} if table_exists(con, "fantasy_players") else {}
    data.weekly_team = _weekly_teams(con, season, pids, weeks)
    if table_exists(con, "espn_player_weekly_points"):
        data.outcomes = {(int(r["scoring_period"]), int(r["espn_player_id"])): r["actual_points"] for r in fetch_dicts(
            con, f"""SELECT scoring_period, espn_player_id, actual_points FROM espn_player_weekly_points
                     WHERE season = ? AND espn_player_id IN ({_marks(pids)}) AND scoring_period IN ({_marks(weeks)})""",
            [season, *pids, *weeks])}
    if src == "espn":
        if table_exists(con, "espn_projection_history"):
            for r in fetch_dicts(con, f"""SELECT scoring_period, espn_player_id, projected_points, synced_at, snapshot_date
                                          FROM espn_projection_history
                                          WHERE season = ? AND projected_points IS NOT NULL
                                            AND espn_player_id IN ({_marks(pids)}) AND scoring_period IN ({_marks(weeks)})""",
                                 [season, *pids, *weeks]):
                data.espn_history.setdefault((int(r["scoring_period"]), int(r["espn_player_id"])), []).append(
                    {"value": r["projected_points"], "synced_at": r["synced_at"], "snapshot_date": str(r["snapshot_date"])})
    else:
        _load_ours(con, data, season, weeks, set(pids))
    return data

"""Add/drop scenario simulator for waiver decisions (issue #178). Pure: no database, no provider call.

One pair per request: add one available player, drop one rostered player, or both. The roster change is valued by the
trade analyzer's own machinery, unchanged:

  * trades.side_lineups: the roster before and after, the exact optimal starting lineup in every remaining week
    (team_lineups.project_team over lineup.assign_lineup) and their comparison (team_lineups.compare). An add is a
    one-player "receive" (it arrives in an active roster spot, trades.arrives_active) and a drop a one-player "send";
  * trades.legal_roster / roster_issues: active capacity (IR and taxi slots excluded, reserve_slots) and the
    provider's position limits. A move the roster cannot hold is not valued: it is reported with the drop the trade
    engine would force (trades.resolve_roster), never silently applied;
  * the evaluation window is the trade engine's (LeagueState.window): the first scoring period none of whose NFL games
    has kicked off, through the final one. A week already under way is never credited;
  * waiver_roster.ir_slot_eligible: an IR occupant without an IR-eligible status blocks adds at the provider;
  * waiver_decisions.resource_diagnostics: free agent, waiver priority or FAAB, the same words as the waiver table;
  * trades.roster_legality: when rostered players are missing from the player data (Sleeper IDP), or a league that
    starts slots no engine fills has no player count, a roster the count shows legal is `unverified` (legal None, a
    ROSTER_UNVERIFIED note, no open-spot count), never legal (issue #253 G4). Where every rostered player is stored
    (ESPN, IDP/P/HC included) the count is exact; dropping a player who can start only in such a slot gets an
    UNSUPPORTED_SLOT_PLAYER note (his lineup value is not modelled).

Honest data: a week's delta is None unless both lineups are complete; `known_delta` sums known projections only and
is labelled as such. A missing projection is unknown, never 0. Nothing here predicts whether a claim succeeds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, eligible, unsupported_of
from .team_lineups import (
    IR_HOLD_NOTE,
    STREAM_NOTE,
    RosterPlayer,
    Window,
    arrives_active,
    lineup_relevant,
    projection,
    public_weeks,
    r,
    slot_filled,
    source_label,
    starters,
    stream_summary,
)
from .trades import (
    ROSTER_UNVERIFIED,
    EvaluationUnavailable,
    LeagueState,
    TradeError,
    legal_roster,
    resolve_roster,
    roster_issues,
    roster_legality,
    side_lineups,
    streaming_enabled,
)
from .waiver_decisions import resource_diagnostics
from .waiver_roster import ir_slot_eligible

CONTRACT = "waiver-add-drop/v1"
# Free agents offered in the add picker (best known projected points over the window first). A bound on the
# response, not on what may be simulated: any available player can be requested by id.
ADD_OPTIONS_LIMIT = 60
# waiver_decisions: a meaningful forward gain is at least 1 projected point per covered week.
MEANINGFUL_PER_WEEK = 1.0

# Problems stop the evaluation (the move as asked cannot be simulated); notes qualify an evaluated move.
NO_TEAM = "NO_TEAM"
NO_REMAINING_WEEKS = "NO_REMAINING_WEEKS"
ADD_ON_ROSTER = "ADD_ON_ROSTER"
ADD_ROSTERED = "ADD_ROSTERED"
ADD_NOT_AVAILABLE = "ADD_NOT_AVAILABLE"
DROP_NOT_ON_ROSTER = "DROP_NOT_ON_ROSTER"
DROP_REQUIRED = "DROP_REQUIRED"

WAIVER_CLAIM = "WAIVER_CLAIM"
GAME_LOCKED = "GAME_LOCKED"
LOCK_UNKNOWN = "LOCK_UNKNOWN"
DROP_RESERVED = "DROP_RESERVED"
IR_SLOT_INELIGIBLE = "IR_SLOT_INELIGIBLE"
NFL_INELIGIBLE = "NFL_INELIGIBLE"
NOT_LINEUP_ELIGIBLE = "NOT_LINEUP_ELIGIBLE"
UNDROPPABLE_LIST = "UNDROPPABLE_LIST"
ADD_INJURY = "ADD_INJURY"
UNSUPPORTED_SLOT_PLAYER = "UNSUPPORTED_SLOT_PLAYER"

POSITION_ORDER = {p: i for i, p in enumerate(FIXED_SLOTS)}


class AddDropError(ValueError):
    """A request that is not one pair (the same player twice)."""


@dataclass
class AddDropContext:
    """What the pure evaluation needs beyond the league state, loaded by the API (app/api/add_drop.py)."""
    provider: str = "ESPN"                      # the provider label used in prose ("nothing is sent to ESPN")
    team_row: dict = field(default_factory=dict)        # fantasy_teams row: name, waiver_rank, faab_spent
    rules: dict = field(default_factory=lambda: {"is_faab": None})   # waiver_data.waiver_rules(league)
    waivers: dict | None = None                 # league_rules.waivers_section facts (type, period, process days)
    directory: dict = field(default_factory=dict)       # requested id -> fantasy_players row (name, status)
    locks: dict = field(default_factory=dict)   # pro team -> start_sit.PlayerLock for `lock_week`
    lock_week: int | None = None                # the provider's current scoring period
    games: dict = field(default_factory=dict)   # projections.load_schedule: (team, week) -> (opponent, is_home)
    covered: set = field(default_factory=set)   # weeks the NFL schedule covers (a team absent from one is on bye)
    evaluated_at: str | None = None             # when the window was decided (ISO-8601 UTC)
    # Whether the provider's sync tells waivers from free agents. Sleeper's does not (every unrostered player is
    # written FREEAGENT), so there a FREEAGENT status does not mean "no claim needed".
    waiver_status_known: bool = True


def _item(code: str, message: str, player_id: int | None = None, tone: str = "info") -> dict:
    return {"code": code, "player_id": player_id, "message": message, "tone": tone}


def _name(p: RosterPlayer | dict | None, pid: int | None) -> str:
    if isinstance(p, RosterPlayer) and p.name:
        return p.name
    if isinstance(p, dict) and p.get("full_name"):
        return p["full_name"]
    return f"Player {pid}"


def _bye_weeks(p: RosterPlayer, window: Window, ctx: AddDropContext) -> list[int] | None:
    """Remaining weeks his NFL team has no game; None when the schedule does not cover the window (unknown)."""
    if not p.pro_team or not window.weeks or not set(window.weeks) <= ctx.covered:
        return None
    return [w for w in window.weeks if (p.pro_team, w) not in ctx.games]


def _lock(p: RosterPlayer, ctx: AddDropContext) -> dict | None:
    lk = ctx.locks.get(p.pro_team)
    if lk is None:
        return None
    return {"week": ctx.lock_week, "status": lk.status, "reason": lk.reason,
            "kickoff_utc": lk.kickoff_utc.isoformat() if lk.kickoff_utc else None}


def _card(p: RosterPlayer, state: LeagueState, window: Window, ctx: AddDropContext, roster_status: str,
          team_id: int | None = None) -> dict:
    return {"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
            "injury_status": p.injury_status, "roster_status": roster_status,
            "availability_status": state.free_agent_status.get(p.espn_player_id),
            "team_id": team_id, "team_name": state.team_names.get(team_id) if team_id is not None else None,
            "on_ir": p.on_ir, "on_taxi": p.on_taxi,
            "lineup_eligible": lineup_relevant(p.position, state.slots),
            "weekly_projection": {w: projection(state.projections, p.espn_player_id, w) for w in window.weeks},
            "nfl_ineligible_weeks": sorted(w for w in p.unavailable_weeks if w in window.weeks),
            "bye_weeks": _bye_weeks(p, window, ctx), "kickoff_lock": _lock(p, ctx)}


def _known_points(p: RosterPlayer, state: LeagueState, window: Window) -> tuple[float | None, int]:
    vals = [v for w in window.weeks if (v := projection(state.projections, p.espn_player_id, w)) is not None]
    return (r(sum(vals)) if vals else None), len(vals)


def _options(state: LeagueState, roster: list[RosterPlayer] | None, window: Window, add: int | None) -> dict:
    """The pickers: available players (FREEAGENT / WAIVERS, never a rostered one) and the team's roster."""
    rows = []
    for p in state.free_agents:
        if not lineup_relevant(p.position, state.slots):
            continue
        pts, n = _known_points(p, state, window)
        rows.append({"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                     "pro_team": p.pro_team, "injury_status": p.injury_status,
                     "availability_status": state.free_agent_status.get(p.espn_player_id),
                     "known_projected_points": pts, "projected_weeks": n})
    rows.sort(key=lambda x: (x["known_projected_points"] is None, -(x["known_projected_points"] or 0.0),
                             x["espn_player_id"]))
    shown = rows[:ADD_OPTIONS_LIMIT]
    if add is not None and all(x["espn_player_id"] != add for x in shown):
        shown += [x for x in rows if x["espn_player_id"] == add]
    drops = []
    for p in roster or []:
        pts, n = _known_points(p, state, window)
        drops.append({"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                      "pro_team": p.pro_team, "injury_status": p.injury_status, "on_ir": p.on_ir,
                      "on_taxi": p.on_taxi, "known_projected_points": pts, "projected_weeks": n})
    drops.sort(key=lambda x: (POSITION_ORDER.get(x["position"], 9), x["on_ir"] or x["on_taxi"],
                              -(x["known_projected_points"] or 0.0), x["name"] or "", x["espn_player_id"]))
    return {"add": shown, "add_total": len(rows), "add_limit": ADD_OPTIONS_LIMIT, "drop": drops}


def _active(players: list[RosterPlayer]) -> int:
    return sum(1 for p in players if not p.reserved)


def depth(before: list[RosterPlayer], after: list[RosterPlayer] | None, slots: list[str]) -> list[dict]:
    """Rostered players per position, active and reserved (IR or taxi), before and after the move, beside the
    league's starting slots for that position (fixed and the flex slots it may fill)."""
    positions = sorted({p.position for p in before + (after or []) if p.position} | {s for s in slots if s in FIXED_SLOTS},
                       key=lambda pos: (POSITION_ORDER.get(pos, 9), pos))

    def side(players: list[RosterPlayer], pos: str) -> dict:
        mine = sorted((p for p in players if p.position == pos), key=lambda p: (p.reserved, p.name or "", p.espn_player_id))
        return {"active": sum(1 for p in mine if not p.reserved), "reserved": sum(1 for p in mine if p.reserved),
                "players": [{"espn_player_id": p.espn_player_id, "name": p.name, "reserved": p.reserved} for p in mine]}
    flex = Counter(s for s in slots if s in FLEX_ELIGIBILITY)
    out = []
    for pos in positions:
        b = side(before, pos)
        a = side(after, pos) if after is not None else None
        out.append({"position": pos, "starting_slots": slots.count(pos),
                    "flex_slots": [{"slot": s, "count": n} for s, n in sorted(flex.items()) if eligible(s, pos)],
                    "before": b, "after": a, "active_change": (a["active"] - b["active"]) if a is not None else None})
    return out


def _named(rows: list[dict], names: dict[int, str]) -> list[dict]:
    return [{**x, "name": x.get("name") or names.get(x.get("player_id"))} for x in rows]


def _week_rows(core: dict, slots: list[str], ctx: AddDropContext, names: dict[int, str]) -> list[dict]:
    """The comparison's week rows, with who enters and leaves the lineup, open starting slots and byes."""
    def on_bye(players: list[RosterPlayer], w: int) -> list[int] | None:
        if w not in ctx.covered:
            return None                     # the schedule does not cover the week: byes are unknown
        return sorted(p.espn_player_id for p in players if not p.reserved and p.pro_team
                      and lineup_relevant(p.position, slots) and (p.pro_team, w) not in ctx.games)
    out = []
    for row, b, a in zip(core["compare"]["weeks"], core["before_team"]["weeks"], core["after_team"]["weeks"]):
        sb, sa = starters(b), starters(a)
        w = row["week"]
        out.append({**row,
                    "entered": [{"espn_player_id": i, "name": names.get(i)} for i in sorted(sa - sb)],
                    "exited": [{"espn_player_id": i, "name": names.get(i)} for i in sorted(sb - sa)],
                    "open_slots_before": sum(1 for x in b["assignment"] if not slot_filled(x)),
                    "open_slots_after": sum(1 for x in a["assignment"] if not slot_filled(x)),
                    "on_bye_before": on_bye(core["before"], w), "on_bye_after": on_bye(core["after"], w),
                    "missing_projection_ids": sorted(set(b["missing_projection_ids"]) | set(a["missing_projection_ids"]))})
    return out


def _lineup(team_week: dict, names: dict[int, str]) -> dict:
    return {"week": team_week["week"], "projection_complete": team_week["projection_complete"],
            "known_lineup_points": team_week["known_lineup_points"],
            "projected_lineup_points": team_week["projected_lineup_points"],
            "assignment": _named(team_week["assignment"], names), "bench": _named(team_week["bench"], names)}


def _assumptions(state: LeagueState, provider: str) -> list[str]:
    out = [
        f"Analysis only: nothing is claimed, added or dropped, and nothing is sent to {provider}.",
        "Lineup impact: the trade analyzer's exact optimal-lineup simulation for every remaining week, with the league's "
        "real roster slots and flex rules. Only starters count; bench points are never value.",
        "A projected 0 (bye, injury) is a real value; a missing projection is unknown and never counted as 0. A total "
        "that includes an unknown is shown as known-only.",
        "The added player arrives in an active roster spot. IR and taxi players stay rostered but never start, and "
        "dropping one frees no active spot.",
        IR_HOLD_NOTE,
        "Evaluation window: from the first scoring period none of whose NFL games has kicked off; a week already under "
        "way is never credited.",
    ]
    if streaming_enabled(state):
        out.append(STREAM_NOTE)
    else:
        out.append("Apart from this one move, the roster stays as it is for every remaining week.")
    return out


LIMITATIONS = [
    "Projections can be wrong, and future injuries are unknowable.",
    "A waiver claim can fail: other teams may claim the same player first. No claim-success probability is given.",
    "Kickoff locks and waiver timing come from the synced schedule and settings; verify the move in the provider "
    "before making it.",
    "Undroppable-list restrictions are not modeled.",
]


def evaluate_add_drop(state: LeagueState, team_id: int | None, add: int | None, drop: int | None,
                      ctx: AddDropContext | None = None) -> dict:
    """One add/drop pair (either side may be absent) for `team_id`. Never mutates `state`."""
    ctx = ctx or AddDropContext()
    if add is not None and add == drop:
        raise AddDropError("The same player cannot be both added and dropped.")
    if team_id is not None and team_id not in state.rosters:
        raise LookupError(f"Team {team_id} is not in this league.")
    window = state.window()
    roster = state.rosters.get(team_id) if team_id is not None else None
    fa = {p.espn_player_id: p for p in state.free_agents}
    owner = {p.espn_player_id: (t, p) for t, ps in state.rosters.items() for p in ps}
    problems: list[dict] = []
    notes: list[dict] = []
    out: dict[str, Any] = {
        "contract": CONTRACT,
        "team": {"id": team_id, "name": state.team_names.get(team_id) if team_id is not None else None},
        "request": {"add": add, "drop": drop},
        "evaluation": {"start_week": window.start, "final_week": window.final,
                       "last_regular_season_week": window.last_regular, "weeks": list(window.weeks),
                       "earliest_actionable_week": state.start_week,
                       "uncertain": bool((state.start_week_reason or {}).get("uncertain")),
                       "warning": (state.start_week_reason or {}).get("warning"),
                       "start_week_reason": state.start_week_reason or {}, "evaluated_at": ctx.evaluated_at},
        "projection_source": source_label(state.projection_source, state.projections),
        "impact_source": "exact optimal-lineup simulation (the trade analyzer's)",
        "kdst_streaming": stream_summary(state.projections, window),
        "options": _options(state, roster, window, add if add in fa else None),
        "players": {"add": None, "drop": None},
        "assumptions": _assumptions(state, ctx.provider),
        "limitations": LIMITATIONS,
    }

    # ---- who the ids are (the API has already checked that each is in this league's player data) ----
    add_p = drop_p = None
    if add is not None:
        if add in fa:
            add_p = fa[add]
            out["players"]["add"] = _card(add_p, state, window, ctx,
                                          "waivers" if state.free_agent_status.get(add) == "WAIVERS" else "free_agent")
        elif add in owner:
            t, p = owner[add]
            out["players"]["add"] = _card(p, state, window, ctx, "own_roster" if t == team_id else "other_roster", t)
            if t == team_id:
                problems.append(_item(ADD_ON_ROSTER, f"{_name(p, add)} is already on your roster, so there is nothing "
                                                     "to add.", add, "caution"))
            else:
                problems.append(_item(ADD_ROSTERED, f"{_name(p, add)} is rostered by {state.team_names.get(t) or f'team {t}'}: "
                                                    "only a free agent or a player on waivers can be added.", add, "caution"))
        else:
            row = ctx.directory.get(add) or {}
            out["players"]["add"] = {"espn_player_id": add, "name": row.get("full_name"), "position": row.get("position"),
                                     "pro_team": row.get("pro_team"), "injury_status": row.get("injury_status"),
                                     "roster_status": "not_available",
                                     "availability_status": row.get("availability_status")}
            problems.append(_item(ADD_NOT_AVAILABLE, f"{_name(row, add)} is not on the free-agent or waiver list in the "
                                                     f"last {ctx.provider} sync, so he cannot be added.", add, "caution"))
    if drop is not None:
        if roster is not None and drop in {p.espn_player_id for p in roster}:
            drop_p = next(p for p in roster if p.espn_player_id == drop)
            out["players"]["drop"] = _card(drop_p, state, window, ctx, "own_roster", team_id)
        else:
            t, p = owner.get(drop, (None, fa.get(drop)))
            row = ctx.directory.get(drop) or {}
            who = _name(p, drop) if p is not None else _name(row, drop)
            where = (f"he is on {state.team_names.get(t) or f'team {t}'}'s roster" if t is not None
                     else "he is not on any roster")
            out["players"]["drop"] = {"espn_player_id": drop, "name": p.name if p else row.get("full_name"),
                                      "position": p.position if p else row.get("position"),
                                      "pro_team": p.pro_team if p else row.get("pro_team"),
                                      "roster_status": "other_roster" if t is not None else "not_rostered",
                                      "team_id": t}
            if team_id is not None:
                problems.append(_item(DROP_NOT_ON_ROSTER, f"{who} cannot be dropped: {where}, not on "
                                                          f"{state.team_names.get(team_id) or 'this team'}'s.",
                                      drop, "caution"))
    if team_id is None:
        problems.insert(0, _item(NO_TEAM, "Pick a team: the owner's team is not known for this league, so there is no "
                                          "roster to change.", None, "caution"))
    if (add is not None or drop is not None) and not window.weeks:
        problems.append(_item(NO_REMAINING_WEEKS, (state.start_week_reason or {}).get("warning") or
                              "No remaining fantasy scoring periods: a move made now cannot change any lineup.", None,
                              "caution"))

    # ---- qualifying notes on the players asked for ----
    for p, role in ((add_p, "add"), (drop_p, "drop")):
        if p is None:
            continue
        lock = out["players"][role]["kickoff_lock"]
        if lock and lock["status"] == "LOCKED":
            notes.append(_item(GAME_LOCKED, f"{p.name}'s week {lock['week']} game has kicked off, so {ctx.provider} may "
                                            f"not allow this {'add' if role == 'add' else 'drop'} until that lock lifts. "
                                            f"The impact below starts at week {window.start}." if window.start else
                                            f"{p.name}'s week {lock['week']} game has kicked off.", p.espn_player_id,
                               "caution"))
        elif lock and lock["status"] == "UNKNOWN":
            notes.append(_item(LOCK_UNKNOWN, f"Whether {p.name}'s week {lock['week']} game has kicked off is unknown "
                                             f"({lock['reason']}); check {ctx.provider} before acting.", p.espn_player_id))
        blocked = out["players"][role]["nfl_ineligible_weeks"]
        if blocked:
            notes.append(_item(NFL_INELIGIBLE, f"{p.name} is verified NFL-ineligible in week"
                                               f"{'s' if len(blocked) > 1 else ''} {', '.join(map(str, blocked))}: he "
                                               "cannot start then, whatever his projection says.", p.espn_player_id,
                               "caution"))
    if add_p is not None:
        if state.free_agent_status.get(add) == "WAIVERS":
            notes.append(_item(WAIVER_CLAIM, f"{add_p.name} is on waivers: he can only be acquired by a claim"
                                             f"{_processing(ctx)}. The claim can fail, and he is not on your roster "
                                             "until it is processed.", add, "caution"))
        if not lineup_relevant(add_p.position, state.slots):
            notes.append(_item(NOT_LINEUP_ELIGIBLE, f"{add_p.name} ({add_p.position}) cannot fill any starting slot in "
                                                    "this league, so he cannot change a lineup.", add, "caution"))
        if (add_p.injury_status or "").upper() in ("IR", "INJURY_RESERVE", "OUT", "DOUBTFUL", "SUSPENSION"):
            notes.append(_item(ADD_INJURY, f"{add_p.name} is listed {add_p.injury_status}. He is added to an active "
                                           "spot; moving him to IR is a separate move this simulation does not make.",
                               add, "caution"))
    unsupported = unsupported_of(state.slots)
    if drop_p is not None and unsupported and not lineup_relevant(drop_p.position, state.slots):
        notes.append(_item(UNSUPPORTED_SLOT_PLAYER, f"{drop_p.name} ({drop_p.position}) can start only in "
                                                    f"{', '.join(unsupported)}, which no engine fills: what he adds to "
                                                    "your lineup is not modelled, so losing him is not counted.", drop,
                           "caution"))
    if drop_p is not None and drop_p.reserved:
        notes.append(_item(DROP_RESERVED, f"{drop_p.name} is in {'an IR' if drop_p.on_ir else 'a taxi'} slot: dropping "
                                          "him frees no active roster spot and changes no lineup.", drop))
    if roster is not None and add is not None:
        # dropping the occupant himself clears the block
        bad = [p for p in roster if p.on_ir and not ir_slot_eligible(p.injury_status) and p.espn_player_id != drop]
        for p in bad:
            notes.append(_item(IR_SLOT_INELIGIBLE, f"{p.name} is in an IR slot with status "
                                                   f"{p.injury_status or 'unknown'}, which is not IR-eligible: "
                                                   f"{ctx.provider} blocks adds until he is moved off IR.",
                               p.espn_player_id, "caution"))
    if state.undroppable_list_in_use and drop_p is not None:
        notes.append(_item(UNDROPPABLE_LIST, f"This league uses an undroppable list; whether {drop_p.name} is on it is "
                                             "not known here.", drop))

    # ---- roster capacity, the same rules as the trade engine ----
    legality = roster_legality(state, team_id)
    roster_block: dict[str, Any] = {"active_capacity": state.active_capacity, "position_limits": state.position_limits,
                                    "active_before": _active(roster) if roster is not None and not legality else None,
                                    "active_after": None, "open_spots_before": None, "open_spots_after": None,
                                    "legal": None, "drop_required": None, "issues": None, "suggested_drop": None,
                                    "ir_slot_blockers": [n["player_id"] for n in notes if n["code"] == IR_SLOT_INELIGIBLE]}
    if legality:
        roster_block["legality"] = legality
    if roster is not None and not legality:
        roster_block["open_spots_before"] = max(0, state.active_capacity - _active(roster))
    out["roster"] = roster_block
    out["depth"] = depth(roster, None, state.slots) if roster is not None else []
    asked = add is not None or drop is not None
    move_ok = asked and not problems
    if move_ok:
        sends = [drop] if drop_p is not None else []
        receives = [add_p] if add_p is not None else []
        before = list(roster)
        pre = [p for p in roster if p.espn_player_id not in set(sends)] + [arrives_active(p) for p in receives]
        legal = legal_roster(pre, state, before)
        roster_block.update(legal=legal, drop_required=not legal, issues=roster_issues(state, before, pre),
                            active_after=_active(pre), open_spots_after=max(0, state.active_capacity - _active(pre)))
        if legal and legality:
            # the count shows room, but the count is not verified: never "legal" (an over-capacity count stays illegal)
            roster_block.update(legal=None, drop_required=None, open_spots_after=None)
            notes.append(_item(ROSTER_UNVERIFIED, legality["reason"].replace("the provider's app", ctx.provider),
                               None, "caution"))
        if legality:
            roster_block.update(open_spots_after=None, active_after=None)
            if roster_block["issues"]:
                roster_block["issues"] = {**roster_block["issues"], "active_before_drops": None, "over_capacity_by": None}
        if legal is False:
            problems.append(_item(DROP_REQUIRED, _drop_required_text(state, before, pre, add_p, drop_p, bool(legality)),
                                  drop or add,
                                  "caution"))
            roster_block["suggested_drop"] = sug = _suggest_drop(state, window, before, pre, add)
            if sug and sug.get("unsupported_slot_player"):
                notes.append(_item(UNSUPPORTED_SLOT_PLAYER, sug["note"], sug["espn_player_id"], "caution"))
            move_ok = False
        else:
            out["depth"] = depth(before, pre, state.slots)
    out["problems"] = problems
    out["notes"] = notes
    out["status"] = "options" if not asked else "evaluated" if move_ok else "blocked"
    out["impact"] = _impact(state, window, team_id, add_p, drop_p, ctx) if move_ok else None
    out["acquisition"] = _acquisition(state, add_p, ctx, out["impact"]) if add_p is not None else None
    return out


def _processing(ctx: AddDropContext) -> str:
    days = ((ctx.waivers or {}).get("process_days") or {})
    if days.get("display"):
        return f" ({days['display'].lower()})"
    return " (when claims process is not in the synced settings)"


def _drop_required_text(state: LeagueState, before, pre, add_p, drop_p, counted_only: bool = False) -> str:
    issues = roster_issues(state, before, pre)
    parts = []
    if issues["over_capacity_by"]:
        parts.append(f"{'at least ' if counted_only else ''}{_active(before)} of {state.active_capacity} active spots "
                     "are used")
    for lim in issues["position_limits"]:
        parts.append(f"the league allows at most {lim['limit']} {lim['position']}")
    why = " and ".join(parts) or "the roster would break the league's limits"
    if drop_p is not None and drop_p.reserved:
        return (f"{drop_p.name} is in {'an IR' if drop_p.on_ir else 'a taxi'} slot, so dropping him frees no active spot: "
                f"{why}. Pick an active player to drop.")
    if drop_p is not None:
        return f"Dropping {drop_p.name} is not enough: {why}. Pick another player to drop."
    return f"A drop is required to add {add_p.name if add_p else 'this player'}: {why}. Pick a player to drop."


def _suggest_drop(state: LeagueState, window: Window, before, pre, add: int | None) -> dict | None:
    """The trade engine's forced drop for this roster (the legal removal keeping the most projected starting points;
    never the incoming player unless nothing else is legal). A suggestion to try, not part of any delta."""
    if not window.weeks:
        return None
    try:
        res = resolve_roster(state, window, before, pre, {add} if add is not None else set())
    except EvaluationUnavailable as err:
        return {"available": False, "reason": str(err)}
    except TradeError as err:
        return {"available": False, "reason": str(err)}
    keep = [d for d in res["forced_drops"] if not d["is_incoming"]]
    if len(keep) != 1 or len(res["forced_drops"]) != 1:
        return {"available": False, "reason": "No single player other than the one being added can be dropped to make "
                                              "this roster legal."}
    d = keep[0]
    return {"available": True, "espn_player_id": d["espn_player_id"], "name": d["name"], "position": d["position"],
            "lineup_cost": res["lineup_cost"], "known_lineup_cost": res["known_lineup_cost"], "basis": res["basis"],
            # a player no engine values, suggested only because no other single drop is legal: no cost, flagged
            **({"unsupported_slot_player": True, "unverified": True,
                "note": res["unvalued_drop"]["note"]} if d.get("unsupported_slot_player") else {}),
            "rule": "The legal drop that keeps the most projected starting points over the window (the trade "
                    "analyzer's forced-drop rule). A player who can start only in a slot no engine fills is suggested "
                    "only when no other drop is legal."}


def _impact(state: LeagueState, window: Window, team_id: int, add_p, drop_p, ctx: AddDropContext) -> dict:
    """The trade analyzer's side computation with a one-player send (the drop) and a one-player receive (the add)."""
    core = side_lineups(state, window, team_id, [drop_p.espn_player_id] if drop_p else [], [add_p] if add_p else [])
    names = {p.espn_player_id: p.name for p in core["before"] + core["after"]}
    weeks = _week_rows(core, state.slots, ctx, names)
    bt, at = public_weeks(core["before_team"]), public_weeks(core["after_team"])
    return {
        "first_week": weeks[0] if weeks else None,
        "rest_of_season": {k: v for k, v in core["compare"].items() if k != "weeks"},
        "weeks": weeks,
        "before": core["before_team"]["summary"], "after": core["after_team"]["summary"],
        "lineup": {"before": _lineup(bt[0], names), "after": _lineup(at[0], names)} if bt else None,
    }


def _acquisition(state: LeagueState, add_p: RosterPlayer, ctx: AddDropContext, impact: dict | None) -> dict:
    status = state.free_agent_status.get(add_p.espn_player_id)
    ros = (impact or {}).get("rest_of_season") or {}
    weeks = len((impact or {}).get("weeks") or [])
    gain = ros.get("ros_delta")
    meaningful = gain is not None and weeks > 0 and gain / weeks >= MEANINGFUL_PER_WEEK
    res = resource_diagnostics(status, ctx.rules, ctx.team_row, meaningful=meaningful)
    w = ctx.waivers or {}
    return {**res, "availability_status": status, "claim_required": status == "WAIVERS",
            "waiver_type": w.get("type"), "waiver_period": w.get("period"), "process_days": w.get("process_days")}

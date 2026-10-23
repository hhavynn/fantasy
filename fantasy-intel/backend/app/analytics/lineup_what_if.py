"""What-if lineups on Start/Sit (issue #171): try moves against the engine's lock-constrained recommendation.

Read-only and computed per request: nothing is stored and nothing is sent to ESPN or Sleeper. A scenario is an ordered
list of moves, "put player P in slot S", applied to the recommended lineup of app/analytics/start_sit.py. The baseline
comes from that same evaluation (its `capture` hook), so an empty scenario is the recommendation exactly, and every rule
is the engine's own:

  * slot instances and positional eligibility: start_sit.build_slot_instances and lineup.eligible (lineup.slot_positions);
  * kickoff locks: a starter whose game has kicked off keeps his slot, a bench player whose game has kicked off stays
    on the bench (start_sit.preserve_locked_assignments);
  * fantasy IR and taxi players (app/analytics/reserve_slots.py) are rostered but never start;
  * a verified NFL-ineligible player scores 0, as the engine scores him.

Move semantics, from the current scenario lineup:
  * P on the bench: P takes S and S's occupant goes to the bench;
  * P in another slot T: P and S's occupant swap; the occupant must be eligible for T (an empty S leaves T empty).

Scenario parameter (`whatif`): "w{week}t{team}", optionally followed by "~" and at most MAX_MOVES moves
"SLOT_ID.PLAYER_ID" joined by ",", e.g. "w7t3~FLEX_0.4241,RB_1.3929". The key ties a scenario to one team and week
(the league is the request's own scope). A malformed or oversized value is rejected (ScenarioSyntaxError, a 422). A
well-formed scenario that breaks any rule, or names another team or week, is not applied at all: the recommendation
is shown and every problem is listed with a plain explanation.

Totals follow the engine's completeness rule: a starter without a projection is unknown, never 0. The total is then
None, and `known_total` sums the known projections only (the UI labels it as such).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .league_rules import build_league_rules
from .lineup import FIXED_SLOTS, FLEX_SLOTS, eligible, slot_positions
from .start_sit import (ACTION_NO_CHANGE, LOCK_UNKNOWN, PlayerLock, SlotInstance, load_start_sit_evaluation,
                        no_change_strength)

CONTRACT = "start-sit-what-if/v1"
MAX_MOVES = 20
MAX_ALTERNATIVES = 3      # legal single moves listed per recommended starting slot (issue #172)
MAX_PARAM_LEN = 400

_SLOT_TYPES = "|".join(re.escape(s) for s in sorted((*FIXED_SLOTS, *FLEX_SLOTS), key=len, reverse=True))
_MOVE = rf"(?:{_SLOT_TYPES})_\d{{1,2}}\.-?\d{{1,12}}"
_PARAM = re.compile(rf"w(\d{{1,2}})t(\d{{1,6}})(?:~({_MOVE}(?:,{_MOVE}){{0,{MAX_MOVES - 1}}}))?")

# Problem codes (stable; the UI keys explanations and tests on them).
UNKNOWN_SLOT = "UNKNOWN_SLOT"
LOCKED_SLOT = "LOCKED_SLOT"
NOT_ON_ROSTER = "NOT_ON_ROSTER"
RESERVED = "RESERVED"
LOCKED_PLAYER = "LOCKED_PLAYER"
INELIGIBLE = "INELIGIBLE"
INELIGIBLE_SWAP = "INELIGIBLE_SWAP"
DUPLICATE = "DUPLICATE"
WRONG_SCOPE = "WRONG_SCOPE"


class ScenarioSyntaxError(ValueError):
    """A `whatif` value that is not a well-formed, bounded scenario."""


@dataclass(frozen=True)
class Scenario:
    week: int
    team: int
    moves: tuple[tuple[str, int], ...]

    @property
    def key(self) -> str:
        return scenario_key(self.week, self.team)


def scenario_key(week: int, team: int) -> str:
    return f"w{int(week)}t{int(team)}"


def encode(key: str, moves: list[tuple[str, int]] | tuple[tuple[str, int], ...]) -> str:
    return f"{key}~{','.join(f'{s}.{p}' for s, p in moves)}" if moves else key


def parse_scenario(raw: str | None) -> Scenario | None:
    """The scenario in a `whatif` value, None for no value. Never trusted beyond its shape: every slot and player is
    checked against this team's roster and the engine's rules when it is applied."""
    if raw is None or raw == "":
        return None
    if len(raw) > MAX_PARAM_LEN:
        raise ScenarioSyntaxError(f"The what-if scenario is longer than {MAX_PARAM_LEN} characters.")
    m = _PARAM.fullmatch(raw)
    if m is None:
        raise ScenarioSyntaxError(
            f"The what-if scenario is not in the expected form (w<week>t<team>, then up to {MAX_MOVES} moves "
            "SLOT_ID.PLAYER_ID).")
    moves: list[tuple[str, int]] = []
    if m.group(3):
        for item in m.group(3).split(","):
            slot_id, pid = item.rsplit(".", 1)
            moves.append((slot_id, int(pid)))
    return Scenario(int(m.group(1)), int(m.group(2)), tuple(moves))


# ---- the board: the engine's constraints for one team and week ------------------------------------------------------

@dataclass
class Board:
    slots: list[SlotInstance]                    # canonical starting-slot order
    baseline: dict[str, int | None]              # slot_id -> recommended player (the engine's lineup)
    players: dict[int, dict]                     # every rostered player this week (starters, bench, IR, taxi)
    values: dict[int, float | None]              # the engine's projection per player (None = unknown)
    locked_slots: dict[str, int | None] = field(default_factory=dict)   # slot_id -> his kicked-off starter
    blocked: dict[int, tuple[str, str]] = field(default_factory=dict)   # pid -> (code, why he cannot move or start)

    def slot(self, slot_id: str) -> SlotInstance | None:
        return next((s for s in self.slots if s.slot_id == slot_id), None)

    def label(self, slot: SlotInstance) -> str:
        """"RB 2" when the league has several slots of a type, else "FLEX"."""
        same = sum(1 for s in self.slots if s.slot_type == slot.slot_type)
        return f"{slot.slot_type} {slot.index + 1}" if same > 1 else slot.slot_type

    def name(self, pid: int | None) -> str:
        p = self.players.get(pid) if pid is not None else None
        return (p or {}).get("full_name") or f"Player {pid}"


def _positions(slot_type: str) -> str:
    allowed = slot_positions(slot_type)
    return allowed[0] if len(allowed) == 1 else ", ".join(allowed[:-1]) + " or " + allowed[-1]


def _problem(code: str, message: str, slot_id: str | None = None, player_id: int | None = None) -> dict:
    return {"code": code, "slot_id": slot_id, "player_id": player_id, "message": message}


def apply_move(board: Board, state: dict[str, int | None], slot_id: str, pid: int) -> tuple[dict[str, int | None], dict | None]:
    """(the lineup after putting `pid` in `slot_id`, None) or (the unchanged lineup, the problem)."""
    slot = board.slot(slot_id)
    if slot is None:
        return state, _problem(UNKNOWN_SLOT, f"This league has no {slot_id} starting slot.", slot_id, pid)
    label = board.label(slot)
    if slot_id in board.locked_slots:
        holder = board.locked_slots[slot_id]
        return state, _problem(LOCKED_SLOT, f"{label} is locked: {board.name(holder)}'s game has kicked off, so he "
                                            "keeps the slot.", slot_id, pid)
    if pid not in board.players:
        return state, _problem(NOT_ON_ROSTER, f"Player {pid} is not on this team's roster this week.", slot_id, pid)
    if pid in board.blocked:
        code, why = board.blocked[pid]
        return state, _problem(code, why, slot_id, pid)
    p = board.players[pid]
    if not eligible(slot.slot_type, p.get("position")):
        return state, _problem(INELIGIBLE, f"{board.name(pid)} ({p.get('position') or 'no position'}) cannot play "
                                           f"{label}: it takes {_positions(slot.slot_type)}.", slot_id, pid)
    if state.get(slot_id) == pid:
        return state, _problem(DUPLICATE, f"{board.name(pid)} is already in {label}.", slot_id, pid)
    occupant = state.get(slot_id)
    origin = next((sid for sid, x in state.items() if x == pid), None)
    new = dict(state)
    new[slot_id] = pid
    if origin is not None:                  # a starter changing slots swaps with the occupant
        other = board.slot(origin)
        if occupant is not None and not eligible(other.slot_type, board.players[occupant].get("position")):
            return state, _problem(
                INELIGIBLE_SWAP, f"Moving {board.name(pid)} to {label} would send {board.name(occupant)} "
                                 f"({board.players[occupant].get('position')}) to {board.label(other)}, which takes "
                                 f"{_positions(other.slot_type)}.", slot_id, pid)
        new[origin] = occupant
    return new, None


def run_moves(board: Board, moves) -> tuple[dict[str, int | None], list[dict], list[dict]]:
    """(final lineup, applied move records, problems). All or nothing: with any problem the baseline is returned."""
    state = dict(board.baseline)
    applied, problems = [], []
    for slot_id, pid in moves:
        before = state
        state, problem = apply_move(board, state, slot_id, pid)
        if problem is not None:
            problems.append(problem)
            continue
        origin = next((sid for sid, x in before.items() if x == pid), None)
        applied.append({"slot_id": slot_id, "player_id": pid, "from_slot_id": origin,
                        "displaced_player_id": before.get(slot_id),
                        "displaced_to_slot_id": origin if before.get(slot_id) is not None else None})
    if problems:
        return dict(board.baseline), [], problems
    return state, applied, []


def value_of(board: Board, pid: int) -> float | None:
    return board.values.get(pid)


def totals(board: Board, state: dict[str, int | None]) -> dict:
    """The engine's recommended-total rule: any starter without a projection makes the total unknown."""
    known, missing = 0.0, []
    for slot in board.slots:
        pid = state.get(slot.slot_id)
        if pid is None:
            continue
        v = value_of(board, pid)
        if v is None:
            missing.append(pid)
        else:
            known += v
    starters = sum(1 for s in board.slots if state.get(s.slot_id) is not None)
    return {"total": round(known, 2) if not missing else None, "known_total": round(known, 2),
            "complete": not missing, "missing_projection_ids": missing,
            "starters": starters, "starters_with_projection": starters - len(missing)}


def _delta(a: dict, b: dict) -> tuple[float | None, float]:
    """(scenario minus recommended, or None when either is incomplete; the same on known projections only)."""
    full = round(a["total"] - b["total"], 2) if a["total"] is not None and b["total"] is not None else None
    return full, round(a["known_total"] - b["known_total"], 2)


# ---- building the board from the engine's evaluation ---------------------------------------------------------------

def build_board(cap: dict) -> Board:
    slots: list[SlotInstance] = cap["slot_instances"]
    baseline: dict[str, int | None] = {s.slot_id: None for s in slots}
    for row in cap["recommended_lineup"]:
        if row.get("slot_id") in baseline:
            baseline[row["slot_id"]] = row.get("player_id")
    players: dict[int, dict] = {}
    for where, rows in (("starter", cap["starters"]), ("bench", cap["bench"]), ("ir", cap["ir"])):
        for p in rows:
            pid = p.get("espn_player_id")
            if pid is not None and pid not in players:
                players[pid] = {**p, "_where": where}
    unavailable = cap["unavailable_ids"]
    values = {pid: (0.0 if pid in unavailable else cap["projections"].get(pid)) for pid in players}
    board = Board(slots=slots, baseline=baseline, players=players, values=values)

    for item in cap["locked_starters"]:
        pid = item.get("player_id")
        slot = item.get("slot")
        if slot is not None:
            board.locked_slots[slot.slot_id] = pid
        if pid is not None:
            where = f" in {board.label(slot)}" if slot is not None else " where he is"
            board.blocked[pid] = (LOCKED_PLAYER, f"{board.name(pid)}'s game has kicked off, so he stays{where}.")
    for pid in cap["locked_bench_ids"]:
        board.blocked[pid] = (LOCKED_PLAYER, f"{board.name(pid)}'s game has kicked off, so he stays on the bench.")
    for pid in cap["taxi_ids"]:
        board.blocked[pid] = (RESERVED, f"{board.name(pid)} is on the taxi squad: rostered, but he cannot start.")
    for pid in cap["ir_ids"]:
        board.blocked[pid] = (RESERVED, f"{board.name(pid)} is in a fantasy IR slot and cannot start without a "
                                        "roster move.")
    return board


def alternatives(board: Board, key: str, limit: int = MAX_ALTERNATIVES) -> list[dict]:
    """For each recommended starting slot that is not locked, the best `limit` bench players who could legally take it
    as a single move (apply_move: eligibility, kickoff locks, IR and taxi), with the projected lineup total's change
    against the recommendation (<= 0; None when either total is unknown, `known_delta` on known projections only) and
    the what-if scenario that tries it. Closest first; ties by player id. Never a second lineup model (issue #172)."""
    base = totals(board, board.baseline)
    starting = {pid for pid in board.baseline.values() if pid is not None}
    bench = sorted(pid for pid in board.players if pid not in starting and pid not in board.blocked)
    out = []
    for slot in board.slots:
        if slot.slot_id in board.locked_slots:
            continue
        options = []
        for pid in bench:
            new, problem = apply_move(board, board.baseline, slot.slot_id, pid)
            if problem is not None:
                continue
            full, known = _delta(totals(board, new), base)
            options.append({"player_id": pid, "name": board.name(pid), "position": board.players[pid].get("position"),
                            "projection": value_of(board, pid), "delta": full, "known_delta": known,
                            "whatif": encode(key, [(slot.slot_id, pid)])})
        options.sort(key=lambda o: (o["delta"] is None, -(o["delta"] if o["delta"] is not None else o["known_delta"]),
                                    o["player_id"]))
        if options:
            out.append({"slot_id": slot.slot_id, "slot": slot.slot_type, "label": board.label(slot),
                        "starter_id": board.baseline.get(slot.slot_id), "options": options[:limit]})
    return out


def add_alternatives(ev: dict, board: Board) -> dict:
    """GET /api/start-sit: attach `alternatives` and grade a NO_CHANGE recommendation against the closest of them; the
    summary carries the decision's strength. A next-week preview lists none."""
    alts = [] if ev.get("is_future_preview") else alternatives(board, scenario_key(ev["scoring_period"], ev["team"]["id"]))
    ev["alternatives"] = alts
    summary = ev["summary"]
    complete = summary.get("projections_complete", summary.get("projection_complete", False))
    known = [o for a in alts for o in a["options"]]
    best = None
    if known:
        best = sorted(known, key=lambda o: (o["delta"] is None, -(o["delta"] if o["delta"] is not None else 0.0),
                                            o["player_id"]))[0]
    for d in ev["decisions"]:
        if d["action"] == ACTION_NO_CHANGE and d["recommendation_strength"] is None:
            d["recommendation_strength"] = no_change_strength(best, complete)
    summary["recommendation_strength"] = ev["decisions"][0]["recommendation_strength"] if ev["decisions"] else None
    return ev


def _where(state: dict[str, int | None], pid: int) -> str | None:
    return next((sid for sid, x in state.items() if x == pid), None)


def _sort_key(board: Board):
    def key(pid: int):
        v = value_of(board, pid)
        return (v is None, -(v or 0.0), board.name(pid), pid)
    return key


def evaluate_what_if(con: Any, *, team_id: int | None = None, week: int | None = None, source: str | None = None,
                     scenario: Scenario | None = None, now: datetime | None = None) -> dict:
    cap: dict = {}
    ev = load_start_sit_evaluation(con, team_id=team_id, week=week, now=now, source=source, capture=cap)
    board = build_board(cap)
    week_no, team_no = ev["scoring_period"], ev["team"]["id"]
    key = scenario_key(week_no, team_no)

    problems: list[dict] = []
    applied: list[dict] = []
    state = dict(board.baseline)
    moves: list[tuple[str, int]] = []
    if scenario is not None:
        if (scenario.week, scenario.team) != (week_no, team_no):
            problems.append(_problem(
                WRONG_SCOPE, f"This scenario was made for team {scenario.team} in week {scenario.week}, so it is not "
                             f"applied to team {team_no} in week {week_no}."))
        else:
            state, applied, problems = run_moves(board, scenario.moves)
            if not problems:
                moves = list(scenario.moves)

    rec, scen = totals(board, board.baseline), totals(board, state)
    delta, known_delta = _delta(scen, rec)
    coverage = ev["summary"].get("lineup_coverage")
    if coverage:
        # Slots no engine fills (#253 G3): both totals leave them out, so neither is complete. The starters there are
        # kept as they are in both lineups, so the delta stays exact.
        for t in (rec, scen):
            t["total"] = None
            t["projections_complete"] = t["complete"]       # the delta's condition; the totals are not complete
            t["complete"] = False
            t["unsupported_slots"] = coverage["unsupported_slots"]
    order = _sort_key(board)

    # each applied move, and the scenario without it (None when a later move depends on it)
    move_rows = []
    for i, m in enumerate(applied):
        rest = moves[:i] + moves[i + 1:]
        _, _, broken = run_moves(board, rest)
        move_rows.append({**m, "index": i, "removable": not broken,
                          "whatif_without": (encode(key, rest) if rest else None) if not broken else None,
                          "blocked_by": broken[0]["message"] if broken else None})

    can_add = len(moves) < MAX_MOVES
    slot_rows = []
    for slot in board.slots:
        sid = slot.slot_id
        pid = state.get(sid)
        options, blocked = [], []
        if sid not in board.locked_slots:
            for cand in sorted(board.players, key=order):
                if cand == pid:
                    continue
                after, problem = apply_move(board, state, sid, cand)
                if problem is not None:
                    # position mismatches are stated once per slot (eligible_positions), not once per player
                    if problem["code"] != INELIGIBLE:
                        blocked.append({"player_id": cand, "code": problem["code"], "message": problem["message"]})
                    continue
                origin = _where(state, cand)
                if origin is not None and pid is not None and board.slot(origin).slot_type == slot.slot_type:
                    continue        # two starters trading identical slots (RB 1 and RB 2) changes nothing
                t = totals(board, after)
                d, kd = _delta(t, rec)
                options.append({
                    "player_id": cand, "from_slot_id": origin, "displaced_player_id": pid,
                    "displaced_to_slot_id": origin if pid is not None else None,
                    "total": t["total"], "known_total": t["known_total"], "complete": t["complete"],
                    "delta": d, "known_delta": kd,
                    "whatif": encode(key, moves + [(sid, cand)]) if can_add else None,
                })
        slot_rows.append({
            "slot_id": sid, "slot": slot.slot_type, "label": board.label(slot), "is_flex": slot.is_flex,
            "eligible_positions": list(slot_positions(slot.slot_type)),
            "locked": sid in board.locked_slots,
            "lock_reason": (f"{board.name(board.locked_slots[sid])}'s game has kicked off, so he keeps this slot."
                            if sid in board.locked_slots else None),
            "recommended_player_id": board.baseline.get(sid), "scenario_player_id": pid,
            "changed": board.baseline.get(sid) != pid,
            "options": options, "blocked": blocked,
        })

    locks: dict = cap["locks"]
    origin_of: dict = cap["origin"]
    player_rows = []
    for pid in sorted(board.players, key=order):
        p = board.players[pid]
        lock = locks.get(p.get("pro_team")) or PlayerLock(team=p.get("pro_team"), status=LOCK_UNKNOWN)
        code, why = board.blocked.get(pid, (None, None))
        where = p["_where"]
        if where == "bench" and pid in cap["taxi_ids"]:
            where = "taxi"
        player_rows.append({
            "player_id": pid, "name": p.get("full_name"), "position": p.get("position"), "pro_team": p.get("pro_team"),
            "projected_points": value_of(board, pid), "projection_origin": origin_of.get(pid),
            "lock_status": lock.status, "kickoff_utc": lock.kickoff_utc.isoformat() if lock.kickoff_utc else None,
            "opponent": lock.opponent, "is_home": lock.is_home, "injury_status": p.get("injury_status"),
            "nfl_ineligible": pid in cap["unavailable_ids"], "roster_spot": where,
            "recommended_slot_id": _where(board.baseline, pid), "scenario_slot_id": _where(state, pid),
            "movable": code is None, "restriction": code, "restriction_reason": why,
        })

    rules = build_league_rules(cap["league_row"])
    support = next((r for r in rules["engine_support"] if r["code"] == "lineup_lock"), None)
    lock_rule = rules["lineup_lock"]
    limitations = [
        "A what-if is computed for this page only: nothing is saved and nothing is sent to the provider.",
        "Kickoff locks are the Start/Sit engine's own: stored NFL kickoff times and final scores, so exact provider "
        "lock acceptance cannot be guaranteed.",
        "A starter without a projection is unknown, not zero: a total that includes one is shown as known-only.",
    ]
    return {
        "contract": CONTRACT,
        "league": ev["league"], "team": ev["team"], "scoring_period": week_no,
        "is_future_preview": ev["is_future_preview"],
        "projection_source": ev["projection_source"], "projection_source_detail": ev["projection_source_detail"],
        "key": key,
        "scenario": {
            "requested": scenario is not None, "applied": scenario is not None and not problems,
            "whatif": encode(key, moves) if moves else None, "moves": move_rows, "move_limit": MAX_MOVES,
        },
        "errors": problems,
        "changed_slot_ids": [s.slot_id for s in board.slots if board.baseline.get(s.slot_id) != state.get(s.slot_id)],
        "slots": slot_rows,
        "players": player_rows,
        "totals": {"recommended": rec, "scenario": scen, "delta": delta, "known_delta": known_delta,
                   "complete": rec["complete"] and scen["complete"],
                   **({"lineup_coverage": coverage} if coverage else {})},
        "lineup_lock": {"rule": lock_rule, "engine_status": (support or {}).get("status"),
                        "engine_detail": (support or {}).get("detail")},
        "freshness": ev["freshness"],
        "schedule_warnings": ev["schedule_warnings"],
        "limitations": limitations,
    }

"""Exact before/after trade simulation on ESPN's weekly projections. Hypothetical only.

A trade's value is what it does to both teams' optimal starting lineups over the remaining weeks:

    roster before -> optimal lineup every week -> projected starting points
    apply trade   -> resolve roster capacity    -> optimal lineup every week again
    delta = after - before, independently for each team

No generic trade-chart values, no consolidation tax, no star premium, no playoff weighting, no
acceptance probability. Bench points never count. Hypothetical free-agent pickups are reported
separately and never enter the canonical delta. Nothing here reads or writes the database.
See docs/v2_trade_evaluator.md.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations

from .team_lineups import (PROJECTION_SOURCE, Projections, RosterPlayer, Window, best_fa_addition, compare,
                           known_total, project_team, public_weeks, r, replacement_context, roster_marginal_value,
                           starters)

SUPPORTED_SHAPES = {(1, 1), (2, 1), (1, 2), (2, 2)}
MAX_FORCED_DROPS = 3

ASSUMPTIONS = [
    "Projected points source: ESPN weekly projections, unmodified (no role, matchup, schedule, injury or market multipliers).",
    "Trade impact: our exact optimal-lineup simulation for every remaining fantasy week, using the league's real roster slots and FLEX rules.",
    "Each team's current roster is assumed to be its roster for every remaining week (no future waiver moves).",
    "Only starters count; bench points are never value.",
    "A projected 0 (bye, injury) is a real value; a missing projection is unknown and is never treated as 0.",
    "Incoming players occupy active roster spots; they are never assumed to go to IR.",
    "Players currently in an IR slot keep their ESPN projection (0 while out) and are assumed activatable when ESPN projects them; the active spot an activation would need is not modelled.",
    "Regular season and playoffs are reported separately and never weighted against each other.",
]
LIMITATIONS = [
    "ESPN projections can be wrong.",
    "Projections are not adjusted by our role/usage context yet; that context is shown for explanation only.",
    "Future injuries are unknowable.",
    "Future waiver acquisitions are not guaranteed; hypothetical FA replacements are shown separately and never included in the trade delta.",
    "ESPN undroppable-list restrictions are not modeled.",
    "Manager acceptance is not predicted.",
]


class TradeError(ValueError):
    pass


@dataclass
class LeagueState:
    """Everything the evaluator needs, loaded once (see trade_data.load_league_state)."""
    season: int
    slots: list[str]                               # starting slots (lineup.lineup_slots)
    final_week: int
    last_regular: int | None
    start_week: int                                # default evaluation start (trade_data.evaluation_start_week)
    rosters: dict[int, list[RosterPlayer]]
    free_agents: list[RosterPlayer]
    projections: Projections
    active_capacity: int                           # starting + bench slots (IR excluded)
    position_limits: dict[str, int] = field(default_factory=dict)   # max rostered per position; {} = not enforced
    team_names: dict[int, str] = field(default_factory=dict)
    league_id: int | None = None
    start_week_reason: dict = field(default_factory=dict)
    undroppable_list_in_use: bool | None = None
    first_projection_week: int | None = None       # earliest week whose projections were loaded

    def window(self, start_week: int | None = None) -> Window:
        start = self.start_week if start_week is None else start_week
        return Window(tuple(range(start, self.final_week + 1)), self.last_regular)


# --------------------------------------------------------------------- roster rules --

def _active(players: list[RosterPlayer]) -> int:
    return sum(1 for p in players if not p.on_ir)


def _position_counts(players: list[RosterPlayer]) -> Counter:
    return Counter(p.position for p in players)


def _legal(players: list[RosterPlayer], state: LeagueState, before: list[RosterPlayer]) -> bool:
    """Active roster within capacity and every position within ESPN's limit. A limit the team
    already exceeded before the trade is not blamed on the trade."""
    if _active(players) > max(state.active_capacity, _active(before)):
        return False
    counts, prior = _position_counts(players), _position_counts(before)
    return all(counts[pos] <= max(lim, prior[pos]) for pos, lim in state.position_limits.items())


def resolve_roster(state: LeagueState, window: Window, before: list[RosterPlayer], after: list[RosterPlayer],
                   incoming: set[int]) -> dict:
    """Forced drops for an over-capacity post-trade roster: enumerate legal removals and keep the one
    that maximises the remaining projected starting-lineup total (least costly in actual lineup
    terms, not the lowest value or raw projection). Incoming players are never the default drop;
    if dropping one would be strictly cheaper, the trade is flagged as bringing a redundant asset."""
    if _legal(after, state, before):
        return {"forced_drops": [], "lineup_cost": 0.0, "redundant_incoming_asset": None, "basis": None}
    full = project_team(after, state.slots, window, state.projections)
    full_total = known_total(full)
    pool = sorted(after, key=lambda p: p.espn_player_id)
    for k in range(1, MAX_FORCED_DROPS + 1):
        best: dict[bool, tuple] = {}
        for combo in combinations(pool, k):
            rest = [p for p in after if p not in combo]
            if not _legal(rest, state, before):
                continue
            team = project_team(rest, state.slots, window, state.projections)
            has_incoming = any(p.espn_player_id in incoming for p in combo)
            key = (round(known_total(team), 9), tuple(-p.espn_player_id for p in combo))
            if has_incoming not in best or key > best[has_incoming][0]:
                best[has_incoming] = (key, combo, team)
        if best:
            break
    else:
        raise TradeError("no legal roster exists after this trade within the forced-drop limit")
    chosen = best.get(False) or best[True]
    _, combo, team = chosen
    complete = full["summary"]["projection_complete"] and team["summary"]["projection_complete"]
    cost = full_total - known_total(team)
    redundant = None
    if False in best and True in best and best[True][0][0] > best[False][0][0]:
        rc = best[True][1]
        redundant = {"player_ids": [p.espn_player_id for p in rc if p.espn_player_id in incoming],
                     "names": [p.name for p in rc if p.espn_player_id in incoming],
                     "lineup_points_saved_by_cutting_it": r(best[True][0][0] - best[False][0][0]),
                     "note": "redundant incoming asset: cutting a newly acquired player would be the cheapest legal drop"}
    return {
        "forced_drops": [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                          "is_incoming": p.espn_player_id in incoming} for p in combo],
        "lineup_cost": r(cost) if complete else None, "known_lineup_cost": r(cost),
        "redundant_incoming_asset": redundant,
        "incoming_dropped_because_no_alternative": False not in best,
        "basis": "complete ESPN projections" if complete else "known ESPN projections only (some missing)",
    }


# ---------------------------------------------------------------------- explanation --

def _slot_labels(assignment: list[dict]) -> dict[int, str]:
    """player_id -> slot label with an ordinal when the slot repeats (RB1, RB2, FLEX)."""
    totals = Counter(a["slot"] for a in assignment)
    seen: Counter = Counter()
    out = {}
    for a in assignment:
        seen[a["slot"]] += 1
        if a["player_id"] is not None:
            out[a["player_id"]] = f"{a['slot']}{seen[a['slot']]}" if totals[a["slot"]] > 1 else a["slot"]
    return out


def _usage(team: dict, pid: int) -> dict:
    weeks, slots = [], Counter()
    for w in team["weeks"]:
        lab = _slot_labels(w["assignment"]).get(pid)
        if lab:
            weeks.append(w["week"])
            slots[lab] += 1
    return {"weeks_started": weeks, "slots": dict(slots.most_common())}


def _fmt_usage(name: str, pos: str | None, u: dict, n: int) -> str:
    who = f"{name} ({pos})" if pos else name
    if not u["weeks_started"]:
        return f"{who} does not start in any of the {n} remaining weeks"
    slots = ", ".join(f"{s} x{c}" for s, c in u["slots"].items())
    if len(u["weeks_started"]) <= 2 and n > 2:
        return f"{who} starts only in week{'s' if len(u['weeks_started']) > 1 else ''} " \
               f"{', '.join(map(str, u['weeks_started']))} ({slots})"
    return f"{who} starts {len(u['weeks_started'])} of {n} weeks ({slots})"


def _side(state: LeagueState, window: Window, team_id: int, sends: list[int], receives: list[RosterPlayer],
          names: dict[int, RosterPlayer]) -> dict:
    roster = state.rosters[team_id]
    incoming = {p.espn_player_id for p in receives}
    before = list(roster)
    pre = [p for p in roster if p.espn_player_id not in set(sends)] + [replace(p, on_ir=False) for p in receives]
    res = resolve_roster(state, window, before, pre, incoming)
    dropped = {d["espn_player_id"] for d in res["forced_drops"]}
    after = [p for p in pre if p.espn_player_id not in dropped]
    bt = project_team(before, state.slots, window, state.projections)
    at = project_team(after, state.slots, window, state.projections)
    cmp = compare(bt, at, window)
    n = len(window.weeks)

    weekly = []
    for row, b, a in zip(cmp["weeks"], bt["weeks"], at["weeks"]):
        sb, sa = starters(b), starters(a)
        weekly.append({**row, "entered": sorted(sa - sb), "exited": sorted(sb - sa)})

    incoming_usage = [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                       **_usage(at, p.espn_player_id)} for p in receives if p.espn_player_id not in dropped]
    outgoing_usage = [{"espn_player_id": pid, "name": names[pid].name, "position": names[pid].position,
                       **_usage(bt, pid)} for pid in sends]
    displaced = []
    for p in after:
        if p.espn_player_id in incoming:
            continue
        ub, ua = _usage(bt, p.espn_player_id), _usage(at, p.espn_player_id)
        lost = sorted(set(ub["weeks_started"]) - set(ua["weeks_started"]))
        gained = sorted(set(ua["weeks_started"]) - set(ub["weeks_started"]))
        if lost or gained:
            displaced.append({"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                              "starts_lost": lost, "starts_gained": gained})

    notes = [f"Receives {_fmt_usage(u['name'] or str(u['espn_player_id']), u['position'], u, n)}"
             for u in incoming_usage]
    notes += [f"Sends {_fmt_usage(u['name'] or str(u['espn_player_id']), u['position'], u, n)} (before the trade)"
              for u in outgoing_usage]
    for d in displaced:
        parts = ([f"loses {len(d['starts_lost'])}"] if d["starts_lost"] else []) + \
                ([f"gains {len(d['starts_gained'])}"] if d["starts_gained"] else [])
        notes.append(f"{d['name']} ({d['position']}) {' / '.join(parts)} start(s)")
    for d in res["forced_drops"]:
        notes.append(f"Must drop {d['name']} ({d['position']}) to stay within roster limits")

    open_before = max(0, state.active_capacity - _active(before))
    open_after = max(0, state.active_capacity - _active(after))
    fill = None
    if open_after > open_before:
        fill = best_fa_addition(after, state.free_agents, state.slots, window, state.projections)
        fill["note"] = "best hypothetical FA fill for the opened roster spot; not included in the canonical delta"

    first = res["forced_drops"][0] if res["forced_drops"] else None
    return {
        "team_id": team_id, "team_name": state.team_names.get(team_id),
        "sends": sends, "receives": sorted(incoming),
        "before": bt["summary"], "after": at["summary"],
        "deltas": {k: v for k, v in cmp.items() if k != "weeks"},
        "weekly": weekly,
        "lineups": {"before": public_weeks(bt), "after": public_weeks(at)},
        "roster": {
            "active_capacity": state.active_capacity, "active_before": _active(before), "active_after": _active(after),
            "open_active_roster_spots": open_after, "open_active_roster_spots_before": open_before,
            "forced_drops": res["forced_drops"],
            "forced_drop_id": first["espn_player_id"] if first else None,
            "forced_drop_name": first["name"] if first else None,
            "forced_drop_lineup_cost": res["lineup_cost"],
            "forced_drop_known_lineup_cost": res.get("known_lineup_cost", 0.0),
            "forced_drop_basis": res["basis"],
            "redundant_incoming_asset": res["redundant_incoming_asset"],
            "incoming_dropped_because_no_alternative": res.get("incoming_dropped_because_no_alternative", False),
        },
        "incoming_usage": incoming_usage, "outgoing_usage": outgoing_usage, "displaced": displaced,
        "fit_notes": notes,
        "hypothetical_fa_fill": fill,
        "incomplete": {"missing_weeks": sorted(set(bt["summary"]["missing_weeks"]) | set(at["summary"]["missing_weeks"])),
                       "missing_players": sorted(set(bt["summary"]["missing_players"]) | set(at["summary"]["missing_players"]))},
        "_before_team": bt,
    }


def result_label(a: float | None, b: float | None) -> str:
    """Zero is the dividing line; no arbitrary threshold."""
    if a is None or b is None:
        return "INCOMPLETE_DATA"
    sa = "GAIN" if a > 0 else "LOSS" if a < 0 else "UNCHANGED"
    sb = "GAIN" if b > 0 else "LOSS" if b < 0 else "UNCHANGED"
    if sa == sb == "GAIN":
        return "MUTUAL_PROJECTED_GAIN"
    if sa == sb == "LOSS":
        return "BOTH_LOSE"
    if sa == sb == "UNCHANGED":
        return "NO_PROJECTED_CHANGE"
    return f"TEAM_A_{sa}_TEAM_B_{sb}"


# ------------------------------------------------------------------------ evaluator --

def validate(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int]) -> None:
    if team_a == team_b:
        raise TradeError("teams must be different")
    for t in (team_a, team_b):
        if t not in state.rosters:
            raise TradeError(f"unknown team {t}")
    if (len(a_sends), len(b_sends)) not in SUPPORTED_SHAPES:
        raise TradeError("each side must send 1 or 2 players (1-for-1, 2-for-1, 1-for-2, 2-for-2)")
    ids = list(a_sends) + list(b_sends)
    if len(set(ids)) != len(ids):
        raise TradeError("a player appears more than once")
    for team, sends in ((team_a, a_sends), (team_b, b_sends)):
        own = {p.espn_player_id for p in state.rosters[team]}
        for pid in sends:
            if pid not in own:
                raise TradeError(f"player {pid} is not on team {team}'s current roster")


def evaluate_trade(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int],
                   start_week: int | None = None) -> dict:
    """Hypothetical before/after evaluation for both teams. Never mutates `state`."""
    a_sends, b_sends = [int(x) for x in a_sends], [int(x) for x in b_sends]
    validate(state, team_a, team_b, a_sends, b_sends)
    if start_week is not None and not (1 <= start_week <= state.final_week):
        raise TradeError(f"start_week must be between 1 and {state.final_week}")
    if start_week is not None and state.first_projection_week and start_week < state.first_projection_week:
        raise TradeError(f"start_week must be >= {state.first_projection_week} (earliest loaded projection week)")
    window = state.window(start_week)
    names = {p.espn_player_id: p for t in (team_a, team_b) for p in state.rosters[t]}
    a_out = [names[pid] for pid in a_sends]
    b_out = [names[pid] for pid in b_sends]
    side_a = _side(state, window, team_a, a_sends, b_out, names)
    side_b = _side(state, window, team_b, b_sends, a_out, names)

    players = []
    for team, sends, side in ((team_a, a_sends, side_a), (team_b, b_sends, side_b)):
        roster = state.rosters[team]
        base = side.pop("_before_team")
        for pid in sends:
            p = names[pid]
            players.append({
                "espn_player_id": pid, "name": p.name, "position": p.position, "pro_team": p.pro_team,
                "injury_status": p.injury_status, "on_ir": p.on_ir, "from_team_id": team,
                "to_team_id": team_b if team == team_a else team_a,
                "weekly_projection": {w: state.projections.get(pid, {}).get(w) for w in window.weeks},
                "marginal_value_on_current_team": roster_marginal_value(roster, pid, state.slots, window,
                                                                        state.projections, base),
                "replacement_context": replacement_context(roster, pid, state.free_agents, state.slots, window,
                                                           state.projections, base),
            })

    da, db = side_a["deltas"], side_b["deltas"]
    a_ros, b_ros = da["ros_delta"], db["ros_delta"]

    def combo(key):
        x, y = da[key], db[key]
        return (r(x + y) if x is not None and y is not None else None,
                r(min(x, y)) if x is not None and y is not None else None)

    comb, min_side = combo("ros_delta")
    reg_c, reg_m = combo("regular_season_delta")
    po_c, po_m = combo("playoff_delta")
    return {
        "evaluation": {"start_week": window.start, "final_week": window.final,
                       "last_regular_season_week": window.last_regular, "weeks": list(window.weeks),
                       "start_week_overridden": start_week is not None,
                       "start_week_reason": state.start_week_reason if start_week is None
                       else {"reason": f"explicit start_week override ({start_week})"}},
        "trade": {"shape": f"{len(a_sends)}-for-{len(b_sends)}", "team_a_id": team_a, "team_b_id": team_b,
                  "team_a_sends": a_sends, "team_b_sends": b_sends},
        "team_a": side_a, "team_b": side_b,
        "result": {"label": result_label(a_ros, b_ros), "team_a_ros_delta": a_ros, "team_b_ros_delta": b_ros,
                   "combined_ros_delta": comb, "min_side_ros_delta": min_side,
                   "combined_regular_season_delta": reg_c, "min_side_regular_season_delta": reg_m,
                   "combined_playoff_delta": po_c, "min_side_playoff_delta": po_m,
                   "regular_season_label": result_label(da["regular_season_delta"], db["regular_season_delta"]),
                   "playoff_label": result_label(da["playoff_delta"], db["playoff_delta"]),
                   "note": "Labels and deltas come straight from the exact lineup simulation on ESPN's baseline; "
                           "combined/min-side are descriptive ranking fields, not a score or acceptance probability."},
        "players": players,
        "projection_source": PROJECTION_SOURCE,
        "trade_impact_source": "exact optimal-lineup simulation",
        "assumptions": ASSUMPTIONS,
        "limitations": LIMITATIONS,
    }

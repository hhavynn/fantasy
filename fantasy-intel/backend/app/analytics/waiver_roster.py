"""Pure, snapshot-based add/drop planning. No scoring or availability semantics live here."""
from __future__ import annotations

import json
from collections import Counter

from ..espn.constants import BLANK_SLOT_LABEL, DEFAULT_POSITION_MAP, IR_SLOT
from .lineup import (FIXED_SLOTS, FLEX_SLOTS, assign_lineup, dual_positions, eligible, lineup_total, positions_of,
                     slot_positions)
from .reserve_slots import ESPN_IR_STATUSES, TAXI, active_capacity, ir_rules, ir_slot_eligible, is_reserved, is_taxi
from .team_lineups import STREAMER_IDS


# A fantasy IR slot may hold only a player with an IR-eligible injury status; while an IR occupant has another (healthy
# or unknown) status, the provider blocks adds until he is moved (plan_roster_move, add_drop). ESPN's statuses are
# fixed; a Sleeper league's come from its own settings (reserve_slots.ir_rules, issue #253 G7), and a status those
# settings do not decide is unknown, never assumed eligible or not.
IR_ELIGIBLE_STATUSES = ESPN_IR_STATUSES


def object_value(value) -> dict:
    return json.loads(value) if isinstance(value, str) else (value or {})


def roster_constraints(league: dict) -> dict:
    slots = object_value(league.get("roster_slots"))
    raw = object_value(league.get("settings_raw"))
    # ESPN's blank-labelled slot (id 22) is a recognised non-starter (issue #253 G12: the rules page counts it as bench),
    # but whether it holds a roster spot is unverified, so a league that starts any keeps its capacity unknown: an add is
    # never called legal on the strength of a slot nobody has confirmed.
    supported = set(FIXED_SLOTS) | set(FLEX_SLOTS) | {"BE", "IR", TAXI, BLANK_SLOT_LABEL}
    valid = bool(slots) and not any(k not in supported and v for k, v in slots.items())
    valid = valid and all(isinstance(v, int) and v >= 0 for v in slots.values())
    blank_slots = valid and bool(slots.get(BLANK_SLOT_LABEL))
    rs = raw.get("rosterSettings") or {}
    warnings = [] if rs else ["Raw roster restrictions were not supplied; verify positional caps."]
    if blank_slots:
        warnings.append("This league has ESPN's blank-labelled roster slot; whether it holds a roster spot is unverified, "
                        "so roster capacity is not established.")
    limits = {}
    for key, val in (rs.get("positionLimits") or {}).items():
        pos = DEFAULT_POSITION_MAP.get(int(key)) if str(key).isdigit() else key
        if pos in FIXED_SLOTS and val is not None and int(val) > 0:
            limits[pos] = int(val)
    return {"capacity": active_capacity(slots) if valid and not blank_slots else None,   # IR and taxi slots are not active
            "position_limits": limits, "snapshot_complete": True,
            "settings_complete": bool(rs),
            "ir_rules": ir_rules(raw),                                      # None: ESPN's fixed IR statuses
            "undroppable_membership_unknown": bool(rs.get("isUsingUndroppableList")),
            "warnings": warnings}


def competing_positions(slots: list[str], positions, players=()) -> set[str]:
    """The slot group of `positions`: every position sharing a starting slot with one of them, transitively (an OP slot
    links QB with RB/WR/TE; K and D/ST stand alone), and every position a player in `players` may fill along with one
    of them (lineup.dual_positions: an RB/WR links RB and WR, issue #253 G11). assign_lineup decomposes into these
    groups (fixed slots fill per position, every flex slot takes RB or WR, and a dual player starts at one of his
    positions), so a lineup change confined to one group's players moves only that group's slots. Empty when no slot
    takes any of `positions`."""
    links = [d for d in (dual_positions(p, slots) for p in players) if d]
    comp, frontier = set(), [p for p in positions if p]
    while frontier:
        cur = frontier.pop()
        nxt = [q for s in set(slots) if eligible(s, cur) for q in slot_positions(s)]
        nxt += [q for d in links if cur in d for q in d]
        for q in nxt:
            if q not in comp:
                comp.add(q)
                frontier.append(q)
    return comp


def unknown_value_players(players: list[dict], slots: list[str], positions=None) -> list[dict]:
    """Available players who could fill one of `slots` but have no historical value (raw_value None), limited to the
    slot group of `positions` when given. The lineup solver orders such a player as 0, so while one exists in a group
    that group's lineup value is unknown, not a sum that counts him as 0 (frontend/PRODUCT.md: missing is never zero)."""
    group = competing_positions(slots, positions, players) if positions is not None else None
    return [p for p in players if p.get("available") and p.get("raw_value") is None
            and any(eligible(s, p) for s in slots) and (group is None or not positions_of(p).isdisjoint(group))]


def group_total(assignment: list[dict], group: set[str]) -> float | None:
    """Lineup value of the slots in a slot group (competing_positions). team_state's lineup rows carry value None for a
    player without a value, so a whole-lineup lineup_total is not defined there; callers sum the group a change can
    move, after checking it has no unknown player (unknown_value_players). None if a None row is in the group anyway."""
    rows = [a for a in assignment if any(eligible(a["slot"], q) for q in group)]
    if any(a["value"] is None for a in rows):
        return None
    return sum(a["value"] for a in rows)


def player_names(players: list[dict], limit: int = 3) -> str:
    names = [p.get("full_name") or f"player {p['espn_player_id']}" for p in players]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _holes(players, slots):
    return sum(a["player_id"] is None for a in assign_lineup(players, slots))


def projected_players(players: list[dict], week: int) -> list[dict]:
    """For future comparisons use supplied weekly points, never invent missing projections."""
    return [{**p, "value": p.get("projections", {}).get(week),
             "available": p.get("projections", {}).get(week) is not None and week not in p.get("bye_weeks", [])
             and not is_reserved(p)} for p in players]


def _streamers(stream: dict | None, slots: list[str], week: int) -> list[dict]:
    """The K/D/ST waiver streamer for this week (team_lineups.STREAM_NOTE): a slot filler both rosters share."""
    return [{"espn_player_id": STREAMER_IDS[pos], "position": pos, "value": per[week], "available": True}
            for pos, per in (stream or {}).items() if pos in slots and per.get(week) is not None]


def forward_effect(before: list[dict], after: list[dict], slots: list[str], weeks: list[int],
                   stream: dict | None = None) -> dict:
    """Projected starting-lineup gain of `after` over `before`, week by week. `stream` (position -> week ->
    baseline) lets either lineup stream K/D/ST in those weeks, so holding one is worth only what it adds
    above streaming there."""
    gains, missing = [], []
    for w in weeks:
        relevant = [p for p in before + after if any(eligible(s, p) for s in slots)
                    and not is_reserved(p) and w not in p.get("bye_weeks", [])]
        if any(p.get("projections", {}).get(w) is None for p in relevant):
            missing.append(w)
            continue
        extra = _streamers(stream, slots, w)
        a = assign_lineup(projected_players(before, w) + extra, slots)
        b = assign_lineup(projected_players(after, w) + extra, slots)
        gains.append({"week": w, "gain": round(lineup_total(b) - lineup_total(a), 3),
                      "holes_before": sum(x["player_id"] is None for x in a),
                      "holes_after": sum(x["player_id"] is None for x in b)})
    return {"weeks": gains, "missing_weeks": missing,
            "total_gain": round(sum(g["gain"] for g in gains), 3) if weeks and not missing else None}


def plan_roster_move(state: dict, candidate: dict, constraints: dict | None = None) -> dict:
    constraints = constraints if constraints is not None else roster_constraints({
        "roster_slots": state.get("roster_slots"), "settings_raw": {"rosterSettings": {"source": "explicit_slots"}}})
    players, slots = state["players"], state["slots"]
    active = [p for p in players if not is_reserved(p)]
    capacity = constraints.get("capacity")
    limits = constraints.get("position_limits", {})
    warnings = list(constraints.get("warnings", []))
    result = {"status": "UNKNOWN", "drop_required": None, "drop": None, "alternatives": [], "excluded": [],
              "active_count": len(active), "capacity": capacity, "position_limits": limits,
              "warnings": warnings, "summary": "Roster legality cannot be established from the available settings."}
    if capacity is None or not slots or not constraints.get("snapshot_complete", True):
        return result
    verdicts = [ir_slot_eligible(p.get("injury_status"), constraints.get("ir_rules"))
                for p in players if p.get("lineup_slot_id") == IR_SLOT]
    if any(v is False for v in verdicts):
        result["summary"] = ("An IR occupant has healthy or unknown eligibility; resolve or verify IR eligibility in ESPN before adding."
                             if constraints.get("ir_rules") is None else
                             "An IR occupant's injury status does not allow the IR slot under this league's rules; move him "
                             "or verify his IR eligibility before adding.")
        return result
    if any(v is None for v in verdicts):
        result["summary"] = ("An IR occupant's injury status is not decided by this league's synced IR rules; verify "
                             "his IR eligibility before adding.")
        return result
    if constraints.get("settings_complete") is False:
        return result
    counts = Counter(p.get("position") for p in players)

    def legal(removed=None):
        if len(active) + 1 - bool(removed) > capacity:
            return False
        after = counts.copy()
        after[candidate["position"]] += 1
        if removed:
            after[removed["position"]] -= 1
        return all(after[pos] <= cap for pos, cap in limits.items())

    # Direct pure callers with explicit slot counts can establish capacity without raw ESPN settings.
    if legal():
        result.update(status="ADD_WITHOUT_DROP", drop_required=False,
                      summary=f"Add without dropping: {len(active)} of {capacity} active spots occupied.")
        return result
    result["drop_required"] = True
    if candidate["position"] in limits and limits[candidate["position"]] == 0:
        result.update(status="INCOMPATIBLE", summary="This position is not permitted by the known roster limits.")
        return result
    base_holes = _holes(players, slots)
    base_lineup = assign_lineup(players, slots)
    starters = {a["player_id"] for a in base_lineup if a["player_id"] is not None}
    weeks = state.get("weeks", [])
    possible = 0
    for p in players:
        pid = p["espn_player_id"]
        reason = None
        if p.get("lineup_slot_id") == IR_SLOT:
            reason = "IR removal does not free an active spot"
        elif is_taxi(p):
            reason = "Taxi-squad removal does not free an active spot"
        elif p.get("locked") or p.get("undroppable"):
            reason = "Known locked or undroppable player"
        elif not legal(p):
            reason = "Single drop does not satisfy capacity and positional limits"
        if reason:
            result["excluded"].append({"espn_player_id": pid, "reason": reason})
            continue
        possible += 1
        remaining = [q for q in players if q["espn_player_id"] != pid]
        after = remaining + [candidate]
        forward = forward_effect(players, after, slots, weeks, state.get("stream"))
        projected_improvement = forward["total_gain"] is not None and forward["total_gain"] > 0
        short_term = False
        if candidate["position"] in ("K", "D/ST") and p["position"] == candidate["position"] and weeks:
            stream_forward = forward_effect(players, after, [s for s in slots if s == candidate["position"]], weeks[:1])
            short_term = stream_forward["total_gain"] is not None and stream_forward["total_gain"] >= 1
        # The add and this drop move only the slot groups of their two positions: the net gain and removal cost are
        # exact sums over that group, or unknown while it holds a player without a value (never counted as 0).
        group = competing_positions(slots, positions_of(candidate) | positions_of(p), players + [candidate])
        unknown = unknown_value_players(players + [candidate], slots, group)
        base_total = group_total(base_lineup, group) if not unknown else None
        net = (group_total(assign_lineup(after, slots), group) - base_total) if base_total is not None else None
        raw = p.get("raw_value")
        cand_raw = candidate.get("raw_value")
        if _holes(after, slots) > base_holes:
            reason = "Would leave a necessary starting slot unfillable"
        elif any(w["holes_after"] > w["holes_before"] for w in forward["weeks"]):
            reason = "Would create a necessary future starting-slot hole, including bye coverage"
        elif raw is None:
            reason = "Drop value is unknown; cannot defend the sacrifice"
        elif net is None and pid in starters and not projected_improvement and not short_term:
            reason = f"Lineup cost unknown (no value for {player_names(unknown)}); a current starter is not dropped"
        elif (((net is not None and net < -1e-6) and not projected_improvement) or (forward["total_gain"] is not None and forward["total_gain"] < -1e-6)) and not short_term:
            reason = "Transaction reduces known current or future starting lineup value"
        elif ((cand_raw is None and not projected_improvement) or (cand_raw is not None and cand_raw < raw and (net is None or net <= 0) and not projected_improvement)) and not short_term:
            reason = "No evidence the acquisition justifies giving up this player"
        elif constraints.get("undroppable_membership_unknown") and p.get("undroppable") is not False:
            reason = "Undroppable list enabled but membership is unavailable"
        if reason:
            result["excluded"].append({"espn_player_id": pid, "reason": reason})
            continue
        removal_loss = (max(0, base_total - group_total(assign_lineup(remaining, slots), group))
                        if base_total is not None else None)
        # Coverage is observed eligibility, not an arbitrary positional value penalty.
        coverage = sorted({s for s in slots if eligible(s, p)})
        same = [q for q in remaining if q.get("position") == p["position"] and q.get("available")]
        redundant_streamer = p["position"] in ("K", "D/ST") and same and removal_loss == 0
        comparable_weeks = [w for w in weeks if all(q.get("projections", {}).get(w) is not None
                            or w in q.get("bye_weeks", []) or is_reserved(q)
                            for q in players if any(eligible(s, q) for s in slots))]
        future_starts = sum(any(a["player_id"] == pid for a in assign_lineup(projected_players(players, w), slots))
                            for w in comparable_weeks)
        raw100 = p.get("raw_value_100")
        value_text = f"{raw100:.1f}/100" if raw100 is not None else f"{raw:.1f}/10"
        sacrifice = [f"Give up {p.get('full_name') or pid}: historical value {value_text}.",
                     f"Coverage: {', '.join(coverage) or 'no configured starting slot'}; {len(same)} other available {p['position']}s remain.",
                     f"Removal alone costs {removal_loss:.1f} current lineup value." if removal_loss is not None else
                     f"Removal cost to the current lineup is unknown: no historical value for {player_names(unknown)} "
                     "(unknown is not zero)."]
        if weeks:
            sacrifice.append(f"Starts in {future_starts} of {len(comparable_weeks)} comparable future lineups; incomplete weeks are not treated as zero.")
        if short_term and forward["total_gain"] is not None and forward["total_gain"] < 0:
            sacrifice.append(f"Short-term swap sacrifices {-forward['total_gain']:.1f} projected points over the complete future horizon; reconsider next week.")
        result["alternatives"].append({"espn_player_id": pid, "name": p.get("full_name"), "position": p["position"],
                                       "value": raw, "value_known": True, "sacrifice": sacrifice,
                                       "net_lineup_gain": round(net, 3) if net is not None else None, "forward": forward,
                                       # Known nets and removal costs rank ahead of unknown ones.
                                       "sort": (0 if short_term else 1, -(forward["total_gain"] or 0),
                                                net is None, -(net or 0), removal_loss is None, removal_loss or 0,
                                                future_starts,
                                                0 if redundant_streamer else 1, raw, pid)})
    result["alternatives"].sort(key=lambda d: d.pop("sort"))
    if result["alternatives"]:
        drop = result["alternatives"][0]
        result.update(status="ADD_WITH_DROP", drop=drop,
                      summary=f"If acquired, drop {drop['name']} to satisfy active capacity and positional limits.")
    else:
        result.update(status="MOVE_REQUIRED" if possible else "INCOMPATIBLE",
                      summary="A roster move is required; no defensible legal single drop was identified." if possible
                      else "No single active-player drop can satisfy the known roster restrictions.")
    return result

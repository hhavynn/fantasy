"""Pure, snapshot-based add/drop planning. No scoring or availability semantics live here."""
from __future__ import annotations

import json
from collections import Counter

from ..espn.constants import DEFAULT_POSITION_MAP, IR_SLOT
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, assign_lineup, eligible, lineup_total
from .reserve_slots import TAXI, active_capacity, is_reserved, is_taxi
from .team_lineups import STREAMER_IDS


# A fantasy IR slot may hold only a player with one of these provider injury statuses; while an IR occupant has any
# other (healthy or unknown) status, the provider blocks adds until he is moved (plan_roster_move, add_drop).
IR_ELIGIBLE_STATUSES = ("IR", "INJURY_RESERVE", "OUT")


def ir_slot_eligible(injury_status: str | None) -> bool:
    return injury_status in IR_ELIGIBLE_STATUSES


def object_value(value) -> dict:
    return json.loads(value) if isinstance(value, str) else (value or {})


def roster_constraints(league: dict) -> dict:
    slots = object_value(league.get("roster_slots"))
    raw = object_value(league.get("settings_raw"))
    supported = set(FIXED_SLOTS) | set(FLEX_ELIGIBILITY) | {"BE", "IR", TAXI}
    valid = bool(slots) and not any(k not in supported and v for k, v in slots.items())
    valid = valid and all(isinstance(v, int) and v >= 0 for v in slots.values())
    rs = raw.get("rosterSettings") or {}
    limits = {}
    for key, val in (rs.get("positionLimits") or {}).items():
        pos = DEFAULT_POSITION_MAP.get(int(key)) if str(key).isdigit() else key
        if pos in FIXED_SLOTS and val is not None and int(val) > 0:
            limits[pos] = int(val)
    return {"capacity": active_capacity(slots) if valid else None,       # IR and taxi slots are not active
            "position_limits": limits, "snapshot_complete": True,
            "settings_complete": bool(rs),
            "undroppable_membership_unknown": bool(rs.get("isUsingUndroppableList")),
            "warnings": ([] if rs else ["Raw roster restrictions were not supplied; verify positional caps."])}


def unknown_value_players(players: list[dict], slots: list[str]) -> list[dict]:
    """Available players who could fill one of `slots` but have no historical value (raw_value None). The lineup
    solver orders such a player as 0, so while any exists a lineup value total or difference is unknown, not a sum
    that counts him as 0 (frontend/PRODUCT.md: missing is never zero)."""
    return [p for p in players if p.get("available") and p.get("raw_value") is None
            and any(eligible(s, p.get("position")) for s in slots)]


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
        relevant = [p for p in before + after if any(eligible(s, p.get("position")) for s in slots)
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
    if any(p.get("lineup_slot_id") == IR_SLOT and not ir_slot_eligible(p.get("injury_status")) for p in players):
        result["summary"] = "An IR occupant has healthy or unknown eligibility; resolve or verify IR eligibility in ESPN before adding."
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
    # Lineup value arithmetic needs every possible starter's value (the candidate's too); otherwise the net gain and
    # removal cost are unknown, never sums that count a missing value as 0.
    unknown = unknown_value_players(players + [candidate], slots)
    base_total = lineup_total(state["lineup"]) if not unknown else None
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
        net = lineup_total(assign_lineup(after, slots)) - base_total if base_total is not None else None
        raw = p.get("raw_value")
        cand_raw = candidate.get("raw_value")
        if _holes(after, slots) > base_holes:
            reason = "Would leave a necessary starting slot unfillable"
        elif any(w["holes_after"] > w["holes_before"] for w in forward["weeks"]):
            reason = "Would create a necessary future starting-slot hole, including bye coverage"
        elif raw is None:
            reason = "Drop value is unknown; cannot defend the sacrifice"
        elif (((net is not None and net < -1e-6) and not projected_improvement) or (forward["total_gain"] is not None and forward["total_gain"] < -1e-6)) and not short_term:
            reason = "Transaction reduces known current or future starting lineup value"
        elif ((cand_raw is None and not projected_improvement) or (cand_raw is not None and cand_raw < raw and (net is None or net <= 0) and not projected_improvement)) and not short_term:
            reason = "No evidence the acquisition justifies giving up this player"
        elif constraints.get("undroppable_membership_unknown") and p.get("undroppable") is not False:
            reason = "Undroppable list enabled but membership is unavailable"
        if reason:
            result["excluded"].append({"espn_player_id": pid, "reason": reason})
            continue
        removal_loss = (max(0, base_total - lineup_total(assign_lineup(remaining, slots)))
                        if base_total is not None else None)
        # Coverage is observed eligibility, not an arbitrary positional value penalty.
        coverage = sorted({s for s in slots if eligible(s, p["position"])})
        same = [q for q in remaining if q.get("position") == p["position"] and q.get("available")]
        redundant_streamer = p["position"] in ("K", "D/ST") and same and removal_loss == 0
        comparable_weeks = [w for w in weeks if all(q.get("projections", {}).get(w) is not None
                            or w in q.get("bye_weeks", []) or is_reserved(q)
                            for q in players if any(eligible(s, q.get("position")) for s in slots))]
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
                                       # An unknown net / removal cost is unknown for every alternative alike (it
                                       # depends on the roster and candidate, not the drop), so 0 only ties them here.
                                       "sort": (0 if short_term else 1, -(forward["total_gain"] or 0), -(net or 0),
                                                removal_loss or 0, future_starts,
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

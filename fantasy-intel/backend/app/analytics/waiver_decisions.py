"""Explainable acquisition decisions; all thresholds are heuristics, never probabilities.

Meaningful historical lineup improvement: >=1 on the existing 0–10 player-value scale. That scale is a
within-position percentile, not fantasy points, so a FLEX or OP comparison across positions compares percentiles.
Meaningful forward gain: >=1 ESPN point per covered week. Stashes need value >=6,
an improvement over same-position depth >=1, and an observed need or rising role.
These are decision thresholds, not changes to the shared player scoring model.
"""
from __future__ import annotations

from .lineup import assign_lineup, lineup_total
from .waiver_roster import forward_effect, player_names, unknown_value_players

VALUE_SCALE = "0-10 position percentile, not fantasy points"
VALUE_SCALE_TEXT = "0-10 position-percentile scale, not fantasy points"


def acquisition_utility(state: dict, candidate: dict, move: dict, *, need: float, opportunity: float | None) -> dict:
    players, slots = state["players"], state["slots"]
    drop = move.get("drop")
    after = [p for p in players if not drop or p["espn_player_id"] != drop["espn_player_id"]] + [candidate]
    cand_raw = candidate.get("raw_value")
    unknown = ([candidate] if cand_raw is None else []) + unknown_value_players(players, slots)
    comparable = not unknown
    gain = round(lineup_total(assign_lineup(after, slots)) - lineup_total(state["lineup"]), 3) if comparable else None
    depth = [p["raw_value"] for p in players if p["position"] == candidate["position"] and p.get("raw_value") is not None]
    depth_unknown = any(p.get("raw_value") is None for p in players if p["position"] == candidate["position"])
    depth_gain = None if depth_unknown or cand_raw is None else cand_raw - min(depth) if depth else cand_raw
    forward = forward_effect(players, after, slots, state.get("weeks", []), state.get("stream"))
    forward_mean = (forward["total_gain"] / len(state["weeks"]) if forward["total_gain"] is not None and state.get("weeks") else None)
    starter = (gain is not None and gain >= 1) or (forward_mean is not None and forward_mean >= 1)
    stash = (candidate["position"] not in ("K", "D/ST") and (candidate.get("raw_value") or 0) >= 6
             and depth_gain is not None and depth_gain >= 1 and (need >= 0.35 or candidate.get("rising")))
    if drop and candidate.get("raw_value") is not None and candidate["raw_value"] < drop["value"] and not starter:
        stash = False
    summary = (f"Improves your current lineup by {gain:.1f} historical value ({VALUE_SCALE_TEXT})."
               if gain is not None and gain >= 1 else
               f"Improves covered future lineups by {forward_mean:.1f} projected points per week." if starter else
               "Potential bench stash: stronger depth with an observed need or rising role." if stash else
               f"Roster comparison is incomplete: no historical value for {player_names(unknown)}; unknown is not zero."
               if not comparable else
               "No meaningful improvement over your current starters or positional depth is demonstrated.")
    return {"meaningful": bool(starter or stash), "starter_gain": gain, "depth_gain": depth_gain,
            "unknown_value_ids": [p["espn_player_id"] for p in unknown],
            "stash": bool(stash and not starter), "forward": forward, "summary": summary,
            # None when no evidence exists (no historical gain, no forward gain, no stash): an unknown benefit ranks
            # after every known one (api/routes.py WAIVER_ORDER), never among real ~0 gains.
            "ranking_utility": (None if gain is None and forward_mean is None and not stash else
                                round(max(0, gain or 0, forward_mean or 0) + (2 * need if starter or stash else 0)
                                      + (1 if stash else 0), 3))}


def resource_diagnostics(status: str, league: dict, team: dict, *, meaningful: bool, alternative: dict | None = None) -> dict:
    result = {"kind": "UNKNOWN", "spends_priority": None, "preserve_priority": False,
              "priority": team.get("waiver_rank"), "remaining": None, "budget": league.get("faab_budget"),
              "bid": None, "alternative": alternative, "summary": "Acquisition rules are unknown; verify in ESPN."}
    if status == "FREEAGENT":
        result.update(kind="FREE_AGENT", spends_priority=False,
                      summary="Available as a free agent: no waiver priority or FAAB claim spend indicated. Check ESPN transaction timing.")
    elif status == "WAIVERS" and league.get("is_faab") is True:
        budget, spent = league.get("faab_budget"), team.get("faab_spent")
        remaining = max(0, budget - spent) if budget is not None and spent is not None else None
        result.update(kind="FAAB", spends_priority=False, remaining=remaining,
                      summary=f"FAAB claim: {remaining:g} of {budget:g} budget remains; no calibrated bid estimate." if remaining is not None
                      else "FAAB claim: remaining budget unknown; no calibrated bid estimate.")
        if remaining == 0:
            result["summary"] += " Budget exhausted: verify whether zero-dollar claims are allowed."
    elif status == "WAIVERS" and league.get("is_faab") is False:
        rank = team.get("waiver_rank")
        rolling = league.get("waiver_rule") == "ROLLING"
        preserve = rolling and rank is not None and rank <= 3 and (not meaningful or alternative is not None)
        summary = f"Waiver priority #{rank}. " if rank is not None else "Waiver priority unknown. "
        summary += ("Rolling priority is valuable; preserve it for a clearer upgrade." if preserve else
                    "Rolling priority is spent on a successful claim; weigh the demonstrated benefit." if rolling else
                    "Priority reset/processing rule is not confirmed as rolling; check ESPN.")
        if alternative:
            summary += f" Comparable free agent: {alternative['name']}."
        result.update(kind="PRIORITY", spends_priority=True, preserve_priority=preserve, summary=summary)
    return result


def evaluate_streaming(position: str, evidence: dict, *, required: int) -> dict:
    weeks = evidence.get("weeks", [])
    first = weeks[0] if weeks else {}
    points, incumbent = first.get("points"), evidence.get("incumbent_points")
    gain = points - incumbent if points is not None and incumbent is not None else None
    complete = points is not None and incumbent is not None and bool(first.get("opponent")) and not first.get("bye")
    category, summary = "INCOMPLETE", "Streaming evidence is incomplete: need upcoming projection, opponent and roster comparison."
    if not required:
        category, summary = "UNNECESSARY_STASH", f"Your league has no starting {position} requirement."
    elif first.get("bye"):
        category, summary = "NO_DEMONSTRATED_IMPROVEMENT", "On bye in the upcoming week."
    elif complete and gain >= 1:
        holds = sum(1 for w in weeks[:3] if w.get("points") is not None and w.get("incumbent_points") is not None
                    and w["points"] - w["incumbent_points"] >= 1 and w.get("opponent") and not w.get("bye"))
        category = "MULTIWEEK_HOLD" if holds >= 2 else "SHORT_TERM_STREAMER"
        summary = f"Week {first['week']} vs {first['opponent']}: ESPN projects {points:.1f}, {gain:+.1f} versus your lineup."
    elif complete:
        category, summary = "NO_DEMONSTRATED_IMPROVEMENT", "Upcoming projection does not demonstrate a meaningful upgrade; avoid an extra bench stash."
    return {"category": category, "complete": complete, "gain": gain, "summary": summary, **evidence}


def classify_action(utility: dict, move: dict, resource: dict, *, available: bool | None,
                    actionable: bool | None = True, streaming: dict | None = None) -> dict:
    action, rec = "NOT_AN_UPGRADE", "IGNORE"
    if actionable is False:
        action = "SEASON_COMPLETE"
    elif not utility.get("meaningful") and utility.get("starter_gain") is None and not streaming:
        action, rec = "MONITOR", "WAIT"
    elif not utility.get("meaningful") and (utility.get("starter_gain") or 0) <= 0 and not streaming:
        action = "NOT_AN_UPGRADE"
    elif move["status"] in ("MOVE_REQUIRED", "INCOMPATIBLE"):
        action, rec = "NO_LEGAL_MOVE", "WAIT"
    elif move["status"] == "UNKNOWN":
        action, rec = "VERIFY_LEGALITY", "WAIT"
    elif actionable is None or available is not True:
        action, rec = "MONITOR", "WAIT"
    elif streaming and streaming["category"] == "INCOMPLETE":
        action, rec = "MONITOR", "WAIT"
    elif streaming and streaming["category"] not in ("SHORT_TERM_STREAMER", "MULTIWEEK_HOLD"):
        action = "NOT_AN_UPGRADE"
    elif utility.get("meaningful") or streaming and streaming.get("gain", 0) >= 1:
        if resource["kind"] == "UNKNOWN" or resource["kind"] == "FAAB" and resource.get("remaining") == 0:
            action, rec = "VERIFY_COST", "WAIT"
        elif resource.get("preserve_priority"):
            action, rec = "PRESERVE_PRIORITY", "WAIT"
        elif resource["kind"] == "FREE_AGENT":
            action, rec = "ADD_NOW", "CLAIM"
        else:
            action, rec = "WAIVER_CLAIM", "CLAIM"
    elif (utility.get("starter_gain") or 0) > 0:
        action, rec = "MONITOR", "WAIT"
    return {"action": action, "recommendation": rec,
            "actionable": rec == "CLAIM", "category": "STREAMING" if streaming else "STASH" if utility.get("stash") else "UPGRADE"}

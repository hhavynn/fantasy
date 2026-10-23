"""Multi-week stash planner (#218): free agents worth a bench spot now for what they add to THIS roster's starting lineup
in later weeks, kept apart from current-week pickups, plus evidence-only "if a starter is out" contingencies.

Exact part. Over the current week and the next `horizon` weeks (2, 4 or 6), each candidate is the #216 evaluator's
exact two-lineup difference (marginal_utility.scenario): this roster with the free agent added and its drop candidate
removed, minus this roster as it is. A missing projection in any of those weeks makes the candidate unknown, never 0.

  * stash          adds nothing this week and adds later (the weeks he starts and whom he fills in for are listed);
  * pick up now    adds this week already: a current-week waiver decision (the Waivers page), never listed as a stash;
  * no gain        adds nothing in the horizon.

The drop candidate is the active player whose removal costs this roster least over the same weeks (exact, ties by id),
or nobody when an active roster spot is open. Players in IR or taxi slots are never dropped or moved. An addition must
keep every position within the league's roster limit after the drop. Only QB, RB, WR and TE are planned: K and D/ST are
streamed week by week (#181), not stashed.

Candidates are free agents whose availability is known (FREEAGENT or WAIVERS), projected in every horizon week. Among
them only the non-dominated ones per position are evaluated (a free agent projected no higher than another at his
position in every horizon week cannot add more than that one), capped at MAX_EVALUATED by later-week projection; both
cuts are counted.

Ranking policy (deterministic): later-week gain, then the earliest week he starts, then player id.

Contingency part. From the injury opportunity tree (#274, analytics/opportunity_tree.py): when a starter is out or
returning, the teammates whose observed (or, labelled, hypothesised) volume changed and who are claimable in this
league. Each is an if-then statement with its sample; it carries no lineup number and no probability that the event
happens."""

from __future__ import annotations

from .marginal_utility import scenario
from .team_lineups import RosterPlayer, Window, lineup_relevant, project_team, projection, r, without

HORIZONS = (2, 4, 6)
STASH_POSITIONS = ("QB", "RB", "WR", "TE")
ADDABLE = ("FREEAGENT", "WAIVERS")
MAX_EVALUATED = 40
MAX_LISTED = 10
CLAIMABLE = ("free_agent", "waivers", "available_claim_unknown")
POLICY = ("Stashes are ranked by the starting-lineup points they add in the later weeks (exact lineup difference with "
          "the drop candidate removed), then by the earliest week they start, then by player id.")
NOTES = (
    "Projected lineup points, not a forecast that anything happens: no injury or role-change probability is used.",
    "Waiver availability is not guaranteed; a player on waivers needs a claim.",
    "K and D/ST are streamed week by week, not stashed.",
)


class StashPlanError(ValueError):
    """A plan that cannot be specified (an unknown team, an unsupported horizon)."""


def _legal(candidates: list[RosterPlayer], players: list[RosterPlayer], before: list[RosterPlayer], limits: dict):
    """Candidates whose addition keeps every position within the league's limit (a limit the roster already exceeded is
    not tightened): team_value._position_legal's rule."""
    if not limits:
        return list(candidates)
    count = lambda ps, pos: sum(1 for p in ps if p.position == pos)        # noqa: E731
    return [c for c in candidates
            if c.position not in limits or count(players, c.position) + 1 <= max(limits[c.position],
                                                                                  count(before, c.position))]


def _non_dominated(pool: list[RosterPlayer], weeks, proj) -> list[RosterPlayer]:
    """Per position, drop a free agent projected no higher than another one in every week (ties: the lower id stays)."""
    out = []
    for p in pool:
        pv = [projection(proj, p.espn_player_id, w) for w in weeks]
        dominated = any(q.position == p.position and q is not p
                        and all(qv >= v for qv, v in zip((projection(proj, q.espn_player_id, w) for w in weeks), pv))
                        and (any(qv > v for qv, v in zip((projection(proj, q.espn_player_id, w) for w in weeks), pv))
                             or q.espn_player_id < p.espn_player_id)
                        for q in pool)
        if not dominated:
            out.append(p)
    return out


def _drop_candidate(roster, slots, hw, proj, base, capacity):
    """(the player to drop or None, his exact removal cost over the horizon, why). Nobody when a spot is open."""
    active = [p for p in roster if not p.reserved]
    if len(active) < capacity:
        return None, None, "open_spot"
    costs = []
    for p in active:
        s = scenario(roster, slots, hw, proj, remove=[p.espn_player_id], base=base)
        if s["_ros_exact"] is not None:
            costs.append((-s["_ros_exact"], p.espn_player_id, p, s["ros_delta"]))
    if not costs:
        return None, None, "unknown"
    _, _, p, delta = min(costs, key=lambda c: (c[0], c[1]))
    return p, delta, "least_cost"


def plan_stashes(players: list[RosterPlayer], slots: list[str], window: Window, proj, *, free_agents: list[RosterPlayer],
                 free_agent_status: dict, active_capacity: int, position_limits: dict | None = None,
                 horizon: int = 4, projection_source: str) -> dict:
    """The exact part of the plan for one roster (see the module docstring). Pure: the caller supplies the state."""
    if horizon not in HORIZONS:
        raise StashPlanError(f"horizon must be one of {', '.join(map(str, HORIZONS))}")
    weeks = list(window.weeks)
    out = {"horizon": horizon, "projection_source": projection_source, "policy": POLICY, "notes": list(NOTES),
           "unit": "projected starting-lineup points (exact optimal lineup with the change minus without it)"}
    if len(weeks) < 2:
        return {**out, "status": "no_later_weeks", "weeks": weeks, "stashes": [], "pick_up_now": [], "drop": None,
                "counts": {}, "reason": "Fewer than two fantasy weeks remain, so there is no later week to stash for."}
    hweeks = weeks[:1 + horizon]
    hw = Window(tuple(hweeks), window.last_regular)
    now, later = hweeks[0], hweeks[1:]
    base = project_team(players, slots, hw, proj)
    drop, drop_cost, drop_why = _drop_candidate(players, slots, hw, proj, base, active_capacity)
    rostered = {p.espn_player_id for p in players}
    pool = [p for p in free_agents if p.espn_player_id not in rostered and p.position in STASH_POSITIONS
            and lineup_relevant(p.position, slots) and free_agent_status.get(p.espn_player_id) in ADDABLE]
    counts = {"free_agents": len(free_agents), "considered": len(pool)}
    projected = [p for p in pool if all(projection(proj, p.espn_player_id, w) is not None for w in hweeks)]
    counts["excluded_unprojected"] = len(pool) - len(projected)
    after_drop = without(players, [drop.espn_player_id]) if drop is not None else list(players)
    legal = _legal(projected, after_drop, players, position_limits or {})
    counts["excluded_position_limit"] = len(projected) - len(legal)
    front = _non_dominated(legal, hweeks, proj)
    counts["dominated"] = len(legal) - len(front)
    front.sort(key=lambda p: (-sum(projection(proj, p.espn_player_id, w) for w in later), p.espn_player_id))
    evaluated = front[:MAX_EVALUATED]
    counts["not_evaluated_cap"] = len(front) - len(evaluated)
    if drop_why == "unknown":
        return {**out, "status": "drop_unknown", "weeks": hweeks, "now_week": now, "later_weeks": later,
                "stashes": [], "pick_up_now": [], "drop": None, "counts": counts,
                "reason": "Every active player's removal cost is unknown (a projection is missing), so no drop can be "
                          "chosen and no addition evaluated."}
    stashes, now_adds, unknown, no_gain = [], [], 0, 0
    names = {p.espn_player_id: p.name for p in [*players, *free_agents]}
    for fa in evaluated:
        s = scenario(players, slots, hw, proj, remove=[drop.espn_player_id] if drop else (), add=[fa], base=base)
        by_week = {w["week"]: w for w in s["weeks"]}
        d_now = by_week[now]["delta"]
        later_d = [by_week[w]["delta"] for w in later]
        if d_now is None or any(d is None for d in later_d):
            unknown += 1
            continue
        gain_later = r(sum(later_d))
        row = {"espn_player_id": fa.espn_player_id, "name": fa.name, "position": fa.position, "pro_team": fa.pro_team,
               "availability": free_agent_status.get(fa.espn_player_id), "this_week_delta": d_now,
               "later_delta": gain_later, "horizon_delta": s["ros_delta"],
               "weeks": [{"week": w, "delta": by_week[w]["delta"],
                          "starts": fa.espn_player_id in by_week[w]["entered_lineup"],
                          "fills_in_for": by_week[w]["left_lineup"]} for w in hweeks]}
        if d_now > 0:
            now_adds.append(row)
        elif gain_later > 0:
            starts = [w["week"] for w in row["weeks"] if w["starts"] and w["week"] != now]
            row["first_start_week"] = min(starts) if starts else None
            row["why"] = _why(row, now, names, proj, fa, drop.espn_player_id if drop else None)
            stashes.append(row)
        else:
            no_gain += 1
    stashes.sort(key=lambda x: (-x["later_delta"], x["first_start_week"] or 99, x["espn_player_id"]))
    now_adds.sort(key=lambda x: (-x["this_week_delta"], x["espn_player_id"]))
    counts.update({"evaluated": len(evaluated), "stashes": len(stashes), "pick_up_now": len(now_adds),
                   "no_gain": no_gain, "unknown": unknown})
    return {**out, "status": "evaluated", "weeks": hweeks, "now_week": now, "later_weeks": later,
            "drop": None if drop is None else {"espn_player_id": drop.espn_player_id, "name": drop.name,
                                               "position": drop.position, "horizon_cost": drop_cost},
            "drop_basis": drop_why, "stashes": stashes[:MAX_LISTED],
            "pick_up_now": [{k: x[k] for k in ("espn_player_id", "name", "position", "pro_team", "availability",
                                               "this_week_delta", "horizon_delta")} for x in now_adds[:5]],
            "counts": counts, "reason": None if stashes else "No free agent adds starting points only in later weeks."}


def _why(row: dict, now: int, names: dict, proj, fa: RosterPlayer, drop_id: int | None) -> str:
    """Why now: the weeks he starts and whom he fills in for (from the exact lineups), and his projection trend."""
    parts = []
    for w in row["weeks"]:
        if w["week"] == now or not w["starts"]:
            continue
        who = ", ".join((names.get(i) or f"Player {i}") + (" (the drop)" if i == drop_id else "")
                        for i in w["fills_in_for"]) or "an open slot"
        parts.append(f"week {w['week']}: starts in place of {who} ({'+' if w['delta'] > 0 else ''}{w['delta']:.1f})")
    p_now = projection(proj, fa.espn_player_id, now)
    later = [projection(proj, fa.espn_player_id, w["week"]) for w in row["weeks"] if w["week"] != now]
    trend = f"projected {p_now:.1f} this week, {sum(later) / len(later):.1f} a week after"
    return "; ".join(parts + [trend])


def contingencies(tree: dict, team_id: int | None = None) -> dict:
    """If-then scenarios from the opportunity tree whose named beneficiary is claimable in this league: the starter, the
    event (out or returning), the beneficiary, the basis (observed or hypothesis) and the sample. No probabilities and no
    lineup numbers."""
    items, abstained = [], 0
    for team in tree.get("teams") or []:
        for s in team.get("scenarios") or []:
            if s.get("outcome") == "abstain":
                abstained += 1
                continue
            for e in s.get("effects") or []:
                own = e.get("ownership") or {}
                if own.get("claim") not in CLAIMABLE or e.get("espn_player_id") is None:
                    continue
                items.append({"espn_player_id": e["espn_player_id"], "name": e.get("full_name"),
                              "position": e.get("position"), "team": team.get("team"), "claim": own.get("claim"),
                              "if": {"kind": s["kind"], "starter": s["subject"].get("full_name"),
                                     "starter_position": s["subject"].get("position"), "certainty": s.get("certainty")},
                              "basis": e.get("basis"), "observed": e.get("observed"), "unit": s.get("unit_label"),
                              "sample": s.get("sample"), "reasons": e.get("reasons") or []})
    items.sort(key=lambda x: (x["basis"] != "observed", x["team"] or "", x["name"] or "", x["espn_player_id"]))
    return {"items": items, "scenarios_abstained": abstained,
            "note": "If-then evidence only: what changed for these players when the named starter was out or came back, "
                    "with its sample. Not a prediction that it happens, and no lineup value is attached."}

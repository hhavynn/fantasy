"""Multi-week stash planner (#218): free agents worth a bench spot now for what they add to THIS roster's starting lineup
over the next weeks, kept apart from current-week pickups, plus evidence-only "if a starter is out" contingencies.

Exact part. Over the current week and the next `horizon` weeks (2, 4 or 6), each candidate is the #216 evaluator's
exact two-lineup difference (marginal_utility.scenario): this roster with the free agent added and its drop candidate
removed, minus this roster as it is. A missing projection in any of those weeks makes the candidate unknown, never 0.
Only a candidate whose net difference over the whole horizon is positive is listed:

  * pick up now    adds starting points this week (and over the horizon): a current-week waiver decision (the
                   Waivers page), never listed as a stash;
  * stash          adds nothing this week (or costs points this week, which is stated) and more than makes it up later;
  * no gain        adds nothing over the horizon, net.

The drop candidate is the active player whose removal costs this roster least over the same weeks (exact, ties by id),
or nobody when an active roster spot is open and the active count is exact (trades.roster_legality). When the count is
unverified an open spot is never assumed: the least-cost drop is used and the plan says why. Players in IR or taxi
slots are never dropped or moved. An addition must keep every position within the league's roster limit after that one
drop. Only QB, RB, WR and TE are planned: K and D/ST are streamed week by week (#181), not stashed.

Candidates are free agents whose availability is known (FREEAGENT or WAIVERS), projected in every horizon week. Before
any lineup is solved, each gets an upper bound on what he can add: per week, his projection (0 in a week he is verified
NFL-ineligible) minus the lowest starter he could displace through the slots his position reaches in this roster's
current lineup (0 for an empty slot). Adding one player changes an optimal lineup by one replacement chain that ends
with that starter leaving, and removing the drop never adds points, so a bound of 0 or less means no gain (counted, not
solved). The rest are evaluated exactly, at most MAX_PER_POSITION per position in bound order; every cut is counted.

Ranking policy (deterministic): net horizon gain, then the earliest later week he starts, then player id.

Contingency part. From the injury opportunity tree (#274, analytics/opportunity_tree.py): when a starter is out or
returning, the teammates whose observed (or, labelled, hypothesised) volume changed and who are claimable in this
league. Each is an if-then statement with its sample, the team's game this week and the evidence's as-of; it carries no
lineup number and no probability that the event happens."""

from __future__ import annotations

from .lineup import DUAL_ELIGIBILITY, eligible
from .marginal_utility import scenario
from .team_lineups import RosterPlayer, Window, lineup_relevant, project_team, projection, r, slot_filled, without

HORIZONS = (2, 4, 6)
STASH_POSITIONS = ("QB", "RB", "WR", "TE")
ADDABLE = ("FREEAGENT", "WAIVERS")
MAX_PER_POSITION = 50
MAX_LISTED = 10
MAX_PICK_UP_NOW = 5
EPS = 1e-9                  # float noise in an exact lineup difference is not a gain
CLAIMABLE = ("free_agent", "waivers", "available_claim_unknown")
POLICY = ("Listed only when the net starting-lineup points over the horizon are positive (exact lineup difference with "
          "the drop candidate removed). Stashes are ranked by that net gain, then by the earliest later week they "
          "start, then by player id.")
NOTES = (
    "Projected lineup points, not a forecast that anything happens: no injury or role-change probability is used.",
    "Waiver availability is not guaranteed; a player on waivers needs a claim, and bids are not planned here.",
    "K and D/ST are streamed week by week, not stashed.",
    "One drop candidate is used for every addition, so a position at its roster limit stays closed unless that "
    "drop plays it.",
)


class StashPlanError(ValueError):
    """A plan that cannot be specified (an unsupported horizon)."""


def _legal(candidates: list[RosterPlayer], players: list[RosterPlayer], before: list[RosterPlayer], limits: dict):
    """Candidates whose addition keeps every position within the league's limit (a limit the roster already exceeded is
    not tightened): team_value._position_legal's rule."""
    if not limits:
        return list(candidates)
    count = lambda ps, pos: sum(1 for p in ps if p.position == pos)        # noqa: E731
    return [c for c in candidates
            if c.position not in limits or count(players, c.position) + 1 <= max(limits[c.position],
                                                                                  count(before, c.position))]


def _effective(proj, p: RosterPlayer, week: int) -> float:
    """What he can score in a lineup that week: 0 when verified NFL-ineligible (the lineup engine's rule)."""
    return 0.0 if week in p.unavailable_weeks else projection(proj, p.espn_player_id, week)


def _floors(base: dict, hweeks: list[int]) -> dict[int, dict[str, float] | None]:
    """Per week and position: the lowest starter a player at that position could push out of the current lineup (0 for
    an empty slot it reaches), following the replacement chain through the slots each displaced starter can take. None
    for a week whose lineup is not exact (no bound: the candidate is solved and comes out unknown)."""
    out = {}
    by_week = {w["week"]: w for w in base["weeks"]}
    for wk in hweeks:
        week = by_week[wk]
        if not week["projection_complete"]:
            out[wk] = None
            continue
        rows = week["assignment"]
        floors = {}
        for pos in STASH_POSITIONS:
            if DUAL_ELIGIBILITY:                        # eligibility widens per player: every slot may be reached
                reached = set(range(len(rows)))
            else:
                reached, seen = set(), {pos}
                frontier = [pos]
                while frontier:
                    q = frontier.pop()
                    for i, a in enumerate(rows):
                        if i not in reached and eligible(a["slot"], q):
                            reached.add(i)
                            if slot_filled(a) and a["position"] not in seen:
                                seen.add(a["position"])
                                frontier.append(a["position"])
            vals = [(a["points"] if slot_filled(a) else 0.0) for i, a in enumerate(rows) if i in reached]
            floors[pos] = min(vals) if vals else None
        out[wk] = floors
    return out


def _bound(p: RosterPlayer, floors: dict, hweeks: list[int], proj) -> float:
    """An upper bound on the starting points adding him can gain over the horizon (inf when a week has no bound)."""
    total = 0.0
    for wk in hweeks:
        f = floors[wk]
        if f is None:
            return float("inf")
        if f.get(p.position) is None:                   # no slot takes him: he cannot start
            continue
        total += max(0.0, _effective(proj, p, wk) - f[p.position])
    return total


def _drop_candidate(roster, slots, hw, proj, base, capacity, count_unverified: str | None):
    """(the player to drop or None, his exact removal cost over the horizon, why). Nobody when a spot is open and the
    active count is exact; with an unverified count an open spot is never assumed."""
    active = [p for p in roster if not p.reserved]
    if len(active) < capacity and not count_unverified:
        return None, None, "open_spot"
    costs = []
    for p in active:
        s = scenario(roster, slots, hw, proj, remove=[p.espn_player_id], base=base)
        if s["_ros_exact"] is not None:
            costs.append((-s["_ros_exact"], p.espn_player_id, p, s["ros_delta"]))
    if not costs:
        return None, None, "unknown"
    _, _, p, delta = min(costs, key=lambda c: (c[0], c[1]))
    return p, delta, ("roster_count_unverified" if len(active) < capacity else "least_cost")


def plan_stashes(players: list[RosterPlayer], slots: list[str], window: Window, proj, *, free_agents: list[RosterPlayer],
                 free_agent_status: dict, active_capacity: int, position_limits: dict | None = None,
                 horizon: int = 4, projection_source: str, roster_count_unverified: str | None = None,
                 undroppable_list_in_use: bool | None = None) -> dict:
    """The exact part of the plan for one roster (see the module docstring). Pure: the caller supplies the state.
    `roster_count_unverified` is trades.roster_legality's reason when the active count is not exact."""
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
    drop, drop_cost, drop_why = _drop_candidate(players, slots, hw, proj, base, active_capacity, roster_count_unverified)
    drop_notes = []
    if drop_why == "roster_count_unverified":
        drop_notes.append(roster_count_unverified + " An open spot is not assumed, so the least-cost drop is used.")
    if undroppable_list_in_use and drop is not None:
        drop_notes.append(f"This league uses an undroppable list; whether {drop.name} is on it is not known here.")
    rostered = {p.espn_player_id for p in players}
    pool = [p for p in free_agents if p.espn_player_id not in rostered and p.position in STASH_POSITIONS
            and lineup_relevant(p.position, slots) and free_agent_status.get(p.espn_player_id) in ADDABLE]
    counts = {"free_agents": len(free_agents), "considered": len(pool)}
    projected = [p for p in pool if all(projection(proj, p.espn_player_id, w) is not None for w in hweeks)]
    counts["excluded_unprojected"] = len(pool) - len(projected)
    after_drop = without(players, [drop.espn_player_id]) if drop is not None else list(players)
    legal = _legal(projected, after_drop, players, position_limits or {})
    counts["excluded_position_limit"] = len(projected) - len(legal)
    floors = _floors(base, hweeks)
    bounded = [(_bound(p, floors, hweeks, proj), p) for p in legal]
    possible = [(b, p) for b, p in bounded if b > EPS]
    counts["no_possible_gain"] = len(legal) - len(possible)
    possible.sort(key=lambda bp: (-bp[0], bp[1].espn_player_id))
    per_pos, evaluated = {}, []
    for _, p in possible:
        per_pos[p.position] = per_pos.get(p.position, 0) + 1
        if per_pos[p.position] <= MAX_PER_POSITION:
            evaluated.append(p)
    counts["not_evaluated_cap"] = len(possible) - len(evaluated)
    if drop_why == "unknown":
        return {**out, "status": "drop_unknown", "weeks": hweeks, "now_week": now, "later_weeks": later,
                "stashes": [], "pick_up_now": [], "drop": None, "drop_basis": drop_why, "counts": counts,
                "reason": "Every active player's removal cost is unknown (a projection is missing), so no drop can be "
                          "chosen and no addition evaluated."}
    stashes, now_adds, unknown, no_gain = [], [], 0, counts["no_possible_gain"]
    names = {p.espn_player_id: p.name for p in [*players, *free_agents]}
    drop_id = drop.espn_player_id if drop else None
    for fa in evaluated:
        s = scenario(players, slots, hw, proj, remove=[drop_id] if drop else (), add=[fa], base=base)
        exact = s["_weeks_exact"]
        if s["_ros_exact"] is None or any(d is None for d in exact):
            unknown += 1
            continue
        total, d_now, d_later = s["_ros_exact"], exact[0], sum(exact[1:])
        if total <= EPS:
            no_gain += 1
            continue
        by_week = {w["week"]: w for w in s["weeks"]}
        row = {"espn_player_id": fa.espn_player_id, "name": fa.name, "position": fa.position, "pro_team": fa.pro_team,
               "availability": free_agent_status.get(fa.espn_player_id), "this_week_delta": r(d_now),
               "later_delta": r(d_later), "horizon_delta": r(total), "_net": total,
               "weeks": [{"week": w, "delta": by_week[w]["delta"],
                          "starts": fa.espn_player_id in by_week[w]["entered_lineup"],
                          "fills_in_for": by_week[w]["left_lineup"]} for w in hweeks]}
        if d_now > EPS:
            now_adds.append(row)
            continue
        starts = [w["week"] for w in row["weeks"] if w["starts"] and w["week"] != now]
        row["first_start_week"] = min(starts) if starts else None
        row["why"] = _why(row, now, names, proj, fa, drop_id)
        stashes.append(row)
    stashes.sort(key=lambda x: (-x["_net"], x["first_start_week"] or 99, x["espn_player_id"]))
    now_adds.sort(key=lambda x: (-x["_net"], x["espn_player_id"]))
    counts.update({"evaluated": len(evaluated), "stashes": len(stashes), "pick_up_now": len(now_adds),
                   "no_gain": no_gain, "unknown": unknown})
    if stashes:
        reason = None
    elif evaluated and unknown == len(evaluated):
        reason = ("Every evaluated free agent's value is unknown: a rostered player's projection is missing in a horizon "
                  "week.")
    else:
        reason = "No free agent adds net starting points over the horizon without adding them this week."
    return {**out, "status": "evaluated", "weeks": hweeks, "now_week": now, "later_weeks": later,
            "drop": None if drop is None else {"espn_player_id": drop.espn_player_id, "name": drop.name,
                                               "position": drop.position, "horizon_cost": drop_cost},
            "drop_basis": drop_why, "drop_notes": drop_notes,
            "stashes": [{k: v for k, v in x.items() if not k.startswith("_")} for x in stashes[:MAX_LISTED]],
            "pick_up_now": [{k: x[k] for k in ("espn_player_id", "name", "position", "pro_team", "availability",
                                               "this_week_delta", "horizon_delta")} for x in now_adds[:MAX_PICK_UP_NOW]],
            "counts": counts, "reason": reason}


def _signed(v: float) -> str:
    return f"{r(v):+g}"


def _why(row: dict, now: int, names: dict, proj, fa: RosterPlayer, drop_id: int | None) -> str:
    """Why now: this week's cost if any, the weeks he starts and whom he fills in for, the weeks the drop is missed
    (from the exact lineups), and his projection trend."""
    who = lambda ids: ", ".join((names.get(i) or f"Player {i}") + (" (the drop)" if i == drop_id else "")   # noqa: E731
                                for i in ids)
    parts = []
    for w in row["weeks"]:
        if w["starts"] and w["week"] != now:
            parts.append(f"week {w['week']}: starts in place of {who(w['fills_in_for']) or 'an open slot'} "
                         f"({_signed(w['delta'])})")
        elif w["delta"] < 0:
            label = "this week" if w["week"] == now else f"week {w['week']}"
            parts.append(f"{label}: {_signed(w['delta'])}, {who(w['fills_in_for']) or 'the drop'} leaves the lineup")
    p_now = _effective(proj, fa, now)
    later = [_effective(proj, fa, w["week"]) for w in row["weeks"] if w["week"] != now]
    trend = f"projected {p_now:.1f} this week, {sum(later) / len(later):.1f} a week after"
    return "; ".join(parts + [trend])


def contingencies(tree: dict) -> dict:
    """If-then scenarios from the opportunity tree whose named beneficiary is claimable in this league: the starter, the
    event (out or returning), the beneficiary, the basis (observed or hypothesis), the sample, the team's game this week
    and the evidence's as-of. No probabilities and no lineup numbers."""
    items, abstained = [], {}
    for team in tree.get("teams") or []:
        evidence = {"game": team.get("game"), "injury_freshness": team.get("injury_freshness"),
                    "depth_chart": team.get("depth_chart"),
                    "coverage": (team.get("coverage") or {}).get("status")}
        for s in team.get("scenarios") or []:
            if s.get("outcome") == "abstain":
                code = (s.get("abstain") or {}).get("code") or "unknown"
                abstained[code] = abstained.get(code, 0) + 1
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
                              "sample": s.get("sample"), "reasons": e.get("reasons") or [], **evidence})
    items.sort(key=lambda x: (x["basis"] != "observed", x["team"] or "", x["name"] or "", x["espn_player_id"]))
    return {"items": items, "scenarios_abstained": sum(abstained.values()), "abstained_by_code": abstained,
            "generated_at": tree.get("generated_at"), "sources": tree.get("sources"),
            "notices": [{"code": n.get("code"), "text": n.get("text")} for n in tree.get("notices") or []],
            "note": "If-then evidence only: what changed for these players when the named starter was out or came back, "
                    "with its sample and as-of. Not a prediction that it happens, and no lineup value is attached. It "
                    "is this week's evidence: re-check it after the team's game."}

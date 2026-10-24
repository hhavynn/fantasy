"""Counteroffers (#214): bounded modifications of one existing trade offer that make the deal better for the side it
treats worse, each one exactly evaluated for both teams. Hypothetical, pure and read-only (no database, no provider).

The offer is team A sending `a_sends` to team B for `b_sends`. A counteroffer changes it by a small, fixed number of
edits, and never touches a locked player:

  * remove one unlocked player from either side (each side keeps at least one player);
  * add one player from either roster (an allowed position, not excluded, within the per-side limit);
  * swap one unlocked player on a side for another player from the same roster;
  * with `max_added_per_side` 2, one more addition to the best few single edits (a bounded second step).

Illegal packages are refused before evaluation (trades.validate: ownership, duplicates, the per-side limit; locked and
excluded players). Every remaining package is evaluated by the same exact engine as Trade Discovery
(trade_discovery.TradeSearch._pair: both teams' optimal lineups rebuilt every remaining week, forced drops included,
missing projections never 0), under a deterministic work budget counted in lineup totals and a wall-clock cap.

A counteroffer qualifies when both teams' rest-of-season deltas are known and the smaller of the two is larger than in
the original offer: the side the offer treats worse gains, measured on the same exact scale. The order is fixed
(larger smaller-side delta, larger combined delta, fewer players, fewer edits, then player ids), and a package whose
effects on both teams equal a better-ranked or smaller one's is hidden. At most MAX_SHOWN are returned, and each is
re-run through the full manual evaluator (trades.evaluate_trade, at most MANUAL_CHECKS runs), whose deltas and forced
drops must match; one that does not is dropped and counted, never shown. Nothing here estimates whether a manager would accept."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

from .lineup import unsupported_of
from .team_lineups import lineup_relevant, r
from .trade_discovery import DEFAULT_POSITIONS, DELTA_KEYS, EQUIVALENCE_KEYS, TradeSearch, _key
from .trades import MAX_PLAYERS_PER_SIDE, LeagueState, TradeError, evaluate_trade, validate


MAX_ADDED = 2                 # extra players a counteroffer may add to one side
MAX_SHOWN = 3
SECOND_STEP_WIDTH = 4         # single edits a second addition is tried on (max_added_per_side 2)
# Work budget: lineup totals (each side of a package and each forced-drop set it solves, as TradeSearch.work counts
# them, plus the signals that order the candidates). The same unit and size as discovery's per-opponent beam budget.
WORK_BUDGET = 15_000
TIME_CAP_SECONDS = 6.0        # a safety net under hosted request limits; the work budget is the deterministic bound
MANUAL_CHECKS = 2 * MAX_SHOWN  # full manual-evaluator re-checks per request (shown results plus a few mismatches)
EDIT_ORDER = {"remove": 0, "swap": 1, "add": 2}
NOTE = ("Each counteroffer changes the original offer by at most a few players and is evaluated exactly for both "
        "teams, the same computation as the manual analyzer. It is listed because the team the original offer treats "
        "worse projects to do better. No acceptance odds are estimated.")


@dataclass(frozen=True)
class CounterofferConstraints:
    """What a counteroffer may change. `locked` players stay in the deal on their side; `excluded` players are never
    added; added players come from `positions` (None: any). Small fixed caps keep the search bounded."""
    locked: frozenset[int] = frozenset()
    excluded: frozenset[int] = frozenset()
    positions: tuple[str, ...] | None = DEFAULT_POSITIONS
    max_added_per_side: int = 1
    limit: int = MAX_SHOWN
    work_budget: int = WORK_BUDGET
    time_cap_seconds: float = TIME_CAP_SECONDS


def _check(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int],
           c: CounterofferConstraints, max_per_side: int) -> None:
    validate(state, team_a, team_b, a_sends, b_sends, max_per_side)
    if not 0 <= c.max_added_per_side <= MAX_ADDED:
        raise TradeError(f"max_added_per_side must be between 0 and {MAX_ADDED}")
    if not 1 <= c.limit <= MAX_SHOWN:
        raise TradeError(f"limit must be between 1 and {MAX_SHOWN}")
    offer = set(a_sends) | set(b_sends)
    stale = sorted(c.locked - offer)
    if stale:
        raise TradeError(f"locked players must be in the offer: {stale}")
    rostered = {p.espn_player_id for t in (team_a, team_b) for p in state.rosters[t]}
    unknown = sorted(c.excluded - rostered)
    if unknown:
        raise TradeError(f"excluded players must be on one of the two rosters: {unknown}")
    if c.locked & c.excluded:
        raise TradeError("a player cannot be both locked and excluded")
    if c.excluded & offer:
        raise TradeError(f"excluded players must not be in the offer: {sorted(c.excluded & offer)}")
    seen = Counter(p.espn_player_id for t in (team_a, team_b) for p in state.rosters[t])
    both = sorted(x for x, n in seen.items() if n > 1)
    if both:
        raise TradeError(f"players listed twice across the two rosters (stale roster data): {both}")


class _Search:
    def __init__(self, state, team_a, team_b, a_sends, b_sends, c: CounterofferConstraints, max_per_side):
        self.state, self.A, self.B, self.c, self.max_per_side = state, team_a, team_b, c, max_per_side
        self.orig = (tuple(sorted(a_sends)), tuple(sorted(b_sends)))
        # The exact engine of Trade Discovery for this pair of teams; no screening stage runs (no exact shapes).
        self.ts = TradeSearch(state, team_a, shapes=("3-for-3",), opponents=[team_b], positions=None,
                              beam_budget=None)
        self.t0 = time.perf_counter()
        self.spent = 0
        self.evaluated = 0
        self.not_evaluated = 0
        self.termination = "all_candidates_evaluated"
        self.status = {"evaluated": 0, "excluded_invalid_roster": 0, "excluded_evaluation_unavailable": 0,
                       "excluded_missing_data": 0, "no_effect": 0, "not_better": 0, "qualified": 0}
        T = self.ts.T
        rosters = {t: state.rosters[t] for t in (team_a, team_b)}
        self.owner = {p.espn_player_id: t for t, ps in rosters.items() for p in ps}
        self.position = {p.espn_player_id: p.position for ps in rosters.values() for p in ps}
        # Ordering signals (lineup totals, charged to the budget): what a player is worth to his own lineup now and what
        # he would add to the other team's. They only order the candidates; every listed number is exact.
        self.cost, self.adds = {}, {}
        for t, other in ((team_a, team_b), (team_b, team_a)):
            base, obase = sum(self.ts.base[t]), sum(self.ts.base[other])
            for p in rosters[t]:
                x = p.espn_player_id
                self.cost[x] = base - sum(T.vec_change(self.ts.roster_base[t], (x,)))
                self.adds[x] = sum(T.vec_change(self.ts.roster_base[other], (), (x,))) - obase
                self.spent += 2

    # ---- candidates
    def _addable(self, team: int, side: tuple) -> list[int]:
        c = self.c
        return sorted(p.espn_player_id for p in self.state.rosters[team]
                      if p.espn_player_id not in side and p.espn_player_id not in c.excluded
                      and (c.positions is None or p.position in c.positions))

    def _added(self, a: tuple, b: tuple) -> tuple[int, int]:
        return len(set(a) - set(self.orig[0])), len(set(b) - set(self.orig[1]))

    def _legal(self, a: tuple, b: tuple) -> bool:
        """Each side keeps 1..max_per_side players and adds at most max_added_per_side (locked players are never
        removed and excluded ones never added: edits() does not build such packages)."""
        if not a or not b or len(a) > self.max_per_side or len(b) > self.max_per_side or set(a) & set(b):
            return False
        return max(self._added(a, b)) <= self.c.max_added_per_side

    def edits(self, a: tuple, b: tuple, adds_only: bool = False) -> list[tuple]:
        """Every single edit of (a, b) as (kind, side, out_player, in_player, new_a, new_b), legal ones only."""
        out = []
        for side, team, cur in (("a", self.A, a), ("b", self.B, b)):
            other = b if side == "a" else a

            def pkg(new, side=side, other=other):
                new = tuple(sorted(new))
                return (new, other) if side == "a" else (other, new)
            free = [x for x in cur if x not in self.c.locked]
            if not adds_only:
                for x in free:
                    if len(cur) > 1:
                        out.append(("remove", side, x, None, *pkg(y for y in cur if y != x)))
                for x in free:
                    for y in self._addable(team, cur):
                        out.append(("swap", side, x, y, *pkg([*(z for z in cur if z != x), y])))
            for y in self._addable(team, cur):
                out.append(("add", side, None, y, *pkg([*cur, y])))
        return [e for e in out if self._legal(e[4], e[5])]

    def _approx(self, e: tuple, base: tuple[float, float]) -> float:
        """Signal-only estimate of the smaller side's delta after the edit (ordering, never shown)."""
        _, side, x, y = e[:4]
        da = db = 0.0
        if side == "a":            # team A gives more (y) or less (x): A loses/regains his value, B gains/loses
            if x is not None:
                da, db = da + self.cost[x], db - self.adds[x]
            if y is not None:
                da, db = da - self.cost[y], db + self.adds[y]
        else:
            if x is not None:
                da, db = da - self.adds[x], db + self.cost[x]
            if y is not None:
                da, db = da + self.adds[y], db - self.cost[y]
        return min(base[0] + da, base[1] + db)

    def _out_of_budget(self) -> bool:
        if self.spent >= self.c.work_budget:
            self.termination = "work_budget"
            return True
        if time.perf_counter() - self.t0 >= self.c.time_cap_seconds:
            self.termination = "time_cap"
            return True
        return False

    def pair(self, a: tuple, b: tuple):
        shape = f"{len(a)}-for-{len(b)}"
        mine, theirs, st = self.ts._pair(shape, self.B, a, b)
        self.spent += self.ts.pair_work[_key(shape, self.B, a, b)]
        return mine, theirs, st


def _side_out(d: dict, before: dict) -> dict:
    out = {k: d[k] for k in DELTA_KEYS}
    out["forced_drops"] = list(d["forced_drops"])
    out["change_vs_original"] = r(d["ros_delta"] - before["ros_delta"]) \
        if d["ros_delta"] is not None and before["ros_delta"] is not None else None
    return out


def _same(x: tuple, y: tuple) -> bool:
    return all(x[i][k] == y[i][k] for i in (0, 1) for k in EQUIVALENCE_KEYS) \
        and x[0]["forced_drops"] == y[0]["forced_drops"] and x[1]["forced_drops"] == y[1]["forced_drops"]


def _manual_matches(state: LeagueState, A: int, B: int, a: tuple, b: tuple, mine: dict, theirs: dict,
                    max_per_side: int) -> bool:
    res = evaluate_trade(state, A, B, list(a), list(b), max_per_side=max_per_side, scenarios=False)
    ea, eb = res["team_a"], res["team_b"]
    return all(ea["deltas"][k] == mine[k] and eb["deltas"][k] == theirs[k] for k in DELTA_KEYS) \
        and [d["espn_player_id"] for d in ea["roster"]["forced_drops"]] == mine["forced_drops"] \
        and [d["espn_player_id"] for d in eb["roster"]["forced_drops"]] == theirs["forced_drops"]


def counteroffers(state: LeagueState, team_a: int, team_b: int, a_sends, b_sends,
                  constraints: CounterofferConstraints | None = None,
                  max_per_side: int = MAX_PLAYERS_PER_SIDE) -> dict:
    """Up to `constraints.limit` counteroffers to the offer `team_a` sends `a_sends` for `b_sends`, best first, with
    the original offer's exact deltas, what the search covered and why nothing qualified when nothing did."""
    c = constraints or CounterofferConstraints()
    a_sends, b_sends = [int(x) for x in a_sends], [int(x) for x in b_sends]
    _check(state, team_a, team_b, a_sends, b_sends, c, max_per_side)
    s = _Search(state, team_a, team_b, a_sends, b_sends, c, max_per_side)
    oa, ob = s.orig
    o_mine, o_theirs, o_status = s.pair(oa, ob)
    base = {"team_a_sends": list(oa), "team_b_sends": list(ob)}
    out = {"team_a_id": team_a, "team_b_id": team_b,
           "constraints": {"locked": sorted(c.locked), "excluded": sorted(c.excluded),
                           "positions": list(c.positions) if c.positions is not None else None,
                           "max_added_per_side": c.max_added_per_side, "limit": c.limit},
           "evaluation": s.ts.evaluation(), "note": NOTE}
    if o_status != "evaluated" or o_mine["ros_delta"] is None or o_theirs["ros_delta"] is None:
        why = {"excluded_invalid_roster": "The original offer leaves a roster that cannot be made legal.",
               "excluded_evaluation_unavailable": "The original offer's forced drops are too many to resolve exactly."
               }.get(o_status, _unknown_reason(state, oa + ob))
        return {**out, "status": "original_unknown", "reason": why,
                "original": {**base, "status": o_status,
                             **({"team_a": _side_out(o_mine, o_mine), "team_b": _side_out(o_theirs, o_theirs)}
                                if o_status == "evaluated" else {})},
                "results": [], "search": {**_search_out(s), "exhaustive": False, "equivalent_hidden": 0,
                                          "inconsistent_dropped": 0}}
    o_min = min(o_mine["ros_delta"], o_theirs["ros_delta"])
    original = {**base, "status": "evaluated", "team_a": _side_out(o_mine, o_mine),
                "team_b": _side_out(o_theirs, o_theirs), "min_side_ros_delta": o_min}

    found: list[tuple] = []        # (sort key, row, effects)
    seen = {(oa, ob)}
    pkg_edits: dict[tuple, list[dict]] = {(oa, ob): []}
    exact: dict[tuple, tuple] = {}

    def run(cands: list[tuple], parent: tuple) -> None:
        order = sorted(cands, key=lambda e: (-s._approx(e, (exact[parent][0]["ros_delta"],
                                                            exact[parent][1]["ros_delta"])),
                                             EDIT_ORDER[e[0]], e[1], e[2] or 0, e[3] or 0))
        for e in order:
            a, b = e[4], e[5]
            if (a, b) in seen:
                continue
            seen.add((a, b))
            if s.termination != "all_candidates_evaluated" or s._out_of_budget():
                s.not_evaluated += 1
                continue
            mine, theirs, st = s.pair(a, b)
            s.evaluated += 1
            edit = {"kind": e[0], "team_id": s.A if e[1] == "a" else s.B, "out": e[2], "in": e[3]}
            pkg_edits[(a, b)] = pkg_edits[parent] + [edit]
            if st != "evaluated":
                s.status[st] += 1
                continue
            s.status["evaluated"] += 1
            exact[(a, b)] = (mine, theirs)
            if mine["ros_delta"] is None or theirs["ros_delta"] is None:
                s.status["excluded_missing_data"] += 1
                continue
            if _same((mine, theirs), (o_mine, o_theirs)):
                s.status["no_effect"] += 1
                continue
            m = min(mine["ros_delta"], theirs["ros_delta"])
            if not m > o_min:
                s.status["not_better"] += 1
                continue
            s.status["qualified"] += 1
            # equal values: the smaller package, then fewer edits, then ids (so an equivalent hides the bigger one)
            key = (-m, -(mine["ros_delta"] + theirs["ros_delta"]), len(a) + len(b), len(pkg_edits[(a, b)]), a, b)
            found.append((key, (a, b), (mine, theirs)))

    exact[(oa, ob)] = (o_mine, o_theirs)
    run(s.edits(oa, ob), (oa, ob))
    if c.max_added_per_side >= 2:
        firsts = sorted((k for k in exact if k != (oa, ob) and len(pkg_edits[k]) == 1
                         and exact[k][0]["ros_delta"] is not None and exact[k][1]["ros_delta"] is not None),
                        key=lambda k: (-min(exact[k][0]["ros_delta"], exact[k][1]["ros_delta"]), k))
        for k in firsts[:SECOND_STEP_WIDTH]:
            run(s.edits(*k, adds_only=True), k)

    found.sort(key=lambda z: z[0])
    shown, equivalent, inconsistent, checks = [], 0, 0, 0
    for _, (a, b), (mine, theirs) in found:
        if any(_same((mine, theirs), prev) for _, prev in shown):
            equivalent += 1            # same effects as a simpler (earlier-ranked) counteroffer
            continue
        if len(shown) >= c.limit or checks >= MANUAL_CHECKS:
            continue
        checks += 1
        if not _manual_matches(state, s.A, s.B, a, b, mine, theirs, max_per_side):
            inconsistent += 1
            continue
        shown.append(((a, b), (mine, theirs)))
    results = [_result(s, a, b, mine, theirs, o_mine, o_theirs, pkg_edits[(a, b)])
               for (a, b), (mine, theirs) in shown]
    search = {**_search_out(s), "equivalent_hidden": equivalent, "inconsistent_dropped": inconsistent,
              "manual_checks": checks}
    known = s.status["no_effect"] + s.status["not_better"] + s.status["qualified"]
    if results:
        status, reason = "found", None
    elif s.termination != "all_candidates_evaluated" and s.not_evaluated:
        status = "none_within_budget"
        reason = (f"No counteroffer was found before the search stopped ({_stop_text(s)}); "
                  f"{s.not_evaluated} packages were not evaluated and one of them may qualify.")
    elif s.evaluated == 0:
        status = "no_legal_edit"
        reason = ("No package can be built from this offer under the constraints: every player is locked, no "
                  "roster has an allowed player to add, or the per-side limit is reached.")
    elif inconsistent:
        status = "none_verified"
        reason = (f"{inconsistent} counteroffers qualified but the manual analyzer's evaluation did not match, so "
                  "none is shown.")
    elif known == 0:
        status = "none_known"
        reason = (f"Every one of the {s.evaluated} counteroffers evaluated has an unknown impact (a missing "
                  "projection, a roster that cannot be made legal, or forced drops too many to resolve), so none "
                  "can be compared with the offer.")
    else:
        status = "none_better"
        reason = (f"None of the {known} counteroffers with a known impact improves the side the original offer "
                  "treats worse" + (f" ({s.evaluated - known} more have an unknown impact)" if s.evaluated > known
                                    else "") + ".")
    return {**out, "status": status, "reason": reason, "original": original, "results": results, "search": search}


def _unknown_reason(state: LeagueState, ids: tuple) -> str:
    slots = unsupported_of(state.slots)
    if slots and any(not lineup_relevant(p, state.slots)
                     for t in state.rosters.values() for p in t if p.espn_player_id in ids):
        return (f"The offer moves a player who can start only in {', '.join(slots)}, a slot no engine fills, so its "
                "impact is unknown and there is nothing exact to improve on.")
    return ("A projection the original offer needs is missing for at least one evaluated week, so its impact is "
            "unknown and there is nothing exact to improve on.")


def _stop_text(s: _Search) -> str:
    return {"work_budget": f"work budget of {s.c.work_budget:,} lineup totals reached",
            "time_cap": f"time cap of {s.c.time_cap_seconds:g} s reached"}.get(s.termination, "")


def _search_out(s: _Search) -> dict:
    return {"packages_evaluated": s.evaluated, "packages_not_evaluated": s.not_evaluated,
            # every generated package evaluated; the second addition is only tried on the best few, so never exhaustive
            "termination": s.termination, "exhaustive": s.not_evaluated == 0 and s.c.max_added_per_side < 2,
            "work_spent": s.spent, "work_budget": s.c.work_budget,
            "unit": "lineup totals (exact package sides, forced-drop sets and ordering signals)",
            "statuses": dict(s.status), "seconds": round(time.perf_counter() - s.t0, 3),
            "stop_reason": _stop_text(s) or None,
            "scope": "single edit of the offer" + (", plus one more addition to the best few"
                                                     if s.c.max_added_per_side >= 2 else "")}


def _result(s: _Search, a, b, mine, theirs, o_mine, o_theirs, edits) -> dict:
    m, t = mine["ros_delta"], theirs["ros_delta"]
    return {"key": _key(f"{len(a)}-for-{len(b)}", s.B, a, b), "team_a_sends": list(a), "team_b_sends": list(b),
            "edits": edits, "team_a": _side_out(mine, o_mine), "team_b": _side_out(theirs, o_theirs),
            "min_side_ros_delta": min(m, t), "combined_ros_delta": r(m + t),
            "improves": {"team_a": m > o_mine["ros_delta"], "team_b": t > o_theirs["ros_delta"]},
            "consistent_with_manual_evaluator": True,
            "analyzer_link": {"ta": s.A, "tb": s.B, "a": list(a), "b": list(b)}}

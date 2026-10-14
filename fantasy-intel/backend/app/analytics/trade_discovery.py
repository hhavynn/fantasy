"""Automatic trade opportunity discovery on top of the exact lineup trade evaluator. Hypothetical,
pure and read-only (no database, no ESPN).

For one anchor team (the user's, by default) every 1-for-1, 2-for-1 and 1-for-2 trade with every
other roster is a candidate. A candidate's canonical impact is exactly what the manual evaluator
computes: incoming players active, forced drops resolved by the least lineup cost, the optimal
starting lineup rebuilt every remaining week, after minus before, missing projections never 0.
There is no trade-value chart, no new projection, no weighting and no acceptance estimate.

Search (details and proofs in docs/v2_trade_discovery.md):

  Stage A, screening with provable upper bounds. A team's known starting total in one week is the
  value of a maximum-weight assignment of players to slots, a weighted matroid rank function of
  the roster set. With non-negative projections it is monotone (adding a player never lowers it),
  submodular (a player adds no more to a bigger roster than to a smaller one) and a player adds at
  most his own projection. Exact totals are solved for every 1-for-1 swap and every one/two-player
  removal; two-player trades are bounded by

      L(R - s1 - s2 + t) <= min(L(R - s1 + t), L(R - s2 + t), L(R - s1 - s2) + v_t)
      L(R - s + t1 + t2) <= min(L(R - s + t1) + L(R - s + t2) - L(R - s), L(R - s + t1) + v_t2, ...)

  Forced drops only remove players, so these bound either side after the trade. A candidate is
  dropped only when its bound proves the anchor cannot gain (rest-of-season and playoff bounds
  both <= 0) or proves it cannot match the requested category. A negative projection voids the
  bounds, and then nothing is screened out.

  Stage B, exact canonical deltas, best-first. Survivors are evaluated in descending order of the
  upper bound of the requested sort key; the search stops when the K-th best exact value is above
  every remaining bound, so the returned top K is provably the true top K. Exact deltas follow
  trades.side_lineups rule for rule (same lineup optimum with bit-identical totals, the same
  forced-drop choice, the same completeness and rounding), on a faster totals-only solver.

  Stage C, the full manual evaluator (trades.evaluate_trade) for the candidates shown; its deltas
  are checked against stage B and returned as `consistent_with_manual_evaluator`.

A candidate qualifies when the anchor's canonical rest-of-season or playoff delta is positive.
Categories are sign-based (no thresholds). Orderings are descriptive, never acceptance estimates.
"""

from __future__ import annotations

import time
from bisect import insort
from collections import Counter
from dataclasses import replace
from itertools import combinations

from .lineup import FLEX_ELIGIBILITY, assign_lineup, lineup_total
from .team_lineups import RosterPlayer, lineup_relevant, r
from .trades import (MAX_FORCED_DROPS, LeagueState, NoRemainingWeeksError, TradeError, evaluate_trade,
                     legal_roster)

SHAPES: dict[str, tuple[int, int]] = {"1-for-1": (1, 1), "2-for-1": (2, 1), "1-for-2": (1, 2)}   # anchor sends, receives
SORTS = ("my_gain", "their_gain", "combined", "min_side", "playoff_gain")
CATEGORIES = {
    "MUTUAL_PROJECTED_IMPROVEMENT": "Both starting lineups project to gain.",
    "IMPROVES_MINE_COSTS_THEIRS": "Your lineup gains; theirs projects to lose starting points.",
    "IMPROVES_MINE_THEIRS_UNCHANGED": "Your lineup gains; theirs is projected unchanged.",
    "CONSOLIDATION": "Two-for-one in which the side receiving one player upgrades its starting lineup; "
                     "the other side's depth, displacement and forced drops are shown.",
    "PLAYOFF_FOCUSED": "Your playoff delta is positive while your regular-season delta is not.",
    "REGULAR_SEASON_FOCUSED": "Your regular-season delta is positive while your playoff delta is not.",
}
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
DEFAULT_MAX_EXACT = 20000
EPS = 1e-6          # float tolerance on bounds; canonical deltas are rounded to 0.01
NOTE = ("Every candidate's impact is the exact weekly optimal-lineup simulation on ESPN's projections, the same "
        "computation as the manual evaluator. Categories and orderings describe projected starting-lineup results "
        "only; they do not estimate whether a manager would accept.")


# ------------------------------------------------------------------ lineup totals --

class _Totals:
    """Known-projection optimal starting totals per week for arbitrary sets of player ids. With a
    single flex slot type the optimum is: best players per fixed slot, then the best remaining
    flex-eligible players (the exchange argument in analytics/lineup.py); mixed flex types use the
    exact assign_lineup itself."""

    def __init__(self, state: LeagueState, weeks: tuple[int, ...], pids: set[int], positions: dict[int, str | None],
                 teams: dict[int, str | None]):
        self.slots = state.slots
        fixed = [s for s in state.slots if s not in FLEX_ELIGIBILITY]
        self.fixed_groups = [(s, fixed.count(s)) for s in dict.fromkeys(fixed)]   # slot order (contiguous)
        flex = [s for s in state.slots if s in FLEX_ELIGIBILITY]
        self.m = len(flex)
        runs = [s for i, s in enumerate(fixed) if i == 0 or fixed[i - 1] != s]
        # Mixed flex types need the exact search; non-contiguous fixed slots would change the summation order.
        self.general = len(set(flex)) > 1 or len(runs) != len(self.fixed_groups)
        self.elig = set(FLEX_ELIGIBILITY[flex[0]]) if flex else set()
        self.flex_only = [p for p in sorted(self.elig) if p not in dict(self.fixed_groups)]
        self.pos = positions
        self.val = [{p: (positions.get(p), float(state.projections[p][w])) for p in pids
                     if state.projections.get(p, {}).get(w) is not None} for w in weeks]
        self.negative = any(v < 0 for row in self.val for _, v in row.values())
        relevant = {p for p in pids if lineup_relevant(positions.get(p), state.slots) and teams.get(p)}
        self.missing = [frozenset(p for p in relevant if p not in row) for row in self.val]
        self.solves = 0

    def week(self, ids, i: int) -> float:
        row = self.val[i]
        if self.general:
            return lineup_total(assign_lineup([{"espn_player_id": p, "position": self.pos.get(p), "value": row[p][1]}
                                               for p in ids if p in row], self.slots))
        by: dict = {}
        for p in ids:
            e = row.get(p)
            if e is not None:
                lst = by.get(e[0])
                if lst is None:
                    by[e[0]] = [e[1]]
                else:
                    lst.append(e[1])
        # Same picks, same order and the same sum() as project_week (slot order, best first; CPython's
        # float sum is compensated), so totals are bit-identical to the evaluator's and every rounded
        # delta matches.
        picks, rest = [], []
        for pos, n in self.fixed_groups:
            vs = by.get(pos)
            if vs:
                vs.sort(reverse=True)
                picks += vs[:n]
                if pos in self.elig:
                    rest += vs[n:]
        if self.m:
            for pos in self.flex_only:
                rest += by.get(pos, ())
            rest.sort(reverse=True)
            picks += rest[:self.m]
        return sum(picks)

    def vec(self, ids) -> tuple[float, ...]:
        self.solves += len(self.val)
        return tuple(self.week(ids, i) for i in range(len(self.val)))

    def complete(self, ids) -> tuple[bool, ...]:
        s = set(ids)
        return tuple(not (miss & s) for miss in self.missing)

    def value(self, pid: int, i: int) -> float:
        e = self.val[i].get(pid)
        return 0.0 if e is None else e[1]


def _active(players: list[RosterPlayer]) -> list[int]:
    return [p.espn_player_id for p in players if not p.on_ir]


def _minus(ids: list[int], *out: int) -> list[int]:
    return [x for x in ids if x not in out]


# --------------------------------------------------------------------- the search --

class TradeSearch:
    """Stage A runs once in the constructor; `top()` runs stages B (cached) for any sort/filter."""

    def __init__(self, state: LeagueState, anchor: int, shapes=tuple(SHAPES), opponents=None,
                 max_exact: int = DEFAULT_MAX_EXACT):
        t0 = time.perf_counter()
        requested = set(shapes)
        if anchor not in state.rosters:
            raise TradeError(f"unknown team {anchor}")
        if not requested or requested - set(SHAPES):
            raise TradeError(f"shapes must be among {', '.join(SHAPES)}")
        if state.start_week is None:
            reason = (state.start_week_reason or {}).get("reason") or "no actionable week"
            raise NoRemainingWeeksError(
                f"No remaining fantasy scoring periods ({reason}); there are no trades to discover.")
        self.state, self.anchor, self.max_exact = state, anchor, max_exact
        self.shapes = [s for s in SHAPES if s in requested]
        self.opponents = sorted(t for t in state.rosters if t != anchor and (opponents is None or t in set(opponents)))
        self.window = state.window()
        self.weeks = self.window.weeks
        self.split = self.window.last_regular is not None
        self.reg = [i for i, w in enumerate(self.weeks) if self.window.phase(w) == "regular"]
        self.po = [i for i, w in enumerate(self.weeks) if self.window.phase(w) == "playoff"]
        self.players = {p.espn_player_id: p for t in [anchor, *self.opponents] for p in state.rosters[t]}
        self.T = _Totals(state, self.weeks, set(self.players),
                         {p: x.position for p, x in self.players.items()}, {p: x.pro_team for p, x in self.players.items()})
        self.base = {t: self.T.vec(_active(state.rosters[t])) for t in [anchor, *self.opponents]}
        self.base_complete = {t: self.T.complete(_active(state.rosters[t])) for t in [anchor, *self.opponents]}
        self.cands = self._screen()
        self.exact: dict[str, dict | None] = {}
        self.stats = Counter()
        self.stage_a_seconds = round(time.perf_counter() - t0, 3)

    # ---- stage A
    def _ub(self, after, base) -> tuple[float, float, float]:
        d = [a - b for a, b in zip(after, base)]
        return sum(d), sum(d[i] for i in self.reg), sum(d[i] for i in self.po)

    def _screen(self) -> list[tuple]:
        T, A = self.T, self.anchor
        mine = self.state.rosters[A]
        a_act = _active(mine)
        a_base = self.base[A]
        m_ids = sorted(p.espn_player_id for p in mine)
        W = range(len(self.weeks))
        a_minus = {s: T.vec(_minus(a_act, s)) for s in m_ids}
        a_pair = {(s1, s2): T.vec(_minus(a_act, s1, s2)) for s1, s2 in combinations(m_ids, 2)} \
            if "2-for-1" in self.shapes else {}
        out = []
        self.screened_out = 0
        for opp in self.opponents:
            theirs = self.state.rosters[opp]
            o_act = _active(theirs)
            o_base = self.base[opp]
            o_ids = sorted(p.espn_player_id for p in theirs)
            o_minus = {t: T.vec(_minus(o_act, t)) for t in o_ids}
            o_pair = {(t1, t2): T.vec(_minus(o_act, t1, t2)) for t1, t2 in combinations(o_ids, 2)} \
                if "1-for-2" in self.shapes else {}
            a_swap = {(s, t): T.vec(_minus(a_act, s) + [t]) for s in m_ids for t in o_ids}
            o_swap = {(t, s): T.vec(_minus(o_act, t) + [s]) for s in m_ids for t in o_ids}
            v = {p: tuple(T.value(p, i) for i in W) for p in m_ids + o_ids}
            if "1-for-1" in self.shapes:
                for s in m_ids:
                    for t in o_ids:
                        self._add(out, "1-for-1", opp, (s,), (t,), a_swap[s, t], a_base,
                                  lambda s=s, t=t: o_swap[t, s], o_base)
            if "2-for-1" in self.shapes:
                for s1, s2 in combinations(m_ids, 2):
                    for t in o_ids:
                        x1, x2, xp, vt = a_swap[s1, t], a_swap[s2, t], a_pair[s1, s2], v[t]
                        me = tuple(min(x1[i], x2[i], xp[i] + vt[i]) for i in W)
                        self._add(out, "2-for-1", opp, (s1, s2), (t,), me, a_base,
                                  lambda s1=s1, s2=s2, t=t: tuple(
                                      min(o_swap[t, s1][i] + o_swap[t, s2][i] - o_minus[t][i],
                                          o_swap[t, s1][i] + v[s2][i], o_swap[t, s2][i] + v[s1][i]) for i in W),
                                  o_base)
            if "1-for-2" in self.shapes:
                for s in m_ids:
                    for t1, t2 in combinations(o_ids, 2):
                        x1, x2, xm = a_swap[s, t1], a_swap[s, t2], a_minus[s]
                        me = tuple(min(x1[i] + x2[i] - xm[i], x1[i] + v[t2][i], x2[i] + v[t1][i]) for i in W)
                        self._add(out, "1-for-2", opp, (s,), (t1, t2), me, a_base,
                                  lambda s=s, t1=t1, t2=t2: tuple(
                                      min(o_swap[t1, s][i], o_swap[t2, s][i], o_pair[t1, t2][i] + v[s][i])
                                      for i in W),
                                  o_base)
        return out

    def _add(self, out, shape, opp, sends, receives, me_after, me_base, them_after, them_base) -> None:
        me = self._ub(me_after, me_base)
        if not self.T.negative and me[0] <= EPS and not (self.split and me[2] > EPS):
            self.screened_out += 1          # provably no rest-of-season or playoff gain for the anchor
            return
        out.append((shape, opp, sends, receives, me, self._ub(them_after(), them_base)))

    # ---- stage B
    def _side(self, team: int, sends: tuple, receives: tuple) -> dict:
        """Exact canonical comparison for one side (the rules of trades.side_lineups + compare)."""
        st, T = self.state, self.T
        roster = st.rosters[team]
        pre = [p for p in roster if p.espn_player_id not in sends] + \
              [replace(self.players[x], on_ir=False) for x in receives]
        drops: list[int] = []
        if not legal_roster(pre, st, roster):
            combo = self._forced_drops(pre, roster, set(receives))
            drops = [p.espn_player_id for p in combo]
            pre = [p for p in pre if p not in combo]
        act = _active(pre)
        after, done_a = T.vec(act), T.complete(act)
        return self._compare(self.base[team], self.base_complete[team], after, done_a, drops)

    def _forced_drops(self, after: list[RosterPlayer], before: list[RosterPlayer], incoming: set[int]) -> tuple:
        """trades.resolve_roster's choice, on the same totals: the smallest number of drops that makes
        the roster legal, keeping the largest remaining known starting total (ties: lowest ids);
        an incoming player only when no legal drop without one exists."""
        pool = sorted(after, key=lambda p: p.espn_player_id)
        for k in range(1, MAX_FORCED_DROPS + 1):
            best: dict[bool, tuple] = {}
            for combo in combinations(pool, k):
                rest = [p for p in after if p not in combo]
                if not legal_roster(rest, self.state, before):
                    continue
                has_incoming = any(p.espn_player_id in incoming for p in combo)
                key = (round(sum(self.T.vec(_active(rest))), 9), tuple(-p.espn_player_id for p in combo))
                if has_incoming not in best or key > best[has_incoming][0]:
                    best[has_incoming] = (key, combo)
            if best:
                return (best.get(False) or best[True])[1]
        raise TradeError("no legal roster exists after this trade within the forced-drop limit")

    def _compare(self, before, done_b, after, done_a, drops) -> dict:
        d = [a - b for a, b in zip(after, before)]
        ok = [x and y for x, y in zip(done_b, done_a)]

        def agg(idx):
            return None if not all(ok[i] for i in idx) else r(sum(d[i] for i in idx))
        ros = agg(range(len(d)))
        rounded = [r(x) for x, k in zip(d, ok) if k]
        return {"ros_delta": ros,
                "regular_season_delta": agg(self.reg) if self.split else None,
                "playoff_delta": agg(self.po) if self.split else None,
                "known_ros_delta": r(sum(d)),
                "average_weekly_delta": r(sum(d) / len(d)) if ros is not None and d else None,
                "weeks_improved": sum(1 for x in rounded if x > 0),
                "weeks_worsened": sum(1 for x in rounded if x < 0),
                "weeks_unchanged": sum(1 for x in rounded if x == 0),
                "weeks_incomplete": [self.weeks[i] for i, k in enumerate(ok) if not k],
                "projection_complete": ros is not None,
                "forced_drops": drops}

    def evaluate(self, c: tuple) -> dict | None:
        """Exact row for a stage-A candidate; None when it does not qualify (counted in stats)."""
        shape, opp, sends, receives = c[:4]
        key = _key(shape, opp, sends, receives)
        if key in self.exact:
            return self.exact[key]
        self.stats["fully_evaluated"] += 1
        row = None
        try:
            mine = self._side(self.anchor, sends, receives)
            if mine["ros_delta"] is None:
                self.stats["excluded_missing_data"] += 1
            elif not (mine["ros_delta"] > 0 or (mine["playoff_delta"] is not None and mine["playoff_delta"] > 0)):
                self.stats["not_qualified"] += 1
            else:
                theirs = self._side(opp, receives, sends)
                row = self._row(shape, opp, sends, receives, mine, theirs)
                self.stats["qualified"] += 1
        except TradeError:
            self.stats["excluded_invalid_roster"] += 1
        self.exact[key] = row
        return row

    def _row(self, shape, opp, sends, receives, mine, theirs) -> dict:
        m, t = mine["ros_delta"], theirs["ros_delta"]
        drops = {"mine": mine.pop("forced_drops"), "theirs": theirs.pop("forced_drops")}
        return {"key": _key(shape, opp, sends, receives), "shape": shape, "anchor_team_id": self.anchor,
                "opponent_team_id": opp, "opponent_name": self.state.team_names.get(opp),
                "sends": list(sends), "receives": list(receives),
                "mine": mine, "theirs": theirs,
                "combined_ros_delta": r(m + t) if t is not None else None,
                "min_side_ros_delta": min(m, t) if t is not None else None,
                "forced_drops": drops, "categories": _categories(shape, mine, theirs)}

    def simpler_equivalent(self, row: dict) -> str | None:
        """A two-player candidate whose 1-for-1 sub-trade has identical canonical deltas for both
        teams: the extra player changes no projected lineup, so the simpler trade is shown instead."""
        if row["shape"] == "1-for-1":
            return None
        sig = ("ros_delta", "regular_season_delta", "playoff_delta")
        for s in row["sends"]:
            for t in row["receives"]:
                sub = self.evaluate(("1-for-1", row["opponent_team_id"], (s,), (t,)))
                if sub and all(sub[side][k] == row[side][k] for side in ("mine", "theirs") for k in sig):
                    return sub["key"]
        return None

    # ---- ranking
    def top(self, sort: str = "my_gain", category: str | None = None, opponent: int | None = None,
            limit: int = DEFAULT_LIMIT, include_redundant: bool = False) -> dict:
        if sort not in SORTS:
            raise TradeError(f"sort must be one of {', '.join(SORTS)}")
        if category is not None and category not in CATEGORIES:
            raise TradeError(f"category must be one of {', '.join(CATEGORIES)}")
        limit = max(1, min(int(limit), MAX_LIMIT))
        t0 = time.perf_counter()
        before = Counter(self.stats)
        pool = [c for c in self.cands if (opponent is None or c[1] == opponent) and self._may_match(c, category)]
        order = {s: i for i, s in enumerate(SHAPES)}
        pool.sort(key=lambda c: (-_bound(c, sort), order[c[0]], c[1], c[2], c[3]))
        rk = _rank_key(sort)
        found, redundant, examined, cap_hit, last_bound = [], 0, 0, False, None
        for i, c in enumerate(pool):
            ub = _bound(c, sort)
            if len(found) >= limit:
                kv = _sort_value(found[limit - 1], sort)
                if kv is not None and ub < kv - EPS:
                    last_bound = ub
                    break
            if examined >= self.max_exact:
                cap_hit, last_bound = True, ub
                break
            examined += 1
            row = self.evaluate(c)
            if row is None or (category and category not in row["categories"]):
                continue
            eq = self.simpler_equivalent(row)
            if eq and not include_redundant:
                redundant += 1
                continue
            insort(found, {**row, "simpler_equivalent": eq}, key=rk)
        else:
            i = len(pool)
        remaining = len(pool) - i
        results = found[:limit]
        delta = Counter(self.stats) - before
        return {
            "results": results,
            "search": {
                "candidates_considered": len(self.cands) + self.screened_out,
                "screened_out_by_upper_bound": self.screened_out,
                "survivors": len(self.cands),
                "matching_filters": len(pool),
                "fully_evaluated": examined,
                "exact_evaluations_cached": len(self.exact),
                "excluded_invalid_roster": delta["excluded_invalid_roster"],
                "excluded_missing_data": delta["excluded_missing_data"],
                "not_qualified": delta["not_qualified"],
                "qualified_found": len(found) + redundant,
                "simpler_equivalents_hidden": redundant,
                "not_evaluated": remaining,
                "not_evaluated_reason": None if not remaining else (
                    "evaluation cap reached" if cap_hit else
                    f"upper bound of the sort key ({_r(last_bound)}) is below the {limit}th result: "
                    "proven unable to enter the top results"),
                "largest_unevaluated_upper_bound": _r(last_bound) if remaining else None,
                "bounds_valid": not self.T.negative,
                "exhaustive": not cap_hit,
                "max_exact": self.max_exact,
                "lineup_solves_stage_a": self.T.solves,
                "seconds": {"stage_a": self.stage_a_seconds, "stage_b": round(time.perf_counter() - t0, 3)},
            },
        }

    def _may_match(self, c: tuple, category: str | None) -> bool:
        """Can this candidate possibly be in `category`, given its upper bounds? (exact: never drops a
        candidate that could match)."""
        if category is None or self.T.negative:
            return True
        me, them = c[4], c[5]
        if category == "MUTUAL_PROJECTED_IMPROVEMENT":
            return me[0] > EPS and them[0] > EPS
        if category in ("IMPROVES_MINE_COSTS_THEIRS", "IMPROVES_MINE_THEIRS_UNCHANGED"):
            return me[0] > EPS
        if category == "CONSOLIDATION":
            return (c[0] == "2-for-1" and me[0] > EPS) or (c[0] == "1-for-2" and them[0] > EPS)
        if category == "PLAYOFF_FOCUSED":
            return self.split and me[2] > EPS
        if category == "REGULAR_SEASON_FOCUSED":
            return self.split and me[1] > EPS
        return True

    def evaluation(self) -> dict:
        reason = self.state.start_week_reason or {}
        return {"start_week": self.window.start, "final_week": self.window.final,
                "last_regular_season_week": self.window.last_regular, "weeks": list(self.weeks),
                "uncertain": bool(reason.get("uncertain")), "warning": reason.get("warning"),
                "start_week_reason": reason.get("reason")}


DELTA_KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta", "average_weekly_delta",
              "weeks_improved", "weeks_worsened", "weeks_unchanged", "weeks_incomplete", "projection_complete")


def _r(x):
    return None if x is None else round(x, 2)


def _key(shape, opp, sends, receives) -> str:
    return f"{shape}:{opp}:{'-'.join(map(str, sends))}:{'-'.join(map(str, receives))}"


def _bound(c: tuple, sort: str) -> float:
    me, them = c[4], c[5]
    return {"my_gain": me[0], "their_gain": them[0], "combined": me[0] + them[0],
            "min_side": min(me[0], them[0]), "playoff_gain": me[2]}[sort]


def _sort_value(row: dict, sort: str):
    return {"my_gain": row["mine"]["ros_delta"], "their_gain": row["theirs"]["ros_delta"],
            "combined": row["combined_ros_delta"], "min_side": row["min_side_ros_delta"],
            "playoff_gain": row["mine"]["playoff_delta"]}[sort]


def _rank_key(sort: str):
    """Sort key descending (missing values last), then combined delta, shape, opponent, player ids."""
    order = {s: i for i, s in enumerate(SHAPES)}

    def k(row):
        v, c = _sort_value(row, sort), row["combined_ros_delta"]
        return (v is None, -(v or 0.0), c is None, -(c or 0.0), order[row["shape"]], row["opponent_team_id"],
                row["sends"], row["receives"])
    return k


def _categories(shape: str, mine: dict, theirs: dict) -> list[str]:
    out = []
    m, t = mine["ros_delta"], theirs["ros_delta"]
    if m is not None and m > 0 and t is not None:
        out.append("MUTUAL_PROJECTED_IMPROVEMENT" if t > 0 else "IMPROVES_MINE_COSTS_THEIRS" if t < 0
                   else "IMPROVES_MINE_THEIRS_UNCHANGED")
    consolidator = mine if shape == "2-for-1" else theirs if shape == "1-for-2" else None
    if consolidator is not None and consolidator["ros_delta"] is not None and consolidator["ros_delta"] > 0:
        out.append("CONSOLIDATION")
    reg, po = mine["regular_season_delta"], mine["playoff_delta"]
    if reg is not None and po is not None:
        if po > 0 >= reg:
            out.append("PLAYOFF_FOCUSED")
        elif reg > 0 >= po:
            out.append("REGULAR_SEASON_FOCUSED")
    return out


# --------------------------------------------------------------- stage C: detail --

def _usage(items: list[dict]) -> list[dict]:
    return [{k: u[k] for k in ("espn_player_id", "name", "position", "weeks_started", "slots")} for u in items]


def _side_detail(side: dict) -> dict:
    ro = side["roster"]
    pts = ("ros_projected_starting_points", "regular_season_projected_starting_points",
           "playoff_projected_starting_points")
    return {
        "team_id": side["team_id"], "team_name": side["team_name"],
        "deltas": {k: side["deltas"][k] for k in DELTA_KEYS},
        "before": {k: side["before"][k] for k in pts}, "after": {k: side["after"][k] for k in pts},
        "incoming_usage": _usage(side["incoming_usage"]), "outgoing_usage": _usage(side["outgoing_usage"]),
        "displaced": side["displaced"], "fit_notes": side["fit_notes"],
        "forced_drops": ro["forced_drops"], "forced_drop_lineup_cost": ro["forced_drop_lineup_cost"],
        "redundant_incoming_asset": ro["redundant_incoming_asset"],
        "open_active_roster_spots_before": ro["open_active_roster_spots_before"],
        "open_active_roster_spots": ro["open_active_roster_spots"],
        "hypothetical_fa_fill": side["hypothetical_fa_fill"],
        "incomplete": side["incomplete"],
        "ir_activation_sensitivity": side["ir_activation_sensitivity"],
        "weekly": [{"week": w["week"], "phase": w["phase"], "delta": w["delta"], "known_delta": w["known_delta"],
                    "entered": w["entered"], "exited": w["exited"]} for w in side["weekly"]],
    }


def detail(state: LeagueState, row: dict) -> dict:
    """The full manual evaluator for one discovered candidate. Its deltas must equal stage B's."""
    res = evaluate_trade(state, row["anchor_team_id"], row["opponent_team_id"], row["sends"], row["receives"])
    a, b = res["team_a"], res["team_b"]
    consistent = all(a["deltas"][k] == row["mine"][k] and b["deltas"][k] == row["theirs"][k] for k in DELTA_KEYS) \
        and [d["espn_player_id"] for d in a["roster"]["forced_drops"]] == row["forced_drops"]["mine"] \
        and [d["espn_player_id"] for d in b["roster"]["forced_drops"]] == row["forced_drops"]["theirs"]
    players = [{**{k: p[k] for k in ("espn_player_id", "name", "position", "pro_team", "injury_status", "on_ir",
                                     "from_team_id", "to_team_id")},
                "marginal_value_on_current_team": p["marginal_value_on_current_team"]["roster_marginal_lineup_points"]}
               for p in res["players"]]
    return {**row, "consistent_with_manual_evaluator": consistent,
            "result_label": res["result"]["label"], "regular_season_label": res["result"]["regular_season_label"],
            "playoff_label": res["result"]["playoff_label"], "players": players,
            "mine_detail": _side_detail(a), "theirs_detail": _side_detail(b),
            "ir_warning": res["ir_warning"], "ir_assumption": res["ir_assumption"],
            "manual_evaluator_link": {"ta": row["anchor_team_id"], "tb": row["opponent_team_id"],
                                      "a": row["sends"], "b": row["receives"]}}

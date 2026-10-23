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
  bounds: then nothing is screened out and no bound ends the search.

  Stage B, exact canonical deltas, best-first. Survivors are evaluated in descending order of the
  upper bound of the requested sort key; with valid bounds the search stops when the K-th best exact
  value is above every remaining bound, so the returned top K is provably the true top K. With
  invalid bounds every candidate matching the filters is evaluated; an evaluation cap or a work budget
  (DEFAULT_MAX_WORK, counted in lineup weeks solved) ends either search early and is reported in words
  (`termination`, `not_evaluated_reason`, `exhaustive`). Exact deltas follow
  trades.side_lineups rule for rule (same lineup optimum with bit-identical totals, the same
  forced-drop choice, the same completeness and rounding), on a faster totals-only solver.

  Stage C, the full manual evaluator (trades.evaluate_trade) for the candidates shown; its deltas
  are checked against stage B and returned as `consistent_with_manual_evaluator`.

A candidate qualifies when the anchor's canonical rest-of-season or playoff delta is positive.
Categories are sign-based (no thresholds). Orderings are descriptive, never acceptance estimates.

Targeted discovery (issue #182, docs/v2_trade_discovery.md "Targeted discovery") adds two optional inputs, neither of
which is a weight or a new valuation:

  * a TradeTarget (a named player and/or a position the anchor wants to receive) filters the candidates every search
    stage ranks: the exact top K is the exact top K among matching candidates (the bounds are per candidate, so
    filtering never invalidates them), and the beam seeds only matching 1-for-1 packages (it only adds players, so
    every package it grows still matches);
  * focus weeks (a short horizon or a bye week inside the window) add the anchor's focus delta: the same canonical
    weekly deltas summed over those weeks (NULL when any of them is incomplete), its own stage-A bound (a sum of the
    same weekly bounds) and its own sort, `focus_gain`. A candidate whose focus delta is positive also qualifies, and
    missing data is judged over the focus weeks: an unknown rest-of-season total does not exclude a candidate whose
    focus delta is complete. The targeted regular-season horizon is the regular-season weeks as focus weeks
    (trade_practical.regular_season_weeks), so a regular-season-only gain is screened, bounded and qualified too.
"""

from __future__ import annotations

import time
from bisect import insort
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations

from .lineup import FLEX_ELIGIBILITY, assign_lineup, lineup_total, separable, unsupported_of
from .team_lineups import NDIGITS, STREAMER_IDS, RosterPlayer, lineup_relevant, r, slot_filled, stream_levels
from .trades import (EvaluationUnavailable, LeagueState, NoRemainingWeeksError, TradeError, choose_forced_drops,
                     evaluate_trade, legal_roster)

# Exact shapes: searched with provable upper bounds (stage A) and a proven best-first top K (stage B).
SHAPES: dict[str, tuple[int, int]] = {"1-for-1": (1, 1), "2-for-1": (2, 1), "1-for-2": (1, 2)}   # anchor sends, receives
# Larger shapes: a bounded beam expansion (see _Beam). Every row is exact, but the list is NOT a
# proven top K: the bounds above do not extend to these shapes without enumerating them.
HEURISTIC_SHAPES: dict[str, tuple[int, int]] = {"2-for-2": (2, 2), "3-for-1": (3, 1), "1-for-3": (1, 3),
                                                "3-for-2": (3, 2), "2-for-3": (2, 3), "3-for-3": (3, 3)}
ALL_SHAPES = {**SHAPES, **HEURISTIC_SHAPES}
SORTS = ("my_gain", "their_gain", "combined", "min_side", "playoff_gain", "min_side_regular", "min_side_playoff")
# Anchor-side sorts used by targeted discovery only (not offered by the API's `sort`): the anchor's regular-season delta
# and his delta over the search's focus weeks.
TARGET_SORTS = ("regular_gain", "focus_gain")
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
DEFAULT_POSITIONS = SKILL_POSITIONS          # K and D/ST only when asked for (docs: "D/ST and K policy")
BEAM_SEEDS = 6                               # 1-for-1 seeds per opponent (by stage-A smaller-side bound)
BEAM_WIDTH = 6                               # packages kept per expansion step
BEAM_POOL = 4                                # players per side and signal offered to the expansion
CATEGORIES = {
    "MUTUAL_PROJECTED_IMPROVEMENT": "Both starting lineups project to gain.",
    "IMPROVES_MINE_COSTS_THEIRS": "Your lineup gains; theirs projects to lose starting points.",
    "IMPROVES_MINE_THEIRS_UNCHANGED": "Your lineup gains; theirs is projected unchanged.",
    "CONSOLIDATION": "Uneven trade (2-for-1, 3-for-1, 3-for-2 and the reverse) in which the side receiving fewer "
                     "players upgrades its starting lineup; the other side's depth, displacement and forced drops are "
                     "shown.",
    "PLAYOFF_FOCUSED": "Your playoff delta is positive while your regular-season delta is not.",
    "REGULAR_SEASON_FOCUSED": "Your regular-season delta is positive while your playoff delta is not.",
}
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
DEFAULT_MAX_EXACT = 20000
# Work budgets (_Totals.work: lineup weeks solved, GENERAL_COST each on the general solver; about 2-6 microseconds a
# unit). An exact top() call stops once the evaluations it made or reused cost DEFAULT_MAX_WORK, and the beam gives
# each manager an equal share of DEFAULT_MAX_BEAM_WORK; either says so in words. An evaluation's cost is recorded
# once, so a cached one counts the same and a result never depends on what ran before it. The standard matrix leagues
# use under 15% of either budget and FX-17 (#253 M2) under 35%, so they finish exactly as without a budget
# (docs/v2_trade_discovery.md "Work budget").
DEFAULT_MAX_WORK = 1_000_000
DEFAULT_MAX_BEAM_WORK = 2_000_000
GENERAL_COST = 8                         # a general-solver week costs about this many fast weeks
EPS = 1e-6          # float tolerance on bounds
HALF_UNIT = 0.5 * 10 ** -NDIGITS   # a canonical delta is rounded, so it can exceed its exact value by this much
# How far a displayed sort value can exceed the exact value its bound applies to: one rounding for a
# single delta (and for min_side, a min of rounded deltas); combined = round(round(mine) + round(theirs)).
ROUNDING_SLACK = {"my_gain": HALF_UNIT, "their_gain": HALF_UNIT, "playoff_gain": HALF_UNIT,
                  "min_side": HALF_UNIT, "combined": 3 * HALF_UNIT, "min_side_regular": HALF_UNIT,
                  "min_side_playoff": HALF_UNIT, "regular_gain": HALF_UNIT, "focus_gain": HALF_UNIT}
NOTE = ("Every candidate's impact is the exact weekly optimal-lineup simulation on ESPN's projections, the same "
        "computation as the manual evaluator. Categories and orderings describe projected starting-lineup results "
        "only; they do not estimate whether a manager would accept.")


@dataclass(frozen=True)
class TradeTarget:
    """What the anchor asked to receive (issue #182): one named player, at least one player at a position, or both.
    A filter on candidates, never a weight: a matching trade is valued and ranked exactly like any other."""
    player: int | None = None
    position: str | None = None

    def matches(self, receives, position_of) -> bool:
        if self.player is not None and self.player not in receives:
            return False
        return self.position is None or any(position_of(x) == self.position for x in receives)

    @property
    def key(self) -> tuple:
        return (self.player, self.position)


def _target_key(target: TradeTarget | None) -> tuple:
    return (None, None) if target is None else target.key


# ------------------------------------------------------------------ lineup totals --

class _Totals:
    """Known-projection optimal starting totals per week for arbitrary sets of player ids. With a
    single flex slot type the optimum is: best players per fixed slot, then the best remaining
    flex-eligible players (the exchange argument in analytics/lineup.py); mixed flex types use the
    exact assign_lineup itself."""

    def __init__(self, state: LeagueState, weeks: tuple[int, ...], pids: set[int], positions: dict[int, str | None],
                 teams: dict[int, str | None], blocked: dict[int, frozenset] | None = None):
        self.slots = state.slots
        fixed = [s for s in state.slots if s not in FLEX_ELIGIBILITY]
        self.fixed_groups = [(s, fixed.count(s)) for s in dict.fromkeys(fixed)]   # slot order (contiguous)
        flex = [s for s in state.slots if s in FLEX_ELIGIBILITY]
        self.m = len(flex)
        runs = [s for i, s in enumerate(fixed) if i == 0 or fixed[i - 1] != s]
        self.pos = positions
        blocked = blocked or {}
        # A verified NFL-ineligible week (RosterPlayer.unavailable_weeks): he cannot start and his missing
        # or present projection never matters, exactly as in team_lineups.project_week.
        self.val = [{p: (positions.get(p), float(state.projections[p][w])) for p in pids
                     if state.projections.get(p, {}).get(w) is not None and w not in blocked.get(p, ())} for w in weeks]
        self.negative = any(v < 0 for row in self.val for _, v in row.values())
        # Nested flex types (each one's positions inside the next one's, every type contiguous: FLEX then OP, RB/WR
        # then FLEX) with no negative value: filling the flex slots in order, each with the best remaining eligible
        # player, is optimal (an exchange argument: a later slot takes anyone an earlier one could), so it reaches the
        # matching bound and lineup._best_flex returns exactly that first leaf. Ties do not change the values: a tied
        # player of either position stays eligible for every later slot. Other mixes need the exact search.
        types = list(dict.fromkeys(flex))
        flex_runs = [s for i, s in enumerate(flex) if i == 0 or flex[i - 1] != s]
        nested = len(flex_runs) == len(types) and all(
            set(FLEX_ELIGIBILITY[a]) <= set(FLEX_ELIGIBILITY[b]) for a, b in zip(types, types[1:]))
        self.chain = [(s, FLEX_ELIGIBILITY[s]) for s in flex] if len(types) > 1 and nested and not self.negative \
            else None
        # Mixed flex types need the exact search unless nested; non-contiguous fixed slots would change the
        # summation order.
        self.general = (len(types) > 1 and self.chain is None) or len(runs) != len(self.fixed_groups)
        self.elig = {q for s in types for q in FLEX_ELIGIBILITY[s]}
        self.flex_only = [p for p in sorted(self.elig) if p not in dict(self.fixed_groups)]
        # The K/D/ST waiver streamer (team_lineups.project_week): a player every roster always has, worth the
        # week's streaming baseline. Adding one fixed element to every set keeps the lineup total monotone and
        # submodular, so every stage-A bound below stays valid.
        levels = stream_levels(state.projections)
        self.stream = [{pos: per[w] for pos, per in levels.items() if pos in state.slots and per.get(w) is not None}
                       for w in weeks]
        relevant = {p for p in pids if lineup_relevant(positions.get(p), state.slots) and teams.get(p)}
        self.missing = [frozenset(p for p in relevant if p not in row and w not in blocked.get(p, ()))
                        for w, row in zip(weeks, self.val)]
        unsupported = unsupported_of(state.slots)
        if unsupported and not separable(unsupported):
            # an unrecognised slot may take any position, so no delta is exact (team_lineups.delta_exact, #253 G3)
            self.missing = [frozenset(relevant) for _ in weeks]
        self.solves = 0
        # Work done, for the search budgets: one unit per week solved, GENERAL_COST per week on the exact general
        # solver (lineup.assign_lineup), and at least one per subset total even when every week is reused.
        self.work = 0
        self.week_cost = GENERAL_COST if self.general else 1

    def week(self, ids, i: int) -> float:
        row, stream = self.val[i], self.stream[i]
        if self.general:
            return lineup_total(assign_lineup([{"espn_player_id": p, "position": self.pos.get(p), "value": row[p][1]}
                                               for p in ids if p in row]
                                              + [{"espn_player_id": STREAMER_IDS[pos], "position": pos, "value": v}
                                                 for pos, v in stream.items()], self.slots))
        by: dict = {pos: [v] for pos, v in stream.items()}
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
        if self.chain is not None:
            return self._from_sorted({pos: sorted(((v, None) for v in vs), key=lambda vp: -vp[0])
                                      for pos, vs in by.items()})[0]
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
        self.work += len(self.val) * self.week_cost
        return tuple(self.week(ids, i) for i in range(len(self.val)))

    def subsets(self, ids):
        """A function giving sum(self.vec(x)) for any subset x of `ids` (forced-drop resolution solves dozens of them),
        from each week's per-position lists sorted once: the same picks in the same order, so the same sums. A
        position contributes at most its fixed slots plus every flex slot, so only that many players are read, and a
        week in which no removed player starts keeps its total: the picks never reach a removed player, so they are the
        same players in the same order."""
        if self.general:
            return lambda x: sum(self.vec(x))
        everyone = frozenset(ids)
        self.work += len(self.val)
        fixed = dict(self.fixed_groups)
        need = {pos: fixed.get(pos, 0) + (self.m if pos in self.elig else 0) for pos in {*fixed, *self.elig}}
        weeks = []
        for row, stream in zip(self.val, self.stream):
            by: dict = {pos: [(v, None)] for pos, v in stream.items()}       # the streamer is never dropped
            for p in everyone:
                e = row.get(p)
                if e is not None and e[0] in need:
                    by.setdefault(e[0], []).append((e[1], p))
            for lst in by.values():
                lst.sort(key=lambda vp: -vp[0])
            weeks.append((by, *self._from_sorted({pos: lst[:need[pos]] for pos, lst in by.items()})))

        def total(x) -> float:
            keep = set(x)
            if not keep <= everyone:
                return sum(self.vec(x))         # not a subset: solved from scratch
            gone = everyone - keep
            self.solves += len(weeks)
            out, solved = [], 0
            for by, full, starters in weeks:
                if gone.isdisjoint(starters):
                    out.append(full)
                else:
                    out.append(self._from_sorted({pos: [vp for vp in lst if vp[1] not in gone][:need[pos]]
                                                  for pos, lst in by.items()})[0])
                    solved += 1
            self.work += max(1, solved)
            return sum(out)
        return total

    def _from_sorted(self, by: dict) -> tuple[float, set]:
        """week() on per-position (value, player) lists already sorted best first: the total and who starts."""
        picks, rest = [], []
        left = {}
        for pos, n in self.fixed_groups:
            vs = by.get(pos)
            if vs:
                picks += vs[:n]
                if pos in self.elig:
                    left[pos] = vs[n:]
                    rest += vs[n:]
        for pos in self.flex_only:
            vs = by.get(pos)
            if vs:
                left[pos] = vs
                rest += vs
        if self.chain is not None:
            at = dict.fromkeys(left, 0)
            for _, elig in self.chain:
                best = None
                for pos in elig:
                    lst = left.get(pos)
                    if lst is not None and at[pos] < len(lst) and \
                            (best is None or lst[at[pos]][0] > left[best][at[best]][0]):
                        best = pos
                if best is not None:
                    picks.append(left[best][at[best]])
                    at[best] += 1
        elif self.m:
            rest.sort(key=lambda vp: -vp[0])
            picks += rest[:self.m]
        return sum(v for v, _ in picks), {p for _, p in picks}

    def complete(self, ids) -> tuple[bool, ...]:
        s = set(ids)
        return tuple(not (miss & s) for miss in self.missing)

    def value(self, pid: int, i: int) -> float:
        e = self.val[i].get(pid)
        return 0.0 if e is None else e[1]


def _active(players: list[RosterPlayer]) -> list[int]:
    return [p.espn_player_id for p in players if not p.reserved]    # IR and taxi players never start


def _minus(ids: list[int], *out: int) -> list[int]:
    return [x for x in ids if x not in out]


# --------------------------------------------------------------------- the search --

class TradeSearch:
    """Stage A runs once in the constructor; `top()` runs stages B (cached) for any sort/filter."""

    def __init__(self, state: LeagueState, anchor: int, shapes=tuple(SHAPES), opponents=None,
                 max_exact: int = DEFAULT_MAX_EXACT, positions=DEFAULT_POSITIONS, focus_weeks=None,
                 max_work: int = DEFAULT_MAX_WORK, max_beam_work: int = DEFAULT_MAX_BEAM_WORK):
        t0 = time.perf_counter()
        requested = set(shapes)
        if anchor not in state.rosters:
            raise TradeError(f"unknown team {anchor}")
        if not requested or requested - set(ALL_SHAPES):
            raise TradeError(f"shapes must be among {', '.join(ALL_SHAPES)}")
        if state.start_week is None:
            reason = (state.start_week_reason or {}).get("reason") or "no actionable week"
            raise NoRemainingWeeksError(
                f"No remaining fantasy scoring periods ({reason}); there are no trades to discover.")
        self.state, self.anchor, self.max_exact = state, anchor, max_exact
        self.max_work, self.max_beam_work = max_work, max_beam_work
        self.cost: dict[str, int] = {}               # work each exact evaluation (and beam package) cost, by key
        self._ledger: dict[str, int] | None = None    # the evaluations the running top() call has charged
        self._spent = 0
        # Positions that may be traded (players elsewhere stay on rosters and can still be forced drops).
        self.positions = None if positions is None else tuple(sorted(set(positions)))
        self.shapes = [s for s in SHAPES if s in requested]
        self.heuristic_shapes = [s for s in HEURISTIC_SHAPES if s in requested]
        self.opponents = sorted(t for t in state.rosters if t != anchor and (opponents is None or t in set(opponents)))
        self.window = state.window()
        self.weeks = self.window.weeks
        self.split = self.window.last_regular is not None
        self.reg = [i for i, w in enumerate(self.weeks) if self.window.phase(w) == "regular"]
        self.po = [i for i, w in enumerate(self.weeks) if self.window.phase(w) == "playoff"]
        # Focus weeks (targeted discovery): a short horizon or a bye week, every one inside the evaluation window.
        if focus_weeks is not None:
            focus_weeks = tuple(sorted(set(focus_weeks)))
            if not focus_weeks or any(w not in self.weeks for w in focus_weeks):
                raise TradeError(f"focus weeks must be within the evaluation window "
                                 f"(weeks {self.weeks[0]}-{self.weeks[-1]})" if self.weeks else "no evaluation weeks")
        self.focus_weeks = focus_weeks
        self.focus = [self.weeks.index(w) for w in focus_weeks] if focus_weeks else None
        self.players = {p.espn_player_id: p for t in [anchor, *self.opponents] for p in state.rosters[t]}
        self.T = _Totals(state, self.weeks, set(self.players),
                         {p: x.position for p, x in self.players.items()}, {p: x.pro_team for p, x in self.players.items()},
                         {p: x.unavailable_weeks for p, x in self.players.items() if x.unavailable_weeks})
        self.base = {t: self.T.vec(_active(state.rosters[t])) for t in [anchor, *self.opponents]}
        self.base_complete = {t: self.T.complete(_active(state.rosters[t])) for t in [anchor, *self.opponents]}
        self.exact: dict[str, dict | None] = {}
        self.status: dict[str, str] = {}
        self.pairs: dict[str, tuple] = {}            # heuristic exact evaluations (both sides), by key
        self.cands = self._screen() if self.shapes else []
        if not self.shapes:
            self.screened_out = 0
        self._beams: dict[tuple, dict] = {}         # beam() results per target
        self.stage_a_seconds = round(time.perf_counter() - t0, 3)

    def tradeable(self, players) -> list[int]:
        return sorted(p.espn_player_id for p in players if self.positions is None or p.position in self.positions)

    # ---- stage A
    def _ub(self, after, base) -> tuple[float, float, float, float]:
        """(rest of season, regular season, playoffs, focus weeks) sums of one side's weekly values."""
        d = [a - b for a, b in zip(after, base)]
        return sum(d), sum(d[i] for i in self.reg), sum(d[i] for i in self.po), \
            (sum(d[i] for i in self.focus) if self.focus else 0.0)

    def _screen(self) -> list[tuple]:
        T, A = self.T, self.anchor
        mine = self.state.rosters[A]
        a_act = _active(mine)
        a_base = self.base[A]
        m_ids = self.tradeable(mine)
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
            o_ids = self.tradeable(theirs)
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
        if not self.T.negative and me[0] <= EPS and not (self.split and me[2] > EPS) and not (self.focus and me[3] > EPS):
            self.screened_out += 1          # provably no rest-of-season, playoff or focus-week gain for the anchor
            return
        out.append((shape, opp, sends, receives, me, self._ub(them_after(), them_base)))

    # ---- stage B
    def _side(self, team: int, sends: tuple, receives: tuple) -> dict:
        """Exact canonical comparison for one side (the rules of trades.side_lineups + compare)."""
        st, T = self.state, self.T
        roster = st.rosters[team]
        pre = [p for p in roster if p.espn_player_id not in sends] + \
              [replace(self.players[x], on_ir=False, on_taxi=False) for x in receives]
        drops: list[int] = []
        if not legal_roster(pre, st, roster):
            combo = self._forced_drops(pre, roster, set(receives))
            drops = [p.espn_player_id for p in combo]
            pre = [p for p in pre if p not in combo]
        act = _active(pre)
        after, done_a = T.vec(act), T.complete(act)
        if unsupported_of(st.slots):
            # team_lineups.delta_exact: the slots no engine fills add the same to both lineups only when the trade and
            # its forced drops leave the players who can start only there unchanged (#253 G3)
            def offslot(ids) -> frozenset:
                return frozenset(i for i in ids if not lineup_relevant(T.pos.get(i), st.slots))
            if offslot(_active(roster)) != offslot(act):
                done_a = tuple(False for _ in done_a)
        return self._compare(self.base[team], self.base_complete[team], after, done_a, drops)

    def _forced_drops(self, after: list[RosterPlayer], before: list[RosterPlayer], incoming: set[int]) -> tuple:
        """trades.resolve_roster's choice (the same shared trades.choose_forced_drops) on the same
        bit-identical totals."""
        memo: dict[tuple, float] = {}
        of_subset = self.T.subsets(_active(after))      # every roster the rule solves is `after` minus some drops

        def total(players):
            key = tuple(sorted(_active(players)))
            if key not in memo:
                memo[key] = of_subset(list(key))
            return memo[key]
        res = choose_forced_drops(after, before, self.state, incoming, total, self.T.negative)
        return (res["best"].get(False) or res["best"][True])[1]

    def _compare(self, before, done_b, after, done_a, drops) -> dict:
        d = [a - b for a, b in zip(after, before)]
        ok = [x and y for x, y in zip(done_b, done_a)]

        def agg(idx):
            return None if not all(ok[i] for i in idx) else r(sum(d[i] for i in idx))
        ros = agg(range(len(d)))
        rounded = [r(x) for x, k in zip(d, ok) if k]
        focus = {"focus_delta": agg(self.focus)} if self.focus else {}
        return {**focus, "ros_delta": ros,
                "regular_season_delta": agg(self.reg) if self.split else None,
                "playoff_delta": agg(self.po) if self.split else None,
                "known_ros_delta": r(sum(d)),
                "average_weekly_delta": r(sum(d) / len(d)) if ros is not None and d else None,
                "weeks_improved": sum(1 for x in rounded if x > 0),
                "weeks_worsened": sum(1 for x in rounded if x < 0),
                "weeks_unchanged": sum(1 for x in rounded if x == 0),
                "weeks_incomplete": [self.weeks[i] for i, k in enumerate(ok) if not k],
                "projection_complete": ros is not None,
                "weekly_known_deltas": [r(x) for x in d],
                "forced_drops": drops}

    def evaluate(self, c: tuple) -> dict | None:
        """Exact row for a candidate; None when it does not qualify. Cached with its status
        (qualified, not_qualified, excluded_missing_data, excluded_invalid_roster)."""
        shape, opp, sends, receives = c[:4]
        key = _key(shape, opp, sends, receives)
        if key not in self.exact:
            w0 = self.T.work
            self._evaluate(shape, opp, sends, receives, key)
            self.cost[key] = self.T.work - w0
        if self._ledger is not None and key not in self._ledger:
            self._ledger[key] = self.cost[key]
            self._spent += self.cost[key]
        return self.exact[key]

    def _evaluate(self, shape, opp, sends, receives, key) -> None:
        row, status = None, "not_qualified"
        try:
            mine = self._side(self.anchor, sends, receives)
            if self._incomplete(mine):
                status = "excluded_missing_data"
            elif _qualifies(mine):
                theirs = self._side(opp, receives, sends)
                row, status = self._row(shape, opp, sends, receives, mine, theirs), "qualified"
        except EvaluationUnavailable:
            status = "excluded_evaluation_unavailable"
        except TradeError:
            status = "excluded_invalid_roster"
        self.exact[key], self.status[key] = row, status

    def _row(self, shape, opp, sends, receives, mine, theirs) -> dict:
        m, t = mine["ros_delta"], theirs["ros_delta"]
        drops = {"mine": mine.pop("forced_drops"), "theirs": theirs.pop("forced_drops")}
        return {"key": _key(shape, opp, sends, receives), "shape": shape, "anchor_team_id": self.anchor,
                "search_method": "heuristic_beam" if shape in HEURISTIC_SHAPES else "exact",
                "opponent_team_id": opp, "opponent_name": self.state.team_names.get(opp),
                "sends": list(sends), "receives": list(receives),
                "mine": mine, "theirs": theirs,
                "combined_ros_delta": r(m + t) if m is not None and t is not None else None,
                "min_side_ros_delta": min(m, t) if m is not None and t is not None else None,
                "forced_drops": drops, "categories": _categories(shape, mine, theirs)}

    def _incomplete(self, mine: dict) -> bool:
        """The anchor's canonical total is unknown over the horizon the search judges: the focus weeks when it has
        them (a missing projection outside them never hides a complete focus gain), else the rest of the season."""
        return (mine.get("focus_delta") if self.focus else mine["ros_delta"]) is None

    def position_of(self, pid: int) -> str | None:
        p = self.players.get(pid)
        return p.position if p else None

    def _matches(self, target: TradeTarget | None, receives) -> bool:
        return target is None or target.matches(receives, self.position_of)

    def simpler_equivalent(self, row: dict, category: str | None = None,
                           target: TradeTarget | None = None) -> dict | None:
        """The 1-for-1 sub-trade with identical canonical effects for both teams (every aggregate and
        weekly delta and the forced drops: the extra player changes no projected lineup and no roster
        move), but only when that simpler trade is itself eligible for the current results:
        1-for-1 is a searched shape, it qualifies, it belongs to the requested category and it still brings in the
        requested target (same opponent by construction). Otherwise None, and the two-player trade is shown."""
        if row["shape"] == "1-for-1" or "1-for-1" not in self.shapes:
            return None
        for s in row["sends"]:
            for t in row["receives"]:
                if not self._matches(target, (t,)):
                    continue
                sub = self.evaluate(("1-for-1", row["opponent_team_id"], (s,), (t,)))
                if sub and (category is None or category in sub["categories"]) and _same_effects(sub, row):
                    return sub
        return None

    # ---- larger shapes: bounded beam expansion (heuristic coverage, exact rows)
    def _pair(self, shape: str, opp: int, sends: tuple, receives: tuple):
        """Exact both-side comparison of any package (cached); (mine, theirs, status)."""
        key = _key(shape, opp, sends, receives)
        if key not in self.pairs:
            w0 = self.T.work
            try:
                self.pairs[key] = (self._side(self.anchor, sends, receives), self._side(opp, receives, sends), "evaluated")
            except EvaluationUnavailable:
                self.pairs[key] = (None, None, "excluded_evaluation_unavailable")
            except TradeError:
                self.pairs[key] = (None, None, "excluded_invalid_roster")
            self.cost["pair:" + key] = self.T.work - w0
        return self.pairs[key]

    def beam(self, target: TradeTarget | None = None) -> dict:
        """Candidate generation for HEURISTIC_SHAPES, per opponent:

          1. signals from exact lineup totals: what each player costs his own team (removal), what he
             would add to the other team (addition), and his own known projected points;
          2. pools: per side, the BEAM_POOL players best by "adds most to the other team" and by
             "surplus" (own points minus own-team cost), both capped by the position filter;
          3. seeds: the BEAM_SEEDS 1-for-1 pairs from the pools with the largest approximate
             smaller-side gain (signals only), then evaluated exactly;
          4. expansion: add one pool player to either side, evaluate every new package exactly
             (the same _side as stage B, forced drops included), keep the BEAM_WIDTH best by exact
             smaller-side rest-of-season gain, repeat up to the largest requested shape;
          5. packages of a requested shape that qualify (your ROS or playoff delta > 0) become rows;
             a package whose added player changes neither team's effects is padding and is skipped.

        Every row is the exact canonical evaluation, but the list is not exhaustive and not a proven
        top K: unexamined packages may be better. Coverage is reported per opponent.

        With a target, the pools also offer the matching players (the named player; the BEAM_POOL players at the
        position who add most to the anchor) and only matching 1-for-1 pairs seed the beam, so every package grown
        from them still receives the target. Cached per target."""
        tk = _target_key(target)
        if tk in self._beams:
            return self._beams[tk]
        t0 = time.perf_counter()
        T, A, W = self.T, self.anchor, range(len(self.weeks))
        rows: list[dict] = []
        per_opp = []
        if not self.heuristic_shapes:
            self._beams[tk] = {"rows": [], "per_opponent": [], "evaluated": 0, "work_per_opponent": 0, "seconds": 0.0}
            return self._beams[tk]
        max_s = max(HEURISTIC_SHAPES[x][0] for x in self.heuristic_shapes)
        max_r = max(HEURISTIC_SHAPES[x][1] for x in self.heuristic_shapes)
        share = self.max_beam_work // max(1, len(self.opponents))      # each manager's part of the beam budget
        mine_act = _active(self.state.rosters[A])
        m_ids = self.tradeable(self.state.rosters[A])
        base_me = sum(self.base[A])
        own = {p: sum(T.value(p, i) for i in W) for p in self.players}
        my_cost = {x: base_me - sum(T.vec(_minus(mine_act, x))) for x in m_ids}
        total_evals = 0
        for opp in self.opponents:
            o_act = _active(self.state.rosters[opp])
            o_ids = self.tradeable(self.state.rosters[opp])
            if not m_ids or not o_ids:
                per_opp.append({"opponent_team_id": opp, "seeds": 0, "evaluated": 0, "rows": 0, "work": 0,
                                "budget_reached": False})
                continue
            base_o = sum(self.base[opp])
            their_cost = {t: base_o - sum(T.vec(_minus(o_act, t))) for t in o_ids}
            adds_me = {t: sum(T.vec(mine_act + [t])) - base_me for t in o_ids}
            adds_them = {x: sum(T.vec(o_act + [x])) - base_o for x in m_ids}

            def top(ids, score):
                return [i for i in sorted(ids, key=lambda i: (-score(i), i))[:BEAM_POOL]]
            wanted = [t for t in o_ids if self._matches(target, (t,))] if target is not None else []
            pool_t = sorted(set(top(o_ids, lambda t: adds_me[t])) | set(top(o_ids, lambda t: own[t] - their_cost[t]))
                            | set(top(wanted, lambda t: adds_me[t])))
            pool_m = sorted(set(top(m_ids, lambda x: adds_them[x])) | set(top(m_ids, lambda x: own[x] - my_cost[x])))
            approx = sorted(((min(adds_me[t] - my_cost[x], adds_them[x] - their_cost[t]), (x,), (t,))
                             for x in pool_m for t in pool_t if self._matches(target, (t,))),
                            key=lambda z: (-z[0], z[1], z[2]))[:BEAM_SEEDS]
            beam, seen, evals, made, spent, cut = [], set(), 0, 0, 0, False

            def pair(shape, x, t):
                """_pair within this manager's work budget; None once the budget is spent (nothing more is evaluated)."""
                nonlocal evals, spent, cut
                if spent >= share:
                    cut = True
                    return None
                out = self._pair(shape, opp, x, t)
                spent += self.cost["pair:" + _key(shape, opp, x, t)]
                evals += 1
                return out
            for _, x, t in approx:
                got = pair("1-for-1", x, t)
                if got is None:
                    break
                mine, theirs, st = got
                seen.add((x, t))
                if st == "evaluated":
                    beam.append((x, t, mine, theirs))
            for _ in range(max_s + max_r - 2):
                if cut:
                    break
                nxt = []
                for x, t, pm, pt in beam:
                    if cut:
                        break
                    grown = [(tuple(sorted(x + (a,))), t) for a in pool_m if a not in x and len(x) < max_s] + \
                            [(x, tuple(sorted(t + (b,)))) for b in pool_t if b not in t and len(t) < max_r]
                    for nx, nt in grown:
                        if (nx, nt) in seen:
                            continue
                        shape = f"{len(nx)}-for-{len(nt)}"
                        got = pair(shape, nx, nt)
                        if got is None:
                            break
                        seen.add((nx, nt))
                        mine, theirs, st = got
                        if st != "evaluated":
                            continue
                        padding = all(mine[k] == pm[k] and theirs[k] == pt[k] for k in EQUIVALENCE_KEYS) \
                            and mine["forced_drops"] == pm["forced_drops"] and theirs["forced_drops"] == pt["forced_drops"]
                        if padding:
                            continue
                        nxt.append((nx, nt, mine, theirs))
                        if shape in self.heuristic_shapes and not self._incomplete(mine) and _qualifies(mine) \
                                and self._matches(target, nt):
                            rows.append(self._row(shape, opp, nx, nt, dict(mine), dict(theirs)))
                            made += 1
                nxt.sort(key=lambda z: (z[3]["ros_delta"] is None or z[2]["ros_delta"] is None,
                                        -min(z[2]["ros_delta"] or 0.0, z[3]["ros_delta"] or 0.0), z[0], z[1]))
                beam = nxt[:BEAM_WIDTH]
                if not beam:
                    break
            total_evals += evals
            per_opp.append({"opponent_team_id": opp, "seeds": len(approx), "pool_mine": pool_m, "pool_theirs": pool_t,
                            "evaluated": evals, "rows": made, "work": spent, "budget_reached": cut})
        self._beams[tk] = {"rows": rows, "per_opponent": per_opp, "evaluated": total_evals, "work_per_opponent": share,
                           "seconds": round(time.perf_counter() - t0, 3)}
        return self._beams[tk]

    def heuristic_top(self, sort: str, category: str | None, opponent: int | None, limit: int,
                      target: TradeTarget | None = None) -> dict:
        """Rows from the beam for the requested filters, ranked like top(); coverage is heuristic."""
        self._check_sort(sort)
        b = self.beam(target)
        rk = _rank_key(sort)
        found = sorted((x for x in b["rows"] if (opponent is None or x["opponent_team_id"] == opponent)
                        and (category is None or category in x["categories"])
                        and self._matches(target, x["receives"])), key=rk)
        cut = [x["opponent_team_id"] for x in b["per_opponent"]
               if x["budget_reached"] and (opponent is None or x["opponent_team_id"] == opponent)]
        note = ("Exact evaluation of every listed package, but candidate generation is a bounded beam: packages outside "
                "the pools and the beam were never evaluated, so this is not a proven top list.")
        if cut:
            note += (f" The beam also reached its work budget ({b['work_per_opponent']:,} units per manager) with "
                     f"{', '.join(self.state.team_names.get(t) or f'team {t}' for t in cut)}, so it stopped early "
                     "there: these are the best packages found within the budget.")
        return {"results": [{**x, "simpler_equivalent": None} for x in found[:max(1, min(int(limit), MAX_LIMIT))]],
                "search": {"method": "heuristic_beam", "shapes": self.heuristic_shapes, "exhaustive": False,
                           "packages_evaluated": b["evaluated"], "qualifying_rows": len(b["rows"]),
                           "beam": {"seeds": BEAM_SEEDS, "width": BEAM_WIDTH, "pool_per_signal": BEAM_POOL},
                           "per_opponent": b["per_opponent"], "seconds": b["seconds"],
                           "max_work_per_opponent": b["work_per_opponent"], "budget_reached": bool(cut),
                           "budget_reached_opponents": cut, "note": note}}

    # ---- ranking
    def _check_sort(self, sort: str) -> None:
        if sort not in SORTS and sort not in TARGET_SORTS:
            raise TradeError(f"sort must be one of {', '.join(SORTS)}")
        if sort == "focus_gain" and not self.focus:
            raise TradeError("focus_gain needs focus weeks")

    def top(self, sort: str = "my_gain", category: str | None = None, opponent: int | None = None,
            limit: int = DEFAULT_LIMIT, include_redundant: bool = False, target: TradeTarget | None = None) -> dict:
        self._check_sort(sort)
        if category is not None and category not in CATEGORIES:
            raise TradeError(f"category must be one of {', '.join(CATEGORIES)}")
        limit = max(1, min(int(limit), MAX_LIMIT))
        t0 = time.perf_counter()
        # A target only removes candidates; each remaining candidate keeps its own valid bound, so the best-first stop
        # below proves the top K among the matching candidates.
        pool = [c for c in self.cands if (opponent is None or c[1] == opponent) and self._may_match(c, category)
                and self._matches(target, c[3])]
        order = {s: i for i, s in enumerate(SHAPES)}
        pool.sort(key=lambda c: (-_bound(c, sort), order[c[0]], c[1], c[2], c[3]))
        rk = _rank_key(sort)
        bounds_valid = not self.T.negative
        found, keys, examined_keys, redundant = [], set(), [], 0
        termination, stop_bound, stop_at = "all_candidates_evaluated", None, len(pool)
        self._ledger, self._spent = {}, 0      # every evaluation this call makes or reuses, at its recorded cost
        for i, c in enumerate(pool):
            ub = _bound(c, sort)
            if bounds_valid and len(found) >= limit:          # invalid bounds never end the search
                kv = _sort_value(found[limit - 1], sort)
                # Every remaining displayed value is <= its bound + the rounding slack; stop only when that
                # is strictly below the K-th displayed value, so no remaining candidate can tie or beat it.
                if kv is not None and ub + ROUNDING_SLACK[sort] < kv - EPS:
                    termination, stop_bound, stop_at = "proven_by_upper_bounds", ub, i
                    break
            if len(examined_keys) >= self.max_exact:
                termination, stop_bound, stop_at = "evaluation_cap", ub, i
                break
            if self._spent >= self.max_work:
                termination, stop_bound, stop_at = "work_budget", ub, i
                break
            examined_keys.append(_key(*c[:4]))
            row = self.evaluate(c)
            if row is None or (category and category not in row["categories"]) or row["key"] in keys:
                continue
            sub = self.simpler_equivalent(row, category, target)
            if sub is not None and not include_redundant:
                redundant += 1
                if sub["key"] not in keys:      # the eligible simpler trade is shown in its place
                    insort(found, {**sub, "simpler_equivalent": None}, key=rk)
                    keys.add(sub["key"])
                continue
            insort(found, {**row, "simpler_equivalent": sub["key"] if sub else None}, key=rk)
            keys.add(row["key"])
        work, self._ledger = self._spent, None
        remaining = len(pool) - stop_at
        status = Counter(self.status[k] for k in examined_keys)
        reason = {
            "all_candidates_evaluated": None,
            "proven_by_upper_bounds": f"each remaining candidate's upper bound on the sort key (largest "
                                      f"{_r(stop_bound)}) is below the {limit}th result, so none can enter the results",
            "evaluation_cap": f"exact-evaluation cap ({self.max_exact}) reached; the results are the best among the "
                              "candidates evaluated, not a proven top list",
            "work_budget": f"work budget reached ({self.max_work:,} units of lineup work) after {len(examined_keys):,} "
                           f"exact evaluations; {remaining:,} matching candidates were not evaluated, so the results "
                           "are the best found within the budget, not a proven top list",
        }[termination]
        return {
            "results": found[:limit],
            "search": {
                "candidates_considered": len(self.cands) + self.screened_out,
                "screened_out_by_upper_bound": self.screened_out,
                "survivors": len(self.cands),
                "matching_filters": len(pool),
                "fully_evaluated": len(examined_keys),
                "excluded_invalid_roster": status["excluded_invalid_roster"],
                "excluded_missing_data": status["excluded_missing_data"],
                "not_qualified": status["not_qualified"],
                "qualified_found": status["qualified"],
                "simpler_equivalents_hidden": redundant,
                "not_evaluated": remaining,
                "termination": termination,
                "not_evaluated_reason": reason,
                # Only a valid bound says anything about unevaluated candidates.
                "largest_unevaluated_upper_bound": _r(stop_bound) if remaining and bounds_valid else None,
                "bounds_valid": bounds_valid,
                "exhaustive": termination not in ("evaluation_cap", "work_budget"),
                "max_exact": self.max_exact,
                "max_work": self.max_work,
                "work_used": work,
                "exact_evaluations_cached": len(self.exact),
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
        return evaluation(self.state, self.window)


def evaluation(state: LeagueState, window) -> dict:
    """The evaluation window and why it starts where it does (no search needed)."""
    reason = state.start_week_reason or {}
    return {"start_week": window.start, "final_week": window.final,
            "last_regular_season_week": window.last_regular, "weeks": list(window.weeks),
            "uncertain": bool(reason.get("uncertain")), "warning": reason.get("warning"),
            "start_week_reason": reason.get("reason")}


DELTA_KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta", "average_weekly_delta",
              "weeks_improved", "weeks_worsened", "weeks_unchanged", "weeks_incomplete", "projection_complete")


EQUIVALENCE_KEYS = DELTA_KEYS + ("weekly_known_deltas",)


def _same_effects(a: dict, b: dict) -> bool:
    """Identical canonical outcome for both teams: every aggregate and weekly delta, completeness and
    the forced drops (a different drop is a different roster effect even at zero lineup cost)."""
    return all(a[side][k] == b[side][k] for side in ("mine", "theirs") for k in EQUIVALENCE_KEYS) \
        and a["forced_drops"] == b["forced_drops"]


def _r(x):
    return None if x is None else round(x, 2)


def _key(shape, opp, sends, receives) -> str:
    return f"{shape}:{opp}:{'-'.join(map(str, sends))}:{'-'.join(map(str, receives))}"


def _qualifies(mine: dict) -> bool:
    """The anchor gains: rest of season, playoffs or (in a focus search) the focus weeks. Unknown is never a gain."""
    return any(mine.get(k) is not None and mine[k] > 0 for k in ("ros_delta", "playoff_delta", "focus_delta"))


def _bound(c: tuple, sort: str) -> float:
    """Upper bound on a candidate's sort value. Per-phase (and focus-week) bounds are sums of the same weekly
    bounds over those weeks, so they are upper bounds too."""
    me, them = c[4], c[5]
    return {"my_gain": lambda: me[0], "their_gain": lambda: them[0], "combined": lambda: me[0] + them[0],
            "min_side": lambda: min(me[0], them[0]), "playoff_gain": lambda: me[2],
            "min_side_regular": lambda: min(me[1], them[1]), "min_side_playoff": lambda: min(me[2], them[2]),
            "regular_gain": lambda: me[1], "focus_gain": lambda: me[3]}[sort]()


def _min(a, b):
    return None if a is None or b is None else min(a, b)


def _sort_value(row: dict, sort: str):
    m, t = row["mine"], row["theirs"]
    return {"my_gain": m["ros_delta"], "their_gain": t["ros_delta"],
            "combined": row["combined_ros_delta"], "min_side": row["min_side_ros_delta"],
            "playoff_gain": m["playoff_delta"],
            "min_side_regular": _min(m["regular_season_delta"], t["regular_season_delta"]),
            "min_side_playoff": _min(m["playoff_delta"], t["playoff_delta"]),
            "regular_gain": m["regular_season_delta"], "focus_gain": m.get("focus_delta")}[sort]


def _rank_key(sort: str):
    """Sort key descending (missing values last), then combined delta, shape, opponent, player ids."""
    order = {s: i for i, s in enumerate(ALL_SHAPES)}

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
    # The side receiving FEWER players consolidates (shape "a-for-b": the anchor sends a, receives b): the
    # anchor when b < a, the opponent when b > a. Exact 2-for-1 / 1-for-2 behave as before; the uneven
    # heuristic shapes (3-for-1, 1-for-3, 3-for-2, 2-for-3) now qualify too. Only the row's category:
    # `_may_match` and the stage-A bounds never see it.
    sent, got = (int(x) for x in shape.split("-for-"))
    consolidator = mine if got < sent else theirs if got > sent else None
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


def _empty_slots(side: dict) -> list[dict]:
    """Starting slots the team could fill before the trade but not after (no eligible player): per week and slot
    label, how many more of them are empty after than before (a slot already empty before is not the trade's)."""
    out = []
    for b, a in zip(side["lineups"]["before"], side["lineups"]["after"]):
        filled_before = Counter(x["slot"] for x in b["assignment"] if slot_filled(x))
        filled_after = Counter(x["slot"] for x in a["assignment"] if slot_filled(x))
        for slot in dict.fromkeys(x["slot"] for x in a["assignment"]):
            out += [{"week": a["week"], "slot": slot}] * max(0, filled_before[slot] - filled_after[slot])
    return out


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
        **({"legality": ro["legality"]} if ro.get("legality") else {}),
        "hypothetical_fa_fill": side["hypothetical_fa_fill"],
        "incomplete": side["incomplete"],
        "empty_slots_after": _empty_slots(side),
        "issues_before_drops": ro["issues_before_drops"],
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
                                     "from_team_id", "to_team_id", "availability")},
                **({"on_taxi": True} if p.get("on_taxi") else {}),
                "marginal_value_on_current_team": p["marginal_value_on_current_team"]["roster_marginal_lineup_points"]}
               for p in res["players"]]
    return {**row, "consistent_with_manual_evaluator": consistent,
            "result_label": res["result"]["label"], "regular_season_label": res["result"]["regular_season_label"],
            "playoff_label": res["result"]["playoff_label"], "players": players,
            "mine_detail": _side_detail(a), "theirs_detail": _side_detail(b),
            "ir_warning": res["ir_warning"], "ir_assumption": res["ir_assumption"],
            "injury_warnings": res["injury_warnings"], "injury_scenarios": res["injury_scenarios"],
            "manual_evaluator_link": {"ta": row["anchor_team_id"], "tb": row["opponent_team_id"],
                                      "a": row["sends"], "b": row["receives"]}}

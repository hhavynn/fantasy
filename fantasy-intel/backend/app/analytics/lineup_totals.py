"""Exact known-projection optimal starting totals per week for arbitrary sets of player ids: the totals-only solver
behind trade discovery (stages A and B), trade-vs-free-agency and the forced-drop search of the trade evaluator
(trades.resolve_roster). Every total is bit-identical to team_lineups.project_week's `_total` for the same players
(same values, same order, same sum()), so every rounded delta and every forced-drop choice matches.

`base` keeps a roster's sorted values per week once, so that the rosters a trade produces (a few players out, a few in)
are solved by editing them (`vec_change`). `removal_total` serves the forced-drop search (trades.choose_forced_drops),
which only ever removes players from one roster: a week whose lineup the removal provably leaves alone keeps the full
roster's value instead of a new solve (`week_unchanged`), and a single removal's cost comes from one solve per week and
position (`RemovalTotal.single_bound`). Everything here is exact; see docs/v2_trade_discovery.md "Deep leagues". Pure
and read-only."""

from __future__ import annotations

from itertools import pairwise

from .lineup import FLEX_ELIGIBILITY, assign_lineup, lineup_total, separable, unsupported_of
from .team_lineups import STREAMER_IDS, lineup_relevant, stream_levels


class LineupTotals:
    """Known-projection optimal starting totals per week for arbitrary sets of player ids. With a
    single flex slot type the optimum is: best players per fixed slot, then the best remaining
    flex-eligible players (the exchange argument in analytics/lineup.py). Nested flex types (each flex
    slot takes every position the one before it takes, e.g. FLEX then OP) fill slot by slot with the best
    remaining eligible player (`_nested`). Other mixed flex types (e.g. RB/WR + WR/TE) use the exact
    assign_lineup itself. `state` is anything with the league's `slots` and `projections` (trades.LeagueState)."""

    def __init__(self, state, weeks: tuple[int, ...], pids: set[int], positions: dict[int, str | None],
                 teams: dict[int, str | None], blocked: dict[int, frozenset] | None = None,
                 exact_negative: bool = False):
        self.slots = state.slots
        fixed = [s for s in state.slots if s not in FLEX_ELIGIBILITY]
        self.fixed_groups = [(s, fixed.count(s)) for s in dict.fromkeys(fixed)]   # slot order (contiguous)
        flex = [s for s in state.slots if s in FLEX_ELIGIBILITY]
        self.m = len(flex)
        runs = [s for i, s in enumerate(fixed) if i == 0 or fixed[i - 1] != s]
        # Nested flex types: every flex slot's eligible positions include the previous slot's (lineup_slots orders the
        # flex types narrowest first, so FLEX + OP is nested and RB/WR + WR/TE is not).
        nested = len(set(flex)) > 1 and all(set(FLEX_ELIGIBILITY[a]) <= set(FLEX_ELIGIBILITY[b])
                                            for a, b in pairwise(flex))
        # Non-nested mixed flex types need the exact search; non-contiguous fixed slots would change the summation order.
        self.general = (len(set(flex)) > 1 and not nested) or len(runs) != len(self.fixed_groups)
        self.nested = [tuple(FLEX_ELIGIBILITY[s]) for s in flex] if nested and not self.general else None
        self.elig = set(FLEX_ELIGIBILITY[flex[0]]) if flex else set()
        if self.nested:
            self.elig = set(FLEX_ELIGIBILITY[flex[-1]])          # the widest flex type takes every flex position
        self.flex_only = [p for p in sorted(self.elig) if p not in dict(self.fixed_groups)]
        self.fixed_plan = [(pos, n, pos in self.elig) for pos, n in self.fixed_groups]
        self.pos = positions
        blocked = blocked or {}
        # A verified NFL-ineligible week (RosterPlayer.unavailable_weeks): he cannot start and his missing
        # or present projection never matters, exactly as in team_lineups.project_week.
        self.val = [{p: (positions.get(p), float(state.projections[p][w])) for p in pids
                     if state.projections.get(p, {}).get(w) is not None and w not in blocked.get(p, ())} for w in weeks]
        self.negative = any(v < 0 for row in self.val for _, v in row.values())
        # Weeks left to assign_lineup: a week with a negative value, where the exhaustive flex search may keep no leaf
        # at all (a flex fill worth -1 or less stays empty, lineup._best_flex), which the fills here do not reproduce.
        # Always for nested flex types (assign_lineup is what they used before); for every lineup with
        # `exact_negative` (the evaluator's forced-drop search, trades.resolve_roster, which must match project_team bit
        # for bit). The single-flex fill otherwise equals assign_lineup, so for discovery and trade-vs-free-agency a
        # week goes to assign_lineup only when some roster could reach that case (STATUS 1r): its up-to-m most negative
        # flex-eligible values sum to -1 or less, and every flex fill sums to at least that. Standard leagues keep the
        # fill. The tolerance is wider than _best_flex's, so a week near -1 is solved exactly, never guessed.
        def flex_floor(row) -> bool:
            low = sorted(v for q, v in row.values() if q in self.elig and v < 0)[:self.m]
            return bool(low) and sum(low) <= -1.0 + 1e-9
        self.exact_week = [not self.general and (
            ((exact_negative or self.nested is not None) and any(v < 0 for _, v in row.values()))
            or (self.m > 0 and flex_floor(row))) for row in self.val]
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
        self.reused = 0                 # week totals removal_total reused instead of solving

    def week(self, ids, i: int) -> float:
        row, stream = self.val[i], self.stream[i]
        if self.general or self.exact_week[i]:
            return lineup_total(assign_lineup([{"espn_player_id": p, "position": self.pos.get(p), "value": row[p][1]}
                                               for p in ids if p in row]
                                              + [{"espn_player_id": STREAMER_IDS[pos], "position": pos, "value": v}
                                                 for pos, v in stream.items()], self.slots))
        return self._fill(self._by(ids, i))

    def _by(self, ids, i: int) -> dict:
        """Week i's known values per position, best first (the K/D/ST streamer included)."""
        row = self.val[i]
        by: dict = {pos: [v] for pos, v in self.stream[i].items()}
        for p in ids:
            e = row.get(p)
            if e is not None:
                lst = by.get(e[0])
                if lst is None:
                    by[e[0]] = [e[1]]
                else:
                    lst.append(e[1])
        for vs in by.values():
            vs.sort(reverse=True)
        return by

    def _fill(self, by: dict) -> float:
        """The optimal total from per-position values sorted best first (`by` is not modified). Same picks, same order
        and the same sum() as project_week (slot order, best first; CPython's float sum is compensated), so totals are
        bit-identical to the evaluator's and every rounded delta matches."""
        picks: list = []
        if self.nested is not None:
            return self._nested(by, picks)
        rest: list = []
        for pos, n in self.fixed_groups:
            vs = by.get(pos)
            if vs:
                picks += vs[:n]
                if pos in self.elig:
                    rest += vs[n:]
        if self.m:
            for pos in self.flex_only:
                rest += by.get(pos, ())
            rest.sort(reverse=True)
            picks += rest[:self.m]
        return sum(picks)

    def _nested(self, by: dict, picks: list) -> float:
        """Nested flex types, no negative value: the fixed slots as above, then each flex slot in slot order takes the
        best remaining eligible value. assign_lineup's _best_flex returns exactly these values in this order: its
        first leaf (each slot takes its first eligible candidate) is optimal when every slot's positions include the
        previous slot's (swap argument: an optimum that differs first at slot i holds the greedy pick at a later,
        wider slot or not at all; exchanging the two never lowers the total), so it reaches the matching bound and the
        fast path returns it. Ties between positions change who starts, never the values: every later slot takes both
        tied positions. Same values, same order, same sum(): bit-identical to assign_lineup's lineup_total."""
        at: dict = {}
        get = by.get
        for pos, n, flexed in self.fixed_plan:
            vs = get(pos)
            if vs:
                picks += vs[:n]
                if flexed:
                    at[pos] = min(len(vs), n)
        for pos in self.flex_only:
            if get(pos):
                at[pos] = 0
        for elig in self.nested:
            best = where = None
            for pos in elig:
                k = at.get(pos)
                if k is not None:
                    vs = by[pos]
                    if k < len(vs):
                        v = vs[k]
                        if best is None or v > best:
                            best, where = v, pos
            if where is not None:       # else no eligible player is left: the slot stays empty (adds nothing)
                picks.append(best)
                at[where] += 1
        return sum(picks)

    def _profile(self, ids) -> list[tuple[dict, dict] | None]:
        """Per week, (reach, values per position best first) of the roster `ids`; None for a week assign_lineup solves."""
        out: list = []
        for i in range(len(self.val)):
            if self.general or self.exact_week[i]:
                out.append(None)
                continue
            by = self._by(ids, i)
            out.append(self._reach(by))
        return out

    def _reach(self, by: dict) -> tuple[dict, dict]:
        """(reach, by) for one week's sorted values. reach: {position: (the lowest value the lineup uses there, how many
        players beyond those it uses have exactly that value)}; a position it uses no one at is absent."""
        reach = {}
        for pos, c in self._used(by).items():
            if c:
                vs = by[pos]
                reach[pos] = (vs[c - 1], sum(1 for v in vs[c:] if v == vs[c - 1]))
        return reach, by

    def base(self, ids) -> tuple:
        """A roster's sorted values per week, kept once so that rosters differing from it by a few players are solved
        incrementally (vec_change): (ids, their set, per week the values per position best first, or None for a week
        assign_lineup solves)."""
        ids = tuple(ids)
        return ids, frozenset(ids), [None if self.general or self.exact_week[i] else self._by(ids, i)
                                     for i in range(len(self.val))]

    def vec_change(self, base: tuple, out=(), add=()) -> tuple[float, ...]:
        """vec() of the roster `base` describes without the players `out` and with the players `add`, exactly: each
        week's per-position values are edited, not rebuilt, and filled the same way (the fill depends only on them).
        Players in `out` that are not on that roster (IR or taxi players, who never start) are ignored."""
        ids, have, bys = base
        out = [p for p in out if p in have]
        add = [p for p in add if p not in have]
        self.solves += len(self.val)
        return tuple(self.week([p for p in ids if p not in out] + add, i) if by is None
                     else self._fill(self._change(i, by, out, add)) for i, by in enumerate(bys))

    def profile_change(self, base: tuple, out=(), add=()) -> tuple[tuple, list]:
        """(vec, _profile) of the roster `base` describes without `out` and with `add` (see vec_change), for a
        RemovalTotal started from it."""
        ids, have, bys = base
        out = [p for p in out if p in have]
        add = [p for p in add if p not in have]
        rest = [p for p in ids if p not in out] + add
        vec, prof = [], []
        for i, by in enumerate(bys):
            if by is None:
                vec.append(self.week(rest, i))
                prof.append(None)
                continue
            by = self._change(i, by, out, add)
            vec.append(self._fill(by))
            prof.append(self._reach(by))
        self.solves += len(self.val)
        return tuple(vec), prof

    def _change(self, i: int, by: dict, out, add) -> dict:
        """The sorted per-position values `by` without the week-i values of `out` and with those of `add` (each list
        stays sorted best first, as _by builds it). `by` is not modified."""
        row, res, copied = self.val[i], dict(by), set()
        for p, sign in [(p, -1) for p in out] + [(p, 1) for p in add]:
            e = row.get(p)
            if e is None:
                continue
            if e[0] not in copied:
                res[e[0]] = list(res.get(e[0], ()))
                copied.add(e[0])
            if sign < 0:
                res[e[0]].remove(e[1])
            else:
                res[e[0]].append(e[1])
        if add:
            for pos in copied:
                res[pos].sort(reverse=True)
        return res

    def _week_without(self, i: int, by_full: dict, gone) -> float:
        """Week i's total for the roster `by_full` describes minus the players `gone`, by the same fill."""
        return self._fill(self._minus(i, by_full, gone))

    def _minus(self, i: int, by: dict, gone) -> dict:
        """The sorted per-position values `by` without the week-i values of the players `gone` (one equal value each;
        the lists stay sorted). `by` is not modified."""
        row, out = self.val[i], dict(by)
        copied = set()
        for p in gone:
            e = row.get(p)
            if e is None:
                continue
            if e[0] not in copied:
                out[e[0]] = list(out[e[0]])
                copied.add(e[0])
            out[e[0]].remove(e[1])
        return out

    def _minus_value(self, by: dict, pos: str, value: float) -> dict:
        """`by` with one `value` taken out of position `pos` (not modified)."""
        out = dict(by)
        out[pos] = list(out[pos])
        out[pos].remove(value)
        return out

    def _used(self, by: dict) -> dict:
        """Per position, how many of its (sorted) values the week's optimal lineup uses: the same fill as week()."""
        used = {pos: min(n, len(by.get(pos, ()))) for pos, n in self.fixed_groups}
        if not self.m:
            return used
        if self.nested is not None:
            at = {pos: used.get(pos, 0) for pos in self.elig}
            for elig in self.nested:
                best, where = None, None
                for pos in elig:
                    vs = by.get(pos)
                    if vs is not None and at[pos] < len(vs) and (best is None or vs[at[pos]] > best):
                        best, where = vs[at[pos]], pos
                if where is not None:
                    at[where] += 1
            return {**used, **at}
        rest = sorted(((v, pos) for pos in sorted(self.elig) for v in by.get(pos, ())[used.get(pos, 0):]),
                      key=lambda x: -x[0])[:self.m]
        for _, pos in rest:
            used[pos] = used.get(pos, 0) + 1
        return used

    def week_unchanged(self, i: int, rc: dict | None, gone) -> bool:
        """Week i's total is bit-identical without the players `gone`: each position keeps the values its lineup uses
        (a removed player is below the lowest of them, or ties it with enough equal players left beyond them). The
        fill then takes the same values in the same order (a value removed beyond them never beats the value chosen
        ahead of it), so sum() is the same. A week solved by assign_lineup (rc None) is never claimed."""
        if rc is None:
            return False
        row, ties = self.val[i], None
        for p in gone:
            e = row.get(p)
            if e is None:
                continue
            r = rc.get(e[0])
            if r is None or e[1] < r[0]:
                continue
            if e[1] > r[0]:
                return False
            ties = ties or {}
            ties[e[0]] = ties.get(e[0], 0) + 1
            if ties[e[0]] > r[1]:
                return False
        return True

    def removal_total(self, full_ids, memo: dict | None = None, start: tuple | None = None) -> RemovalTotal:
        """The known totals of the roster `full_ids` (active player ids) and of any subset of it, exactly sum(vec(ids)),
        for the forced-drop search (RemovalTotal). `memo` (keyed by the sorted ids) may be shared between rosters;
        `start` is that roster's (vec, _profile) when already known (profile_change)."""
        return RemovalTotal(self, full_ids, {} if memo is None else memo, start)

    def vec(self, ids) -> tuple[float, ...]:
        self.solves += len(self.val)
        return tuple(self.week(ids, i) for i in range(len(self.val)))

    def complete(self, ids) -> tuple[bool, ...]:
        s = set(ids)
        return tuple(not (miss & s) for miss in self.missing)

    def value(self, pid: int, i: int) -> float:
        e = self.val[i].get(pid)
        return 0.0 if e is None else e[1]


class RemovalTotal:
    """total(ids) for the roster `full_ids` and its subsets, exactly sum(T.vec(ids)), built for the forced-drop search
    (trades.choose_forced_drops), which only removes players from one roster. The full roster is solved once; for a
    subset, a week its removed players leave alone (LineupTotals.week_unchanged) keeps the full roster's value, and
    every other week is solved again from the full roster's sorted values minus the removed ones (the same fill on the
    same values, so the same sum()). `dropping(combo)` answers the same for the roster without the players in `combo`
    (RosterPlayer objects; IR and taxi players are not in the lineup) from the removed players alone. `bound` is the
    search's single-removal lower bound (single_bound), or None when a week is solved by assign_lineup."""

    def __init__(self, T: LineupTotals, full_ids, memo: dict, start: tuple | None = None):
        self.T, self.memo = T, memo
        self.full = tuple(sorted(full_ids))
        self.set = frozenset(self.full)
        if start is None:
            self.vec, self.profile = T.vec(list(self.full)), T._profile(self.full)
        else:
            self.vec, self.profile = start
        self.total = memo.setdefault(self.full, sum(self.vec))
        self.masks: dict[int, tuple[int, int]] = {}
        self.by_gone: dict[frozenset, float] = {}
        self._singles: dict[int, float] | None = None
        self.bound = None if any(pr is None for pr in self.profile) else self.single_bound

    def __call__(self, ids) -> float:
        key = tuple(sorted(ids))
        out = self.memo.get(key)
        if out is None:
            if self.set.issuperset(key):
                out = self.without(self.set.difference(key))
            else:                       # not a subset of the roster: solved in full
                out = sum(self.T.vec(list(key)))
            self.memo[key] = out
        return out

    def dropping(self, combo) -> float:
        return self.without(frozenset(p.espn_player_id for p in combo if not p.reserved))

    def without(self, gone) -> float:
        gone = frozenset(gone)
        if not gone:
            return self.total
        out = self.by_gone.get(gone)
        if out is not None:
            return out
        T = self.T
        hard = soft = 0
        for p in gone:
            h, t = self._mask(p)
            hard |= h
            soft |= t
        weeks, solved, todo = list(self.vec), 0, hard | soft
        while todo:
            low = todo & -todo
            todo ^= low
            i = low.bit_length() - 1
            pr = self.profile[i]
            if pr is None:
                weeks[i] = T.week([x for x in self.full if x not in gone], i)
                solved += 1
            elif not hard & low and T.week_unchanged(i, pr[0], gone):
                continue
            else:
                weeks[i] = T._week_without(i, pr[1], gone)
                solved += 1
        T.solves += solved
        T.reused += len(weeks) - solved
        out = self.by_gone[gone] = sum(weeks)
        return out

    def _mask(self, p: int) -> tuple[int, int]:
        """What removing p alone does to the full roster's lineup, as bitmasks of weeks: (weeks it changes, or solved
        by assign_lineup; weeks it ties the lowest value used at his position, which week_unchanged decides). In every
        other week he has no value or is below every value the lineup uses at his position: nothing changes."""
        m = self.masks.get(p)
        if m is None:
            hard = soft = 0
            for i, pr in enumerate(self.profile):
                if pr is None:
                    hard |= 1 << i
                    continue
                e = self.T.val[i].get(p) if p in self.set else None
                r = None if e is None else pr[0].get(e[0])
                if r is None or e[1] < r[0]:
                    continue
                if e[1] > r[0]:
                    hard |= 1 << i
                else:
                    soft |= 1 << i
            m = self.masks[p] = (hard, soft)
        return m

    def single_bound(self, player) -> float:
        """What removing `player` (a RosterPlayer) alone costs the full roster's known total, for the forced-drop
        search's lower bounds (trades.choose_forced_drops `single_bound`). The lineup is a maximum-weight independent set
        of a matroid (players that can start together in distinct slots; no negative value here), so removing a starter
        p lowers a week by v_p minus the best player that can then come in, and two starters at one position are
        interchangeable, so that replacement is the same for both. One solve per week and position (the lowest value
        used there removed) gives it, and with it every starter's cost; a player the lineup does not use costs nothing.
        These are the exact single costs up to float rounding (~1e-13), far inside the search's DROP_EPS. With a negative
        value the argument does not hold, and choose_forced_drops then uses no bound at all (its `negative`)."""
        if player.reserved:
            return 0.0
        if self._singles is None:
            T, cost = self.T, dict.fromkeys(self.full, 0.0)
            for i, (v, pr) in enumerate(zip(self.vec, self.profile)):
                reach, by = pr
                row, repl = T.val[i], {}
                for p in self.full:
                    e = row.get(p)
                    r = None if e is None else reach.get(e[0])
                    if r is None or e[1] < r[0]:
                        continue
                    f = repl.get(e[0])
                    if f is None:
                        t = T._fill(T._minus_value(by, e[0], r[0]))
                        f = repl[e[0]] = r[0] - (v - t)
                    if e[1] > f:
                        cost[p] += e[1] - f
            self._singles = cost
        return self._singles.get(player.espn_player_id, 0.0)

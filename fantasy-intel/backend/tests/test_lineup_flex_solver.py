"""The polynomial flex solver (lineup._best_flex) returns exactly what the previous depth-first search returned.

The previous search is kept below, verbatim, as the oracle. It was exact but exponential on ties and on pools with fewer
positive players than flex slots (an 8-flex league with identical values took 64.6 s for one add/drop request).
"""

import random
import time

import pytest

from app.analytics import lineup
from app.analytics.lineup import (
    FLEX_ELIGIBILITY,
    LineupTooLargeError,
    assign_lineup,
    eligible,
    lineup_slots,
)


def _previous_best_flex(flex, pool):
    """lineup._best_flex before the polynomial solver (origin/main 2d2c1c4), verbatim."""
    per_pos = {}
    for p in pool:
        lst = per_pos.setdefault(p.get("position"), [])
        if len(lst) < len(flex):
            lst.append(p)
    cands = sorted((p for lst in per_pos.values() for p in lst),
                   key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    vals = [c.get("value") or 0.0 for c in cands]
    ids = {}
    bit = [1 << ids.setdefault(c["espn_player_id"], len(ids)) for c in cands]
    ok = [[eligible(s, c.get("position")) for c in cands] for s in flex]
    n, k = len(cands), len(flex)
    any_ok = [[False] * n for _ in range(k + 1)]
    for i in range(k - 1, -1, -1):
        any_ok[i] = [a or b for a, b in zip(any_ok[i + 1], ok[i])]
    best = (-1.0, [None] * k)

    def bound(i, used):
        left, total = k - i, 0.0
        for j in range(n):
            if left == 0 or vals[j] <= 0.0:
                break
            if any_ok[i][j] and not used & bit[j]:
                total += vals[j]
                left -= 1
        return total

    def dfs(i, used, picks, total, prev):
        nonlocal best
        if i == k:
            if total > best[0] + 1e-12:
                best = (total, list(picks))
            return
        if total + bound(i, used) + 1e-9 < best[0] + 1e-12:
            return
        options = [j for j in range(n) if ok[i][j] and not used & bit[j]]
        if not options:
            dfs(i + 1, used, picks + [None], total, -1)
            return
        start = prev + 1 if i and flex[i] == flex[i - 1] else 0
        for j in options:
            if j >= start:
                dfs(i + 1, used | bit[j], picks + [cands[j]], total + vals[j], j)

    dfs(0, 0, [], 0.0, -1)
    return best[1]


def P(i, pos, v):
    return {"espn_player_id": i, "position": pos, "value": v}


def _sorted(pool):
    return sorted(pool, key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))


MODES = ("float", "tenth", "whole", "zeros", "identical", "starved", "negative", "none", "halves")
POSITIONS = ("QB", "RB", "WR", "WR", "TE", "K", None)


def _random_case(rng):
    if rng.random() < 0.6:          # league order (lineup_slots), every flex type
        flex = lineup_slots({s: rng.choice([0, 0, 1, 1, 2, 3]) for s in FLEX_ELIGIBILITY}) or ["FLEX"]
    else:                           # any order, interleaved runs
        flex = [rng.choice(list(FLEX_ELIGIBILITY)) for _ in range(rng.randint(1, 6))]
    mode = rng.choice(MODES)
    pool = []
    for i in range(rng.randint(0, 13)):
        v = rng.uniform(-3.0 if mode == "negative" else 0.0, 25.0)
        v = {"float": v, "tenth": round(v, 1), "whole": float(round(v)), "zeros": v if rng.random() < 0.5 else 0.0,
             "identical": 10.0, "starved": v if i < len(flex) // 2 else 0.0, "negative": round(v, 1),
             "none": rng.choice([None, 0.0, float(round(v))]),
             "halves": rng.choice([None, -1.0, 0.0] + [rng.randint(-2, 12) / 2] * 5)}[mode]
        pid = 100 + rng.randint(0, 16) if rng.random() < 0.15 else 1000 + i     # some ids listed twice
        pool.append(P(pid, rng.choice(POSITIONS), v))
    for _ in range(rng.randint(0, 2) if pool else 0):   # a player listed twice: same or another position/value
        dup = dict(rng.choice(pool))
        if rng.random() < 0.5:
            dup["value"] = round(rng.uniform(-1.0, 20.0), 1)
        if rng.random() < 0.4:
            dup["position"] = rng.choice(POSITIONS)
        pool.append(dup)
    return flex, _sorted(pool), mode


def _key(picks):
    return [p and (p["espn_player_id"], p.get("position"), p.get("value")) for p in picks]


@pytest.mark.parametrize("fast_path", [True, False], ids=["with-fast-path", "dynamic-programme-only"])
def test_flex_solver_matches_the_previous_search_exactly(monkeypatch, fast_path):
    if not fast_path:
        monkeypatch.setattr(lineup, "_matching_bound", lambda *a: None)
    rng = random.Random(20261023)
    seen = {m: 0 for m in MODES}
    for trial in range(2500):
        flex, pool, mode = _random_case(rng)
        seen[mode] += 1
        want = _key(_previous_best_flex(flex, pool))
        assert _key(lineup._best_flex(flex, pool)) == want, (trial, mode, flex, pool)
    assert min(seen.values()) > 200


def test_flex_solver_matches_on_league_shaped_pools():
    """Whole assign_lineup calls on league configurations with many flex slots, the old search's slow cases."""
    rng = random.Random(7)
    configs = [{"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1},
               {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "OP": 1},
               {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "RB/WR": 1, "WR/TE": 1, "FLEX": 2, "OP": 1},
               {"QB": 1, "RB": 1, "WR": 1, "TE": 1, "RB/WR": 2, "WR/TE": 1, "FLEX": 2}]
    for trial in range(300):
        slots = lineup_slots(rng.choice(configs))
        mode = rng.choice(["float", "tenth", "identical", "byes"])
        roster = []
        for i in range(rng.randint(8, 20)):
            v = rng.uniform(0, 25)
            v = {"float": v, "tenth": round(v, 1), "identical": 10.0, "byes": 0.0 if rng.random() < 0.3 else round(v)}[mode]
            roster.append(P(1000 + i, rng.choice(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "D/ST"]), v))
        got = assign_lineup(roster, slots)
        orig, lineup._best_flex = lineup._best_flex, _previous_best_flex
        try:
            want = assign_lineup(roster, slots)
        finally:
            lineup._best_flex = orig
        assert got == want, (trial, slots, roster)


# ------------------------------------------------------------------ timing guards (the old search took seconds)

def _guard_pools():
    def flex(k):
        return lineup_slots({"RB/WR": k // 4, "WR/TE": k // 4, "FLEX": k - 2 * (k // 4) - 1, "OP": 1})
    rng = random.Random(1)
    yield "8-flex identical values", flex(8), [P(1000 + i, rng.choice(["RB", "WR", "TE", "QB"]), 10.0) for i in range(24)]
    rng = random.Random(2)
    yield "8-flex, 18 players, 5 positive", flex(8), [
        P(1000 + i, rng.choice(["RB", "WR", "WR", "TE", "QB"]), rng.uniform(1, 25) if i < 5 else 0.0) for i in range(18)]
    rng = random.Random(3)
    yield "10-flex, 20 players, 7 positive", flex(10), [
        P(1000 + i, rng.choice(["RB", "WR", "WR", "TE", "QB"]), rng.uniform(1, 25) if i < 7 else 0.0) for i in range(20)]
    rng = random.Random(4)
    yield "12-flex, 40 players, mass ties", flex(12), [
        P(1000 + i, rng.choice(["RB", "WR", "WR", "TE", "QB"]), float(rng.choice([0, 5, 5, 10, 10, 10])))
        for i in range(40)]
    rng = random.Random(5)
    yield "12-flex, 40 players, ties and negatives", flex(12), [
        P(1000 + i, rng.choice(["RB", "WR", "WR", "TE", "QB"]), float(rng.choice([-2, 0, 0, 5, 10]))) for i in range(40)]


def _solve_time(flex, pool):
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        lineup._best_flex(flex, pool)
        best = min(best, time.perf_counter() - t)
    return best


@pytest.mark.parametrize("fast_path", [True, False], ids=["with-fast-path", "dynamic-programme-only"])
def test_many_flex_slots_with_ties_or_starved_pools_solve_fast(monkeypatch, fast_path):
    if not fast_path:
        monkeypatch.setattr(lineup, "_matching_bound", lambda *a: None)
    for name, flex, pool in _guard_pools():
        assert _solve_time(flex, _sorted(pool)) < 0.05, name      # ~1 ms; the old search took 0.1-4+ s


def test_the_largest_accepted_lineup_is_still_polynomial(monkeypatch):
    """30 flex slots (the cap), 120 candidates, ties and negatives, no fast path: tens of ms, not hours."""
    monkeypatch.setattr(lineup, "_matching_bound", lambda *a: None)
    flex = lineup_slots({"RB/WR": 7, "WR/TE": 7, "FLEX": 13, "OP": 3})
    assert len(flex) == lineup.MAX_FLEX_SLOTS
    rng = random.Random(6)
    pool = _sorted(P(1000 + i, rng.choice(["RB", "WR", "WR", "TE", "QB"]), float(rng.choice([-3, 0, 2, 2, 7])))
                   for i in range(120))
    assert _solve_time(flex, pool) < 1.0
    assert sum(p is not None for p in lineup._best_flex(flex, pool)) == 30


def test_more_flex_slots_than_any_league_are_refused_not_guessed():
    flex = ["FLEX"] * (lineup.MAX_FLEX_SLOTS + 1)
    with pytest.raises(LineupTooLargeError):
        lineup._best_flex(flex, [P(1, "RB", 5.0)])
    with pytest.raises(LineupTooLargeError):
        assign_lineup([P(1, "RB", 5.0)], lineup_slots({"QB": 1, "FLEX": lineup.MAX_FLEX_SLOTS + 1}))


def test_flex_totals_below_minus_one_still_leave_the_slots_empty():
    """The previous search started from -1.0, so a forced lineup worth -1 or less came back empty; kept as is."""
    pool = _sorted([P(1, "RB", -0.6), P(2, "WR", -0.6)])
    assert lineup._best_flex(["FLEX", "FLEX"], pool) == [None, None] == _previous_best_flex(["FLEX", "FLEX"], pool)
    pool = _sorted([P(1, "RB", -0.4), P(2, "WR", -0.4)])
    assert _key(lineup._best_flex(["FLEX", "FLEX"], pool)) == _key(_previous_best_flex(["FLEX", "FLEX"], pool))

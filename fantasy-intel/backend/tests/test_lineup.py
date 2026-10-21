"""Lineup assignment: real flex slots, no double counting."""

from app.analytics.lineup import assign_lineup, lineup_gain, lineup_slots

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 7, "IR": 1}


def P(i, pos, v, available=True):
    return {"espn_player_id": i, "position": pos, "value": v, "available": available}


def deep_roster():
    return [P(1, "RB", 9), P(2, "RB", 8), P(3, "RB", 7), P(4, "RB", 6),
            P(11, "WR", 9), P(12, "WR", 8), P(13, "WR", 5), P(14, "WR", 4),
            P(21, "TE", 6), P(22, "TE", 3)]


def test_flex_is_one_slot_not_one_rb_plus_one_wr():
    slots = lineup_slots(SLOTS)
    flexy = [s for s in slots if s in ("RB", "WR", "TE", "FLEX")]
    assert len(flexy) == 6                                     # 2 RB + 2 WR + 1 TE + 1 FLEX
    a = assign_lineup(deep_roster(), lineup_slots({"RB": 2, "WR": 2, "FLEX": 1}))
    started = [x for x in a if x["player_id"] is not None]
    assert len(started) == 5                                   # exactly five RB/WR/TE starters, not six
    assert sum(1 for x in started if x["position"] == "RB") + sum(1 for x in started if x["position"] == "WR") == 5


def test_flex_picks_best_remaining_rb():
    a = assign_lineup(deep_roster(), lineup_slots({"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}))
    flex = next(x for x in a if x["slot"] == "FLEX")
    assert flex["player_id"] == 3 and flex["position"] == "RB"   # RB3 (7) beats WR3 (5) and TE2 (3)


def test_flex_picks_best_remaining_wr():
    roster = deep_roster() + [P(15, "WR", 7.5)]
    flex = next(x for x in assign_lineup(roster, lineup_slots({"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}))
                if x["slot"] == "FLEX")
    assert flex["player_id"] == 15 and flex["position"] == "WR"


def test_flex_picks_best_remaining_te():
    roster = deep_roster() + [P(23, "TE", 8)]
    a = assign_lineup(roster, lineup_slots({"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}))
    assert next(x for x in a if x["slot"] == "TE")["player_id"] == 23
    flex = next(x for x in a if x["slot"] == "FLEX")
    assert flex["position"] == "RB" and flex["player_id"] == 3   # TE 21 (6) loses to RB 3 (7)
    roster.append(P(24, "TE", 7.8))
    flex = next(x for x in assign_lineup(roster, lineup_slots({"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}))
                if x["slot"] == "FLEX")
    assert flex["player_id"] == 24 and flex["position"] == "TE"


def test_non_nested_flex_slots_are_optimal():
    a = assign_lineup([P(1, "WR", 9), P(2, "RB", 8), P(3, "TE", 1)], ["RB/WR", "WR/TE"])
    assert sum(x["value"] for x in a) == 17     # RB in RB/WR, WR in WR/TE (greedy would get 10)


def test_superflex_takes_second_qb_when_best():
    a = assign_lineup([P(1, "QB", 9), P(2, "QB", 8), P(3, "WR", 5)], lineup_slots({"QB": 1, "OP": 1}))
    assert {x["player_id"] for x in a} == {1, 2}


def test_unavailable_players_never_fill_slots():
    a = assign_lineup([P(1, "TE", 9, available=False), P(2, "TE", 2)], ["TE"])
    assert a[0]["player_id"] == 2
    assert assign_lineup([P(1, "TE", 9, available=False)], ["TE"])[0]["player_id"] is None


def test_lineup_gain_reports_entry_and_displacement():
    slots = lineup_slots({"RB": 2, "WR": 2, "FLEX": 1})
    g = lineup_gain(deep_roster(), slots, P(99, "WR", 10))
    assert g["enters_lineup"] and g["gain"] == 3.0 and g["displaced_id"] == 3   # flex RB (7) drops out
    g = lineup_gain(deep_roster(), slots, P(98, "RB", 2))
    assert not g["enters_lineup"] and g["gain"] == 0


# ----------------------------------------------------------------------------- exact search, pruned

def _reference_best_flex(flex, pool):
    """The exhaustive search as it was before pruning (kept verbatim as the oracle)."""
    from app.analytics.lineup import eligible

    per_pos = {}
    for p in pool:
        lst = per_pos.setdefault(p.get("position"), [])
        if len(lst) < len(flex):
            lst.append(p)
    cands = sorted((p for lst in per_pos.values() for p in lst),
                   key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    best = (-1.0, [None] * len(flex))

    def dfs(i, used, picks, total):
        nonlocal best
        if i == len(flex):
            if total > best[0] + 1e-12:
                best = (total, list(picks))
            return
        options = [p for p in cands if p["espn_player_id"] not in used and eligible(flex[i], p.get("position"))]
        for p in options:
            dfs(i + 1, used | {p["espn_player_id"]}, picks + [p], total + (p.get("value") or 0.0))
        if not options:
            dfs(i + 1, used, picks + [None], total)

    dfs(0, frozenset(), [], 0.0)
    return best[1]


def test_pruned_flex_search_returns_exactly_what_the_exhaustive_search_did():
    import random

    from app.analytics import lineup

    rng = random.Random(20261021)
    shapes = [["FLEX"], ["FLEX", "FLEX"], ["RB/WR", "WR/TE", "FLEX"], ["FLEX", "FLEX", "FLEX", "OP"],
              ["WR/TE", "FLEX", "OP", "OP"], ["RB/WR", "RB/WR", "FLEX", "OP"], ["OP"], ["WR/TE", "WR/TE"]]
    for trial in range(400):
        flex = rng.choice(shapes)
        n = rng.randint(0, 14)
        # coarse values make exact ties common; a few None and negative values too
        pool = [P(100 + i, rng.choice(["QB", "RB", "WR", "TE", "K"]),
                  rng.choice([None, -1.0, 0.0] + [rng.randint(0, 12) / 2 for _ in range(6)])) for i in range(n)]
        # the same player listed twice (lineup_gain with a candidate already on the roster)
        pool += [dict(rng.choice(pool), value=rng.randint(0, 12) / 2) for _ in range(rng.randint(0, 2) if pool else 0)]
        pool.sort(key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
        got = [p and p["espn_player_id"] for p in lineup._best_flex(flex, pool)]
        want = [p and p["espn_player_id"] for p in _reference_best_flex(flex, pool)]
        assert got == want, (trial, flex, pool)


def test_a_player_listed_twice_fills_one_flex_slot():
    roster = [P(1, "RB", 9), P(1, "RB", 9), P(2, "WR", 3)]
    picks = [a["player_id"] for a in assign_lineup(roster, lineup_slots({"FLEX": 2}))]
    assert sorted(picks) == [1, 2]


def test_three_flex_plus_superflex_is_fast():
    import time

    slots = lineup_slots({"QB": 2, "RB": 3, "WR": 4, "TE": 2, "OP": 1, "D/ST": 1, "K": 1, "FLEX": 3})
    roster = [P(i, pos, 20 - i * 0.37) for i, pos in enumerate(["QB", "RB", "WR", "TE"] * 7)]
    t = time.perf_counter()
    for _ in range(2000):
        assign_lineup(roster, slots)
    assert time.perf_counter() - t < 0.5        # was ~3 ms a call: a 6-team league's waiver run took 20 minutes

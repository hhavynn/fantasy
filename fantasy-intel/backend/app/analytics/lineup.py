"""Deterministic lineup assignment with real interchangeable flex slots.

1. Fill every fixed positional slot (QB, RB, WR, TE, K, D/ST) with the best
   eligible players (an exchange argument shows this is always optimal).
2. Fill the flex slots (RB/WR, WR/TE, FLEX, OP) with an exact search over the
   best remaining eligible players, so non-nested flex combinations
   (e.g. RB/WR + WR/TE) are still optimal. Ties break by player id.

A FLEX is one slot: 2 RB + 2 WR + 1 FLEX = exactly five RB/WR/TE starters.
"""

from __future__ import annotations

FIXED_SLOTS = ("QB", "RB", "WR", "TE", "K", "D/ST")
FLEX_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "RB/WR": ("RB", "WR"),
    "WR/TE": ("WR", "TE"),
    "FLEX": ("RB", "WR", "TE"),
    "OP": ("QB", "RB", "WR", "TE"),
}


def lineup_slots(roster_slots: dict[str, int]) -> list[str]:
    """Expand ESPN slot counts into an ordered list of starting slots (bench/IR excluded)."""
    slots = []
    for s in FIXED_SLOTS:
        slots += [s] * int((roster_slots or {}).get(s, 0))
    for s in sorted(FLEX_ELIGIBILITY, key=lambda k: len(FLEX_ELIGIBILITY[k])):
        slots += [s] * int((roster_slots or {}).get(s, 0))
    return slots


def eligible(slot: str, position: str | None) -> bool:
    return position in FLEX_ELIGIBILITY.get(slot, (slot,))


def _slot_row(slot: str, pick: dict | None) -> dict:
    return {"slot": slot, "player_id": pick["espn_player_id"] if pick else None,
            "position": pick.get("position") if pick else None,
            "value": (pick.get("value") or 0.0) if pick else 0.0,
            "is_flex": slot in FLEX_ELIGIBILITY}


def _best_flex(flex: list[str], pool: list[dict]) -> list[dict | None]:
    """Exact max-value assignment of flex slots: the first maximum in depth-first order over candidates sorted by
    (value desc, id), as an exhaustive search would return it. Two prunings keep that answer and skip the rest:
      * consecutive identical slots take candidates in increasing order (any other order of the same players is a
        later permutation with an equal total, which never replaces the best);
      * a branch whose upper bound (picked so far + the best positive values still eligible) cannot beat the best
        by more than the tie tolerance is cut (only a strictly larger total replaces the best)."""
    # Only the top len(flex) remaining players per position can matter.
    per_pos: dict[str, list[dict]] = {}
    for p in pool:
        lst = per_pos.setdefault(p.get("position"), [])
        if len(lst) < len(flex):
            lst.append(p)
    cands = sorted((p for lst in per_pos.values() for p in lst),
                   key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    vals = [c.get("value") or 0.0 for c in cands]
    ok = [[eligible(s, c.get("position")) for c in cands] for s in flex]
    n, k = len(cands), len(flex)
    # any_ok[i][j]: candidate j fits one of the slots i.. (for the bound)
    any_ok = [[False] * n for _ in range(k + 1)]
    for i in range(k - 1, -1, -1):
        any_ok[i] = [a or b for a, b in zip(any_ok[i + 1], ok[i])]
    best: tuple[float, list] = (-1.0, [None] * k)

    def bound(i: int, used: int) -> float:
        left, total = k - i, 0.0
        for j in range(n):
            if left == 0 or vals[j] <= 0.0:
                break
            if any_ok[i][j] and not used >> j & 1:
                total += vals[j]
                left -= 1
        return total

    def dfs(i: int, used: int, picks: list, total: float, prev: int) -> None:
        nonlocal best
        if i == k:
            if total > best[0] + 1e-12:
                best = (total, list(picks))
            return
        if total + bound(i, used) + 1e-9 < best[0] + 1e-12:
            return
        options = [j for j in range(n) if ok[i][j] and not used >> j & 1]
        if not options:
            dfs(i + 1, used, picks + [None], total, -1)
            return
        start = prev + 1 if i and flex[i] == flex[i - 1] else 0
        for j in options:
            if j >= start:
                dfs(i + 1, used | 1 << j, picks + [cands[j]], total + vals[j], j)

    dfs(0, 0, [], 0.0, -1)
    return best[1]


def assign_lineup(players: list[dict], slots: list[str]) -> list[dict]:
    """players: dicts with espn_player_id, position, value (effective value; None -> 0).
    Players with `available` False are never placed. Returns one dict per slot
    (player_id None = empty slot)."""
    pool = sorted((p for p in players if p.get("available", True)),
                  key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    used: set = set()
    out: list[dict] = []
    flex: list[str] = []
    for slot in slots:
        if slot in FLEX_ELIGIBILITY:
            flex.append(slot)
            continue
        pick = next((p for p in pool if p["espn_player_id"] not in used and p.get("position") == slot), None)
        if pick is not None:
            used.add(pick["espn_player_id"])
        out.append(_slot_row(slot, pick))
    if flex:
        remaining = [p for p in pool if p["espn_player_id"] not in used]
        out += [_slot_row(s, pick) for s, pick in zip(flex, _best_flex(flex, remaining))]
    return out


def lineup_total(assignment: list[dict]) -> float:
    return sum(a["value"] for a in assignment)


def lineup_gain(players: list[dict], slots: list[str], candidate: dict) -> dict:
    """How much the candidate improves the optimal lineup, and whom he displaces."""
    before = assign_lineup(players, slots)
    after = assign_lineup(players + [candidate], slots)
    gain = lineup_total(after) - lineup_total(before)
    before_ids = {a["player_id"] for a in before if a["player_id"] is not None}
    after_ids = {a["player_id"] for a in after if a["player_id"] is not None}
    entered = candidate["espn_player_id"] in after_ids
    displaced = sorted(before_ids - after_ids)
    slot = next((a["slot"] for a in after if a["player_id"] == candidate["espn_player_id"]), None)
    return {"gain": round(gain, 3), "enters_lineup": entered, "slot": slot,
            "displaced_id": displaced[0] if displaced else None}

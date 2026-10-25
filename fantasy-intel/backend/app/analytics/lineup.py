"""Deterministic lineup assignment with real interchangeable flex slots.

1. Fill every fixed positional slot (QB, RB, WR, TE, K, D/ST) with the best
   eligible players (an exchange argument shows this is always optimal).
2. Fill the flex slots (RB/WR, WR/TE, FLEX, OP) with an exact optimum over the
   best remaining eligible players, so non-nested flex combinations
   (e.g. RB/WR + WR/TE) are still optimal. Ties break by player id: the result is
   the one an exhaustive depth-first search returns, computed in polynomial time
   (_best_flex).

A player the provider lists at two positions (issue #253 G11: an RB/WR may fill an RB or a WR slot) breaks step 1's
exchange argument. When one is in the pool, every choice of position for those players is solved by the two steps
above and the best total wins (_assign_dual); without one the two steps run unchanged.

A FLEX is one slot: 2 RB + 2 WR + 1 FLEX = exactly five RB/WR/TE starters.
"""

from __future__ import annotations

import json
from itertools import combinations, product
from types import MappingProxyType

FIXED_SLOTS = ("QB", "RB", "WR", "TE", "K", "D/ST")
FLEX_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "RB/WR": ("RB", "WR"),
    "WR/TE": ("WR", "TE"),
    "FLEX": ("RB", "WR", "TE"),
    "OP": ("QB", "RB", "WR", "TE"),
}
TIE_TOLERANCE = 1e-9    # flex totals this close to the optimum are ties (sums of the same values in another order differ
                        # by ~1e-14; distinct projected totals by far more)
MAX_FLEX_SLOTS = 30     # more flex slots than any real league; a provider can store any count, so more are refused
MAX_SPLIT_IDS = 4       # players listed under two different flex positions in one pool (not seen in real data)
MAX_DUAL_CHOICES = 64   # position choices tried for players eligible at two positions (six RB/WRs in one pool)


UNSUPPORTED_LINEUP = "This league's lineup configuration is not supported."


class LineupTooLargeError(ValueError):
    """The flex slots are beyond what the exact solver accepts (MAX_FLEX_SLOTS, MAX_SPLIT_IDS): refused, never guessed.
    The API answers 422 UNSUPPORTED_LINEUP (app/main.py); the message itself is safe to show as well."""


# Unsupported starting slots (issue #253 G2-G4). Every slot label other than these and FIXED + FLEX starts a player no
# engine values: individual defensive players (IDP), punter, head coach, or a label the parsers do not recognise. A
# blank label (ESPN slot 22) is a non-starter for the parser and the provider adapter, so it is not one.
NON_STARTING_SLOTS = frozenset({"BE", "IR", "TAXI", ""})
# Unsupported slots whose eligible positions are known and never an engine position (league_rules._IDP, _ESPN_OTHER):
# whoever starts there can start nowhere the engines fill, and the reverse. Only then does a lineup change that leaves
# those players alone change the full lineup total by exactly what it changes the engines' slots.
SEPARATE_SLOTS = ("DT", "DE", "DL", "LB", "CB", "S", "DB", "DP", "P", "HC")


class LineupSlots(list):
    """lineup_slots' result: the starting slots the engines fill, in order (a plain list to every consumer), carrying
    `unsupported`, the league's other starting slot labels (IDP, P, HC, unrecognised), each once. A lineup total over
    these slots leaves those out, so it is partial, never complete (team_lineups.project_week, start_sit)."""
    unsupported: tuple[str, ...] = ()


def unsupported_slots(roster_slots: dict | None) -> tuple[str, ...]:
    """Starting slot labels with a count that no engine fills, in a fixed order (SEPARATE_SLOTS, then by label)."""
    out = []
    for label, count in (roster_slots or {}).items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        label = str(label)
        if n > 0 and label not in NON_STARTING_SLOTS and label not in FIXED_SLOTS and label not in FLEX_ELIGIBILITY:
            out.append(label)
    order = {s: i for i, s in enumerate(SEPARATE_SLOTS)}
    return tuple(sorted(set(out), key=lambda s: (order.get(s, len(order)), s)))


def unsupported_of(slots) -> tuple[str, ...]:
    """The unsupported starting slots a slot list carries (LineupSlots); () for a plain list."""
    return tuple(getattr(slots, "unsupported", ()) or ())


def slots_left_out(unsupported) -> str:
    """Why a total is partial when the league starts slots no engine fills (issue #253 G3)."""
    return f"lineup totals leave out {', '.join(unsupported)}, slots no engine fills"


def separable(unsupported) -> bool:
    """Every unsupported slot takes only positions the engines never start (see SEPARATE_SLOTS)."""
    return all(s in SEPARATE_SLOTS for s in unsupported)


def lineup_slots(roster_slots: dict[str, int]) -> LineupSlots:
    """Expand ESPN slot counts into an ordered list of starting slots (bench/IR excluded). Slots no engine fills are not
    in the list; they are named in its `unsupported` attribute."""
    slots = LineupSlots()
    for s in FIXED_SLOTS:
        slots += [s] * int((roster_slots or {}).get(s, 0))
    for s in FLEX_SLOTS:
        slots += [s] * int((roster_slots or {}).get(s, 0))
    slots.unsupported = unsupported_slots(roster_slots)
    return slots


# Slot legality (issue #253 G11). Every engine asks these, never FLEX_ELIGIBILITY itself
# (tests/test_lineup_eligibility.py audits the source), so dual eligibility changes in one place.
FLEX_SLOTS: tuple[str, ...] = tuple(sorted(FLEX_ELIGIBILITY, key=lambda k: len(FLEX_ELIGIBILITY[k])))  # narrowest first
# Stored eligible_slots ids (ESPN's lineupSlotId space; the Sleeper writer stores the same) that name a position.
# Flex, bench and IR ids name slots, not positions, so they never widen eligibility.
POSITION_SLOT_IDS = MappingProxyType({0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K"})
# On: assign_lineup tries each position of a player eligible at two (_assign_dual). False makes every player
# primary-only again, exactly as before G11.
DUAL_ELIGIBILITY = True


def is_flex(slot: str) -> bool:
    return slot in FLEX_ELIGIBILITY


def slot_positions(slot: str) -> tuple[str, ...]:
    """The positions a starting slot takes: a flex slot's list, else the slot's own position."""
    return FLEX_ELIGIBILITY.get(slot, (slot,))


def _field(player, name: str):
    return player.get(name) if isinstance(player, dict) else getattr(player, name, None)


def parse_eligible_slots(raw, position: str | None = None) -> tuple[int, ...] | None:
    """A stored `eligible_slots` value (a list, or the JSON text DuckDB returns for the JSON column) as slot ids; None
    when missing or malformed. With `position` (what loaders pass): only the ids that name a position, and None unless
    one names a position other than `position`, so a single-position player carries nothing and the solver's checks
    (dual_positions, eligible) stay a dictionary lookup for him."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, (list, tuple)):
        return None
    ids = tuple(s for s in raw if type(s) is int)
    if position is None:
        return ids
    named = tuple(s for s in ids if s in POSITION_SLOT_IDS)
    return named if any(POSITION_SLOT_IDS[s] != position for s in named) else None


def positions_of(player) -> frozenset[str]:
    """Every position the provider says a player (a dict or an object with `position`) may fill: the primary position
    plus each position whose own slot id is in his stored `eligible_slots` (a list or its JSON text). Missing or
    malformed evidence is the primary position only: eligibility widens on provider evidence, never on a guess."""
    primary = _field(player, "position")
    out = {primary} if primary else set()
    slots = parse_eligible_slots(_field(player, "eligible_slots"))
    if slots:
        out |= {POSITION_SLOT_IDS[s] for s in slots if s in POSITION_SLOT_IDS}
    return frozenset(out)


def slot_takes(slots) -> frozenset[str]:
    """Every position some slot in `slots` takes."""
    return frozenset(q for s in set(slots) for q in slot_positions(s))


def dual_positions(player, slots) -> frozenset[str]:
    """The positions `player` may fill that `slots` take, when provider evidence adds one to his primary position
    (else empty): the players whose eligibility changes a lineup. Always empty with DUAL_ELIGIBILITY off."""
    if not DUAL_ELIGIBILITY or not _field(player, "eligible_slots"):
        return frozenset()
    taken = positions_of(player) & slot_takes(slots)
    return taken if taken - {_field(player, "position")} else frozenset()


def eligible(slot: str, player) -> bool:
    """May `player` start in `slot`? `player` is a position string (None = unknown, never eligible) or a player (a dict
    or an object with `position`, optionally `eligible_slots`)."""
    allowed = slot_positions(slot)
    if player is None or isinstance(player, str):
        return player in allowed
    if DUAL_ELIGIBILITY and _field(player, "eligible_slots"):
        return not positions_of(player).isdisjoint(allowed)
    return _field(player, "position") in allowed


def _slot_row(slot: str, pick: dict | None) -> dict:
    pick = pick.get("_player", pick) if pick else pick      # a dual player's row keeps his primary position
    return {"slot": slot, "player_id": pick["espn_player_id"] if pick else None,
            "position": pick.get("position") if pick else None,
            "value": (pick.get("value") or 0.0) if pick else 0.0,
            "is_flex": slot in FLEX_ELIGIBILITY}


def _compositions(total: int, caps: list[int]):
    """Every way to take `total` players from lists with `caps` players left (one count per list)."""
    if len(caps) == 1:
        if total <= caps[0]:
            yield (total,)
        return
    rest = sum(caps[1:])
    for x in range(max(0, total - rest), min(caps[0], total) + 1):
        for tail in _compositions(total - x, caps[1:]):
            yield (x, *tail)


def _matching_bound(flex: list[str], elig: dict, cands: list[dict], vals: list[float], pidx: dict) -> float | None:
    """The max-weight matching of the positive candidate values to the flex slots, an upper bound on every leaf of
    the search (a leaf is a matching). Greedy in candidate order is exact: the players that can start together form
    a transversal matroid, and since players of one position fit the same slots, a set fits iff for every set S of
    positions it has no more players at S than slots taking one of S (Hall). None when a player is listed under two
    of these positions (the fast path is then skipped).

    -inf when the first leaf is optimal without it: every slot takes all the positions the slot before it takes (one
    flex type, or FLEX then OP), no value is negative and no player is listed twice. An optimum that first differs from
    the first leaf at slot i holds the first leaf's pick there at a later, wider slot or not at all, and exchanging the
    two never lowers the total; so the fast path returns the first leaf, as it would after computing the matching."""
    if (not vals or vals[-1] >= 0.0) and all(set(elig[a]) <= set(elig[b]) for a, b in zip(flex, flex[1:])) \
            and len({c["espn_player_id"] for c in cands}) == len(cands):
        return float("-inf")
    full = 1 << len(pidx)
    slot_masks = [sum(1 << pidx[q] for q in elig[s]) for s in flex]
    cap = [sum(1 for m in slot_masks if m & S) for S in range(full)]
    cnt = [0] * full
    containing = [[S for S in range(1, full) if S >> i & 1] for i in range(len(pidx))]
    at: dict = {}
    bound, placed = 0.0, 0
    for c, v in zip(cands, vals):
        i = pidx.get(c.get("position"))
        if i is None:
            continue
        pid = c["espn_player_id"]
        if pid in at:
            if at[pid] != i:
                return None
            continue
        at[pid] = i
        if v <= 0.0 or placed == len(flex):
            continue
        if all(cnt[S] < cap[S] for S in containing[i]):
            for S in containing[i]:
                cnt[S] += 1
            bound += v
            placed += 1
    return bound


def _best_flex(flex: list[str], pool: list[dict]) -> list[dict | None]:
    """Exact max-value assignment of the flex slots: the first maximum in depth-first order over candidates sorted by
    (value desc, id), exactly what an exhaustive search returns, in polynomial time.

    The exhaustive search fills slot after slot, trying every eligible unused candidate in that order, and leaves a
    slot empty only when no eligible candidate is left; it keeps the first total that beats the best so far (starting
    from -1.0) by more than a tie tolerance. Three facts make that answer cheap to compute:
      * two players at one position fit exactly the same slots, so the chosen leaf takes each position's players in
        candidate order (swapping a later one in for an earlier one never raises the total and is earlier in the
        order). A state is then just how many players of each position are used, plus which of the rare players
        listed under two positions are used (`MAX_SPLIT_IDS`);
      * consecutive identical slots (a run) take any min(run length, eligible players left) players of their
        positions, so a run is one step of a dynamic programme over these states;
      * the best total from any state (`best`) is exact, even with negative values (a slot cannot stay empty while
        an eligible player is left), so the first leaf in order that reaches the optimum is found greedily, slot by
        slot, without backtracking: the first candidate whose best completion still reaches the optimum.
    A fast path returns the very first leaf when it already reaches an upper bound (`_matching_bound`).
    Cost: (states) x (ways to fill one run), polynomial in the slot and player counts, never exponential. The small
    constants assume canonical slot order, each flex type contiguous, as every caller passes them (lineup_slots and
    start_sit.build_slot_instances): then there are at most four runs (~15 ms worst case at MAX_FLEX_SLOTS). Any other
    order is still exact and polynomial but has one run per slot change, so the state space grows much faster
    (interleaved slots with 4 split players reach ~5 s at 30 slots). The slots are never reordered here: the order is
    part of the result (which player fills which slot). More than MAX_FLEX_SLOTS slots are refused."""
    k = len(flex)
    if not k:
        return []
    if k > MAX_FLEX_SLOTS:
        raise LineupTooLargeError(f"{UNSUPPORTED_LINEUP} At most {MAX_FLEX_SLOTS} flex slots are supported.")
    # Only the top len(flex) remaining players per position can matter.
    per_pos: dict[str, list[dict]] = {}
    for p in pool:
        lst = per_pos.setdefault(p.get("position"), [])
        if len(lst) < k:
            lst.append(p)
    cands = sorted((p for lst in per_pos.values() for p in lst),
                   key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    vals = [c.get("value") or 0.0 for c in cands]
    elig = {s: FLEX_ELIGIBILITY.get(s, (s,)) for s in flex}
    positions = sorted({q for e in elig.values() for q in e})
    pidx = {q: i for i, q in enumerate(positions)}
    # Fast path: the very first leaf (every slot takes its first eligible candidate) is the answer when it already
    # reaches the max-weight matching of the positive values, an upper bound on every leaf.
    bound = _matching_bound(flex, elig, cands, vals, pidx)
    if bound is not None:
        taken: set = set()
        first_leaf: list[dict | None] = []
        for s in flex:
            pick = next((c for c in cands if c.get("position") in elig[s] and c["espn_player_id"] not in taken), None)
            if pick is not None:
                taken.add(pick["espn_player_id"])
            first_leaf.append(pick)
        reach = sum(c.get("value") or 0.0 for c in first_leaf if c is not None)
        if reach >= bound - TIE_TOLERANCE and reach > -1.0 + 1e-12:
            return first_leaf
    # The first candidate per (player id, position): a later listing of the same player at the same position fits the
    # same slots, is worth no more and comes later, so it is never chosen. Players listed twice still take one slot.
    first: dict = {}
    for j, c in enumerate(cands):
        q = pidx.get(c.get("position"))
        if q is not None:
            first.setdefault(c["espn_player_id"], {}).setdefault(q, j)
    lists: list[list[int]] = [[] for _ in positions]
    split: list[dict[int, int]] = []        # players listed under two or more of these positions
    for where in first.values():
        if len(where) == 1:
            (q, j), = where.items()
            lists[q].append(j)
        else:
            split.append(where)
    if len(split) > MAX_SPLIT_IDS:
        raise LineupTooLargeError(f"{UNSUPPORTED_LINEUP} Too many players are listed under two positions.")
    for lst in lists:
        lst.sort()
    size = [len(lst) for lst in lists]
    pre = [[0.0] for _ in lists]
    for q, lst in enumerate(lists):
        for j in lst:
            pre[q].append(pre[q][-1] + vals[j])
    runs: list[list] = []       # [eligible position indexes, length, split players eligible (index, value), slot]
    for s in flex:
        e = tuple(sorted(pidx[q] for q in elig[s]))
        if runs and runs[-1][3] == s:
            runs[-1][1] += 1
        else:
            runs.append([e, 1, [(i, max(vals[j] for q, j in where.items() if q in e))
                                for i, where in enumerate(split) if any(q in e for q in where)], s])
    R = len(runs)
    memo: dict = {}

    def best(r: int, rem: int, c: tuple[int, ...], mask: int) -> float:
        """The most the last `rem` slots of run r and all later runs can add, with c players of each position and the
        split players in `mask` already used."""
        if r == R:
            return 0.0
        key = (r, rem, c, mask)
        out = memo.get(key)
        if out is not None:
            return out
        e, _, splits, _ = runs[r]
        free = [(i, v) for i, v in splits if not mask >> i & 1]
        room = [size[q] - c[q] for q in e]
        take = min(rem, sum(room) + len(free))
        if r == R - 1:      # last run: simply the `take` best players left (forced even when negative)
            items = [v for _, v in free]
            for q, a in zip(e, room):
                items += [vals[j] for j in lists[q][c[q]:c[q] + min(a, take)]]
            out = sum(sorted(items, reverse=True)[:take])
        else:
            out = float("-inf")
            nxt = runs[r + 1][1]
            for u in range(min(len(free), take) + 1):
                if take - u > sum(room):
                    continue
                for group in combinations(free, u):
                    gain0 = sum(v for _, v in group)
                    mask2 = mask
                    for i, _ in group:
                        mask2 |= 1 << i
                    for x in _compositions(take - u, room):
                        c2, gain = list(c), gain0
                        for q, xq in zip(e, x):
                            if xq:
                                gain += pre[q][c[q] + xq] - pre[q][c[q]]
                                c2[q] += xq
                        out = max(out, gain + best(r + 1, nxt, tuple(c2), mask2))
        memo[key] = out
        return out

    state, mask = (0,) * len(positions), 0
    optimum = best(0, runs[0][1], state, mask)
    if not optimum > -1.0 + 1e-12:          # the exhaustive search starts from -1.0 and keeps nothing below it
        return [None] * k
    floor = optimum - TIE_TOLERANCE
    picks: list[dict | None] = []
    total = 0.0
    for r, (e, length, splits, _) in enumerate(runs):
        for o in range(length):
            options = [(lists[q][state[q]], state[:q] + (state[q] + 1,) + state[q + 1:], mask)
                       for q in e if state[q] < size[q]]
            options += [(split[i][q], state, mask | 1 << i)
                        for i, _ in splits if not mask >> i & 1 for q in e if q in split[i]]
            if not options:             # no eligible player left: the slot stays empty, as in the search
                picks.append(None)
                continue
            options.sort()
            chosen, top = None, None
            for opt in options:         # the first candidate whose best completion still reaches the optimum
                reach = total + vals[opt[0]] + best(r, length - o - 1, opt[1], opt[2])
                if reach >= floor and reach > -1.0 + 1e-12:
                    chosen = opt
                    break
                if top is None or reach > top[0]:
                    top = (reach, opt)
            j, state, mask = chosen or top[1]   # (best is exact: `top` only guards against float noise)
            total += vals[j]
            picks.append(cands[j])
    return picks


def assign_lineup(players: list[dict], slots: list[str]) -> list[dict]:
    """players: dicts with espn_player_id, position, value (effective value; None -> 0), optionally eligible_slots.
    Players with `available` False are never placed. Returns one dict per slot
    (player_id None = empty slot). Each row's position is the player's primary position, wherever he starts."""
    pool = sorted((p for p in players if p.get("available", True)),
                  key=lambda p: (-(p.get("value") or 0.0), p["espn_player_id"]))
    duals = [(p, d) for p in pool if p.get("eligible_slots") and (d := dual_positions(p, slots))]
    if duals:
        return _assign_dual(pool, slots, duals)
    return [_slot_row(s, pick) for s, pick in _assign(pool, slots)]


def _assign(pool: list[dict], slots: list[str]) -> list[tuple[str, dict | None]]:
    """Steps 1 and 2 of the module docstring for one position per player: (slot, pick) in slot order."""
    used: set = set()
    out: list = []
    flex: list[str] = []
    for slot in slots:
        if slot in FLEX_ELIGIBILITY:
            flex.append(slot)
            continue
        pick = next((p for p in pool if p["espn_player_id"] not in used and p.get("position") == slot), None)
        if pick is not None:
            used.add(pick["espn_player_id"])
        out.append((slot, pick))
    if flex:
        remaining = [p for p in pool if p["espn_player_id"] not in used]
        out += list(zip(flex, _best_flex(flex, remaining)))
    return out


def _assign_dual(pool: list[dict], slots: list[str], duals: list[tuple[dict, frozenset]]) -> list[dict]:
    """A pool with players eligible at two positions: each such player is given each of his positions in turn (his
    primary first) and the one-position solver above runs for every combination, under exactly its rules. Every legal
    lineup starts each player at one of his positions, so with no negative value the best total over the combinations
    is the optimum over every legal lineup (tests/test_lineup_dual_eligibility.py checks it against an exhaustive
    search); with negative values it is never below the everyone-primary lineup. That first combination is today's
    lineup, and a later one replaces the best so far only when it is higher by more than TIE_TOLERANCE: a dual player
    moves only for a real gain, and ties keep the earlier combination, deterministically. More than MAX_DUAL_CHOICES
    combinations are refused (LineupTooLargeError), never guessed."""
    choices = [[p.get("position"), *sorted(d - {p.get("position")})] for p, d in duals]
    count = 1
    for c in choices:
        count *= len(c)
    if count > MAX_DUAL_CHOICES:
        raise LineupTooLargeError(f"{UNSUPPORTED_LINEUP} Too many players are eligible at two positions.")
    at = {p["espn_player_id"]: i for i, (p, _) in enumerate(duals)}
    best, best_total = None, None
    for combo in product(*choices):
        trial = [p if (i := at.get(p["espn_player_id"])) is None or combo[i] == p.get("position")
                 else {**p, "position": combo[i], "_player": p} for p in pool]
        rows = _assign(trial, slots)
        total = sum((pick.get("value") or 0.0) for _, pick in rows if pick is not None)
        if best_total is None or total > best_total + TIE_TOLERANCE:
            best, best_total = rows, total
    return [_slot_row(s, pick) for s, pick in best]


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

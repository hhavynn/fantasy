"""Exact before/after trade simulation on ESPN's weekly projections. Hypothetical only.

A trade's value is what it does to both teams' optimal starting lineups over the remaining weeks:

    roster before -> optimal lineup every week -> projected starting points
    apply trade   -> resolve roster capacity    -> optimal lineup every week again
    delta = after - before, independently for each team

No generic trade-chart values, no consolidation tax, no star premium, no playoff weighting, no
acceptance probability. Bench points never count. Hypothetical free-agent pickups are reported
separately and never enter the canonical delta. The one opt-in exception is the K/D/ST streaming
baseline (with_streaming): on such a state every lineup, both before and after, may fill K and D/ST
from weekly free-agent streaming. Nothing here reads or writes the database.
See docs/v2_trade_evaluator.md.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations

from .lineup import dual_positions, separable, unsupported_of
from .lineup_totals import LineupTotals
from .player_availability import ELIGIBLE_UNCONFIRMED, INELIGIBLE, NO_WEEK_LEFT, NOTE as AVAILABILITY_NOTE, is_restricted
from .team_lineups import (IR_HOLD_NOTE, PROJECTION_SOURCE, STREAM_NOTE, source_label, STREAM_RANK, Projections, RosterPlayer,
                           StreamingProjections, Window, best_fa_addition, compare, ir_activation_sensitivity,
                           known_total, lineup_relevant, project_team, public_weeks, r, replacement_context,
                           roster_marginal_value,
                           arrives_active, slot_flags, starters, stream_summary, with_stream)

# Manual trades take any player counts from 1 up to this many per side (a performance guard, not a
# rule of the game; override with max_per_side / settings.trade_max_players_per_side).
MAX_PLAYERS_PER_SIDE = 6
# Exact forced-drop resolution budget: legal-candidate drop sets enumerated, and exact lineup
# re-solves. Beyond either, the evaluation is reported unavailable rather than approximated.
MAX_DROP_SETS = 250_000
MAX_DROP_SOLVES = 5_000
DROP_EPS = 1e-6

ASSUMPTIONS = [
    "Projected points source: ESPN weekly projections, unmodified (no role, matchup, schedule, injury or market multipliers).",
    "Trade impact: our exact optimal-lineup simulation for every remaining fantasy week, using the league's real roster slots and FLEX rules.",
    "Each team's current roster is assumed to be its roster for every remaining week (no future waiver moves).",
    "Only starters count; bench points are never value.",
    "A projected 0 (bye, injury) is a real value; a missing projection is unknown and is never treated as 0.",
    "Incoming players occupy active roster spots; they are never assumed to go to IR (a traded IR player arrives as an active-roster asset, subject to capacity and forced drops).",
    "NFL availability is separate from the fantasy IR slot: in weeks a player is verified NFL-ineligible (reserve list, minimum-games rule not yet met) he cannot start for either team, whatever ESPN projects; his projection is shown, never counted and never treated as 0. An earliest eligible week is a minimum, not a return date.",
    "Players currently in an IR slot stay rostered but inactive: they never enter the canonical starting lineup and their projection never affects completeness until a future sync shows ESPN moved them off IR.",
    IR_HOLD_NOTE,
    "IR activation sensitivity (what an IR player would add if activated for free) is shown separately, is deliberately optimistic, and is never in the canonical delta.",
    "Evaluation window: a trade proposed now starts at the first scoring period none of whose NFL games has kicked off; weeks already under way are never credited (no partial-week simulation).",
    "Regular season and playoffs are reported separately and never weighted against each other.",
]
LIMITATIONS = [
    "ESPN projections can be wrong.",
    "Projections are not adjusted by our role/usage context yet; that context is shown for explanation only.",
    "Future injuries are unknowable.",
    "Future waiver acquisitions are not guaranteed; hypothetical FA replacements are shown separately and never included in the trade delta.",
    "ESPN undroppable-list restrictions are not modeled.",
    "Manager acceptance is not predicted.",
]
IR_WARNING = ("Current IR players are excluded from canonical starting-lineup totals until activated. "
              "ESPN future projections are shown separately as activation upside.")


class TradeError(ValueError):
    pass


class NoRemainingWeeksError(TradeError):
    """Every fantasy scoring period through the final one has kicked off: nothing left to change."""


class EvaluationUnavailable(TradeError):
    """Exact forced-drop resolution would exceed its resource budget; no result is invented. `solves`: the lineup totals
    the search had already solved when it stopped (choose_forced_drops' own count), so a caller that budgets work
    (trade discovery's beam) is charged for the work actually done, not only for an evaluation that finished."""

    def __init__(self, message: str = "", solves: int = 0):
        super().__init__(message)
        self.solves = solves


@dataclass
class LeagueState:
    """Everything the evaluator needs, loaded once (see trade_data.load_league_state)."""
    season: int
    slots: list[str]                               # starting slots (lineup.lineup_slots)
    final_week: int
    last_regular: int | None
    start_week: int | None                         # earliest actionable week (trade_data.evaluation_start_week); None = none remain
    rosters: dict[int, list[RosterPlayer]]
    free_agents: list[RosterPlayer]
    projections: Projections
    active_capacity: int                           # starting + bench slots (IR and taxi excluded)
    position_limits: dict[str, int] = field(default_factory=dict)   # max rostered per position; {} = not enforced
    team_names: dict[int, str] = field(default_factory=dict)
    league_id: int | None = None
    start_week_reason: dict = field(default_factory=dict)
    undroppable_list_in_use: bool | None = None
    first_projection_week: int | None = None       # earliest week whose projections were loaded
    availability: dict = field(default_factory=dict)  # espn_player_id -> player_availability record (explanation)
    free_agent_status: dict = field(default_factory=dict)  # free agent id -> ESPN FREEAGENT | WAIVERS; absent = unknown
    projection_source: str = "espn"                # "independent": Ours (a league without provider projections)
    # REQUIRED (keyword-only, no default, #253 G4): team -> rostered players (current lineup rows) missing from the
    # player data, so absent from `rosters` (Sleeper stores no individual defensive players); their roster spots are
    # invisible to every count here. 0 = every rostered player is counted; a team with no entry has no count, so its
    # roster legality is unverified (roster_legality). A state built in memory passes {team: 0} for complete rosters.
    players_missing: dict = field(kw_only=True)

    def window(self, start_week: int | None = None) -> Window:
        start = self.start_week if start_week is None else start_week
        if start is None:
            return Window((), self.last_regular)      # no remaining weeks: never manufacture one
        return Window(tuple(range(start, self.final_week + 1)), self.last_regular)


def with_streaming(state: LeagueState, rank: int = STREAM_RANK) -> LeagueState:
    """The same league state whose lineups value K and D/ST against weekly free-agent streaming
    (team_lineups.StreamingProjections). Opt-in: the trade evaluator, discovery, team value and every
    canonical delta built on the returned state use it consistently; `state` itself is not changed."""
    weeks = sorted({w for per in state.projections.values() for w in per})
    return replace(state, projections=with_stream(state.projections, state.free_agents, state.slots, weeks, rank))


def streaming_enabled(state: LeagueState) -> bool:
    return isinstance(state.projections, StreamingProjections)


# Said only in a league with such a player (issue #253 G11), so every other league's text is unchanged.
DUAL_NOTE = ("A player the provider lists at two positions (for example RB and WR) starts wherever his team scores more "
             "in these lineups, so a team holding one can show a different lineup delta than it would with his primary "
             "position alone. His ranks, replacement level and trade value stay at his primary position.")


def has_dual_player(state: LeagueState) -> bool:
    return any(dual_positions(p, state.slots) for ps in (*state.rosters.values(), state.free_agents) for p in ps)


def assumptions(state: LeagueState) -> list[str]:
    dual = [DUAL_NOTE] if has_dual_player(state) else []
    if not streaming_enabled(state):
        return ASSUMPTIONS + dual if dual else ASSUMPTIONS
    return [STREAM_NOTE if a.startswith("Each team's current roster") else a for a in ASSUMPTIONS] + [
        "Apart from that weekly K/D/ST stream, each team's current roster is assumed to be its roster for every "
        "remaining week (no other waiver moves)."] + dual


# --------------------------------------------------------------------- roster rules --

UNVERIFIED = "unverified"
ROSTER_UNVERIFIED = "ROSTER_UNVERIFIED"


def roster_legality(state: LeagueState, team_id: int | None) -> dict | None:
    """None when the active roster count is exact. `unverified` only when it is not (issue #253 G4): the team has
    rostered players missing from the player data (Sleeper stores no individual defensive players, so their spots are
    invisible and an open spot may not exist), or there is no player count for the team at all (`players_missing` has
    no entry). A provider that stores every rostered player (ESPN, IDP, P and HC included) keeps an exact count. A roster the count already shows over capacity stays illegal: players left
    out can only add to it."""
    unsupported = unsupported_of(state.slots)
    counts = state.players_missing
    if team_id is None:
        return None
    missing = int(counts.get(team_id) or 0)
    if missing:
        why = (f"{missing} rostered player{'s are' if missing != 1 else ' is'} not in the synced player data "
               "(positions Fantasy Intel does not store, such as individual defensive players), so the active roster "
               "count leaves them out and an open spot may not exist.")
    elif team_id not in counts:
        why = ("The synced rosters hold no lineup rows for this team this week, so whether every rostered player is "
               "counted is unknown and the active roster count may be short.")
    else:
        return None
    return {"status": UNVERIFIED, "code": ROSTER_UNVERIFIED, "unsupported_slots": list(unsupported),
            "players_missing": missing,
            "reason": why + " Roster legality is unverified: check your open roster spots in the provider's app "
                            "before acting."}


def _active(players: list[RosterPlayer]) -> int:
    return sum(1 for p in players if not p.reserved)    # IR and taxi players use no active roster spot


def unvalued_ids(players: list[RosterPlayer], state: LeagueState) -> frozenset[int]:
    """Players who can start only in slots no engine fills (IDP, P, HC): their lineup value is unknown, so a drop of
    one is never chosen for its "0" known cost (issue #253). Empty in a league without such slots."""
    if not unsupported_of(state.slots):
        return frozenset()
    return frozenset(p.espn_player_id for p in players if not lineup_relevant(p, state.slots))


def unvalued_note(names: list[str], state: LeagueState) -> str:
    return (f"{', '.join(names)} can start only in {', '.join(unsupported_of(state.slots))}, which no engine fills: "
            "what he adds to the lineup is not modelled, so the cost of losing him is unknown.")


def _position_counts(players: list[RosterPlayer]) -> Counter:
    return Counter(p.position for p in players)


def legal_roster(players: list[RosterPlayer], state: LeagueState, before: list[RosterPlayer]) -> bool:
    """Active roster within capacity and every position within ESPN's limit. A limit the team
    already exceeded before the trade is not blamed on the trade."""
    if _active(players) > max(state.active_capacity, _active(before)):
        return False
    counts, prior = _position_counts(players), _position_counts(before)
    return all(counts[pos] <= max(lim, prior[pos]) for pos, lim in state.position_limits.items())


def minimum_drops(after: list[RosterPlayer], before: list[RosterPlayer], state: LeagueState) -> int:
    """A lower bound on how many players must go: each removal frees at most one active spot and
    lowers at most one position count."""
    cap = max(0, _active(after) - max(state.active_capacity, _active(before)))
    counts, prior = _position_counts(after), _position_counts(before)
    pos = sum(max(0, counts[p] - max(lim, prior[p])) for p, lim in state.position_limits.items())
    return max(1, cap, pos)


def choose_forced_drops(after: list[RosterPlayer], before: list[RosterPlayer], state: LeagueState,
                        incoming: set[int], total, negative: bool = False, total_without=None,
                        single_bound=None) -> dict:
    """The forced drops for an illegal post-trade roster, shared by the evaluator and discovery so both
    pick the same players. `total(players)` is the known starting total over the window.

    Rule (unchanged from V2): the smallest number of removals that makes the roster legal; among
    those, the set keeping the largest remaining known total (ties: lowest player ids); a set without
    an incoming player wins unless none is legal, and the cheapest set with one is reported when it
    would have been strictly cheaper (a redundant incoming asset).

    Exact branch and bound: the total is a sum of weekly maximum-weight lineups, which with
    non-negative projections is monotone submodular in the roster, so removing a set D costs at
    least the sum of its players' single-removal costs. Drop sets are solved in increasing order of
    that bound and the search stops once the bound exceeds the best exact cost, so no unsolved set
    can beat (or tie) the choice. A negative projection voids the bound: every legal set is solved.
    Budgets: MAX_DROP_SETS candidate sets, MAX_DROP_SOLVES exact solves (EvaluationUnavailable).

    `total_without(combo)`, when given, must equal `total` of `after` without the players in `combo` (a tuple of
    objects from `after`); callers that can answer from the removed players alone pass it (LineupTotals.removal_total),
    which saves rebuilding the roster for every candidate set. The search and its choice are the same either way.
    `single_bound(p)`, when given, stands in for each player's single-removal cost in the lower bounds: it must not
    exceed that cost by more than float rounding (LineupTotals' RemovalTotal.single_bound computes it from one solve
    per week and position). The choice does not depend on it: a set is left unsolved only when its bound exceeds the
    best exact cost found by DROP_EPS, so it cannot beat or tie the choice, and the chosen set (bound <= its cost <= the
    best cost) is always solved. Only the order and the number of exact solves change."""
    if total_without is None:
        def total_without(combo):
            out = {id(p) for p in combo}           # the combos hold the very objects of `after`
            return total([p for p in after if id(p) not in out])
    pool = sorted(after, key=lambda p: p.espn_player_id)
    cap = max(state.active_capacity, _active(before))
    counts, prior = _position_counts(after), _position_counts(before)
    limits = {p: max(lim, prior[p]) for p, lim in state.position_limits.items()}
    act = _active(after)
    full = total(after)
    if negative:
        single, solves = {}, 0
    elif single_bound is not None:
        single, solves = {p.espn_player_id: max(0.0, single_bound(p)) for p in pool}, 0
    else:
        single = {p.espn_player_id: max(0.0, full - total_without((p,))) for p in pool}
        solves = len(pool)
    # The MAX_DROP_SOLVES cap charges the single-removal costs as len(pool) solves however they were obtained, so the
    # evaluator (exact singles) and discovery (`single_bound`) abstain on the same rosters: a row discovery lists is never
    # one the evaluator refuses. `solves` itself stays the solves actually made (discovery's work budget counts them).
    charged = len(pool) if single_bound is not None and not negative else 0
    sets = 0
    # Players no engine values (they start only in IDP/P/HC slots) would always look free to drop: they are candidates
    # only when no other legal drop exists, and the choice is then flagged (`unvalued_drop`).
    unvalued = unvalued_ids(after, state)

    def search(cands: list[RosterPlayer]) -> tuple[list, int, list] | None:
        """The legal drop sets of the fewest players, as (lower bound, indexes into `cands`), and that count. Indexes
        follow `cands` (sorted by player id), so index order is id order."""
        nonlocal sets
        k = minimum_drops(after, before, state)
        frees = [0 if p.reserved else 1 for p in cands]
        bound = [single.get(p.espn_player_id, 0.0) for p in cands]
        where = [p.position for p in cands]
        while k <= len(cands):
            legal = []
            for combo in combinations(range(len(cands)), k):
                sets += 1
                if sets > MAX_DROP_SETS:
                    raise EvaluationUnavailable(
                        f"exact forced-drop resolution needs more than {MAX_DROP_SETS:,} candidate drop sets of {k} "
                        "players; evaluation unavailable (no approximate result is substituted)", solves)
                if act - sum(frees[i] for i in combo) > cap:
                    continue
                if limits:
                    gone = Counter(where[i] for i in combo)
                    if any(counts[pos] - gone[pos] > lim for pos, lim in limits.items()):
                        continue
                legal.append((sum(bound[i] for i in combo), combo))
            if legal:
                return legal, k, cands
            k += 1
        return None

    found = search([p for p in pool if p.espn_player_id not in unvalued]) if unvalued else None
    unvalued_drop = False
    if found is None:
        found = search(pool)
        unvalued_drop = bool(unvalued) and found is not None
    if found is None:
        raise TradeError("no legal roster exists after this trade")
    legal, k, cands = found
    arriving = [p.espn_player_id in incoming for p in cands]
    best: dict[bool, tuple] = {}
    for group in (False, True):
        # by lower bound, then player ids (index order is id order)
        items = sorted(x for x in legal if any(arriving[i] for i in x[1]) == group)
        best_total = None
        for lb, idx in items:
            if not negative and best_total is not None and lb > full - best_total + DROP_EPS:
                break
            solves += 1
            if solves + charged > MAX_DROP_SOLVES:
                raise EvaluationUnavailable(f"exact forced-drop resolution needs more than {MAX_DROP_SOLVES:,} lineup "
                                            "solves; evaluation unavailable (no approximate result is substituted)",
                                            solves - 1)         # this solve was refused, every earlier one was made
            combo = tuple(cands[i] for i in idx)
            t = total_without(combo)
            key = (round(t, 9), tuple(-p.espn_player_id for p in combo))
            if group not in best or key > best[group][0]:
                best[group] = (key, combo, t)
            best_total = t if best_total is None else max(best_total, t)
    return {"best": best, "full": full, "drops": k, "candidate_sets": sets, "solves": solves,
            "unvalued_drop": unvalued_drop}


def resolve_roster(state: LeagueState, window: Window, before: list[RosterPlayer], after: list[RosterPlayer],
                   incoming: set[int]) -> dict:
    """Forced drops for an over-capacity post-trade roster: the legal removal that maximises the
    remaining projected starting-lineup total (least costly in actual lineup terms, not the lowest
    value or raw projection), found exactly by choose_forced_drops. Incoming players are never the
    default drop; if dropping one would be strictly cheaper, the trade is flagged as bringing a
    redundant asset."""
    if legal_roster(after, state, before):
        return {"forced_drops": [], "lineup_cost": 0.0, "redundant_incoming_asset": None, "basis": None}
    # The search compares known totals only, on the totals-only solver (lineup_totals.LineupTotals: bit-identical to
    # known_total(project_team(...)), and a drop that leaves a week's lineup alone reuses that week's value instead of
    # a new solve). project_team runs for the two rosters this resolution reports on.
    T = LineupTotals(state, window.weeks, {p.espn_player_id for p in after},
                     {p.espn_player_id: p.position for p in after}, {p.espn_player_id: p.pro_team for p in after},
                     {p.espn_player_id: p.unavailable_weeks for p in after if p.unavailable_weeks}, exact_negative=True,
                     eligibility={p.espn_player_id: p.eligible_slots for p in after})
    known = T.removal_total(p.espn_player_id for p in after if not p.reserved)

    def total(players):
        return known(p.espn_player_id for p in players if not p.reserved)
    negative = any(v < 0 for p in after for w, v in state.projections.get(p.espn_player_id, {}).items()
                   if w in window.weeks)
    res = choose_forced_drops(after, before, state, incoming, total, negative, known.dropping)
    best = res["best"]
    full = project_team(after, state.slots, window, state.projections)
    chosen = best.get(False) or best[True]
    _, combo, _ = chosen
    team = project_team([p for p in after if p not in combo], state.slots, window, state.projections)
    unsupported = unsupported_of(state.slots)
    # The cost is exact when the projections are complete and, with unsupported slots, no dropped player could fill
    # one (team_lineups.delta_exact's rule); otherwise it is known-only.
    complete = all(w["_projections_complete"] for t in (full, team) for w in t["weeks"]) and (
        not unsupported or (separable(unsupported) and all(w["_offslot"] == v["_offslot"]
                                                            for w, v in zip(full["weeks"], team["weeks"]))))
    cost = known_total(full) - known_total(team)
    # A dropped player no engine values (only when no other legal drop exists): no cost at all, not a "0.0" (#253).
    unvalued = [p for p in combo if p.espn_player_id in unvalued_ids(after, state)]
    redundant = None
    if False in best and True in best and best[True][0][0] > best[False][0][0]:
        rc = best[True][1]
        redundant = {"player_ids": [p.espn_player_id for p in rc if p.espn_player_id in incoming],
                     "names": [p.name for p in rc if p.espn_player_id in incoming],
                     "lineup_points_saved_by_cutting_it": r(best[True][0][0] - best[False][0][0]),
                     "note": "redundant incoming asset: cutting a newly acquired player would be the cheapest legal drop"}
    return {
        "forced_drops": [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                          "is_incoming": p.espn_player_id in incoming,
                          **({"unsupported_slot_player": True} if p in unvalued else {})} for p in combo],
        "lineup_cost": r(cost) if complete and not unvalued else None,
        "known_lineup_cost": None if unvalued else r(cost),
        **({"unvalued_drop": {"status": UNVERIFIED, "code": "UNSUPPORTED_SLOT_PLAYER",
                              "note": unvalued_note([p.name or f"Player {p.espn_player_id}" for p in unvalued], state)
                              + " No other legal drop exists, so this choice is unverified."}} if unvalued else {}),
        "redundant_incoming_asset": redundant,
        "incoming_dropped_because_no_alternative": False not in best,
        "basis": ("not valued: the only legal drops include a player who can start only in "
                  f"{', '.join(unsupported)}" if unvalued else "complete ESPN projections" if complete else
                  f"known ESPN projections only: players who can start only in {', '.join(unsupported)} are not "
                  "valued" if unsupported and all(w["_projections_complete"] for t in (full, team) for w in t["weeks"])
                  else "known ESPN projections only (some missing)"),
        "search": {"drops": res["drops"], "candidate_sets": res["candidate_sets"], "lineup_solves": res["solves"],
                   "method": "exact (submodular lower bound)" if not negative else "exact (every legal set solved)"},
    }


# ---------------------------------------------------------------------- explanation --

def _slot_labels(assignment: list[dict]) -> dict[int, str]:
    """player_id -> slot label with an ordinal when the slot repeats (RB1, RB2, FLEX)."""
    totals = Counter(a["slot"] for a in assignment)
    seen: Counter = Counter()
    out = {}
    for a in assignment:
        seen[a["slot"]] += 1
        if a["player_id"] is not None:
            out[a["player_id"]] = f"{a['slot']}{seen[a['slot']]}" if totals[a["slot"]] > 1 else a["slot"]
    return out


def _usage(team: dict, pid: int) -> dict:
    weeks, slots = [], Counter()
    for w in team["weeks"]:
        lab = _slot_labels(w["assignment"]).get(pid)
        if lab:
            weeks.append(w["week"])
            slots[lab] += 1
    return {"weeks_started": weeks, "slots": dict(slots.most_common())}


def _fmt_usage(name: str, pos: str | None, u: dict, n: int) -> str:
    who = f"{name} ({pos})" if pos else name
    if not u["weeks_started"]:
        return f"{who} does not start in any of the {n} remaining weeks"
    slots = ", ".join(f"{s} x{c}" for s, c in u["slots"].items())
    if len(u["weeks_started"]) <= 2 and n > 2:
        return f"{who} starts only in week{'s' if len(u['weeks_started']) > 1 else ''} " \
               f"{', '.join(map(str, u['weeks_started']))} ({slots})"
    return f"{who} starts {len(u['weeks_started'])} of {n} weeks ({slots})"


def side_lineups(state: LeagueState, window: Window, team_id: int, sends: list[int], receives: list[RosterPlayer],
                 before_team: dict | None = None) -> dict:
    """One side of a trade, canonical numbers only: roster after the trade (incoming players active,
    forced drops resolved), optimal lineups before/after every week and their comparison. The trade
    evaluator and trade discovery both use this, so their deltas are the same computation.
    `before_team` (project_team of the current roster) may be passed in to avoid recomputing it."""
    roster = state.rosters[team_id]
    incoming = {p.espn_player_id for p in receives}
    before = list(roster)
    pre = [p for p in roster if p.espn_player_id not in set(sends)] + [arrives_active(p) for p in receives]
    res = resolve_roster(state, window, before, pre, incoming)
    dropped = {d["espn_player_id"] for d in res["forced_drops"]}
    after = [p for p in pre if p.espn_player_id not in dropped]
    bt = before_team or project_team(before, state.slots, window, state.projections)
    at = project_team(after, state.slots, window, state.projections)
    return {"before": before, "pre": pre, "after": after, "resolution": res, "before_team": bt, "after_team": at,
            "compare": compare(bt, at, window)}


def roster_issues(state: LeagueState, before: list[RosterPlayer], pre: list[RosterPlayer]) -> dict:
    """What the post-trade roster (before any forced drop) breaks: active capacity and ESPN position limits."""
    counts, prior = _position_counts(pre), _position_counts(before)
    cap = max(state.active_capacity, _active(before))
    return {"active_before_drops": _active(pre), "active_capacity": state.active_capacity,
            "over_capacity_by": max(0, _active(pre) - cap),
            "position_limits": [{"position": pos, "limit": lim, "count_before_drops": counts[pos],
                                 "over_by": counts[pos] - max(lim, prior[pos])}
                                for pos, lim in sorted(state.position_limits.items())
                                if counts[pos] > max(lim, prior[pos])]}


def _side(state: LeagueState, window: Window, team_id: int, sends: list[int], receives: list[RosterPlayer],
          names: dict[int, RosterPlayer]) -> dict:
    core = side_lineups(state, window, team_id, sends, receives)
    issues = roster_issues(state, core["before"], core["pre"])
    incoming = {p.espn_player_id for p in receives}
    before, after, res = core["before"], core["after"], core["resolution"]
    dropped = {d["espn_player_id"] for d in res["forced_drops"]}
    bt, at, cmp = core["before_team"], core["after_team"], core["compare"]
    n = len(window.weeks)

    weekly = []
    for row, b, a in zip(cmp["weeks"], bt["weeks"], at["weeks"]):
        sb, sa = starters(b), starters(a)
        weekly.append({**row, "entered": sorted(sa - sb), "exited": sorted(sb - sa)})

    incoming_usage = [{"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                       **_usage(at, p.espn_player_id)} for p in receives if p.espn_player_id not in dropped]
    outgoing_usage = [{"espn_player_id": pid, "name": names[pid].name, "position": names[pid].position,
                       **_usage(bt, pid)} for pid in sends]
    displaced = []
    for p in after:
        if p.espn_player_id in incoming:
            continue
        ub, ua = _usage(bt, p.espn_player_id), _usage(at, p.espn_player_id)
        lost = sorted(set(ub["weeks_started"]) - set(ua["weeks_started"]))
        gained = sorted(set(ua["weeks_started"]) - set(ub["weeks_started"]))
        if lost or gained:
            displaced.append({"espn_player_id": p.espn_player_id, "name": p.name, "position": p.position,
                              "starts_lost": lost, "starts_gained": gained})

    notes = [f"Receives {_fmt_usage(u['name'] or str(u['espn_player_id']), u['position'], u, n)}"
             for u in incoming_usage]
    notes += [f"Sends {_fmt_usage(u['name'] or str(u['espn_player_id']), u['position'], u, n)} (before the trade)"
              for u in outgoing_usage]
    for d in displaced:
        parts = ([f"loses {len(d['starts_lost'])}"] if d["starts_lost"] else []) + \
                ([f"gains {len(d['starts_gained'])}"] if d["starts_gained"] else [])
        notes.append(f"{d['name']} ({d['position']}) {' / '.join(parts)} start(s)")
    for d in res["forced_drops"]:
        notes.append(f"Must drop {d['name']} ({d['position']}) to stay within roster limits"
                     + (" (not valued: he starts only in a slot no engine fills)" if d.get("unsupported_slot_player")
                        else ""))

    legality = roster_legality(state, team_id)
    open_before = max(0, state.active_capacity - _active(before))
    open_after = max(0, state.active_capacity - _active(after))
    fill = None
    if open_after > open_before:
        fill = best_fa_addition(after, state.free_agents, state.slots, window, state.projections)
        fill["note"] = "best hypothetical FA fill for the opened roster spot; not included in the canonical delta"

    after_ids = {p.espn_player_id: p for p in after}
    ir_players = []
    for p in before:
        if not p.on_ir:
            continue
        stays = p.espn_player_id in after_ids and after_ids[p.espn_player_id].on_ir
        ir_players.append({
            "espn_player_id": p.espn_player_id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
            "injury_status": p.injury_status, "traded_away": p.espn_player_id in set(sends),
            "before_trade": ir_activation_sensitivity(before, p.espn_player_id, state.slots, window,
                                                      state.projections, None if legality else open_before, bt),
            "after_trade": ir_activation_sensitivity(after, p.espn_player_id, state.slots, window,
                                                     state.projections, None if legality else open_after, at)
            if stays else None,
        })

    first = res["forced_drops"][0] if res["forced_drops"] else None
    return {
        "team_id": team_id, "team_name": state.team_names.get(team_id),
        "sends": sends, "receives": sorted(incoming),
        "before": bt["summary"], "after": at["summary"],
        "deltas": {k: v for k, v in cmp.items() if k != "weeks"},
        "weekly": weekly,
        "lineups": {"before": public_weeks(bt), "after": public_weeks(at)},
        "roster": {
            # an unverified count (roster_legality: players missing from the data) is never shown as the roster's
            "active_capacity": state.active_capacity, "active_before": None if legality else _active(before),
            "active_after": None if legality else _active(after),
            # an unverified roster count (roster_legality) has no known open spot
            "open_active_roster_spots": None if legality else open_after,
            "open_active_roster_spots_before": None if legality else open_before,
            **({"legality": legality} if legality else {}),
            "forced_drops": res["forced_drops"],
            "issues_before_drops": {**issues, "active_before_drops": None, "over_capacity_by": None} if legality
            else issues,
            "forced_drop_search": res.get("search"),
            "forced_drop_id": first["espn_player_id"] if first else None,
            "forced_drop_name": first["name"] if first else None,
            "forced_drop_lineup_cost": res["lineup_cost"],
            "forced_drop_known_lineup_cost": res.get("known_lineup_cost", 0.0),
            "forced_drop_basis": res["basis"],
            **({"forced_drop_unverified": res["unvalued_drop"]} if res.get("unvalued_drop") else {}),
            "redundant_incoming_asset": res["redundant_incoming_asset"],
            "incoming_dropped_because_no_alternative": res.get("incoming_dropped_because_no_alternative", False),
        },
        "incoming_usage": incoming_usage, "outgoing_usage": outgoing_usage, "displaced": displaced,
        "fit_notes": notes,
        "hypothetical_fa_fill": fill,
        "ir_activation_sensitivity": {
            "label": "IR activation sensitivity (hypothetical free activation; not in the canonical delta)",
            "assumption": IR_HOLD_NOTE,
            "players": ir_players,
        },
        "incomplete": {"missing_weeks": sorted(set(bt["summary"]["missing_weeks"]) | set(at["summary"]["missing_weeks"])),
                       "missing_players": sorted(set(bt["summary"]["missing_players"]) | set(at["summary"]["missing_players"]))},
        "_before_team": bt,
    }


def result_label(a: float | None, b: float | None) -> str:
    """Zero is the dividing line; no arbitrary threshold."""
    if a is None or b is None:
        return "INCOMPLETE_DATA"
    sa = "GAIN" if a > 0 else "LOSS" if a < 0 else "UNCHANGED"
    sb = "GAIN" if b > 0 else "LOSS" if b < 0 else "UNCHANGED"
    if sa == sb == "GAIN":
        return "MUTUAL_PROJECTED_GAIN"
    if sa == sb == "LOSS":
        return "BOTH_LOSE"
    if sa == sb == "UNCHANGED":
        return "NO_PROJECTED_CHANGE"
    return f"TEAM_A_{sa}_TEAM_B_{sb}"


# ------------------------------------------------------------------ injury scenarios --

SCENARIO_NOTE = ("Hypothetical return scenarios for an injured traded player: the same exact lineup simulation with "
                 "only his availability changed (for both teams). No probability is attached to any of them, and none "
                 "is a forecast; the canonical row is the one the deltas above use.")


def _with_blocked(state: LeagueState, pid: int, blocked: frozenset) -> LeagueState:
    return replace(state, rosters={t: [replace(p, unavailable_weeks=blocked) if p.espn_player_id == pid else p for p in ps]
                                   for t, ps in state.rosters.items()})


def injury_scenarios(state: LeagueState, window: Window, team_a: int, team_b: int, a_sends: list[int],
                     b_sends: list[int]) -> list[dict]:
    """For each traded player whose NFL availability is restricted or unknown: both teams' deltas if he
    returns at his earliest eligible week (or, eligibility unknown, as ESPN publishes), if his return
    is delayed to the first fantasy playoff week (a reference point, not a forecast), and if he
    contributes nothing for the rest of the season."""
    out = []
    for pid in list(a_sends) + list(b_sends):
        rec = state.availability.get(pid)
        if not is_restricted(rec) or not window.weeks:
            continue
        p = next(x for ps in state.rosters.values() for x in ps if x.espn_player_id == pid)
        outside = frozenset(w for w in p.unavailable_weeks if w not in window.weeks)
        earliest = rec["earliest_eligible_week"]
        named = []
        if rec["status"] == NO_WEEK_LEFT:
            named.append(("no_return", None, "No contribution for the rest of the season (no eligible week remains)"))
        else:
            first = max(window.start, earliest) if earliest else window.start
            if rec["status"] in (INELIGIBLE, ELIGIBLE_UNCONFIRMED):
                named.append(("earliest_eligible_return", first, f"Returns at his earliest eligible week ({first}) "
                                                                  "and ESPN's projection holds from then"))
            else:
                named.append(("as_published", first, "Available now, as ESPN projects (eligibility unverified)"))
            po = (window.last_regular + 1) if window.last_regular is not None else None
            if po is not None and po in window.weeks and po > first:
                named.append(("delayed_to_playoffs", po, f"Hypothetical: return delayed to the first fantasy playoff "
                                                         f"week ({po})"))
            named.append(("no_return", None, "Hypothetical: no contribution for the rest of the season"))
        rows = []
        for kind, ret, text in named:
            blocked = frozenset(w for w in window.weeks if ret is None or w < ret) | outside
            st = _with_blocked(state, pid, blocked)
            by = {x.espn_player_id: x for t in (team_a, team_b) for x in st.rosters[t]}
            row = {"scenario": kind, "return_week": ret, "description": text,
                   "canonical": blocked == p.unavailable_weeks}
            try:
                for key, team, sends, gets in (("team_a", team_a, a_sends, b_sends), ("team_b", team_b, b_sends, a_sends)):
                    c = side_lineups(st, window, team, sends, [by[x] for x in gets])["compare"]
                    row[key] = {k: c[k] for k in ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta")}
            except TradeError as err:
                row["error"] = str(err)
            rows.append(row)
        out.append({"espn_player_id": pid, "name": p.name, "position": p.position, "status": rec["status"],
                    "earliest_eligible_week": earliest, "eligibility_basis": rec["eligibility_basis"],
                    "return_confirmed": rec["return_confirmed"], "scenarios": rows, "note": SCENARIO_NOTE})
    return out


# ------------------------------------------------------------------------ evaluator --

def weeks_label(a: int, b: int) -> str:
    return f"week {a}" if a == b else f"weeks {a}-{b}"


def validate(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int],
             max_per_side: int = MAX_PLAYERS_PER_SIDE) -> None:
    """Any player counts from 1 to `max_per_side` per side (no list of allowed shapes); roster
    capacity and position limits are enforced after the trade by forced drops."""
    if team_a == team_b:
        raise TradeError("teams must be different")
    for t in (team_a, team_b):
        if t not in state.rosters:
            raise TradeError(f"unknown team {t}")
    for side in (a_sends, b_sends):
        if not 1 <= len(side) <= max_per_side:
            raise TradeError(f"each side must send between 1 and {max_per_side} players "
                             f"(the manual analyzer's per-side limit)")
    ids = list(a_sends) + list(b_sends)
    if len(set(ids)) != len(ids):
        raise TradeError("a player appears more than once")
    for team, sends in ((team_a, a_sends), (team_b, b_sends)):
        own = {p.espn_player_id for p in state.rosters[team]}
        for pid in sends:
            if pid not in own:
                raise TradeError(f"player {pid} is not on team {team}'s current roster")


def require_remaining_weeks(state: LeagueState) -> None:
    """NoRemainingWeeksError when every fantasy scoring period has kicked off (nothing a trade could change)."""
    if state.start_week is None:
        reason = state.start_week_reason or {}
        raise NoRemainingWeeksError(
            "No remaining fantasy scoring periods: every week through the final scoring period "
            f"({state.final_week}) has kicked off, so a trade made now cannot change any lineup "
            f"({reason.get('reason') or 'no actionable week'}).")


def evaluation_window(state: LeagueState, start_week: int | None = None) -> Window:
    """The weeks a trade proposed now is evaluated over: from the earliest actionable week, or from an explicit
    `start_week` that is not before it (TradeError otherwise). The analyzer and the scenario comparison share it, so
    every comparison of trades uses one horizon."""
    require_remaining_weeks(state)
    if start_week is not None and not (1 <= start_week <= state.final_week):
        raise TradeError(f"start_week must be between 1 and {state.final_week}")
    if start_week is not None and start_week < state.start_week:
        raise TradeError(
            f"start_week {start_week} is before the earliest actionable week {state.start_week}: "
            f"{weeks_label(start_week, state.start_week - 1)} already kicked off, and gains there are not achievable "
            "by a trade made now (partial-week simulation is not supported)")
    if start_week is not None and state.first_projection_week and start_week < state.first_projection_week:
        raise TradeError(f"start_week must be >= {state.first_projection_week} (earliest loaded projection week)")
    return state.window(start_week)


def evaluate_trade(state: LeagueState, team_a: int, team_b: int, a_sends: list[int], b_sends: list[int],
                   start_week: int | None = None, max_per_side: int = MAX_PLAYERS_PER_SIDE,
                   scenarios: bool = True) -> dict:
    """Hypothetical before/after evaluation for both teams. Never mutates `state`."""
    a_sends, b_sends = [int(x) for x in a_sends], [int(x) for x in b_sends]
    reason = state.start_week_reason or {}
    require_remaining_weeks(state)
    validate(state, team_a, team_b, a_sends, b_sends, max_per_side)
    window = evaluation_window(state, start_week)
    uncertain = bool(reason.get("uncertain")) and window.start == state.start_week
    if start_week is None:
        why = reason
    elif start_week == state.start_week:
        why = {"reason": f"explicit start_week override ({start_week}) equal to the earliest actionable week"}
    else:
        why = {"reason": f"explicit start_week override ({start_week}): actionable "
                         f"{weeks_label(state.start_week, start_week - 1)} excluded"}
    names = {p.espn_player_id: p for t in (team_a, team_b) for p in state.rosters[t]}
    a_out = [names[pid] for pid in a_sends]
    b_out = [names[pid] for pid in b_sends]
    side_a = _side(state, window, team_a, a_sends, b_out, names)
    side_b = _side(state, window, team_b, b_sends, a_out, names)

    players = []
    for team, sends, side in ((team_a, a_sends, side_a), (team_b, b_sends, side_b)):
        roster = state.rosters[team]
        base = side.pop("_before_team")
        sens = {x["espn_player_id"]: x for x in side["ir_activation_sensitivity"]["players"]}
        for pid in sends:
            p = names[pid]
            players.append({
                "espn_player_id": pid, "name": p.name, "position": p.position, "pro_team": p.pro_team,
                "injury_status": p.injury_status, "on_ir": p.on_ir, **slot_flags(p), "from_team_id": team,
                "to_team_id": team_b if team == team_a else team_a,
                "weekly_projection": {w: state.projections.get(pid, {}).get(w) for w in window.weeks},
                "marginal_value_on_current_team": roster_marginal_value(
                    roster, pid, state.slots, window, state.projections, base,
                    side["roster"]["open_active_roster_spots_before"]),
                "ir_activation_sensitivity": sens.get(pid),
                "replacement_context": replacement_context(roster, pid, state.free_agents, state.slots, window,
                                                           state.projections, base),
                "availability": state.availability.get(pid),
            })

    da, db = side_a["deltas"], side_b["deltas"]
    a_ros, b_ros = da["ros_delta"], db["ros_delta"]

    def combo(key):
        x, y = da[key], db[key]
        return (r(x + y) if x is not None and y is not None else None,
                r(min(x, y)) if x is not None and y is not None else None)

    comb, min_side = combo("ros_delta")
    reg_c, reg_m = combo("regular_season_delta")
    po_c, po_m = combo("playoff_delta")
    return {
        "evaluation": {"start_week": window.start, "final_week": window.final,
                       "last_regular_season_week": window.last_regular, "weeks": list(window.weeks),
                       "start_week_overridden": start_week is not None,
                       "earliest_actionable_week": state.start_week, "remaining_weeks": True,
                       "uncertain": uncertain, "warning": reason.get("warning") if uncertain else None,
                       "start_week_reason": why},
        "trade": {"shape": f"{len(a_sends)}-for-{len(b_sends)}", "team_a_id": team_a, "team_b_id": team_b,
                  "team_a_sends": a_sends, "team_b_sends": b_sends,
                  "team_a_sends_count": len(a_sends), "team_b_sends_count": len(b_sends),
                  "max_players_per_side": max_per_side},
        "team_a": side_a, "team_b": side_b,
        "result": {"label": result_label(a_ros, b_ros), "team_a_ros_delta": a_ros, "team_b_ros_delta": b_ros,
                   "combined_ros_delta": comb, "min_side_ros_delta": min_side,
                   "combined_regular_season_delta": reg_c, "min_side_regular_season_delta": reg_m,
                   "combined_playoff_delta": po_c, "min_side_playoff_delta": po_m,
                   "regular_season_label": result_label(da["regular_season_delta"], db["regular_season_delta"]),
                   "playoff_label": result_label(da["playoff_delta"], db["playoff_delta"]),
                   "note": "Labels and deltas come straight from the exact lineup simulation on ESPN's baseline; "
                           "combined/min-side are descriptive ranking fields, not a score or acceptance probability."},
        "players": players,
        "ir_warning": IR_WARNING if any(side["ir_activation_sensitivity"]["players"] for side in (side_a, side_b))
        else None,
        "ir_assumption": IR_HOLD_NOTE,
        "injury_warnings": [{"espn_player_id": p["espn_player_id"], "name": p["name"], "status": p["availability"]["status"],
                             "warnings": p["availability"]["warnings"]}
                            for p in players if p["availability"] and p["availability"]["warnings"]],
        "injury_scenarios": injury_scenarios(state, window, team_a, team_b, a_sends, b_sends) if scenarios else [],
        "availability_note": AVAILABILITY_NOTE,
        "projection_source": source_label(state.projection_source, state.projections),
        "trade_impact_source": "exact optimal-lineup simulation",
        "kdst_streaming": stream_summary(state.projections, window),
        "assumptions": assumptions(state),
        "limitations": LIMITATIONS,
    }

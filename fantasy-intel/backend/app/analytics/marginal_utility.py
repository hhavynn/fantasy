"""Marginal roster utility (#216): the projected starting-lineup points a roster actually gains or loses from a change
(removing one or more of its players, optionally adding a replacement), as opposed to a player's gross market value.

Every number is a difference of two exact optimal lineups (team_lineups.project_team, the same weekly optimizer as the
trade evaluator and the team value page), week by week:

  * A joint change is evaluated jointly. Removing two players is one scenario, never the sum of two single removals;
    the two can differ (the second player may be the first one's backfill), and when more than one player is removed the
    response puts the removal delta next to the summed single deltas and says whether they differ (`nonadditive`,
    compared unrounded).
  * The replacement is explicit. `pool` says where it may come from: `bench` (only the players left on the roster
    move up), `waiver` (plus the best fully projected free agent in the pool the caller supplies, for one removed
    player) or `acquisition` (plus one named hypothetical player). No replacement is assumed when the pool is unknown:
    `free_agents=None` abstains (`unknown_pool`). An empty list is a known, empty pool. Free agents the pool cannot score
    in every week make the waiver result `partial_pool`: a better one may exist.
  * Each week lists who entered and who left the starting lineup (the replacement path; a K/D/ST waiver streamer
    appears under its team_lineups.STREAMER_IDS) and whether the week's delta is exact. A week with a missing projection
    on either side is unknown, never 0 (team_lineups.delta_exact), and so is every aggregate containing it.
  * A player in an IR or taxi slot starts no lineup, so removing him changes no lineup; the response says so instead of
    reporting a value. A verified NFL-ineligible week (RosterPlayer.unavailable_weeks) counts the way the optimizer
    counts it: he cannot start that week.
  * Not modelled: weekly lineup locks (every week is evaluated as if unlocked) and anything after the final fantasy week.

Pure and read-only: the caller supplies the roster, slots, window, projections (with their source label) and, for
`waiver`, the free agents."""

from __future__ import annotations

from .team_lineups import (
    STREAMER_IDS,
    Projections,
    RosterPlayer,
    Window,
    arrives_active,
    best_fa_addition,
    compare,
    project_team,
    r,
    replacement_positions,
    stream_summary,
    without,
)

POOLS = ("bench", "waiver", "acquisition")
UNIT = "projected starting-lineup points (exact optimal lineup with the change minus without it)"
NONADDITIVE_NOTE = ("Removing these players together is evaluated as one lineup. It can differ from the sum of removing "
                    "each one alone, because one player can be the other's backfill; only the joint delta is the "
                    "change to this roster.")
RESERVED_NOTE = "In an IR or taxi slot: he starts no lineup, so removing him changes no lineup."
WAIVER_NOTE = ("Waiver availability is not guaranteed: the free agent is the best fully projected one in the pool "
               "supplied.")
PARTIAL_POOL_REASON = ("Some free agents in the pool lack a projection for an evaluated week and could not be scored, so a "
                       "better replacement may exist.")
HORIZON_NOTE = "Evaluated weeks only: lineup locks and weeks after the final fantasy week are not modelled."
TOLERANCE = 1e-9


class MarginalUtilityError(ValueError):
    """An evaluation that cannot be specified (an unknown player, an unsupported pool)."""


def _lineup_ids(week: dict) -> set[int]:
    """Who starts this week: rostered players by id, a K/D/ST waiver streamer under its STREAMER_IDS id."""
    return {a["player_id"] if a["player_id"] is not None else STREAMER_IDS[a["position"]]
            for a in week["assignment"] if a["player_id"] is not None or a.get("streamer")}


def scenario(players: list[RosterPlayer], slots: list[str], window: Window, proj: Projections,
             remove=(), add: list[RosterPlayer] = (), base: dict | None = None) -> dict:
    """The exact change to this roster's optimal starting lineups from removing `remove` and adding `add` (acquired
    players arrive in an active spot), week by week, with the replacement path. `_ros_exact` is the unrounded total
    (None unless every week is exact), for comparisons."""
    base = base or project_team(players, slots, window, proj)
    after = project_team(without(players, remove) + [arrives_active(p) for p in add], slots, window, proj)
    c = compare(base, after, window)
    weeks = []
    for b, a, row in zip(base["weeks"], after["weeks"], c["weeks"]):
        before_ids, after_ids = _lineup_ids(b), _lineup_ids(a)
        weeks.append({**row, "entered_lineup": sorted(after_ids - before_ids),
                      "left_lineup": sorted(before_ids - after_ids)})
    exact = sum(a["_total"] - b["_total"] for b, a in zip(base["weeks"], after["weeks"]))
    return {"ros_delta": c["ros_delta"], "known_ros_delta": c["known_ros_delta"],
            "regular_season_delta": c["regular_season_delta"], "playoff_delta": c["playoff_delta"],
            "weeks_incomplete": c["weeks_incomplete"], "projection_complete": c["projection_complete"],
            **({"unsupported_slots": c["unsupported_slots"]} if c.get("unsupported_slots") else {}),
            "weeks": weeks, "_ros_exact": exact if c["projection_complete"] else None}


def _public(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


def marginal_utility(players: list[RosterPlayer], slots: list[str], window: Window, proj: Projections,
                     asset_ids, *, projection_source: str, pool: str = "bench",
                     free_agents: list[RosterPlayer] | None = None, acquisition: RosterPlayer | None = None) -> dict:
    """What removing `asset_ids` costs this roster in starting-lineup points, with the replacement `pool` allows.
    A negative delta is points lost. `projection_source` is the label of the projections used (team_lineups
    .source_label): a value is never returned without its source."""
    if not projection_source:
        raise MarginalUtilityError("Name the source of the projections.")
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in asset_ids):
        raise MarginalUtilityError("Player ids must be integers.")    # never a float truncated to another id
    ids = sorted(set(asset_ids))
    by_id = {p.espn_player_id: p for p in players}
    if not ids:
        raise MarginalUtilityError("Name at least one rostered player.")
    unknown = [x for x in ids if x not in by_id]
    if unknown:
        raise MarginalUtilityError(f"Not on this roster: {unknown}.")
    if pool not in POOLS:
        raise MarginalUtilityError(f"pool must be one of {', '.join(POOLS)}.")
    base = project_team(players, slots, window, proj)
    out = {
        "asset_ids": ids, "pool": pool, "unit": UNIT, "projection_source": projection_source,
        "streaming": stream_summary(proj, window),
        "horizon": {"weeks": list(window.weeks), "start_week": window.start, "final_week": window.final,
                    "last_regular_season_week": window.last_regular, "note": HORIZON_NOTE},
        "baseline": base["summary"],
        "reserved": [x for x in ids if by_id[x].reserved],
        **({"reserved_note": RESERVED_NOTE} if any(by_id[x].reserved for x in ids) else {}),
        "replacement": None, "status": "evaluated", "reason": None,
    }
    removal = scenario(players, slots, window, proj, remove=ids, base=base)
    out["removal"] = _public(removal)               # the loss before any replacement
    add: list[RosterPlayer] = []
    if pool == "waiver":
        if len(ids) != 1:
            raise MarginalUtilityError("A waiver replacement is evaluated for one removed player at a time.")
        if free_agents is None:
            return {**out, "status": "unknown_pool", "change": None,
                    "reason": "No free-agent pool was supplied, so no waiver replacement is assumed."}
        me = by_id[ids[0]]
        pool_fas = [p for p in free_agents if p.espn_player_id not in by_id]     # never a rostered (or the removed) player
        fa = best_fa_addition(without(players, ids), pool_fas, slots, window, proj,
                              replacement_positions(me.position, slots)) if not me.reserved else {"player": None}
        if fa["player"] is not None:
            add = [next(p for p in pool_fas if p.espn_player_id == fa["player"]["espn_player_id"])]
            out["replacement"] = {**fa["player"], "kind": "waiver", "note": WAIVER_NOTE}
        out["waiver_candidates"] = {"scored": fa.get("candidates", 0),
                                    "excluded_unprojected": fa.get("excluded_unprojected", 0),
                                    "excluded_rostered": len(free_agents) - len(pool_fas)}
        if fa.get("excluded_unprojected"):
            out["status"], out["reason"] = "partial_pool", PARTIAL_POOL_REASON
    elif pool == "acquisition":
        if acquisition is None:
            raise MarginalUtilityError("An acquisition evaluation needs the hypothetical player.")
        if acquisition.espn_player_id in by_id:
            raise MarginalUtilityError("The acquired player is already on this roster.")
        add = [acquisition]
        out["replacement"] = {"espn_player_id": acquisition.espn_player_id, "name": acquisition.name,
                              "position": acquisition.position, "pro_team": acquisition.pro_team,
                              "kind": "hypothetical acquisition"}
    change = removal if not add else scenario(players, slots, window, proj, remove=ids, add=add, base=base)
    out["change"] = _public(change)                 # the change to this roster, replacement included
    if len(ids) > 1:
        singles = {x: scenario(players, slots, window, proj, remove=[x], base=base) for x in ids}
        exact = [s["_ros_exact"] for s in singles.values()]
        summed = sum(exact) if all(v is not None for v in exact) else None
        out["single_deltas"] = {str(x): s["ros_delta"] for x, s in singles.items()}
        out["sum_of_single_deltas"] = r(summed) if summed is not None else None
        joint = removal["_ros_exact"]
        out["nonadditive"] = None if summed is None or joint is None else abs(joint - summed) > TOLERANCE
        out["nonadditive_note"] = NONADDITIVE_NOTE
    return out

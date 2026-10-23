"""Marginal roster utility (#216): the projected starting-lineup points a roster actually gains or loses from a change
(removing one or more of its players, optionally adding a replacement), as opposed to a player's gross market value.

Every number is a difference of two exact optimal lineups (team_lineups.project_team, the same weekly optimizer as the
trade evaluator and the team value page), week by week:

  * A joint change is evaluated jointly. Removing two players is one scenario, never the sum of two single removals;
    the two can differ (the second player may be the first one's backfill), and when more than one player is removed the
    response puts the joint delta next to the summed single deltas and says whether they differ (`nonadditive`).
  * The replacement is explicit. `pool` says where it may come from: `bench` (only the players left on the roster
    move up), `waiver` (plus the best accessible free agent the caller supplies, for a single removed player) or
    `acquisition` (plus one named hypothetical player). No replacement is assumed when the pool is unknown: a waiver
    evaluation without a supplied free-agent pool abstains.
  * Each week lists who entered and who left the starting lineup (the replacement path; K/D/ST streamers appear under
    their team_lineups.STREAMER_IDS) and whether the week's delta is exact. A week with a missing projection on either
    side is unknown, never 0 (team_lineups.delta_exact), and so is every aggregate containing it.
  * A player in an IR or taxi slot starts no lineup, so removing him changes no lineup; the response says so instead of
    reporting a value.

Pure and read-only: the caller supplies the roster, slots, window, projections and (for `waiver`) the free agents."""

from __future__ import annotations

from .team_lineups import (Projections, RosterPlayer, Window, arrives_active, best_fa_addition, compare, project_team,
                           r, replacement_positions, starters, without)

POOLS = ("bench", "waiver", "acquisition")
UNIT = "projected starting-lineup points (exact optimal lineup with the change minus without it)"
NONADDITIVE_NOTE = ("Removing these players together is evaluated as one lineup. It can differ from the sum of removing "
                    "each one alone, because one player can be the other's backfill; only the joint delta is the "
                    "change to this roster.")
RESERVED_NOTE = "In an IR or taxi slot: he starts no lineup, so removing him changes no lineup."
WAIVER_NOTE = "Waiver availability is not guaranteed: the free agent is the best one in the pool supplied."
TOLERANCE = 1e-9


class MarginalUtilityError(ValueError):
    """An evaluation that cannot be specified (an unknown player, an unsupported pool)."""


def scenario(players: list[RosterPlayer], slots: list[str], window: Window, proj: Projections,
             remove=(), add: list[RosterPlayer] = (), base: dict | None = None) -> dict:
    """The exact change to this roster's optimal starting lineups from removing `remove` and adding `add` (acquired
    players arrive in an active spot), week by week, with the replacement path."""
    base = base or project_team(players, slots, window, proj)
    after = project_team(without(players, remove) + [arrives_active(p) for p in add], slots, window, proj)
    c = compare(base, after, window)
    weeks = []
    for b, a, row in zip(base["weeks"], after["weeks"], c["weeks"]):
        before_ids, after_ids = starters(b), starters(a)
        weeks.append({**row, "entered_lineup": sorted(after_ids - before_ids),
                      "left_lineup": sorted(before_ids - after_ids)})
    return {"ros_delta": c["ros_delta"], "known_ros_delta": c["known_ros_delta"],
            "regular_season_delta": c["regular_season_delta"], "playoff_delta": c["playoff_delta"],
            "weeks_incomplete": c["weeks_incomplete"], "projection_complete": c["projection_complete"],
            **({"unsupported_slots": c["unsupported_slots"]} if c.get("unsupported_slots") else {}),
            "weeks": weeks}


def marginal_utility(players: list[RosterPlayer], slots: list[str], window: Window, proj: Projections,
                     asset_ids, pool: str = "bench", free_agents: list[RosterPlayer] | None = None,
                     acquisition: RosterPlayer | None = None, projection_source: str | None = None) -> dict:
    """What removing `asset_ids` costs this roster in starting-lineup points, with the replacement `pool` allows.
    A negative delta is points lost. `projection_source` is the label of the projections used (team_lineups
    .source_label), carried into the response so a value is never shown without its source."""
    ids = sorted({int(x) for x in asset_ids})
    by_id = {p.espn_player_id: p for p in players}
    if not ids:
        raise MarginalUtilityError("Name at least one rostered player.")
    unknown = [x for x in ids if x not in by_id]
    if unknown:
        raise MarginalUtilityError(f"Not on this roster: {unknown}.")
    if pool not in POOLS:
        raise MarginalUtilityError(f"pool must be one of {', '.join(POOLS)}.")
    out = {
        "asset_ids": ids, "pool": pool, "unit": UNIT, "projection_source": projection_source,
        "horizon": {"weeks": list(window.weeks), "start_week": window.start, "final_week": window.final,
                    "last_regular_season_week": window.last_regular},
        "reserved": [x for x in ids if by_id[x].reserved],
        **({"reserved_note": RESERVED_NOTE} if any(by_id[x].reserved for x in ids) else {}),
        "replacement": None, "status": "evaluated", "reason": None,
    }
    base = project_team(players, slots, window, proj)
    add: list[RosterPlayer] = []
    if pool == "waiver":
        if len(ids) != 1:
            raise MarginalUtilityError("A waiver replacement is evaluated for one removed player at a time.")
        if free_agents is None:
            return {**out, "status": "unknown_pool", "joint": None,
                    "reason": "No free-agent pool was supplied, so no waiver replacement is assumed."}
        me = by_id[ids[0]]
        fa = best_fa_addition(without(players, ids), free_agents, slots, window, proj,
                              replacement_positions(me.position, slots)) if not me.reserved else {"player": None}
        if fa["player"] is not None:
            add = [next(p for p in free_agents if p.espn_player_id == fa["player"]["espn_player_id"])]
            out["replacement"] = {**fa["player"], "kind": "waiver", "note": WAIVER_NOTE}
        out["waiver_candidates"] = {"scored": fa.get("candidates", 0),
                                    "excluded_unprojected": fa.get("excluded_unprojected", 0)}
    elif pool == "acquisition":
        if acquisition is None:
            raise MarginalUtilityError("An acquisition evaluation needs the hypothetical player.")
        if acquisition.espn_player_id in by_id:
            raise MarginalUtilityError("The acquired player is already on this roster.")
        add = [acquisition]
        out["replacement"] = {"espn_player_id": acquisition.espn_player_id, "name": acquisition.name,
                              "position": acquisition.position, "pro_team": acquisition.pro_team,
                              "kind": "hypothetical acquisition"}
    out["joint"] = scenario(players, slots, window, proj, remove=ids, add=add, base=base)
    if len(ids) > 1:
        # removals only on both sides: an acquisition is never part of the comparison
        removal = out["joint"] if not add else scenario(players, slots, window, proj, remove=ids, base=base)
        singles = {x: scenario(players, slots, window, proj, remove=[x], base=base) for x in ids}
        known = [s["ros_delta"] for s in singles.values()]
        summed = r(sum(known)) if all(v is not None for v in known) else None
        joint = removal["ros_delta"]
        out["joint_removal_delta"] = joint
        out["single_deltas"] = {str(x): s["ros_delta"] for x, s in singles.items()}
        out["sum_of_single_deltas"] = summed
        out["nonadditive"] = None if summed is None or joint is None else abs(joint - summed) > TOLERANCE
        out["nonadditive_note"] = NONADDITIVE_NOTE
    return out

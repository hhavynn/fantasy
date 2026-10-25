"""Read-only My Team Value endpoints: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.injury_impact import InjuryImpactUnavailable
from ..analytics.marginal_utility import MarginalUtilityError, marginal_utility
from ..analytics.opportunity_tree import build_opportunity_tree
from ..analytics.stash_planner import HORIZONS, StashPlanError, contingencies, plan_stashes
from ..analytics.playoff_race import NotSynced, load_race_data
from ..analytics.team_lineups import STREAMER_IDS, STREAMER_NAMES, replacement_positions, source_label, without
from ..analytics.team_value import NotFound, _position_legal, build_team_value, load_team_value_inputs
from ..analytics.trades import roster_legality, with_streaming
from .deps import db
from .signal_provenance import trade_provenance

router = APIRouter(prefix="/api", tags=["team value"])

MAX_ASSETS = 3              # players removed together in one roster-utility question
# Free agents a waiver replacement may come from: those whose provider availability is known (ESPN's synced status;
# Sleeper infers FREEAGENT from not being rostered). The league state already loads only these, so this is a defensive
# filter: one of unknown availability is never assumed addable (#216: no free replacement when eligibility is unknown).
ADDABLE = ("FREEAGENT", "WAIVERS")
ROUTE_POOLS = ("bench", "waiver")    # a hypothetical acquisition is the trade analyzer's question, not this page's


@router.get("/my-team/value")
def my_team_value(team_id: int | None = None, trades: bool = True, kdst_streaming: bool = True):
    """Roster strength vs the league, player marginal values, depth, bench usage, upgrades and the
    playoff-race context for one team (default: yours). `trades=false` skips the trade preview;
    `kdst_streaming=false` holds each team's current K and D/ST all season instead of streaming them."""
    with db() as con:
        try:
            race, extras = load_team_value_inputs(con)
            return build_team_value(race, extras, team_id=team_id, trades=trades, kdst_streaming=kdst_streaming)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))


@router.get("/my-team/value/marginal")
def my_team_marginal(ids: list[int] = Query(..., max_length=MAX_ASSETS), team_id: int | None = None,
                     pool: str = "bench", kdst_streaming: bool = True):
    """Marginal roster utility (#216, analytics/marginal_utility.py): what removing one to three of a team's players
    (default team: yours) changes in its exact optimal starting lineups, week by week, with the replacement `pool`
    allows (`bench`: the rest of the roster; `waiver`: the best fully projected free agent whose availability is synced,
    for one player). Joint removals are evaluated jointly, never summed. Read-only."""
    if pool not in ROUTE_POOLS:
        raise HTTPException(422, "pool must be bench or waiver")
    with db() as con:
        try:
            race = load_race_data(con)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        state = with_streaming(race.state) if kdst_streaming else race.state
        teams = {t["team_id"]: t for t in race.teams}
        tid = team_id if team_id is not None else next((t["team_id"] for t in race.teams if t["is_mine"]), None)
        if tid is None:
            raise HTTPException(404, "No team is marked as yours (is_mine); pass team_id.")
        if tid not in teams:
            raise HTTPException(404, f"Unknown team {tid}")
        window = state.window()
        if not window.weeks:
            raise HTTPException(409, "No remaining fantasy scoring periods: there is no lineup left to change.")
        roster = state.rosters.get(tid) or []
        free_agents = None
        availability = None
        if pool == "waiver" and state.free_agents:
            known = [p for p in state.free_agents if state.free_agent_status.get(p.espn_player_id) in ADDABLE]
            # A legal replacement only: adding him keeps every position within the league's roster limit once the
            # removed players are gone (the rule the page's own free-agent suggestions use).
            legal = _position_legal(known, without(roster, ids), roster, state)
            # Counted only among free agents who could fill one of the removed player's slots at all.
            me = next((p for p in roster if p.espn_player_id in set(ids)), None)
            fits = replacement_positions(me.position, state.slots) if me is not None else set()
            kept = {p.espn_player_id for p in legal}
            availability = {"free_agents": len(state.free_agents), "availability_known": len(known),
                            "excluded_unknown_availability": len(state.free_agents) - len(known),
                            "excluded_position_limit": sum(1 for p in known if p.espn_player_id not in kept
                                                           and p.position in fits)}
            free_agents = legal if known else None   # none with known availability: the pool is unknown, not empty
        try:
            res = marginal_utility(roster, state.slots, window, state.projections, ids,
                                   projection_source=source_label(state.projection_source, state.projections),
                                   pool=pool, free_agents=free_agents)
        except MarginalUtilityError as err:
            raise HTTPException(422, str(err))
        if availability is not None:
            res["waiver_availability"] = availability
        everyone = {p.espn_player_id: p for p in [*roster, *state.free_agents]}
        shown = set(res["asset_ids"]) | ({res["replacement"]["espn_player_id"]} if res["replacement"] else set())
        for block in (res["removal"], res["change"]):
            for w in (block or {}).get("weeks", []):
                shown |= set(w["entered_lineup"]) | set(w["left_lineup"])
        streamers = {v: k for k, v in STREAMER_IDS.items()}
        names = {pid: ({"name": STREAMER_NAMES[streamers[pid]], "position": streamers[pid], "pro_team": None}
                       if pid in streamers else {"name": everyone[pid].name, "position": everyone[pid].position,
                                                  "pro_team": everyone[pid].pro_team})
                 for pid in sorted(shown) if pid in streamers or pid in everyone}
        return {**res, "contract": "roster-utility/v1", "pools": list(ROUTE_POOLS),
                "team": {k: teams[tid][k] for k in ("team_id", "name", "is_mine")}, "names": names,
                "provenance": trade_provenance(con, race.league, state)}


@router.get("/my-team/stash")
def my_team_stash(team_id: int | None = None, horizon: int = 4, kdst_streaming: bool = True):
    """Multi-week stash planner (#218, analytics/stash_planner.py) for one team (default: yours): free agents that add
    projected starting points to this roster only in later weeks (exact lineup differences with the drop candidate
    removed), kept apart from current-week pickups, plus "if a starter is out" contingencies from the injury
    opportunity tree with their samples. No probabilities. Read-only."""
    if horizon not in HORIZONS:
        raise HTTPException(422, f"horizon must be one of {', '.join(map(str, HORIZONS))}")
    with db() as con:
        try:
            race = load_race_data(con)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        state = with_streaming(race.state) if kdst_streaming else race.state
        teams = {t["team_id"]: t for t in race.teams}
        tid = team_id if team_id is not None else next((t["team_id"] for t in race.teams if t["is_mine"]), None)
        if tid is None:
            raise HTTPException(404, "No team is marked as yours (is_mine); pass team_id.")
        if tid not in teams:
            raise HTTPException(404, f"Unknown team {tid}")
        try:
            plan = plan_stashes(state.rosters.get(tid) or [], state.slots, state.window(), state.projections,
                                free_agents=state.free_agents, free_agent_status=state.free_agent_status,
                                active_capacity=state.active_capacity, position_limits=state.position_limits,
                                horizon=horizon,
                                projection_source=source_label(state.projection_source, state.projections),
                                roster_count_unverified=(roster_legality(state, tid) or {}).get("reason"),
                                undroppable_list_in_use=state.undroppable_list_in_use)
        except StashPlanError as err:
            raise HTTPException(422, str(err))
        try:                        # tid is already in this league, so only "not available yet" is expected here
            tree = build_opportunity_tree(con, team_id=tid)
        except InjuryImpactUnavailable as err:
            contingency = {"available": False, "reason": str(err), "items": []}
        else:
            contingency = {"available": True, "week": tree.get("week"), **contingencies(tree)}
        return {**plan, "contract": "stash-plan/v1",
                "team": {k: teams[tid][k] for k in ("team_id", "name", "is_mine")},
                "contingencies": contingency, "provenance": trade_provenance(con, race.league, state)}

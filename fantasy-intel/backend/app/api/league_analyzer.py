"""Read-only League analyzer endpoint (issue #191): no writes, no syncs, no provider calls.

A legacy router mounted again under /v2/leagues/{league_id}/api/ (app/api/league_scope.py), so a multi-league request is
authorized for its league before anything here runs (an unknown or inaccessible league is 404 before any parameter is
validated) and reads that league's file only. Team ids are checked against that league's own teams: an id from another
league is simply an unknown team (404)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException

from ..analytics.league_analyzer import (
    SameTeam,
    TeamNotFound,
    build_league_analyzer,
    check_teams,
    load_compatibility,
    load_side_inputs,
)
from ..analytics.playoff_race import NotSynced
from ..analytics.team_value import NotFound, load_team_value_inputs
from .deps import db

router = APIRouter(prefix="/api", tags=["league analyzer"])
ERROR_HEADER = "X-FI-Error-Code"


@router.get("/league-analyzer")
def league_analyzer(team_id: int | None = None, vs: int | None = None, horizon: Literal["ros", "week"] = "ros",
                    kdst_streaming: bool = True):
    """Every roster's projected starters by slot group, normalized as points above replacement per starting slot and a
    league-relative score with ranks; unknown positions stay unknown. `team_id` is the focus team (default: yours);
    `vs` adds a two-team comparison (starters, bench, bye conflicts, positional needs); `horizon` is the rest of the
    season or this week; `kdst_streaming=false` holds each team's K and D/ST (contract league-analyzer/v1)."""
    with db() as con:
        try:
            race, extras = load_team_value_inputs(con)
            focus, other = check_teams(race, team_id, vs)
            side = load_side_inputs(con, [focus, other]) if other is not None else None
            compat = load_compatibility(con)
            return build_league_analyzer(race, extras, team_id=focus, vs=other, horizon=horizon,
                                         kdst_streaming=kdst_streaming, side=side, compatibility=compat)
        # Every answer of this route says which case it is in X-FI-Error-Code; `detail` stays the readable sentence. These
        # run inside league_scope, so a caller who may not see the league only ever gets its plain 404.
        except NotSynced as err:
            raise HTTPException(503, str(err), headers={ERROR_HEADER: "not_synced"})
        except TeamNotFound as err:
            raise HTTPException(404, str(err), headers={ERROR_HEADER: err.code})
        except NotFound as err:
            raise HTTPException(404, str(err), headers={ERROR_HEADER: "not_found"})
        except SameTeam as err:
            raise HTTPException(422, str(err), headers={ERROR_HEADER: err.code})

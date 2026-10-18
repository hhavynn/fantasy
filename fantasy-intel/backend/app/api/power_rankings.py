"""Read-only League Power Rankings endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.power_rankings import NotFound, NotSynced, build_power_rankings, load_power_inputs
from .deps import db

router = APIRouter(prefix="/api", tags=["power rankings"])


@router.get("/power-rankings")
def power_rankings(team_id: int | None = None, kdst_streaming: bool = True):
    """Every roster in the league ranked on lineup strength, depth and injury cover, replacement options and
    all-play, each scaled within the league, with week-over-week movement recomputed point-in-time.
    `team_id` only picks the focus team (default: yours); `kdst_streaming=false` holds each team's K and D/ST."""
    with db() as con:
        try:
            race, previous, extras = load_power_inputs(con)
            return build_power_rankings(race, previous, extras, team_id=team_id, kdst_streaming=kdst_streaming)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))

"""Read-only My Team Value endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.playoff_race import NotSynced
from ..analytics.team_value import NotFound, build_team_value, load_team_value_inputs
from .deps import db

router = APIRouter(prefix="/api", tags=["team value"])


@router.get("/my-team/value")
def my_team_value(team_id: int | None = None, trades: bool = True):
    """Roster strength vs the league, player marginal values, depth, bench usage, upgrades and the
    playoff-race context for one team (default: yours). `trades=false` skips the trade preview."""
    with db() as con:
        try:
            race, extras = load_team_value_inputs(con)
            return build_team_value(race, extras, team_id=team_id, trades=trades)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))

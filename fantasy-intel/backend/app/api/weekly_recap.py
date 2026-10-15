"""Read-only Weekly League Recap endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.weekly_recap import NotFound, NotSynced, build_weekly_recap, load_recap_data
from .deps import db

router = APIRouter(prefix="/api", tags=["weekly recap"])


@router.get("/weekly-recap")
def weekly_recap(week: int | None = Query(None, ge=1, description="Matchup period; default the latest completed")):
    """Scoreboard, awards, standout player scores, league activity and standings through one completed
    matchup period. Only matchups with an ESPN winner (home, away or tie) count."""
    with db() as con:
        try:
            return build_weekly_recap(load_recap_data(con, week))
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))

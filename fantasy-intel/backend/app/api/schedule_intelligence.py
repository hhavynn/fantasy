"""Read-only schedule intelligence endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.schedule_intelligence import NotFound, NotSynced, build_schedule_intelligence
from .deps import db

router = APIRouter(prefix="/api", tags=["schedule intelligence"])


@router.get("/schedule-intelligence")
def schedule_intelligence(team_id: int | None = None,
                          position: str = Query("all", description="all | QB | RB | WR | TE"),
                          phase: str = Query("all", description="all | regular | playoffs"),
                          player_id: int | None = None):
    """Each remaining week's opponent and what that defense allowed in games completed before the week."""
    with db() as con:
        try:
            return build_schedule_intelligence(con, team_id=team_id, position=position.upper()
                                               if position.lower() != "all" else "all",
                                               phase=phase.lower(), player_id=player_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))

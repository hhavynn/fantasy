"""Read-only Weekly Start/Sit Command Center API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.start_sit import load_start_sit_evaluation
from .deps import db

router = APIRouter(prefix="/api", tags=["start-sit"])


@router.get("/start-sit")
def get_start_sit(
    team_id: int | None = Query(None, description="Fantasy team ID (defaults to manager's team)"),
    week: int | None = Query(None, description="Fantasy scoring period / NFL week (defaults to current week)"),
    source: str = Query("espn", description="Projection source: espn (default) | independent"),
):
    """Compare manager's actual ESPN lineup against optimal lock-constrained lineup."""
    with db() as con:
        try:
            return load_start_sit_evaluation(con, team_id=team_id, week=week, source=source)
        except LookupError as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            msg = str(err)
            if "outside valid fantasy season" in msg or msg.startswith("source must be"):
                raise HTTPException(422, msg)
            raise HTTPException(503, msg)

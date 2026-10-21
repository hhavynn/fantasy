"""Read-only weekly projection movers.

GET /api/projections/movers[?source=espn|independent&view=ros|week&position=QB&baseline=<id>&limit=15&season=2026]

Who moved between two stored forecasts and by how much (app.analytics.projection_movers), with the data recorded over
the same period next to each listed mover (app.analytics.mover_evidence). ESPN unless source=independent. Nothing is
computed from outcomes, trained or written.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import mover_evidence
from ..analytics import projection_movers as pm
from ..config import get_settings
from .deps import db

router = APIRouter(prefix="/api", tags=["projection movers"])


@router.get("/projections/movers")
def projection_movers(source: str | None = None, view: str = "ros", position: str | None = None,
                      baseline: str | None = None, limit: int = Query(15, ge=1, le=pm.MAX_LIMIT),
                      season: int | None = None):
    season = season or get_settings().nfl_season
    with db() as con:
        try:
            result = pm.movement(con, season, source=source, view=view, position=position, baseline=baseline,
                                 limit=limit)
        except ValueError as err:
            raise HTTPException(422, str(err))
        return mover_evidence.attach(con, season, result)

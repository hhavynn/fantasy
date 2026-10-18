"""Read-only Injury Impact Center API router (docs/injury_impact.md)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.injury_impact import InjuryImpactUnavailable, build_injury_impact
from .deps import db

router = APIRouter(prefix="/api", tags=["injury-impact"])


@router.get("/injury-impact")
def get_injury_impact(
    position: str = Query("ALL", description="ALL | QB | RB | WR | TE"),
    status: str = Query("all", description="all | out (out, doubtful, IR, suspended) | questionable"),
    team_id: int | None = Query(None, description="Fantasy team whose roster counts as yours (default: the owner's)"),
):
    """Current-week QB/RB/WR/TE absences and questionable statuses with each player's observed workload, the
    teammates whose opportunity depends on it (observed with/without splits and a labelled pro-rata estimate),
    the team's game, and injury-data freshness."""
    with db() as con:
        try:
            return build_injury_impact(con, position=position, status=status, team_id=team_id)
        except ValueError as err:
            raise HTTPException(422, str(err))
        except LookupError as err:
            raise HTTPException(404, str(err))
        except InjuryImpactUnavailable as err:
            raise HTTPException(503, str(err))

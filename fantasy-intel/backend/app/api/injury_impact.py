"""Read-only Injury Impact Center API router (docs/injury_impact.md, docs/opportunity_tree.md,
docs/availability_timeline.md)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.availability_timeline import AvailabilityTimelineUnavailable, build_availability_timeline
from ..analytics.injury_impact import InjuryImpactUnavailable, build_injury_impact
from ..analytics.opportunity_tree import build_opportunity_tree
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


@router.get("/injury-impact/opportunity-tree")
def get_opportunity_tree(
    team_id: int | None = Query(None, description="Fantasy team whose roster counts as yours (default: the owner's)"),
):
    """Per NFL team with an injured or returning starter: the position group's depth chart (depth rank, availability,
    recent snap / route / touch share, personnel changes) and one scenario per starter with its sample, the teammates
    the data supports (observed) or a labelled hypothesis, or an abstention, and league-specific ownership."""
    with db() as con:
        try:
            return build_opportunity_tree(con, team_id=team_id)
        except LookupError as err:
            raise HTTPException(404, str(err))
        except InjuryImpactUnavailable as err:
            raise HTTPException(503, str(err))


@router.get("/injury-impact/availability-timeline")
def get_availability_timeline(
    team_id: int | None = Query(None, description="Fantasy team whose roster to follow (default: the owner's)"),
):
    """The selected team's roster, each player with his availability events in order (NFL injury report and weekly
    roster status, the provider's status, owner-curated news), each citing its source and time, the level the lineup
    tools read, practice participation as published (unknown when not), and this week's changes."""
    with db() as con:
        try:
            return build_availability_timeline(con, team_id=team_id)
        except LookupError as err:
            raise HTTPException(404, str(err))
        except AvailabilityTimelineUnavailable as err:
            raise HTTPException(503, str(err))

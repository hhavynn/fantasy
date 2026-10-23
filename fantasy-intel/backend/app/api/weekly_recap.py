"""Read-only Weekly League Recap endpoints: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..analytics import decision_feedback as dfb
from ..analytics.weekly_recap import NotFound, NotSynced, build_weekly_recap, load_recap_data
from .deps import db
from .leagues_v2 import NOT_FOUND

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


@router.get("/weekly-recap/feedback")
def weekly_recap_feedback(request: Request,
                          week: int | None = Query(None, ge=1, description="Matchup period; default the latest completed"),
                          source: str | None = Query(None, description="espn | independent; default the league's own")):
    """Decision feedback for the viewer's OWN team (app/analytics/decision_feedback.py): the start/sit calls implied by
    inputs archived before kickoff, graded against the week's outcomes, plus the team's season to date.

    The route takes no team. It reads fantasy_teams.is_mine: the signed-in viewer's saved team on /v2 (the owner's
    team on the laptop). A request naming any team_id gets the same 404 as an unknown league: on /v2 a URL team_id
    becomes the perspective of a viewer without a saved team, so accepting it would let anyone read another team's
    feedback (and share its cached answer)."""
    if "team_id" in request.query_params:
        raise HTTPException(404, NOT_FOUND)
    if source is not None and source.lower() not in dfb.SOURCES:
        raise HTTPException(422, f"source must be one of {', '.join(dfb.SOURCES)}")
    with db() as con:
        try:
            return dfb.build_feedback(dfb.load_feedback_data(con, week, source))
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))

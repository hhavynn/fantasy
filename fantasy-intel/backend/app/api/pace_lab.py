"""Read-only pace and game-script lab (issue #211): no writes, no syncs, no provider calls, no betting lines. Global
nflverse aggregates for every NFL offense; no league data is read. Mounted like the Research lab: /api/pace-lab on the
classic site and, owner-only, under /v2/leagues/{league_id}/api/pace-lab, where the league and platform-admin checks
run before any parameter is validated or any file is read."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ..analytics.pace_lab import MAX_REG_WEEK, MAX_SPAN, MIN_REG_WEEK, PaceLabError, PaceQuery, build_pace_lab
from .deps import db

router = APIRouter(prefix="/api/pace-lab", tags=["research"])

TEAM = r"^[A-Za-z]{2,3}$"


@router.get("")
def pace_lab(
    season: int | None = Query(None, ge=1999, le=2100, description="Last season pooled; default: the newest stored"),
    seasons: int = Query(1, ge=1, le=MAX_SPAN, description="How many seasons to pool, ending at `season`"),
    team: str | None = Query(None, pattern=TEAM, description="NFL team (franchise code, e.g. LV for OAK)"),
    opponent: str | None = Query(None, pattern=TEAM, description="Only the team's games against this opponent"),
    window: Literal["season", "last8", "last4"] = Query("season", description="The team's games used"),
    through_week: int | None = Query(None, ge=MIN_REG_WEEK, le=MAX_REG_WEEK,
                                     description="Last week of the newest season used (point-in-time view)"),
    mix_competitive: float | None = Query(None, ge=0, le=100, description="Hypothetical weight of a competitive game"),
    mix_leading: float | None = Query(None, ge=0, le=100, description="Hypothetical weight of a game the team leads"),
    mix_trailing: float | None = Query(None, ge=0, le=100, description="Hypothetical weight of a game the team trails"),
):
    """Observed team pace, neutral pass rate and per-script tendencies with shrunk estimates (sample sizes, priors,
    missingness), every offense's pace, an optional opponent matchup, a HYPOTHETICAL script-mix scenario and the
    frozen backtest result."""
    parts = (mix_competitive, mix_leading, mix_trailing)
    if any(p is not None for p in parts) and team is None:
        raise HTTPException(422, "A script scenario needs a team.")
    if opponent is not None and team is None:
        raise HTTPException(422, "An opponent filter needs a team.")
    mix = None if all(p is None for p in parts) else tuple(p or 0.0 for p in parts)
    q = PaceQuery(season=season, span=seasons, team=team.upper() if team else None,
                  opponent=opponent.upper() if opponent else None, window=window, through_week=through_week, mix=mix)
    with db() as con:
        try:
            return build_pace_lab(con, q)
        except PaceLabError as err:
            raise HTTPException(422, str(err))

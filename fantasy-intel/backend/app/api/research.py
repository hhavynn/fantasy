"""Read-only Fantasy Research Lab endpoints: no writes, no syncs, no ESPN calls. Metrics cover the whole
NFL WR / RB / TE pool from nflverse data; ESPN league data only supplies optional profile/trade links."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from ..analytics.research import ResearchError, build_opportunity, build_player_research
from .deps import db

router = APIRouter(prefix="/api/research", tags=["research"])


@router.get("/opportunity")
def opportunity(
    position: Literal["WR", "RB", "TE"] = Query(..., description="Positional pool"),
    window: Literal["season", "last3", "last5"] = Query("season", description="Player's qualifying games used"),
    season: int | None = Query(None, ge=1999, le=2100, description="Default: the synced season"),
    min_games: int | None = Query(None, ge=1, le=18, description="Minimum qualifying games in the window"),
    min_volume: float | None = Query(None, ge=0, le=40,
                                     description="Minimum targets/G (WR, TE) or carries+targets/G (RB)"),
    team: str | None = Query(None, min_length=2, max_length=3, description="NFL team abbreviation"),
):
    """Expected opportunity (xFP/G) vs production (PPR/G, FPOE/G) for every qualifying player at the
    position, with position-specific workload metrics, momentum, percentiles and data coverage."""
    with db() as con:
        try:
            return build_opportunity(con, season, position, window, min_games, min_volume,
                                     team.upper() if team else None)
        except ResearchError as err:
            raise HTTPException(422, str(err))


@router.get("/players/{gsis_id}")
def player(gsis_id: str = Path(..., min_length=4, max_length=20),
           season: int | None = Query(None, ge=1999, le=2100)):
    """One player's season / last-3 / last-5 windows, momentum and a weekly timeline in which byes,
    missed games and missing source rows stay distinct from measured zeros."""
    with db() as con:
        try:
            out = build_player_research(con, gsis_id, season)
        except ResearchError as err:
            raise HTTPException(422, str(err))
        if out is None:
            raise HTTPException(404, "No WR, RB or TE usage stored for this player and season")
        return out

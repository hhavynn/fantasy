"""Read-only Player Rankings & Value Explorer API router (docs/player_rankings.md)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.rankings import MAX_LIMIT, RankingsUnavailable, build_rankings
from .deps import db

router = APIRouter(prefix="/api", tags=["rankings"])


@router.get("/rankings")
def get_rankings(
    position: str = Query("ALL", description="ALL | QB | RB | WR | TE"),
    horizon: str = Query("week", description="week (current scoring period) | ros (remaining regular season)"),
    source: str | None = Query(None, description="Projection source: espn | independent (default: the league's own, espn when it has ESPN projections, else independent)"),
    sort: str = Query("rank", description="rank | name | espn | ours | rating | opportunity | production | xfp | "
                                         "ppr | consistency | trend"),
    dir: str | None = Query(None, description="asc | desc (default: asc for rank/name/consistency, else desc)"),
    q: str = Query("", description="Name search (accent- and case-insensitive substring)"),
    status: str = Query("all", description="all | rostered | available (free agent or waivers)"),
    limit: int = Query(100, description=f"1..{MAX_LIMIT}"),
    offset: int = Query(0, description="rows to skip (>= 0)"),
):
    """Skill players ranked by projected fantasy points (league scoring) with position-relative 0-100.0 ratings,
    opportunity, production, consistency, role trend, availability and roster status. Ranks are computed on the
    whole pool before the position / search / roster filters."""
    with db() as con:
        try:
            return build_rankings(con, position=position, horizon=horizon, source=source, sort=sort, dir=dir, q=q,
                                  status=status, limit=limit, offset=offset)
        except ValueError as err:
            raise HTTPException(422, str(err))
        except RankingsUnavailable as err:
            raise HTTPException(503, str(err))

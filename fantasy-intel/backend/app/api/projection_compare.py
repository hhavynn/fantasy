"""Read-only projection-source comparison (app.analytics.projection_compare, contract `projection-compare/v1`).

GET /api/rankings/compare?position=ALL&horizon=week&scope=position&sort=disagreement&show=all&status=all&q=&limit=100

Where the league provider's projections and Fantasy Intel's own (Ours) rank a player differently, with the model's
stored drivers and the recorded forecast history behind the large gaps and the next-week floor / ceiling range. Mounted
once here and again under /v2/leagues/{league_id} through league_scope.mount_legacy_routers. No training, syncs or
writes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.projection_compare import MAX_LIMIT, build_compare
from ..analytics.rankings import RankingsUnavailable
from .deps import db

router = APIRouter(prefix="/api", tags=["projection compare"])


@router.get("/rankings/compare")
def get_projection_compare(
    position: str = Query("ALL", description="ALL | QB | RB | WR | TE"),
    horizon: str = Query("week", description="week (current scoring period) | ros (remaining regular season)"),
    scope: str = Query("position", description="position (rank within the position) | overall (QB, RB, WR, TE together)"),
    sort: str = Query("disagreement", description="disagreement | delta | points | provider_rank | ours_rank | name"),
    dir: str | None = Query(None, description="asc | desc (default: asc for ranks and name, else desc)"),
    show: str = Query("all", description="all | large | compared (both ranked) | missing (either side unranked)"),
    status: str = Query("all", description="all | rostered | available (free agent or waivers)"),
    q: str = Query("", description="Name search (accent- and case-insensitive substring)"),
    limit: int = Query(100, description=f"1..{MAX_LIMIT}"),
    offset: int = Query(0, description="rows to skip (>= 0)"),
):
    """Provider and Ours ranks side by side, the rank and points delta (never 0 for a missing side), the evidence
    behind large deltas and the next-week range from the distribution engine."""
    with db() as con:
        try:
            return build_compare(con, position=position, horizon=horizon, scope=scope, sort=sort, dir=dir, q=q,
                                 status=status, show=show, limit=limit, offset=offset)
        except ValueError as err:
            raise HTTPException(422, str(err))
        except RankingsUnavailable as err:
            raise HTTPException(503, str(err))

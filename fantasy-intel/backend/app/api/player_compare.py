"""Read-only Player Comparison Lab.

GET /api/compare?a=<espn_player_id>&b=<espn_player_id>&source=espn|independent

Two players side by side from stored numbers only (app.analytics.player_compare, docs/player_comparison_lab.md):
nothing is forecast, and every historical figure stops at the last completed week.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import forward_ours
from ..analytics.player_compare import CompareError, LeagueUnavailable, UnknownPlayer, compare
from .deps import db
from .routes import _league

router = APIRouter(prefix="/api", tags=["player comparison"])


@router.get("/compare")
def compare_players(a: int, b: int,
                    source: str | None = Query(None, description="Projection source: espn | independent (default: the league's own, espn when it has ESPN projections, else independent)")):
    """Side-by-side comparison of two ESPN player ids (a == b is allowed).

    422 for a non-integer id or a bad source, 404 naming `a` or `b` for an unknown player (`a` is checked first),
    503 when no league is synced."""
    try:
        forward_ours.normalize(source)
    except ValueError as err:
        raise HTTPException(422, str(err))
    with db() as con:
        _league(con)
        try:
            return compare(con, a, b, source)
        except UnknownPlayer as err:
            raise HTTPException(404, str(err))
        except LeagueUnavailable as err:
            raise HTTPException(503, str(err))
        except CompareError as err:
            raise HTTPException(500, str(err))

"""Read-only Player Comparison Lab.

GET /api/compare?a=<espn_player_id>&b=<espn_player_id>&source=espn|independent
GET /api/compare/shortlist?ids=<id>,<id>[,<id>[,<id>]]&slot=<slot>&team_id=<id>&source=espn|independent

Two players side by side from stored numbers only (app.analytics.player_compare, docs/player_comparison_lab.md):
nothing is forecast, and every historical figure stops at the last completed week. The shortlist (issue #175) puts
two to four of the same player cards beside one lineup slot of this league (app.analytics.player_shortlist).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import forward_ours
from ..analytics.player_compare import CompareError, LeagueUnavailable, UnknownPlayer, compare
from ..analytics.player_shortlist import MAX_PLAYERS, ShortlistError, parse_ids, shortlist
from .deps import db
from .perspective import resolve_team
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


@router.get("/compare/shortlist")
def compare_shortlist(ids: str = Query(..., max_length=120, description=f"2 to {MAX_PLAYERS} comma-separated ESPN player ids, each once"),
                      slot: str | None = Query(None, max_length=8, description="A starting slot of this league (QB, RB, FLEX, OP, ...): eligibility and best projected for that slot"),
                      team_id: int | None = Query(None, description="The team the view is for (defaults to the manager's team): roster status"),
                      source: str | None = Query(None, description="Projection source: espn | independent (as GET /api/compare)")):
    """Two to four players for one lineup slot: the comparison lab's cards, the lineup engine's slot eligibility and
    the best stored projection among the eligible players. No start/sit verdict.

    422 for bad ids, a count outside 2-4, a repeated id, a slot the league does not start or a bad source; 404 naming
    the player for an unknown id or for a team not in the league; 503 when no league is synced."""
    try:
        forward_ours.normalize(source)
        pids = parse_ids(ids)
    except ValueError as err:
        raise HTTPException(422, str(err))
    with db() as con:
        lg = _league(con)
        team = resolve_team(con, lg, team_id)
        try:
            return shortlist(con, pids, slot=slot or None, team=team, source=source)
        except ShortlistError as err:
            raise HTTPException(422, str(err))
        except UnknownPlayer as err:
            raise HTTPException(404, str(err))
        except LeagueUnavailable as err:
            raise HTTPException(503, str(err))
        except CompareError as err:
            raise HTTPException(500, str(err))

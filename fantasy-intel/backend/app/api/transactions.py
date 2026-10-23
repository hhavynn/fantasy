"""Read-only league activity feed: no writes, no syncs, no provider calls."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ..analytics.transactions_feed import (DEFAULT_LIMIT, MAX_LIMIT, NotSynced, StaleCursor, UnknownManager,
                                           UnknownSeason, build_transactions_feed)
from .deps import db

router = APIRouter(prefix="/api", tags=["transactions"])


@router.get("/transactions")
def transactions(
    season: int | None = Query(None, ge=1999, le=2100, description="A season this league synced (default: the latest)"),
    manager: int | None = Query(None, ge=0, description="Only moves involving this fantasy team"),
    type: Literal["trade", "waiver", "free_agent", "drop", "commissioner"] | None = Query(None),  # noqa: A002
    position: Literal["QB", "RB", "WR", "TE", "K", "D/ST"] | None = Query(None),
    week: int | None = Query(None, ge=1, le=30, description="Scoring period of the move"),
    sort: Literal["date", "week", "type"] = Query("date"),
    dir: Literal["asc", "desc"] = Query("desc"),  # noqa: A002
    cursor: str | None = Query(None, max_length=200, description="next_cursor of the previous page"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    """Executed adds, drops and trades the league sync stored, one event per move (mirrored trade rows merged),
    filtered and paged. Times are UTC."""
    with db() as con:
        try:
            return build_transactions_feed(con, manager=manager, kind=type, position=position, week=week, sort=sort,
                                           direction=dir, cursor=cursor, limit=limit, season=season)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except (UnknownManager, UnknownSeason) as err:
            raise HTTPException(404, str(err))
        except StaleCursor as err:
            raise HTTPException(400, str(err))

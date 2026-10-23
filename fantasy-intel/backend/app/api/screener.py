"""Read-only advanced player screener (issue #226, contract screener/v1): no writes, no syncs, no provider calls.

Mounted twice like every legacy router: here (the classic single-league deployment), and under /v2/leagues/{league_id}
through league_scope.mount_legacy_routers, whose dependency authorizes the caller for the league (unknown and
inaccessible leagues are the same 404) before this handler runs, before any parameter of the screen is validated and
before any file is read. The league ownership annotation therefore only ever reads the authorized league's file.

The screen is typed and bounded (app/analytics/screener.py): allowlisted metric keys, numeric values inside each
metric's bounds, at most MAX_FILTERS filters in at most MAX_GROUPS groups, at most MAX_LIMIT rows, one parameterized
read of one season. No client text ever reaches SQL. No cache of its own: identical league-scoped GETs are answered
from the per-league response cache (league, versions, visibility epoch, perspective, path and query)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.screener import CONTRACT, ScreenerError, build_screen, limits, presets, registry, validate
from .deps import db

router = APIRouter(prefix="/api/screener", tags=["screener"])

_SHORT = dict(max_length=8)


@router.get("")
def screen(
    pos: list[str] | None = Query(None, description="WR, RB or TE; repeat for several (default all three)"),
    season: int | None = Query(None, ge=1999, le=2100, description="Default: the synced season"),
    week_from: str | None = Query(None, alias="from", description="First week of the range (1-18)", **_SHORT),
    week_to: str | None = Query(None, alias="to", description="Last week (default: the latest week with data)",
                                **_SHORT),
    last: str | None = Query(None, description="Only each player's last N games in the range (1-8)", **_SHORT),
    min_games: str | None = Query(None, description="Minimum games played in the window (default 1)", **_SHORT),
    min_routes: str | None = Query(None, description="Minimum routes in the window (needs route data)", **_SHORT),
    f: list[str] | None = Query(None, description="Filter group:metric:op:value, e.g. 1:targets_pg:gte:5; "
                                                  "same group = AND, groups = OR"),
    owner: str | None = Query(None, max_length=16, description="any | available | rostered"),
    sort: str | None = Query(None, max_length=32, description="A metric key, name or games"),
    dir: str | None = Query(None, description="asc | desc", **_SHORT),
    limit: str | None = Query(None, description="Rows to return (1-100, default 50)", **_SHORT),
):
    """Every WR / RB / TE matching a typed multi-metric screen over an exact week range, with each metric's sample
    (games counted of games played), unit, source and data cutoff, rows the screen could not decide (missing inputs,
    never 0) and, for the league, each player's ownership (unknown is never available). 422 for an invalid screen."""
    try:
        q = validate(season=season, positions=pos, week_from=week_from, week_to=week_to, last=last,
                     min_games=min_games, min_routes=min_routes, filters=f, owner=owner, sort=sort, direction=dir,
                     limit=limit)
    except ScreenerError as err:
        raise HTTPException(422, str(err)) from None
    with db() as con:
        try:
            return build_screen(con, q)
        except ScreenerError as err:
            raise HTTPException(422, str(err)) from None


@router.get("/metrics")
def metrics():
    """The typed metric registry (per position, unit, aggregation, denominator, source and filter bounds), the query
    limits and the presets: what a client (the screener page, later natural-language search) may ask for."""
    return {"contract": CONTRACT, "metrics": registry(), "limits": limits(), "presets": presets()}

"""Read-only floor / ceiling ranges around the served Ours projection (app.analytics.projection_ranges).

GET /api/projections/ranges?week=7

For the run every consumer view serves for the week (projection_source.select_run), each RB / WR / TE player's floor
(10th percentile), median and ceiling (90th percentile) of his points if he plays, keyed by ESPN player id. Pages
that show Ours numbers (Start/Sit, Matchup, Compare, a player's forward view) read it next to their own response.
No training, syncs or writes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import projection_ranges as pr
from ..analytics.legacy_league import latest_league
from .deps import db

router = APIRouter(prefix="/api", tags=["projection ranges"])

SCHEMA_VERSION = "projection-ranges/v1"


def build_response(con, week: int | None) -> dict:
    lg = latest_league(con, ["league_id", "season", "current_week"])
    if lg is None:
        raise LookupError("No ESPN league synced yet.")
    return {"schema_version": SCHEMA_VERSION, **pr.for_week(con, lg["season"], week or lg["current_week"])}


@router.get("/projections/ranges")
def projection_ranges(week: int | None = Query(None, ge=1, le=18)):
    """Floor / median / ceiling per player for the served Ours run of the week."""
    with db() as con:
        try:
            return build_response(con, week)
        except LookupError as err:
            raise HTTPException(503, str(err))

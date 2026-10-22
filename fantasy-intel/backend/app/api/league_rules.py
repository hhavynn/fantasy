"""Read-only league rules endpoint: no writes, no syncs, no provider calls. Reads the league's synced `leagues` row
only (app/analytics/league_rules.py)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.league_rules import NOT_SYNCED, build_league_rules
from ..analytics.legacy_league import latest_league
from .deps import db

router = APIRouter(prefix="/api", tags=["league rules"])


@router.get("/league-rules")
def league_rules():
    """The league's roster slots and eligible positions, scoring, lineup lock, waivers, trade deadline and playoffs,
    as synced; missing values are null with a reason, rules this service cannot read are listed as unmapped, and each
    rule says whether the analysis engines apply it."""
    with db() as con:
        lg = latest_league(con)
    if lg is None:
        raise HTTPException(503, NOT_SYNCED)
    return build_league_rules(lg)

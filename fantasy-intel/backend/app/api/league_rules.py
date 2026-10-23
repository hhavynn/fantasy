"""Read-only league rules and league compatibility endpoints: no writes, no syncs, no provider calls. They read the
league's synced file only (app/analytics/league_rules.py, app/analytics/league_compatibility.py). Both are legacy
routers mounted again under /v2/leagues/{league_id}/api/ (app/api/league_scope.py), so a multi-league request is
authorized for its league first and reads that league's file only: never a global answer."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.league_compatibility import build_compatibility, load_facts
from ..analytics.league_rules import NOT_SYNCED, build_league_rules, skill_runs_rescored
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
        rescored = skill_runs_rescored(con, lg)
    return build_league_rules(lg, ours_rescored=rescored)


@router.get("/league-compatibility")
def league_compatibility():
    """Per analysis tool, whether this league is supported, on four separate dimensions (import, roster, scoring,
    feature), each handled / partial / unsupported / unverified with the reason, the missing input, the provider
    boundary and an action (contract league-compatibility/v1)."""
    with db() as con:
        lg = latest_league(con)
        if lg is None:
            raise HTTPException(503, NOT_SYNCED)
        facts = load_facts(con, lg)
    return build_compatibility(lg, facts)

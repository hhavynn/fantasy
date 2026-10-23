"""Read-only Playoff Race Lab endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.playoff_race import NotFound, NotSynced, build_playoff_race, load_race_data, parse_overrides
from .deps import db

router = APIRouter(prefix="/api", tags=["playoff race"])


@router.get("/playoff-race")
def playoff_race(team_id: int | None = None,
                 base: str = Query("projected", description="projected | actual"),
                 override: list[str] | None = Query(None, description="MATCHUP_ID:home|away|tie|none, repeatable")):
    """Standings, all-play, projected remaining matchups and a deterministic scenario with overrides. With at least one
    override the answer adds a `whatif` block (inputs, assumptions, method, changes and gated record bounds)."""
    with db() as con:
        try:
            return build_playoff_race(load_race_data(con), team_id=team_id, base=base.lower(),
                                      overrides=parse_overrides(override))
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))

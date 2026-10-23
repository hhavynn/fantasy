"""Read-only schedule intelligence endpoints: no writes, no syncs, no provider calls.

Both routes are on this one router, which app/main.py also mounts under /v2/leagues/{league_id} with `league_scope`
(authorization, the league's own files, 404 for an inaccessible league)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.bye_planner import build_bye_planner
from ..analytics.schedule_intelligence import NotFound, NotSynced, build_schedule_intelligence
from ..db.connection import table_exists
from .deps import db
from .signal_provenance import Provenance, coverage, payload

router = APIRouter(prefix="/api", tags=["schedule intelligence"])


@router.get("/schedule-intelligence")
def schedule_intelligence(team_id: int | None = None,
                          position: str = Query("all", description="all | QB | RB | WR | TE"),
                          phase: str = Query("all", description="all | regular | playoffs"),
                          player_id: int | None = None):
    """Each remaining week's opponent and what that defense allowed in games completed before the week."""
    with db() as con:
        try:
            return build_schedule_intelligence(con, team_id=team_id, position=position.upper()
                                               if position.lower() != "all" else "all",
                                               phase=phase.lower(), player_id=player_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))


@router.get("/schedule-intelligence/bye-planner")
def bye_planner(team_id: int | None = Query(None, description="Fantasy team to plan for (defaults to yours)")):
    """For each remaining week through the final scoring period: whether the roster can fill every starting slot,
    byes, verified NFL absences and what is unknown (contract bye-planner.v1)."""
    with db() as con:
        try:
            out = build_bye_planner(con, team_id=team_id)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        out["provenance"] = bye_planner_provenance(con, out)
        return out


def bye_planner_provenance(con, d: dict) -> dict:
    """The roster (provider sync), the NFL schedule (coverage: planned weeks with a loaded schedule) and the NFL
    weekly rosters behind the reserve-list availability."""
    prov = Provenance(con)
    weeks = d["window"]["weeks"]
    loaded = len(d["schedule"]["weeks_loaded"])
    missing = d["schedule"]["weeks_missing"]
    schedule = prov.nflverse(
        "schedule", "NFL schedule and byes (nflverse)",
        cov=coverage(loaded, len(weeks), "planned weeks with a loaded NFL schedule") if weeks else None,
        reason=None if d["schedule"]["available"] and not missing else
        ("No NFL schedule is stored." if not d["schedule"]["available"]
         else f"No NFL schedule is loaded for week {', '.join(map(str, missing))}."))
    rosters = prov.nflverse(
        "injuries", "NFL reserve lists (nflverse weekly rosters)",
        reason=None if table_exists(con, "nfl_rosters_weekly") else
        "No NFL weekly rosters are stored, so no reserve-list absence can be verified.")
    return payload(prov.league(), schedule, rosters)

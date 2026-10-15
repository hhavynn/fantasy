"""Read-only Luck vs. Skill Lab endpoint: no writes, no syncs, no ESPN calls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.luck_lab import NotFound, NotSynced, build_luck_lab, load_luck_data
from .deps import db

router = APIRouter(prefix="/api", tags=["luck lab"])


@router.get("/luck-lab")
def luck_lab(team_id: int | None = None, schedule_of: int | None = None):
    """Expected wins, schedule luck and difficulty, notable results, consistency and counterfactual
    opponent schedules over completed, fully observed regular-season periods."""
    with db() as con:
        try:
            return build_luck_lab(load_luck_data(con), team_id=team_id, schedule_of=schedule_of)
        except NotSynced as err:
            raise HTTPException(503, str(err))
        except NotFound as err:
            raise HTTPException(404, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))

"""Independent read-only manager scouting endpoint."""

from fastapi import APIRouter, HTTPException

from ..analytics.manager_scouting import build_manager_scouting
from .deps import db

router = APIRouter(prefix="/api/managers", tags=["manager scouting"])


@router.get("/{team_id}/scouting")
def manager_scouting(team_id: int):
    with db() as con:
        try:
            report = build_manager_scouting(con, team_id)
        except LookupError as exc:
            raise HTTPException(503, str(exc)) from exc
        if report is None:
            raise HTTPException(404, "Unknown team")
        return report

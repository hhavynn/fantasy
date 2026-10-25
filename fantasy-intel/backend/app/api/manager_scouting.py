"""Independent read-only manager scouting endpoints."""

from fastapi import APIRouter, HTTPException

from ..analytics.manager_scouting import build_manager_scouting
from ..analytics.trade_counterparty import NotSynced, build_trade_counterparty
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


@router.get("/{team_id}/counterparty")
def trade_counterparty(team_id: int):
    """Descriptive patterns in this team's recorded, executed trades (issue #215): each with its numerator,
    denominator and date window; small samples suppressed. No acceptance likelihood, nothing written or sent."""
    with db() as con:
        try:
            report = build_trade_counterparty(con, team_id)
        except NotSynced as exc:
            raise HTTPException(503, str(exc)) from exc
        if report is None:
            raise HTTPException(404, "Unknown team")
        return report

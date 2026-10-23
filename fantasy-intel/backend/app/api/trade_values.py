"""Read-only trade values board (issue #184, contract trade-values/v1): no writes, no syncs, no provider calls.

Mounted twice like every legacy router: here, and under /v2/leagues/{league_id} through league_scope.mount_legacy_routers
(authorization, then the per-league response cache keyed by league, versions, perspective, path and query). The
endpoint takes no query parameters of its own and keeps no cache: everything it reads is the scoped league's file."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..analytics.legacy_league import latest_league
from ..analytics.trade_data import load_league_state
from ..analytics.trade_values import build_trade_values
from .deps import db
from .signal_provenance import trade_provenance

router = APIRouter(prefix="/api/trades", tags=["trade values"])


@router.get("/values")
def trade_values():
    """Every rostered player's Fantasy Intel model estimate of value: projected points above replacement at his
    position over the remaining weeks, in this league's scoring and over the trade analyzer's window, with the
    calculation week by week, its uncertainty, and the change since last week's stored projection snapshot (null and
    explained when none exists, never 0). Not a trade price. 503 when the league is not synced."""
    with db() as con:
        lg = latest_league(con, ["league_id", "season", "name", "current_week", "final_scoring_period", "synced_at"])
        state = load_league_state(con) if lg else None
        if lg is None or state is None:
            raise HTTPException(503, "League current/final week unknown. Run a sync first.")
        return build_trade_values(con, lg, state, provenance=trade_provenance(con, lg, state))

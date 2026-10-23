"""Read-only trade scenario comparison API (issue #183): GET /api/trades/scenarios?scenarios=TA:TB:A.A:B.B,...

The scenarios live on the manager's device (frontend/lib/trade-scenarios.ts); the page sends them here only to be
compared against the league's current files (app/analytics/trade_scenarios.py, on the trade analyzer's league state
and lineup simulation). Analysis only: there is no write endpoint, nothing is stored, and no trade is proposed to or
submitted on ESPN or Sleeper. League-scoped through mount_legacy_routers.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.legacy_league import latest_league
from ..analytics.trade_data import load_league_state, player_names
from ..analytics.trade_scenarios import (
    MAX_IDS_PER_SIDE,
    MAX_SCENARIOS,
    ScenarioError,
    compare_scenarios,
    parse_scenarios,
)
from ..analytics.trades import NoRemainingWeeksError, TradeError, with_streaming
from ..config import get_settings
from .deps import db
from .signal_provenance import snapshot_versions, trade_provenance

router = APIRouter(prefix="/api", tags=["trades"])

# 5 scenarios x (two 6-digit teams, 2 x 12 ids of up to 14 characters, separators)
_MAX_LEN = MAX_SCENARIOS * (2 * 7 + 2 * MAX_IDS_PER_SIDE * 15 + 2)


@router.get("/trades/scenarios")
def get_trade_scenarios(
    scenarios: str = Query("", max_length=_MAX_LEN,
                           description="Scenarios in saved order, comma-separated, each TEAM_A:TEAM_B:A_SENDS:B_SENDS "
                                       "with player ids separated by dots"),
    start_week: int | None = Query(None, ge=1, le=30, description="Shared first evaluated week (default: the earliest "
                                                                  "actionable week)"),
):
    """Compare up to five trade scenarios side by side: next-week, regular-season, playoff and rest-of-season lineup
    deltas for both teams of each, over one shared window and one data snapshot. A scenario whose players have moved
    is reported stale and not re-valued; scenarios sharing a player are reported as conflicts (they are alternatives,
    never summed). Read-only: nothing is stored, proposed or sent anywhere.

    The list is one parameter, not a repeated one: its order is the saved order, and one ordered value keeps the
    league-scoped response cache keyed on exactly what was asked."""
    try:
        reqs = parse_scenarios(scenarios)
    except ScenarioError as err:
        raise HTTPException(422, str(err))
    with db() as con:
        lg = latest_league(con, ["league_id", "season", "name", "current_week", "final_scoring_period", "synced_at"])
        if not lg:
            raise HTTPException(503, "League not synced yet. Run scripts/sync_espn.py.")
        state = load_league_state(con)
        if state is None:
            raise HTTPException(503, "League current/final week unknown. Run scripts/sync_espn.py.")
        state = with_streaming(state)      # as the analyzer's default: K and D/ST valued against weekly streaming
        try:
            out = compare_scenarios(state, reqs, start_week, max_per_side=get_settings().trade_max_players_per_side)
        except NoRemainingWeeksError as err:
            raise HTTPException(409, str(err))
        except (TradeError, ScenarioError) as err:
            raise HTTPException(422, str(err))
        ids = {pid for req in reqs for pid in req.players()}
        ids |= {d["espn_player_id"] for row in out["scenarios"] if row["status"] == "evaluated"
                for side in ("team_a", "team_b") for d in row[side]["forced_drops"]}
        ids |= {pid for row in out["scenarios"] if row["status"] == "evaluated"
                for side in ("team_a", "team_b") for pid in row[side]["missing_players"]}
        names = player_names(con, ids)
        out["names"] = {pid: {"name": n["full_name"], "position": n["position"], "pro_team": n["pro_team"]}
                        for pid, n in sorted(names.items())}
        out["teams"] = [{"team_id": t, "name": state.team_names.get(t)} for t in sorted(state.rosters)]
        out["league"] = {k: lg.get(k) for k in ("league_id", "season", "name", "current_week", "final_scoring_period")}
        out["data_version"] = snapshot_versions(con)[0]
        out["provenance"] = trade_provenance(con, lg, state, start_week)
        return out

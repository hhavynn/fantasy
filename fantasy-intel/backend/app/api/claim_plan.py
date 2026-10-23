"""Read-only waiver claim planner API (issue #180): GET /api/waivers/claim-plan?claims=ADD:DROP:BID,...

The plan lives on the manager's device; the page sends it here only to be checked. Analysis only: there is no write
endpoint, nothing is stored, and nothing is sent to ESPN or Sleeper. Each claim is the add/drop simulator's scenario
(app/analytics/claim_plan.py over app/analytics/add_drop.py). League-scoped through mount_legacy_routers.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics.claim_plan import MAX_CLAIMS, ClaimPlanError, evaluate_claim_plan, parse_claim
from .add_drop import load_simulation
from .deps import db

router = APIRouter(prefix="/api", tags=["waivers"])


@router.get("/waivers/claim-plan")
def get_claim_plan(
    team_id: int | None = Query(None, ge=0, le=999_999, description="Fantasy team the plan is for (defaults to the owner's team)"),
    claims: str = Query("", max_length=MAX_CLAIMS * 65,
                        description="Claims in priority order, comma-separated, each ADD[:DROP[:BID]] (empty parts for none)"),
):
    """Check an ordered waiver claim plan against the synced league: availability, the drop, roster capacity, IR and
    lock rules, claims that depend on the same drop or open spot, and FAAB bids where the provider supplies the
    rules. Read-only: nothing is stored, claimed or sent anywhere.

    The plan is one parameter, not a repeated one: its order is the claim order, and the league-scoped response cache
    keys on the sorted query pairs, so repeated values in another order would share one cached answer."""
    parts = [c for c in claims.split(",")] if claims else []
    if len(parts) > MAX_CLAIMS:
        raise HTTPException(422, f"A plan holds at most {MAX_CLAIMS} claims.")
    if any(len(c) > 64 for c in parts):
        raise HTTPException(422, "A claim is ADD[:DROP[:BID]].")
    try:
        plan = [parse_claim(c) for c in parts]
    except ClaimPlanError as err:
        raise HTTPException(422, str(err))
    ids = [x for c in plan for x in (c.add, c.drop) if x is not None]
    with db() as con:
        sim = load_simulation(con, team_id, ids)
        try:
            out = evaluate_claim_plan(sim.state, sim.team_id, plan, sim.ctx)
        except ClaimPlanError as err:
            raise HTTPException(422, str(err))
        except LookupError as err:
            raise HTTPException(404, str(err))
        return sim.finish(con, out)

"""Read-only waiver claim planner API (issue #180): GET /api/waivers/claim-plan.

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
    claim: list[str] = Query([], max_length=MAX_CLAIMS,
                             description="Claims in priority order, each ADD[:DROP[:BID]] (empty parts for none)"),
):
    """Check an ordered waiver claim plan against the synced league: availability, the drop, roster capacity, IR and
    lock rules, claims that depend on the same drop or open spot, and FAAB bids where the provider supplies the
    rules. Read-only: nothing is stored, claimed or sent anywhere."""
    if any(len(c) > 64 for c in claim):
        raise HTTPException(422, "A claim is ADD[:DROP[:BID]].")
    try:
        claims = [parse_claim(c) for c in claim]
    except ClaimPlanError as err:
        raise HTTPException(422, str(err))
    ids = [x for c in claims for x in (c.add, c.drop) if x is not None]
    with db() as con:
        sim = load_simulation(con, team_id, ids)
        try:
            out = evaluate_claim_plan(sim.state, sim.team_id, claims, sim.ctx)
        except ClaimPlanError as err:
            raise HTTPException(422, str(err))
        except LookupError as err:
            raise HTTPException(404, str(err))
        return sim.finish(con, out)

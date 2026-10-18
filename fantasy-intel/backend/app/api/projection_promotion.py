"""Read-only promotion tracker for the independent projection engine.

GET /api/projections/independent/promotion

Per position: the stored held-out status vs the frozen PROMOTION_CRITERIA and the matched
pregame-ESPN evidence so far (app.analytics.promotion_tracker). No training, no syncs, no writes.
The tracker reuses the research modules (polars); the hosted API bundle ships without them, so
there it answers available: false instead of a copy of the criteria, still with the production model's
governance (projection_source.governance, SQL-free constants) so the hosted site never implies validation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .deps import db

router = APIRouter(prefix="/api", tags=["independent projections"])

SCHEMA_VERSION = "independent-promotion/v1"


@router.get("/projections/independent/promotion")
def projection_promotion():
    """How far each position is from the frozen promotion criteria. Never promotes anything."""
    try:
        from ..analytics.promotion_tracker import build_tracker
    except ImportError as err:
        from ..analytics.projection_source import MODEL_VERSION, governance
        return {"schema_version": SCHEMA_VERSION, "available": False, "model_version": MODEL_VERSION,
                "governance": governance(MODEL_VERSION),
                "unavailable_reason": f"The promotion tracker needs the research packages ({err.name}), which this "
                                      f"API deployment does not include, so evidence progress is not shown here. "
                                      "Open the Data quality page on a local backend."}
    with db() as con:
        try:
            return build_tracker(con)
        except LookupError as err:
            raise HTTPException(503, str(err))

"""Read-only projection-performance scorecard.

GET /api/projections/performance[?season=2026]
GET /api/projections/replay[?season=2026]        weekly model replay snapshots (app.analytics.model_replay)

Serves the newest published report that verifies (app.analytics.performance_report): it is computed offline by
scripts/projection_performance.py and published with the database, so the hosted API needs SQL alone and runs no
statistics. Without a verified report it answers available: false with the reason and the production model's
governance, never a number.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from ..analytics import model_replay as mr
from ..analytics import performance_report as pr
from ..config import get_settings
from .deps import db

router = APIRouter(prefix="/api", tags=["independent projections"])

# Local screenshots and browser tests only: serve a report marked synthetic (the page labels it on every section).
# Never set on the hosted deployment; without it a synthetic report is rejected like a damaged one.
ALLOW_SYNTHETIC_ENV = "PROJECTION_PERFORMANCE_ALLOW_SYNTHETIC"


@router.get("/projections/performance")
def projection_performance(season: int | None = None):
    """Accuracy of the production projections against ESPN, as published. Never computes or promotes anything."""
    season = season or get_settings().nfl_season
    with db() as con:
        return pr.response(con, season, allow_synthetic=os.getenv(ALLOW_SYNTHETIC_ENV) == "1")


@router.get("/projections/replay")
def model_replay(season: int | None = None):
    """Today's model replayed on every finished week, and how it compares with the previous model, week by week.
    Hindsight, never prospective evidence; computed offline by scripts/model_replay.py."""
    season = season or get_settings().nfl_season
    with db() as con:
        return mr.response(con, season)

"""Public 0-100.0 rating adapter for the Player Rankings page.

The public 0-100.0 rating contract belongs to the separate ratings workstream, which is not merged. This module
is the interface/adapter the rankings build against: no rating formula is defined here. Today it maps the existing
`player_scores` 0-10 scores linearly (`round(score x 10, 1)`, clamped to [0, 100], missing stays missing). When
that work lands, only this module changes.

Ratings are position-relative percentiles: not a trade price and not comparable to projected points.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

SCALE = "0-100"
DECIMALS = 1
CONTRACT = {"scale": "0-100", "decimals": 1, "contract": "adapter", "adapter": "player_scores 0-10 x 10",
            "semantics": "position-relative percentile; not a trade price; not comparable to points"}


def to_public(score_0_10: float | None) -> float | None:
    """A player_scores 0-10 score on the public scale; None / NaN stay None (never 0)."""
    if score_0_10 is None:
        return None
    x = float(score_0_10)
    if math.isnan(x):
        return None
    return round(min(100.0, max(0.0, round(x * 10, DECIMALS))), DECIMALS)


def public_rating(row: Mapping) -> dict:
    """{"overall", "opportunity", "production"} on the public scale from a player_scores row."""
    return {"overall": to_public(row.get("value_score")),
            "opportunity": to_public(row.get("opportunity_score")),
            "production": to_public(row.get("production_score"))}

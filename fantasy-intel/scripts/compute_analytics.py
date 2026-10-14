"""Recompute ID mapping, player scores, manager metrics and waiver rankings
from whatever is already in DuckDB (no network).

    cd backend && uv run python ../scripts/compute_analytics.py
"""

import json
import sys

import _bootstrap  # noqa: F401

from app.analytics.pipeline import run_analytics
from app.config import get_settings
from app.db.connection import open_db


def main() -> int:
    settings = get_settings()
    with open_db(settings.db_path) as con:
        stats = run_analytics(con, settings.season)
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

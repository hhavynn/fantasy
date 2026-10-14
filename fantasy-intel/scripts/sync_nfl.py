"""Import current-season nflverse data into DuckDB, then recompute analytics.

    cd backend && uv run python ../scripts/sync_nfl.py
"""

import json
import sys

import _bootstrap  # noqa: F401

from app.analytics.pipeline import run_analytics
from app.config import get_settings
from app.db.connection import open_db
from app.nfl.sync import run_nfl_sync


def main() -> int:
    settings = get_settings()
    stats = run_nfl_sync(lambda: open_db(settings.db_path), settings.nfl_season)
    print(json.dumps({k: v for k, v in stats.items() if k != "warnings"}, indent=2, default=str))
    for w in stats["warnings"]:
        print("WARNING:", w)
    with open_db(settings.db_path) as con:
        run_analytics(con, settings.nfl_season)
    print(f"NFL sync complete -> {settings.db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

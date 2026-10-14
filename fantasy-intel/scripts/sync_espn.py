"""Import your ESPN league into DuckDB, then recompute analytics.

    cd backend && uv run python ../scripts/sync_espn.py
"""

import json
import sys

import _bootstrap  # noqa: F401

from app.analytics.pipeline import run_analytics
from app.config import get_settings
from app.db.connection import open_db
from app.espn.sync import run_espn_sync


def main() -> int:
    settings = get_settings()
    stats = run_espn_sync(lambda: open_db(settings.db_path), settings)
    print(json.dumps({k: v for k, v in stats.items() if k != "warnings"}, indent=2, default=str))
    for w in stats["warnings"]:
        print("WARNING:", w)
    with open_db(settings.db_path) as con:
        run_analytics(con, settings.season)
    print(f"ESPN sync complete -> {settings.db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

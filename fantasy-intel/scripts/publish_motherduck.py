"""Publish the local DuckDB file to MotherDuck so the hosted dashboard shows fresh data.
Run it after the sync scripts. Needs a MotherDuck access token in .env as MOTHERDUCK_TOKEN
(read-scaling tokens are read-only and cannot publish).

    cd backend && uv run python ../scripts/publish_motherduck.py [--database fantasy_intel]
"""

import argparse
import sys

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.db.publish import publish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="fantasy_intel",
                        help="MotherDuck database to replace (the API's MOTHERDUCK_DATABASE)")
    args = parser.parse_args()
    settings = get_settings()
    stamp = publish(settings.db_path, args.database)
    print(f"Published {settings.db_path} -> md:{args.database} at {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

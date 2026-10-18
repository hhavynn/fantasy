"""Publish the local DuckDB file to MotherDuck so the hosted dashboard shows fresh data.
Run it after the sync scripts. Needs a MotherDuck access token in .env as MOTHERDUCK_TOKEN
(read-scaling tokens are read-only and cannot publish).

    cd backend && uv run python ../scripts/publish_motherduck.py [--database fantasy_intel]
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from app.config import DATA_DIR, get_settings
from app.db.publish import publish

HISTORY = DATA_DIR / "logs" / "publish-history.jsonl"


def record_publish(stamp: str, database: str, local_path, history=HISTORY) -> None:
    """Append one line per successful publish (manual ones too), so evidence_health.py knows when the hosted
    copy was last refreshed without querying MotherDuck. Best effort: the publish already succeeded."""
    from app.db.connection import connect, schema_version

    try:
        con = connect(local_path, read_only=True, retries=4)
        try:
            version = schema_version(con)
        finally:
            con.close()
    except Exception:
        version = None
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"published_at": stamp, "database": database, "local_schema_version": version,
                                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    except OSError as err:
        print(f"WARNING: could not record the publish in {history}: {err}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="fantasy_intel",
                        help="MotherDuck database to replace (the API's MOTHERDUCK_DATABASE)")
    args = parser.parse_args()
    settings = get_settings()
    stamp = publish(settings.db_path, args.database)
    print(f"Published {settings.db_path} -> md:{args.database} at {stamp}")
    record_publish(stamp, args.database, settings.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

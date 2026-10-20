"""Publish the local DuckDB file to Cloudflare R2 as the hosted dashboard's snapshot. Run it after the
sync scripts. Needs SNAPSHOT_BUCKET, R2_ACCOUNT_ID and a read/write R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
in .env (the hosted API gets a read-only token instead).

    cd backend && uv run python ../scripts/publish_snapshot.py
"""

import sys

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.db.snapshot_publish import publish_snapshot
from publish_motherduck import record_publish


def main() -> int:
    settings = get_settings()
    m = publish_snapshot(settings.db_path, settings.r2_config(), prefix=settings.snapshot_prefix)
    target = f"r2:{settings.snapshot_bucket}/{settings.snapshot_prefix}"
    print(f"Published {settings.db_path} -> {target}/{m.key.rsplit('/', 1)[-1]} ({m.bytes / 1e6:.1f} MB) at {m.published_at}")
    record_publish(m.published_at, target, settings.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Publish the local league as per-league files for the multi-league read path (Phase 2), after the syncs.

    cd backend && uv run python ../scripts/publish_leagues.py

Needs LEAGUE_PUBLISH_ID (the league's `lg_...` id in the control plane) and FI_SYNC_DB_URL (the fi_sync connection
string; never the service-role key). Objects go to R2 when SNAPSHOT_BUCKET/R2_* are set (read/write token), else to
LEAGUE_FILES_DIR. Prints ids, versions and counts only. The Phase 1a snapshot (publish_snapshot.py) keeps
publishing alongside until Phase 5 retires it.
"""
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.db.league_publish import DirWriter, R2Writer, publish_league


def main() -> int:
    settings = get_settings()
    league_id = (os.getenv("LEAGUE_PUBLISH_ID") or "").strip()
    dsn = (os.getenv("FI_SYNC_DB_URL") or "").strip()
    if not league_id or not dsn:
        print("LEAGUE_PUBLISH_ID and FI_SYNC_DB_URL must both be set", file=sys.stderr)
        return 2
    files_dir = (os.getenv("LEAGUE_FILES_DIR") or "").strip()
    writer = R2Writer(settings.r2_config()) if settings.r2_configured else DirWriter(Path(files_dir)) if files_dir else None
    if writer is None:
        print("no object store: set the R2 settings or LEAGUE_FILES_DIR", file=sys.stderr)
        return 2
    import psycopg
    with psycopg.connect(dsn, prepare_threshold=None, connect_timeout=10) as con:
        res = publish_league(settings.db_path, writer, con, league_id)
    state = "published" if res.applied else "superseded by a newer publish (nothing changed)"
    print(f"{league_id}: {state}; data {res.data_version}; global {res.global_version}; fence {res.fence}; "
          f"collected {len(res.deleted)} old objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Publish the laptop's global file (nflverse, contexts, the model registry and the reference model runs) for the
multi-league read path (Phase 4), after the syncs and analytics.

    cd backend && uv run python ../scripts/publish_global.py

Needs FI_SYNC_DB_URL (the fi_sync connection string; never the service-role key). Objects go to R2 when
SNAPSHOT_BUCKET/R2_* are set (read/write token), else to LEAGUE_FILES_DIR. An unchanged global file uploads
nothing. League files, Los Subies included, are built by the sync jobs from this file (app/sync/build.py);
publish_leagues.py stays for a manual rollback. Prints versions only.
"""
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.db.connection import utcnow
from app.db.league_publish import GLOBAL_STAMP, DirWriter, R2Writer, publish_global
from app.db.league_split import content_hash, split_database


def main() -> int:
    settings = get_settings()
    dsn = (os.getenv("FI_SYNC_DB_URL") or "").strip()
    if not dsn:
        print("FI_SYNC_DB_URL must be set", file=sys.stderr)
        return 2
    files_dir = (os.getenv("LEAGUE_FILES_DIR") or "").strip()
    writer = R2Writer(settings.r2_config()) if settings.r2_configured else DirWriter(Path(files_dir)) if files_dir else None
    if writer is None:
        print("no object store: set the R2 settings or LEAGUE_FILES_DIR", file=sys.stderr)
        return 2
    import psycopg
    with tempfile.TemporaryDirectory() as tmp, psycopg.connect(dsn, prepare_threshold=None, connect_timeout=10) as con:
        out = split_database(settings.db_path, Path(tmp) / "league-unused.duckdb", Path(tmp) / "global.duckdb",
                             stamp=utcnow().isoformat(timespec="seconds"), global_stamp=GLOBAL_STAMP)
        current = con.execute("SELECT content_hash FROM fi.global_snapshot").fetchone()
        con.rollback()
        unchanged = current is not None and current[0] == content_hash(out.global_path)
        version = publish_global(out.global_path, out.global_version, writer, con)
    state = "unchanged (current version kept)" if unchanged else "published"
    print(f"global: {state}; version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

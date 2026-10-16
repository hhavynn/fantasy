"""Backfill the kicker opportunity tables (nfl_kick_attempts, nfl_team_game_drives) from nflverse play-by-play.

The regular syncs keep them current once they exist (sync_nfl.py for the live season, sync_nfl_history.py for
finished seasons). This script fills them in an existing database WITHOUT rebuilding anything else:

    cd backend && uv run python ../scripts/sync_kicking_pbp.py --history --seasons 2019-2025
    cd backend && uv run python ../scripts/sync_kicking_pbp.py --live                 # the live NFL season

Each season is replaced in one transaction (delete + insert of that season's rows only). The history database
accepts finished seasons only, the live database only the live season, mirroring the two syncs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import REPO_ROOT, get_settings
from app.db.connection import SyncRun, connect, init_db, replace_where
from app.nfl.kicking_pbp import derive_kicks, derive_team_drives, drive_reconciliation


def history_path() -> Path:
    raw = Path(os.getenv("NFL_HISTORY_DB_PATH") or "data/nfl_history.duckdb")
    return raw if raw.is_absolute() else REPO_ROOT / raw


def parse_seasons(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return sorted(set(out))


def run(db_path: Path, seasons: list[int], loader=None) -> dict:
    import nflreadpy as nfl

    load = loader or (lambda s: nfl.load_pbp([s]))
    payload = {s: load(s) for s in seasons}         # download everything before opening a write connection
    con = connect(db_path)
    stats: dict = {"db": str(db_path), "seasons": {}}
    try:
        init_db(con)
        sync = SyncRun(con, "kicking_pbp")
        sync.start(stats={"seasons": seasons})
        try:
            for s, pbp in payload.items():
                kicks, drives = derive_kicks(pbp, s), derive_team_drives(pbp, s)
                con.execute("BEGIN TRANSACTION")
                try:
                    stats["seasons"][s] = {
                        "kicks": replace_where(con, "nfl_kick_attempts", kicks, "season = ?", [s]),
                        "team_games": replace_where(con, "nfl_team_game_drives", drives, "season = ?", [s]),
                        "drive_reconciliation": drive_reconciliation(kicks, drives)}
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
        except Exception as err:
            sync.finish("error", f"{type(err).__name__}: {err}")
            raise
        sync.finish("ok", f"seasons {seasons}", stats)
    finally:
        con.close()
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--history", action="store_true", help="the history database (finished seasons)")
    target.add_argument("--live", action="store_true", help="the live database (the live NFL season only)")
    ap.add_argument("--seasons", default=None, help="e.g. 2019-2025 (history only)")
    ap.add_argument("--db", default=None, help="override the database path")
    args = ap.parse_args(argv)
    settings = get_settings()
    if args.live:
        seasons = [settings.nfl_season]
        db = Path(args.db) if args.db else settings.db_path
    else:
        if not args.seasons:
            ap.error("--history needs --seasons")
        seasons = parse_seasons(args.seasons)
        if seasons[-1] >= settings.nfl_season:
            ap.error(f"the history database holds finished seasons only (< {settings.nfl_season})")
        db = Path(args.db) if args.db else history_path()
    print(json.dumps(run(db, seasons), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

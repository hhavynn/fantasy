"""Research: the frozen walk-forward backtest of the pace lab (issue #211; docs/research/pace_lab.md).

    cd backend
    uv run python ../scripts/pace_lab_backtest.py history --seasons 2019-2025 --out <path>.duckdb
        # nflverse schedules + play-by-play through app/nfl/history.py's own writer: only the play-by-play aggregates
        # (nfl_team_week, nfl_team_game_drives, ...) and nfl_schedules, into a separate file (never the live database)
    uv run python ../scripts/pace_lab_backtest.py run --db <path>.duckdb [--write]
        # the frozen backtest; --write records the result in backend/app/analytics/pace_lab_backtest.json

Read-only on the database it backtests. Never writes a projection, forecast, ledger, registry or production table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

import duckdb  # noqa: E402

from app.analytics import pace_backtest as bt  # noqa: E402
from app.analytics import pace_lab  # noqa: E402

RESULT = pace_lab.BACKTEST_FILE


def _seasons(text: str) -> list[int]:
    if "-" in text:
        a, b = text.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in text.split(",")]


def cmd_history(args) -> int:
    import nflreadpy as nfl

    from app.config import get_settings
    from app.db.connection import SyncRun, connect, init_db
    from app.nfl import history

    seasons = history.validate_request(_seasons(args.seasons), args.live_season, args.out, get_settings().db_path)
    payload = history.fetch(seasons, loaders={"nfl_schedules": lambda s: nfl.load_schedules(s)})
    con = connect(args.out)
    try:
        init_db(con)
        run = SyncRun(con, "nfl_history")
        stats = history.write(con, seasons, payload)
        try:
            stats["nflreadpy_version"] = version("nflreadpy")
        except PackageNotFoundError:
            stats["nflreadpy_version"] = None
        run.finish("ok", f"pace lab seasons {seasons[0]}-{seasons[-1]}", stats)
    finally:
        con.close()
    print(json.dumps({k: v for k, v in stats.items() if not k.startswith("pbp_")}, default=str))
    return 0


def _fingerprint(con, first: int, last: int) -> str:
    h = hashlib.sha256()
    for sql in ("SELECT season, week, team, offense_plays, neutral_plays, neutral_pass_rate FROM nfl_team_week "
                "WHERE season BETWEEN ? AND ? ORDER BY 1, 2, 3",
                "SELECT season, week, home_team, away_team, home_score, away_score, overtime FROM nfl_schedules "
                "WHERE season BETWEEN ? AND ? AND game_type = 'REG' ORDER BY 1, 2, 3"):
        for row in con.execute(sql, [first, last]).fetchall():
            h.update(repr(row).encode())
    return h.hexdigest()


def cmd_run(args) -> int:
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        seasons = pace_lab.available_seasons(con)
        if not seasons:
            print("no nfl_team_week rows", file=sys.stderr)
            return 1
        games, source = pace_lab.load_games(con, seasons[0], seasons[-1])
        fingerprint = _fingerprint(con, seasons[0], seasons[-1])
        meta = fetch_meta(con)
    finally:
        con.close()
    result = bt.walk_forward(games)
    try:
        nflreadpy = version("nflreadpy")
    except PackageNotFoundError:
        nflreadpy = None
    out = {**result, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "data": {"seasons_stored": seasons, "games": len(games), "overtime_recorded": source["overtime_recorded"],
                    "fingerprint_sha256": fingerprint, "nflreadpy_version": nflreadpy, **meta}}
    text = json.dumps(out, indent=1, sort_keys=True)
    print(text)
    if args.write:
        RESULT.write_text(text + "\n")
        print(f"wrote {RESULT}", file=sys.stderr)
    return 0


def fetch_meta(con) -> dict:
    try:
        row = con.execute("SELECT finished_at, stats FROM sync_runs WHERE source = 'nfl_history' AND status = 'ok' "
                          "ORDER BY finished_at DESC LIMIT 1").fetchone()
    except duckdb.Error:
        return {}
    if not row:
        return {}
    stats = json.loads(row[1]) if row[1] else {}
    return {"ingested_at": str(row[0]), "ingest_nflreadpy_version": stats.get("nflreadpy_version")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("history")
    h.add_argument("--seasons", required=True)
    h.add_argument("--out", type=Path, required=True)
    h.add_argument("--live-season", type=int, default=2026)
    r = sub.add_parser("run")
    r.add_argument("--db", type=Path, required=True)
    r.add_argument("--write", action="store_true")
    args = ap.parse_args()
    return cmd_history(args) if args.cmd == "history" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())

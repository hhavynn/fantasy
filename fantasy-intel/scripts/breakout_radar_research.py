"""Research: the Breakout Radar's walk-forward evaluation (issue #212, research only; see docs/research/breakout_radar.md).

    cd backend
    uv run python ../scripts/sync_nfl_history.py --seasons 2021-2025 --db /path/nfl_history.duckdb   # once
    uv run python ../scripts/breakout_radar_research.py --history-db /path/nfl_history.duckdb --seasons 2021-2025

Builds `player_week_usage` for each season inside the HISTORY database (a research file, never the live one:
app/nfl/history.py refuses to share a path with it, and so does this script), then runs
app.analytics.breakout_radar.evaluate on every season and prints the result as JSON (--out to also write it).
Nothing else is written: no forecast, artifact, registry or production table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import _bootstrap  # noqa: F401

from app.analytics import breakout_radar as br  # noqa: E402
from app.analytics.usage import build_player_week_usage  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.connection import connect, fetch_dicts, init_db, table_exists  # noqa: E402
from app.nfl.history import is_history_db  # noqa: E402

COLUMNS = ("season", "week", "gsis_id", "position", "team", "xfp")


def parse_seasons(text: str) -> list[int]:
    if "-" in text:
        a, b = (int(x) for x in text.split("-", 1))
        return list(range(a, b + 1))
    return [int(x) for x in text.split(",")]


def load(history: Path, seasons: list[int]) -> tuple[list[dict], dict]:
    if history.resolve() == Path(get_settings().db_path).resolve():
        raise SystemExit("refusing to use the live database: pass the research history file (sync_nfl_history.py)")
    con = connect(history)
    try:
        init_db(con)
        if not is_history_db(con):
            raise SystemExit(f"{history} is not an nfl_history database (scripts/sync_nfl_history.py)")
        built = {}
        for season in seasons:
            present = table_exists(con, "player_week_usage") and con.execute(
                "SELECT count(*) FROM player_week_usage WHERE season = ?", [season]).fetchone()[0]
            built[season] = {"rows": present, "built_now": False} if present else {
                "rows": build_player_week_usage(con, season)["rows"], "built_now": True}
        rows = fetch_dicts(con, f"""SELECT {', '.join(COLUMNS)} FROM player_week_usage
                                    WHERE season IN ({', '.join('?' for _ in seasons)}) AND position IN ('RB', 'WR', 'TE')
                                    ORDER BY season, week, gsis_id""", seasons)
        source = con.execute("SELECT max(finished_at) FROM sync_runs WHERE source = 'nfl_history' AND status = 'ok'"
                             ).fetchone()
    finally:
        con.close()
    return rows, {"built": built, "history_synced_at": str(source[0]) if source else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--seasons", default="2021-2025")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    seasons = parse_seasons(args.seasons)
    rows, meta = load(args.history_db, seasons)
    result = {"seasons": seasons, "rows": len(rows), **meta,
              "pooled": br.evaluate(rows),
              "by_season": {s: br.evaluate([r for r in rows if r["season"] == s]) for s in seasons}}
    text = json.dumps(result, indent=1, default=str)
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

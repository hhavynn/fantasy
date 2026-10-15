"""Download finished nflverse seasons into a SEPARATE DuckDB file for forecast research.

    cd backend && uv run python ../scripts/sync_nfl_history.py --seasons 2020-2025
    cd backend && uv run python ../scripts/sync_nfl_history.py --seasons 2020-2025 --db /path/to/nfl_history.duckdb

Never touches the live database (FANTASY_DB_PATH). Rebuilds all tables for exactly the
requested seasons. The default location data/nfl_history.duckdb is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import REPO_ROOT, get_settings
from app.nfl.history import run_history_sync


def default_history_path() -> Path:
    raw = Path(os.getenv("NFL_HISTORY_DB_PATH") or "data/nfl_history.duckdb")
    return raw if raw.is_absolute() else REPO_ROOT / raw


def parse_seasons(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", required=True, help="e.g. 2020-2025 or 2022,2023")
    parser.add_argument("--db", type=Path, default=None, help="History DuckDB path (default data/nfl_history.duckdb)")
    args = parser.parse_args(argv)
    settings = get_settings()
    path = args.db or default_history_path()
    try:
        stats = run_history_sync(path, parse_seasons(args.seasons), settings.nfl_season, settings.db_path)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2
    print(json.dumps(stats, indent=2, default=str))
    print(f"NFL history written -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

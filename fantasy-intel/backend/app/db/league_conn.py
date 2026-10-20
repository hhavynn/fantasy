"""Layered read-only connections for one league (multi-league Phase 2).

One in-memory DuckDB instance per (league file, global file) pair, with both attached READ_ONLY as `lg` and `gl`,
external access off and its configuration locked: nothing but those two files is readable, nothing is writable.
Each request gets its own cursor (a separate DuckDB connection to the instance) with
`search_path = lg.main, gl.main`, so every unqualified table name the engines use resolves to the league's copy
first and the shared global data second.

The perspective team reaches the engines through the column they already read: a cursor-local temporary view
`fantasy_teams` shadows the league table with `is_mine = (team_id = <perspective>)`, or `false` for every team when
the viewer has no perspective. No engine changes; another cursor never sees this one's view.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

_MAX_TEAM_ID = 1_000_000


def perspective_team(raw) -> int | None:
    """The perspective as an integer team id, or None. Anything else (including injection attempts) is None: the
    perspective is a preference, never trusted input."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 7:
        value = int(raw)
    else:
        return None
    return value if 0 <= value < _MAX_TEAM_ID else None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


class LayeredDatabases:
    """The per-process set of open instances, keyed by (league path, global path)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._instances: dict[tuple[str, str], duckdb.DuckDBPyConnection] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._instances)

    def _instance(self, league_path: Path, global_path: Path) -> duckdb.DuckDBPyConnection:
        key = (str(league_path), str(global_path))
        with self._lock:
            inst = self._instances.get(key)
            if inst is None:
                inst = duckdb.connect(":memory:")
                inst.execute(f"ATTACH '{_q(league_path)}' AS lg (READ_ONLY)")
                inst.execute(f"ATTACH '{_q(global_path)}' AS gl (READ_ONLY)")
                inst.execute("SET enable_external_access = false")
                inst.execute("SET lock_configuration = true")
                self._instances[key] = inst
            return inst

    def cursor(self, league_path: Path, global_path: Path, *, perspective: int | None) -> duckdb.DuckDBPyConnection:
        cur = self._instance(league_path, global_path).cursor()
        cur.execute("SET search_path = 'lg.main,gl.main'")
        team = perspective_team(perspective)
        mine = "false" if team is None else f"(team_id = {team})"        # an int we produced: safe to inline
        cur.execute(f"CREATE TEMP VIEW fantasy_teams AS SELECT * REPLACE ({mine} AS is_mine) FROM lg.main.fantasy_teams")
        return cur

    def forget(self, path: Path) -> None:
        """Drop instances using `path` (the file cache evicted it). Cursors already handed out keep working: DuckDB
        keeps the instance alive until its last connection closes, then the file's space is freed."""
        p = str(path)
        with self._lock:
            for key in [k for k in self._instances if p in k]:
                del self._instances[key]

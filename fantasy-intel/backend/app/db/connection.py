"""DuckDB connection + idempotent write helpers.

DuckDB allows one read-write process at a time. Sync scripts fetch everything
over the network first and only then open a short write transaction, and the
API opens short-lived read-only connections, so the two rarely collide.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import duckdb
import pyarrow as pa

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def connect(db_path: Path, read_only: bool = False, retries: int = 20) -> duckdb.DuckDBPyConnection:
    """Open the database, retrying briefly if another process holds the lock."""
    db_path = Path(db_path)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as err:  # lock held by another process
            last_err = err
            time.sleep(0.25)
    raise DatabaseBusy(str(last_err))


class DatabaseBusy(RuntimeError):
    pass


SCHEMA_VERSION = 7

# Derived tables whose definitions changed in each version. Raw ESPN/nflverse tables
# are never dropped; derived ones are rebuilt by re-running the syncs/analytics.
_REBUILD = {
    2: ["player_week_usage", "player_scores", "team_position_needs", "waiver_recommendations",
        "manager_metrics", "manager_classifications", "nfl_team_week", "nfl_redzone_usage"],
    3: ["nfl_pbp_player_week", "nfl_team_week", "player_week_usage", "player_context"],  # FTN code columns renamed
    4: ["player_week_usage"],   # external route columns added; external_* tables are never dropped
    5: [],                      # create-only: espn_projection_history + player_ros_projections
    6: ["team_week_lineup_projections", "team_ros_lineup_projections"],   # V2 lineup tables only
    7: ["independent_projection_runs", "independent_player_projections"],  # participation columns added
}
_V2_REBUILD = _REBUILD[2]


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    legacy = any(table_exists(con, t) for t in _V2_REBUILD)
    version = int(row[0]) if row else (1 if legacy else SCHEMA_VERSION)
    for v, tables in sorted(_REBUILD.items()):
        if version < v:
            for t in tables:
                con.execute(f"DROP TABLE IF EXISTS {t}")
    con.execute(SCHEMA_PATH.read_text())
    con.execute("DELETE FROM schema_meta WHERE key = 'schema_version'")
    con.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)])


def schema_version(con: duckdb.DuckDBPyConnection) -> int | None:
    if not table_exists(con, "schema_meta"):
        return None
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else None


@contextmanager
def open_db(db_path: Path, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(db_path, read_only=read_only)
    try:
        if not read_only:
            init_db(con)
        yield con
    finally:
        con.close()


def _jsonify(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()[0]
    )


def table_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()]


def _rows_to_arrow(con: duckdb.DuckDBPyConnection, table: str, rows: Sequence[dict]) -> pa.Table:
    cols = table_columns(con, table)
    unknown = set().union(*(r.keys() for r in rows)) - set(cols)
    if unknown:
        raise KeyError(f"{table}: unknown columns {sorted(unknown)}")
    data = {c: [_jsonify(r.get(c)) for r in rows] for c in cols if any(c in r for r in rows)}
    return pa.table(data)


def upsert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[dict],
    key_cols: Sequence[str],
) -> int:
    """Replace rows by key: delete matching keys, then insert. Idempotent."""
    if not rows:
        return 0
    seen: dict[tuple, dict] = {}
    for r in rows:  # last write wins for duplicate keys within one batch
        seen[tuple(r.get(k) for k in key_cols)] = r
    rows = list(seen.values())
    tmp = f"_tmp_{uuid.uuid4().hex[:8]}"
    con.register(tmp, _rows_to_arrow(con, table, rows))
    try:
        cond = " AND ".join(f"t.{k} IS NOT DISTINCT FROM s.{k}" for k in key_cols)
        con.execute(f"DELETE FROM {table} t WHERE EXISTS (SELECT 1 FROM {tmp} s WHERE {cond})")
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {tmp}")
    finally:
        con.unregister(tmp)
    return len(rows)


def merge_columns(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[dict],
    key_cols: Sequence[str],
    value_cols: Sequence[str],
) -> int:
    """Set only `value_cols` on rows whose key exists; insert rows whose key is new.
    Every other column of an existing row is left untouched."""
    if not rows:
        return 0
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[tuple(r.get(k) for k in key_cols)] = r
    rows = list(seen.values())
    tmp = f"_tmp_{uuid.uuid4().hex[:8]}"
    con.register(tmp, _rows_to_arrow(con, table, rows))
    try:
        cond = " AND ".join(f"t.{k} IS NOT DISTINCT FROM s.{k}" for k in key_cols)
        sets = ", ".join(f"{c} = s.{c}" for c in value_cols)
        con.execute(f"UPDATE {table} t SET {sets} FROM {tmp} s WHERE {cond}")
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {tmp} s "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {cond})")
    finally:
        con.unregister(tmp)
    return len(rows)


def replace_where(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[dict],
    where_sql: str,
    params: Iterable[Any] = (),
) -> int:
    """Delete a partition (e.g. one season / one scoring period) and insert rows."""
    con.execute(f"DELETE FROM {table} WHERE {where_sql}", list(params))
    if not rows:
        return 0
    tmp = f"_tmp_{uuid.uuid4().hex[:8]}"
    con.register(tmp, _rows_to_arrow(con, table, rows))
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {tmp}")
    finally:
        con.unregister(tmp)
    return len(rows)


def replace_frame_partition(
    con: duckdb.DuckDBPyConnection,
    table: str,
    frame: Any,
    season_col: str | None,
    season: int | None,
) -> int:
    """Store a source dataframe (polars/arrow) as a season partition of `table`.

    Creates the table from the frame's columns on first use. If nflverse adds
    or renames columns the table is rebuilt from the current frame (other
    seasons are dropped with it; we only keep the current season for the MVP).
    """
    arrow = frame.to_arrow() if hasattr(frame, "to_arrow") else frame
    tmp = f"_tmp_{uuid.uuid4().hex[:8]}"
    con.register(tmp, arrow)
    try:
        if table_exists(con, table) and table_columns(con, table) != list(arrow.column_names):
            con.execute(f"DROP TABLE {table}")
        if not table_exists(con, table):
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM {tmp} WHERE false")
        if season_col is None:
            con.execute(f"DELETE FROM {table}")
        else:
            con.execute(f"DELETE FROM {table} WHERE {season_col} = ?", [season])
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM {tmp}")
    finally:
        con.unregister(tmp)
    return arrow.num_rows


class SyncRun:
    """Records a sync run in `sync_runs` (start + finish)."""

    def __init__(self, con: duckdb.DuckDBPyConnection, source: str):
        self.con = con
        self.source = source
        self.run_id = f"{source}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.started_at = utcnow()

    def finish(self, status: str, message: str = "", stats: dict | None = None) -> None:
        upsert_rows(self.con, "sync_runs", [{
            "run_id": self.run_id,
            "source": self.source,
            "started_at": self.started_at,
            "finished_at": utcnow(),
            "status": status,
            "message": message,
            "stats": stats or {},
        }], ["run_id"])


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

"""Copy the local DuckDB file to MotherDuck for the hosted dashboard.

Syncs keep writing the local file; publishing replaces the hosted database with a full copy of it
and stamps `schema_meta.published_at`, which the API uses as its data version (see api/deps.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from .connection import DatabaseBusy, schema_version, utcnow

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_database_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"MotherDuck database name must be letters, digits and underscores: {name!r}")
    return name


def publish(local_path: Path, database: str) -> str:
    """Replace md:<database> with the contents of `local_path`. Needs a read/write MOTHERDUCK_TOKEN.
    Returns the published_at stamp."""
    database = check_database_name(database)
    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(f"No local database at {local_path}. Run the sync scripts first.")
    con = duckdb.connect()
    try:
        try:
            quoted = str(local_path).replace("'", "''")
            con.execute(f"ATTACH '{quoted}' AS local_src (READ_ONLY)")
        except duckdb.IOException as err:  # a sync holds the write lock
            raise DatabaseBusy(str(err))
        con.execute("USE local_src")
        if schema_version(con) is None:
            raise ValueError(f"{local_path} has no fantasy-intel schema. Run the sync scripts first.")
        con.execute("ATTACH 'md:'")
        con.execute(f"CREATE OR REPLACE DATABASE {database} FROM local_src")
        stamp = utcnow().isoformat(timespec="seconds")
        con.execute(f"DELETE FROM {database}.schema_meta WHERE key = 'published_at'")
        con.execute(f"INSERT INTO {database}.schema_meta VALUES ('published_at', ?)", [stamp])
        return stamp
    finally:
        con.close()

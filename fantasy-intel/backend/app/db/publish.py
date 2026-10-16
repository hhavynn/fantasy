"""Copy the local DuckDB file to MotherDuck for the hosted dashboard.

Syncs keep writing the local file; publishing replaces every table of the hosted database with the
local copy in one transaction and stamps `schema_meta.published_at`, which the API uses as its data
version (see api/deps.py).

The hosted database itself is never dropped: a running API holds an attachment to it, and MotherDuck
keeps a dropped database cached in that process ("Catalog ... has been deleted") even across
reconnects, so `CREATE OR REPLACE DATABASE` would break every warm Vercel instance until it recycles.
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


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def replace_tables(con: duckdb.DuckDBPyConnection, source: str, target: str, stamp: str) -> int:
    """Make `target`'s main schema an exact copy of `source`'s, in one transaction: every source table
    is recreated from the source rows, target tables the source no longer has are dropped, and
    schema_meta gets the publish stamp. Readers see either the old or the new data, never a mix.
    Constraints are not copied (the hosted copy is only read). Returns the number of tables."""
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main' "
        "ORDER BY table_name", [source]).fetchall()]
    stale = {r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main'",
        [target]).fetchall()} - set(tables)
    src, dst = _ident(source), _ident(target)
    con.execute("BEGIN TRANSACTION")
    try:
        for t in tables:
            con.execute(f"CREATE OR REPLACE TABLE {dst}.main.{_ident(t)} AS SELECT * FROM {src}.main.{_ident(t)}")
        for t in sorted(stale):
            con.execute(f"DROP TABLE {dst}.main.{_ident(t)}")
        con.execute(f"DELETE FROM {dst}.main.schema_meta WHERE key = 'published_at'")
        con.execute(f"INSERT INTO {dst}.main.schema_meta VALUES ('published_at', ?)", [stamp])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return len(tables)


def publish(local_path: Path, database: str) -> str:
    """Copy `local_path` into md:<database> (created on first use). Needs a read/write MOTHERDUCK_TOKEN.
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
        con.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
        stamp = utcnow().isoformat(timespec="seconds")
        replace_tables(con, "local_src", database, stamp)
        return stamp
    finally:
        con.close()

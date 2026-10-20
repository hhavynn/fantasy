"""Build spike_fi_global and spike_lg_001..N on MotherDuck from a local copy of fantasy.duckdb.

League 001 is uploaded once; 002..N are MotherDuck clones of it (CREATE DATABASE ... FROM), which is how
we'd also seed fixtures cheaply. Writes use the laptop token (the sync identity)."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from pathlib import Path

import duckdb

from common import GLOBAL_DB, LOCAL_DB, N_LEAGUES, connect, league_db, record
from tables import LEAGUE_TABLES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", type=int, default=N_LEAGUES)
    args = ap.parse_args()
    out: dict = {"leagues": args.leagues}
    snap = Path(tempfile.mkdtemp()) / "src.duckdb"
    shutil.copy(LOCAL_DB, snap)  # never hold a lock on the live file
    con = connect("md:", "laptop")
    con.execute(f"ATTACH '{snap}' AS src (READ_ONLY)")
    all_tables = [r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name='src' AND schema_name='main'").fetchall()]
    league = [t for t in all_tables if t in LEAGUE_TABLES]
    glob = [t for t in all_tables if t not in LEAGUE_TABLES]
    out["league_tables"], out["global_tables"] = len(league), len(glob)

    t0 = time.perf_counter()
    con.execute(f"CREATE DATABASE IF NOT EXISTS {GLOBAL_DB}")
    con.execute("BEGIN")
    for t in glob:
        con.execute(f'CREATE OR REPLACE TABLE {GLOBAL_DB}.main."{t}" AS SELECT * FROM src.main."{t}"')
    con.execute(f"CREATE OR REPLACE TABLE {GLOBAL_DB}.main._global_meta AS SELECT 'v1' AS version")
    con.execute("COMMIT")
    out["global_upload_s"] = round(time.perf_counter() - t0, 1)

    first = league_db(1)
    t0 = time.perf_counter()
    con.execute(f"CREATE DATABASE IF NOT EXISTS {first}")
    con.execute("BEGIN")
    for t in league:
        con.execute(f'CREATE OR REPLACE TABLE {first}.main."{t}" AS SELECT * FROM src.main."{t}"')
    con.execute(f"CREATE OR REPLACE TABLE {first}.main._sync_meta (fence BIGINT, data_version BIGINT, "
                "fi_global_version VARCHAR, committed_at TIMESTAMP)")
    con.execute(f"INSERT INTO {first}.main._sync_meta VALUES (0, 0, 'v1', now())")
    con.execute("COMMIT")
    out["league_upload_s"] = round(time.perf_counter() - t0, 1)

    clone_ms, errors = [], []
    for i in range(2, args.leagues + 1):
        t0 = time.perf_counter()
        try:
            con.execute(f"CREATE DATABASE IF NOT EXISTS {league_db(i)} FROM {first}")
        except duckdb.Error as err:
            errors.append({"db": league_db(i), "error": str(err)[:300]})
            if len(errors) > 3:
                break
        clone_ms.append(round((time.perf_counter() - t0) * 1000, 1))
    out["clone_ms"] = clone_ms
    out["clone_errors"] = errors
    sizes = con.execute("SELECT database_name, database_size FROM pragma_database_size() "
                        f"WHERE database_name IN ('{GLOBAL_DB}', '{first}')").fetchall()
    out["sizes"] = sizes
    out["local_league_rows"] = {t: con.execute(f'SELECT count(*) FROM src.main."{t}"').fetchone()[0] for t in league}
    print(record("setup", out))


if __name__ == "__main__":
    main()

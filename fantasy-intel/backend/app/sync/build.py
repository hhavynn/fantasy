"""Build one league's file on the runner (spec §3.5), from payloads already fetched (no network here).

    working DB = a copy of the current global file
               + the league's previous league file (same season): tables a job does not rebuild carry forward
               + the ESPN payloads (write_synced: write_all + injury retrieval log), member SWIDs scrubbed
               + the league's Ours, K and D/ST runs from the global file's reference runs, in its own scoring
                 (app/analytics/rescore.py; derived on every build, not carried forward; a global file published
                 before reference runs existed carries them forward instead, as before)
               + run_analytics (the laptop's pipeline, unchanged)
    -> split_database -> the league file. The runner's copy of the global tables is discarded: readers attach the
       published global file the job read, whose version the pointer names.

Any exception leaves nothing behind but the temporary directory the caller owns; nothing is visible until the
fenced pointer swap (app/db/league_publish.commit_league).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ..analytics.rescore import DERIVED_TABLES, apply_reference
from ..db.connection import connect, init_db, table_exists
from ..db.league_split import LEAGUE_TABLES, REFERENCE_SCORING, split_database
from ..espn.parse import anon_member
from ..espn.sync import EspnPayloads, write_synced
from .raw_store import scrub


class EmptyLeague(RuntimeError):
    """The provider returned the league with no teams for the season: nothing a league page could show, so it is never
    published as ready (issue #167). Classified `dead` / provider_empty (failures.py)."""


@dataclass(frozen=True)
class Built:
    league_path: Path
    league_version: str
    external_id: int
    season: int
    name: str | None


def _q(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _carry_forward(con, previous: Path, season: int, *, skip: frozenset[str] = frozenset()) -> int:
    """Copy the previous league file's rows into the working DB (by column name). A previous file from another
    season starts the league over: a new season is a new league on the provider."""
    con.execute(f"ATTACH '{_q(previous)}' AS prev (READ_ONLY)")
    try:
        row = con.execute("SELECT max(season) FROM prev.main.leagues").fetchone()
        if not row or row[0] != season:
            return 0
        tables = dict(con.execute(
            "SELECT table_name, sql FROM duckdb_tables() WHERE database_name = 'prev' AND schema_name = 'main'").fetchall())
        here = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() AND schema_name = 'main'"
        ).fetchall()}
        copied = 0
        for t in sorted(set(tables) & (LEAGUE_TABLES - skip)):
            if t not in here:          # created at runtime by its own module, not by schema.sql: same definition
                con.execute(tables[t])
            con.execute(f"INSERT INTO main.{_ident(t)} BY NAME SELECT * FROM prev.main.{_ident(t)}")
            copied += 1
        return copied
    finally:
        con.execute("DETACH prev")


def build_league_file(work_dir: Path, *, global_path: Path, previous: Path | None, payloads,
                      owner_swid: str | None, stamp: str) -> Built:
    """`payloads`: EspnPayloads or SleeperPayloads (app/sleeper/write.py). `owner_swid`: the credential owner's SWID
    for a private ESPN league (marks their team as the stored default perspective, as on the laptop), None for a
    public one. `stamp`: the version's UTC time (ISO)."""
    from ..analytics.pipeline import run_analytics

    from ..sleeper.write import SleeperPayloads, write_sleeper

    work_dir = Path(work_dir)
    work = work_dir / "work.duckdb"
    shutil.copyfile(global_path, work)
    sleeper = isinstance(payloads, SleeperPayloads)
    if sleeper:
        external_id, season = int(payloads.league["league_id"]), int(payloads.league["season"])
        name = payloads.league.get("name")
    else:
        scrub(payloads)
        external_id, season = int(payloads.league["id"]), int(payloads.league["seasonId"])
        name = (payloads.league.get("settings") or {}).get("name")
    con = connect(work)
    try:
        init_db(con)
        refs = table_exists(con, REFERENCE_SCORING)
        if previous is not None:
            _carry_forward(con, Path(previous), season, skip=DERIVED_TABLES if refs else frozenset())
        if sleeper:
            write_sleeper(con, payloads)
        else:
            write_synced(con, payloads, swid=anon_member(owner_swid) if owner_swid else None, my_team_id=None)
        if not con.execute("SELECT count(*) FROM fantasy_teams WHERE season = ?", [season]).fetchone()[0]:
            raise EmptyLeague("the provider returned no teams for this league season")
        if refs:
            apply_reference(con, season)
        run_analytics(con, season)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    league_out = work_dir / "league.duckdb"
    out = split_database(work, league_out, work_dir / "global-unused.duckdb", stamp=stamp,
                         league=(external_id, season))
    (work_dir / "global-unused.duckdb").unlink(missing_ok=True)
    work.unlink(missing_ok=True)
    return Built(out.league_path, out.league_version, external_id, season, name)


def league_tables_equal(a: Path, b: Path, *, ignore_columns: frozenset[str], ignore_tables: frozenset[str]) -> list[str]:
    """Tables whose rows differ between two league files, ignoring the given columns and tables (tests and the local
    job-vs-laptop check use it)."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{_q(a)}' AS a (READ_ONLY)")
        con.execute(f"ATTACH '{_q(b)}' AS b (READ_ONLY)")
        names = {d: {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = 'main'", [d]).fetchall()}
            for d in ("a", "b")}
        differ = sorted((names["a"] ^ names["b"]) - ignore_tables)
        for t in sorted((names["a"] & names["b"]) - ignore_tables):
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE database_name = 'a' AND schema_name = 'main' "
                "AND table_name = ? ORDER BY column_index", [t]).fetchall() if r[0] not in ignore_columns]
            sel = ", ".join(_ident(c) for c in cols)
            diff = con.execute(f"SELECT count(*) FROM ((SELECT {sel} FROM a.main.{_ident(t)} EXCEPT ALL "
                               f"SELECT {sel} FROM b.main.{_ident(t)}) UNION ALL (SELECT {sel} FROM b.main.{_ident(t)} "
                               f"EXCEPT ALL SELECT {sel} FROM a.main.{_ident(t)}))").fetchone()[0]
            if diff:
                differ.append(t)
        return differ
    finally:
        con.close()

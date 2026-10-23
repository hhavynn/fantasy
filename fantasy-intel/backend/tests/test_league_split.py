"""Splitting the analytics database into one league file and one global file (Phase 2)."""
import duckdb
import pytest

import json

from app.db.league_split import (BOTH_TABLES, GLOBAL_TABLES, LEAGUE_TABLES, REFERENCE_SCORING, REFERENCE_TABLES,
                                 split_database)
from app.db.connection import init_db
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("split") / "e2e.duckdb")


def _tables(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'").fetchall()}
    finally:
        con.close()


def test_every_schema_table_is_classified_exactly_once(e2e):
    assert not (LEAGUE_TABLES & GLOBAL_TABLES) and not (BOTH_TABLES & (LEAGUE_TABLES | GLOBAL_TABLES))
    unclassified = _tables(e2e) - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES
    assert not unclassified, f"classify these in app/db/league_split.py: {sorted(unclassified)}"


def test_split_reproduces_every_table_in_exactly_one_file(e2e, tmp_path):
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00")
    league, glob, src = _tables(out.league_path), _tables(out.global_path), _tables(e2e)
    refs = {REFERENCE_TABLES[t] for t in src if t in REFERENCE_TABLES} | {REFERENCE_SCORING}
    assert league == (LEAGUE_TABLES | BOTH_TABLES) & src and glob == ((GLOBAL_TABLES | BOTH_TABLES) & src) | refs
    con = duckdb.connect()
    con.execute(f"ATTACH '{e2e}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{out.league_path}' AS lg (READ_ONLY)")
    con.execute(f"ATTACH '{out.global_path}' AS gl (READ_ONLY)")
    for t in sorted(_tables(e2e) - BOTH_TABLES):
        db = "lg" if t in LEAGUE_TABLES else "gl"
        assert con.execute(f'SELECT count(*) FROM (SELECT * FROM src.main."{t}" EXCEPT ALL SELECT * FROM {db}.main."{t}")').fetchone() == (0,), t
        assert con.execute(f'SELECT count(*) FROM src.main."{t}"').fetchone() == con.execute(f'SELECT count(*) FROM {db}.main."{t}"').fetchone(), t


def test_versions_carry_the_file_digest_and_the_stamp(e2e, tmp_path):
    from app.db.r2 import file_sha256
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00")
    from app.db.league_split import VERSION_RE
    assert out.league_version == f"20261020T120000Z-{file_sha256(out.league_path)[:24]}-{out.league_path.stat().st_size}"
    assert out.global_version == f"20261020T120000Z-{file_sha256(out.global_path)[:24]}-{out.global_path.stat().st_size}"
    assert VERSION_RE.fullmatch(out.league_version) and len(out.league_version) <= 64
    con = duckdb.connect(str(out.league_path), read_only=True)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'published_at'").fetchone() == ("2026-10-20T12:00:00",)
    con.close()


def test_league_tables_keep_only_the_requested_league(e2e, tmp_path):
    con = duckdb.connect(str(e2e), read_only=True)
    league_id, season = con.execute("SELECT league_id, season FROM leagues").fetchone()
    con.close()
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00",
                         league=(league_id, season))
    lg = duckdb.connect(str(out.league_path), read_only=True)
    assert lg.execute("SELECT DISTINCT league_id, season FROM fantasy_teams").fetchall() == [(league_id, season)]
    lg.close()
    with pytest.raises(ValueError, match="not in"):
        split_database(e2e, tmp_path / "x.duckdb", tmp_path / "y.duckdb", stamp="2026-10-20T12:00:00", league=(1, 1999))


def test_a_database_with_two_leagues_is_refused_even_when_one_is_named(e2e, tmp_path):
    import shutil
    two = tmp_path / "two.duckdb"
    shutil.copy(e2e, two)
    con = duckdb.connect(str(two))
    con.execute("INSERT INTO leagues SELECT * REPLACE (league_id + 1 AS league_id) FROM leagues")
    con.close()
    for league in (None, con_league(e2e)):
        with pytest.raises(ValueError, match="more than one league"):
            split_database(two, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-20T12:00:00",
                           league=league)


def con_league(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute("SELECT league_id, season FROM leagues").fetchone()
    finally:
        con.close()



def test_every_table_the_nfl_sync_creates_is_global():
    """The NFL sync creates raw nflverse tables on the fly (not in schema.sql); they are league-independent."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "nfl" / "sync.py").read_text()
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if getattr(target, "id", None) == "RAW_DATASETS" and isinstance(node.value, ast.Dict):
            names = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    assert names, "RAW_DATASETS not found in app/nfl/sync.py"
    assert names <= GLOBAL_TABLES, f"classify as global: {sorted(names - GLOBAL_TABLES)}"


def test_every_schema_sql_table_is_classified():
    import re
    from pathlib import Path
    schema = (Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql").read_text()
    created = set(re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\(", schema, re.IGNORECASE))
    assert created
    assert not created - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES, sorted(created - LEAGUE_TABLES - GLOBAL_TABLES - BOTH_TABLES)


def test_content_hash_ignores_bytes_and_row_order_but_not_data(tmp_path):
    from app.db.league_split import content_hash

    def db(name, rows, stamp):
        p = tmp_path / name
        con = duckdb.connect(str(p))
        con.execute("CREATE TABLE schema_meta (key VARCHAR, value VARCHAR)")
        con.execute("INSERT INTO schema_meta VALUES ('published_at', ?), ('schema_version', '7')", [stamp])
        con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?, ?)", rows)
        con.close()
        return p

    a = db("a.duckdb", [(1, "x"), (2, "y")], "2026-10-20T12:00:00")
    b = db("b.duckdb", [(2, "y"), (1, "x")], "2026-10-21T12:00:00")
    c = db("c.duckdb", [(1, "x"), (2, "z")], "2026-10-20T12:00:00")
    assert content_hash(a) == content_hash(b) != content_hash(c)


# ----------------------------------------------------------------------------- reference copies (Phase 4)

def test_the_global_file_carries_reference_copies_of_the_model_runs(e2e, tmp_path):
    out = split_database(e2e, tmp_path / "lg.duckdb", tmp_path / "gl.duckdb", stamp="2026-10-21T00:00:00")
    con = duckdb.connect()
    con.execute(f"ATTACH '{e2e}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{out.global_path}' AS gl (READ_ONLY)")
    for source, ref in REFERENCE_TABLES.items():
        if source not in _tables(e2e):
            continue
        assert con.execute(f'SELECT count(*) FROM (SELECT * FROM src.main."{source}" EXCEPT ALL '
                           f'SELECT * FROM gl.main."{ref}")').fetchone() == (0,), source
        assert con.execute(f'SELECT count(*) FROM src.main."{source}"').fetchone() == \
            con.execute(f'SELECT count(*) FROM gl.main."{ref}"').fetchone(), source
    got = con.execute(f"SELECT season, scoring_items FROM gl.main.{REFERENCE_SCORING}").fetchall()
    want = con.execute("SELECT season, scoring_items FROM src.main.leagues").fetchall()
    assert [(s, json.loads(i)) for s, i in got] == [(s, json.loads(i)) for s, i in want]


def test_reference_tables_hold_no_league_identity(e2e):
    """Columns that identify a league, team or member never reach the global file through a reference copy."""
    forbidden = {"league_id", "team_id", "owner_id", "member_id", "swid", "team_name", "abbrev", "is_mine", "owners"}
    con = duckdb.connect(str(e2e), read_only=True)
    try:
        for source in REFERENCE_TABLES:
            if source in _tables(e2e):
                cols = {r[0] for r in con.execute(f'DESCRIBE "{source}"').fetchall()}
                assert not cols & forbidden, (source, cols & forbidden)
    finally:
        con.close()


def test_a_working_copy_that_already_holds_references_is_split_without_copying_them_twice(e2e, tmp_path):
    first = split_database(e2e, tmp_path / "lg1.duckdb", tmp_path / "gl1.duckdb", stamp="2026-10-21T00:00:00")
    work = tmp_path / "work.duckdb"
    con = duckdb.connect(str(work))
    con.execute(f"ATTACH '{first.global_path}' AS gl (READ_ONLY)")
    con.execute(f"ATTACH '{first.league_path}' AS lg (READ_ONLY)")
    con.execute("COPY FROM DATABASE gl TO work")
    for t in sorted(_tables(first.league_path) - BOTH_TABLES):
        con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM lg.main."{t}"')
    con.close()
    second = split_database(work, tmp_path / "lg2.duckdb", tmp_path / "gl2.duckdb", stamp="2026-10-21T00:00:00")
    a = duckdb.connect(str(second.global_path), read_only=True)
    assert a.execute(f"SELECT count(*) FROM {REFERENCE_SCORING}").fetchone() == (1,)
    a.close()


def test_forecast_input_references_carry_only_player_model_and_run_keys():
    """The forecast-input tables (#225) reach every league file through the global references: they may name a
    player (espn_player_id) and the model and run keys, nothing that identifies a league, team, member or owner."""
    con = duckdb.connect()
    try:
        init_db(con)
        allowed = {
            "independent_projection_models": {"artifact_id", "position", "model_kind", "model_version", "columns",
                                              "means", "stds", "coef", "intercept", "model_sha256"},
            "independent_projection_row_inputs": {"run_id", "espn_player_id", "artifact_id", "position", "model_kind",
                                                  "input_values", "imputed", "mixture_column", "scale", "shift",
                                                  "inputs_version", "inputs_digest", "base_run_id", "changed"},
        }
        for table, cols in allowed.items():
            assert table in REFERENCE_TABLES and table in LEAGUE_TABLES
            got = {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
            assert got == cols, (table, got ^ cols)
    finally:
        con.close()

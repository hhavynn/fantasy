"""Splitting the analytics database into one league file and one global file (Phase 2)."""
import duckdb
import pytest

from app.db.league_split import GLOBAL_TABLES, LEAGUE_TABLES, BOTH_TABLES, split_database
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
    league, glob = _tables(out.league_path), _tables(out.global_path)
    assert league == LEAGUE_TABLES | BOTH_TABLES and glob == GLOBAL_TABLES | BOTH_TABLES
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
    assert out.league_version == "20261020T120000Z-" + file_sha256(out.league_path)[:24]
    assert out.global_version == "20261020T120000Z-" + file_sha256(out.global_path)[:24]
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

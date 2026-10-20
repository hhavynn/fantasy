"""The layered connection: league file + global file attached read-only, league first, perspective as is_mine."""
import duckdb
import pytest

from app.db.league_conn import LayeredDatabases, perspective_team
from app.db.league_split import split_database
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("conn")
    e2e = build_e2e_db(d / "e2e.duckdb")
    out = split_database(e2e, d / "lg.duckdb", d / "gl.duckdb", stamp="2026-10-20T12:00:00")
    return out


def test_unqualified_names_resolve_league_first_then_global(files):
    dbs = LayeredDatabases()
    cur = dbs.cursor(files.league_path, files.global_path, perspective=None)
    assert cur.execute("SELECT count(*) FROM fantasy_teams").fetchone()[0] == 4          # league table
    assert cur.execute("SELECT count(*) FROM nfl_players").fetchone()[0] > 0             # global table
    assert cur.execute("SELECT value FROM schema_meta WHERE key='published_at'").fetchone() == ("2026-10-20T12:00:00",)


def test_the_perspective_team_is_the_only_is_mine_and_cursors_do_not_share_it(files):
    dbs = LayeredDatabases()
    a = dbs.cursor(files.league_path, files.global_path, perspective=2)
    b = dbs.cursor(files.league_path, files.global_path, perspective=3)
    none = dbs.cursor(files.league_path, files.global_path, perspective=None)
    mine = "SELECT team_id FROM fantasy_teams WHERE is_mine"
    assert a.execute(mine).fetchall() == [(2,)] and b.execute(mine).fetchall() == [(3,)]
    assert none.execute(mine).fetchall() == []
    assert len(dbs) == 1, "one instance per file pair"


def test_the_files_are_read_only_and_nothing_else_is_reachable(files, tmp_path):
    cur = LayeredDatabases().cursor(files.league_path, files.global_path, perspective=None)
    for sql in ("INSERT INTO lg.main.fantasy_teams SELECT * FROM lg.main.fantasy_teams",
                "CREATE TABLE lg.main.x (i INT)", f"ATTACH '{tmp_path / 'other.duckdb'}' AS other",
                f"SELECT * FROM read_csv('{tmp_path / 'x.csv'}')", "SET enable_external_access = true"):
        with pytest.raises(duckdb.Error):
            cur.execute(sql)


def test_dropping_an_instance_does_not_break_a_running_cursor(files):
    dbs = LayeredDatabases()
    cur = dbs.cursor(files.league_path, files.global_path, perspective=1)
    dbs.forget(files.league_path)
    assert len(dbs) == 0
    assert cur.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (1,)


@pytest.mark.parametrize("raw,expected", [(None, None), ("6", 6), (6, 6), ("0", 0), ("abc", None), ("6; DROP", None),
                                          ("-1", None), ("99999999999", None), (True, None)])
def test_perspective_team_accepts_only_small_non_negative_integers(raw, expected):
    assert perspective_team(raw) == expected

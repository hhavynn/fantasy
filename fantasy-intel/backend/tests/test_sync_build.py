"""A league file built by a sync job equals what the laptop path produces from the same data (spec §3.5)."""
import shutil
from types import SimpleNamespace

import duckdb
import pytest

from app.db.connection import open_db
from app.db.league_split import split_database
from app.espn.sync import fetch_all, run_espn_sync
from app.sync.build import EmptyLeague, build_league_file, league_tables_equal
from espn_fixture import MY_SWID, SEASON, FakeEspnClient
from golden.golden import build_e2e_db

# wall-clock stamps and per-run ids: different on every run of either path
VOLATILE = frozenset({"synced_at", "detail_synced_at", "projection_synced_at", "run_id", "sync_run_id", "created_at", "computed_at", "started_at", "finished_at",
                      "retrieved_at", "fetch_started_at", "generated_at", "updated_at", "snapshot_date", "as_of", "retrieval_id", "recorded_at"})
IGNORE = frozenset({"sync_runs", "schema_meta"})
# The waiver archive (#346) is compared on its own: its capture time, id and input stamps are per run (its content hash
# is not, and stays compared), while those column names elsewhere must stay compared.
ARCHIVE = "waiver_recommendation_snapshots"
ARCHIVE_VOLATILE = ("captured_at", "snapshot_id", "projection_version", "roster_synced_at")


def archive_rows(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return sorted(con.execute(f"SELECT * EXCLUDE ({', '.join(ARCHIVE_VOLATILE)}) FROM {ARCHIVE}").fetchall(),
                      key=repr)
    finally:
        con.close()


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """The E2E fixture league split into its league and global files: what is published before the job runs."""
    d = tmp_path_factory.mktemp("build")
    db = build_e2e_db(d / "e2e.duckdb")
    out = split_database(db, d / "prev.duckdb", d / "global.duckdb", stamp="2026-10-21T10:00:00",
                         global_stamp="content-addressed")
    return {"db": db, "prev": out.league_path, "global": out.global_path, "dir": d}


def laptop_refresh(base, tmp_path):
    """What the laptop does: sync into the full database, run analytics, split."""
    from app.analytics.pipeline import run_analytics
    db = tmp_path / "laptop.duckdb"
    shutil.copy(base["db"], db)
    run_espn_sync(lambda: open_db(db), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    with open_db(db) as con:
        run_analytics(con, SEASON)
    return split_database(db, tmp_path / "laptop_league.duckdb", tmp_path / "laptop_global.duckdb",
                          stamp="2026-10-21T12:00:00").league_path


def test_a_job_build_equals_the_laptop_refresh_table_by_table(base, tmp_path):
    reference = laptop_refresh(base, tmp_path)
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"],
                              payloads=fetch_all(FakeEspnClient()), owner_swid=MY_SWID, stamp="2026-10-21T12:00:00")
    assert (built.external_id, built.season) == (999, SEASON)
    assert league_tables_equal(reference, built.league_path, ignore_columns=VOLATILE,
                               ignore_tables=IGNORE | {ARCHIVE}) == []
    assert archive_rows(reference) == archive_rows(built.league_path)     # the same captures, carried and appended
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (1,), \
            "the credential owner's team is the stored default perspective, as on the laptop"
        assert "MY_SWID" not in str(con.execute("SELECT * FROM league_members").fetchall())
    finally:
        con.close()


def test_tables_the_job_does_not_rebuild_carry_forward(base, tmp_path):
    con = duckdb.connect(str(base["prev"]), read_only=True)
    try:
        before = con.execute("SELECT count(*) FROM manager_classifications").fetchone()[0]
    finally:
        con.close()
    assert before, "the fixture's previous file has manager classifications"
    payloads = fetch_all(FakeEspnClient())
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"], payloads=payloads,
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM manager_classifications").fetchone()[0] >= before
        assert con.execute("SELECT count(*) FROM fantasy_teams WHERE is_mine").fetchone() == (0,), "public: no owner"
    finally:
        con.close()


def test_ours_kicker_and_dst_runs_come_from_the_global_references_not_the_previous_file(base, tmp_path):
    """The derived tables are rebuilt from the global file on every build: a run only the previous league file has
    (as if published before the references existed) is gone, and the reference runs are there."""
    from app.analytics.rescore import DERIVED_TABLES

    prev = tmp_path / "prev.duckdb"
    shutil.copy(base["prev"], prev)
    con = duckdb.connect(str(prev))
    con.execute("INSERT INTO kicker_projection_runs (run_id, season, week, model_version) "
                "VALUES ('stale-run', 2026, 1, 'kfp-1.1.0')")
    con.close()
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=prev, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect()
    con.execute(f"ATTACH '{built.league_path}' AS lg (READ_ONLY)")
    con.execute(f"ATTACH '{base['global']}' AS gl (READ_ONLY)")
    try:
        assert con.execute("SELECT count(*) FROM lg.kicker_projection_runs WHERE run_id = 'stale-run'").fetchone() == (0,)
        for t in sorted(DERIVED_TABLES):
            have = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'gl'").fetchall()}
            if "ref_" + t in have and t in {r[0] for r in con.execute(
                    "SELECT table_name FROM duckdb_tables() WHERE database_name = 'lg'").fetchall()}:
                assert con.execute(f"SELECT count(*) FROM lg.{t}").fetchone() == \
                    con.execute(f"SELECT count(*) FROM gl.ref_{t}").fetchone(), t
    finally:
        con.close()


def test_a_first_build_needs_no_previous_file(base, tmp_path):
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=None, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM fantasy_teams").fetchone()[0] == 4
        assert {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()} >= {"leagues", "matchups"}
    finally:
        con.close()
    assert sorted(p.name for p in work.iterdir()) == ["league.duckdb"], "only the league file is left"


def test_a_league_the_provider_returns_with_no_teams_is_never_built_as_ready(base, tmp_path):
    """No teams for the season: nothing a league page could show, so the build refuses and nothing is published (the
    worker records provider_empty, which the league's progress step names; issue #167)."""
    payloads = fetch_all(FakeEspnClient())
    payloads.league["teams"] = []
    work = tmp_path / "job"
    work.mkdir()
    with pytest.raises(EmptyLeague):
        build_league_file(work, global_path=base["global"], previous=None, payloads=payloads, owner_swid=None,
                          stamp="2026-10-21T12:00:00")
    assert not (work / "league.duckdb").exists()


def test_a_failed_build_raises_and_publishes_nothing(base, tmp_path):
    payloads = fetch_all(FakeEspnClient())
    del payloads.league["id"]
    work = tmp_path / "job"
    work.mkdir()
    with pytest.raises(KeyError):
        build_league_file(work, global_path=base["global"], previous=base["prev"], payloads=payloads, owner_swid=None,
                          stamp="2026-10-21T12:00:00")
    assert not (work / "league.duckdb").exists()


def test_a_global_file_without_reference_runs_carries_the_previous_ours_forward(base, tmp_path):
    """The switch-over: a global file published before reference runs existed (no ref_league_scoring) leaves the
    previous league file's Ours, K and D/ST runs in place, as builds did before Phase 4."""
    old_global = tmp_path / "old-global.duckdb"
    shutil.copy(base["global"], old_global)
    con = duckdb.connect(str(old_global))
    for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE table_name LIKE 'ref\\_%' ESCAPE '\\'").fetchall():
        con.execute(f'DROP TABLE "{t}"')
    con.close()
    prev = tmp_path / "prev.duckdb"
    shutil.copy(base["prev"], prev)
    con = duckdb.connect(str(prev))
    con.execute("INSERT INTO kicker_projection_runs (run_id, season, week, model_version) "
                "VALUES ('laptop-run', 2026, 1, 'kfp-1.1.0')")
    con.close()
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=old_global, previous=prev, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM kicker_projection_runs WHERE run_id = 'laptop-run'").fetchone() == (1,)
    finally:
        con.close()


def test_a_league_table_created_at_runtime_carries_forward_with_its_definition(base, tmp_path, monkeypatch):
    """A league table created by its own module (not schema.sql) is recreated from the previous file's DDL."""
    from app.db import league_split
    from app.sync import build

    monkeypatch.setattr(league_split, "LEAGUE_TABLES", league_split.LEAGUE_TABLES | {"runtime_league_t"})
    monkeypatch.setattr(build, "LEAGUE_TABLES", league_split.LEAGUE_TABLES)
    prev = tmp_path / "prev.duckdb"
    shutil.copy(base["prev"], prev)
    con = duckdb.connect(str(prev))
    con.execute("CREATE TABLE runtime_league_t (k VARCHAR PRIMARY KEY, q DOUBLE)")
    con.execute("INSERT INTO runtime_league_t VALUES ('a', 0.5), ('b', 0.9)")
    con.close()
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=prev, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T12:00:00")
    con = duckdb.connect(str(built.league_path), read_only=True)
    try:
        assert con.execute("SELECT * FROM runtime_league_t ORDER BY k").fetchall() == [("a", 0.5), ("b", 0.9)]
        assert "PRIMARY KEY" in con.execute("SELECT sql FROM duckdb_tables() WHERE table_name = 'runtime_league_t'"
                                            ).fetchone()[0]
    finally:
        con.close()

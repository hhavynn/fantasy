"""The waiver recommendation archive's league-file contract (issue #346, app/analytics/waiver_archive.py): a league
table that is never derived, append-only, deduplicated by content, carried forward across same-season builds and reset
by a new season, filtered to its league when split, and served under /v2 for the viewer's own team of that league only
(another league's captures, with colliding ids, never cross; naming a team is the same 404 as an unknown league)."""
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from cp_seed import U1, seed_membership, seed_user
from espn_fixture import FakeEspnClient
from golden.golden import E2E_SEASON, build_e2e_db
from league_fixtures import publish_local
from test_league_scope import client, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)
from test_sync_build import base  # noqa: F401  (fixture)

from app.analytics.rescore import DERIVED_TABLES
from app.analytics.waiver_archive import TABLE
from app.db.league_split import (
    BOTH_TABLES,
    GLOBAL_TABLES,
    LEAGUE_TABLES,
    split_database,
)
from app.espn.sync import fetch_all
from app.sync.build import build_league_file

APP = Path(__file__).resolve().parents[1] / "app"
MARK = "Zqarchive"


def rows(path, where="", params=()):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(f"SELECT * FROM {TABLE} {where} ORDER BY ALL", list(params)).fetchall()
    finally:
        con.close()


def test_the_archive_is_a_league_table_and_never_derived():
    assert TABLE in LEAGUE_TABLES and TABLE not in GLOBAL_TABLES | BOTH_TABLES
    assert TABLE not in DERIVED_TABLES, "derived tables are rebuilt, not carried forward: the archive would be lost"


def test_a_schema_upgrade_never_drops_the_archive():
    from app.db.connection import _REBUILD
    assert all(TABLE not in tables for tables in _REBUILD.values())


def test_no_code_path_updates_or_deletes_archived_rows():
    sql = re.compile(rf"(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE|INSERT\s+OR\s+REPLACE\s+INTO)\s+(main\.)?{TABLE}\b",
                     re.IGNORECASE)
    rewrite = re.compile(rf"replace_where\(\s*con\s*,\s*(\"{TABLE}\"|TABLE\b)")    # the module constant or the name
    hits = [f"{p.relative_to(APP)}" for p in [*APP.rglob("*.py"), *APP.rglob("*.sql")]
            if sql.search(p.read_text()) or rewrite.search(p.read_text())]
    assert hits == []


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("archive") / "e2e.duckdb")


def test_an_unchanged_rerun_appends_nothing_and_keeps_earlier_rows_byte_identical(e2e, tmp_path):
    from app.analytics.pipeline import run_analytics
    db = tmp_path / "db.duckdb"
    shutil.copy(e2e, db)
    con = duckdb.connect(str(db))
    try:
        run_analytics(con, int(E2E_SEASON))                       # the fixture changes data after its own run
        before = con.execute(f"SELECT * FROM {TABLE} ORDER BY ALL").fetchall()
        assert before
        assert run_analytics(con, int(E2E_SEASON))["waiver_archive"]["rows"] == 0
        assert con.execute(f"SELECT * FROM {TABLE} ORDER BY ALL").fetchall() == before
        k = con.execute(f"SELECT max(rank), min(archive_k), min(horizon_weeks) FROM {TABLE}").fetchone()
        assert k[0] <= k[1] == 25 and k[2] == 3
    finally:
        con.close()


def test_a_build_carries_the_previous_captures_forward_and_a_new_season_starts_empty(base, tmp_path):  # noqa: F811
    prior = rows(base["prev"])
    assert prior, "the fixture's previous league file has captures"
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"],
                              payloads=fetch_all(FakeEspnClient()), owner_swid=None, stamp="2026-10-21T12:00:00")
    after = rows(built.league_path)
    assert set(prior) <= set(after) and len(after) > len(prior)          # carried byte for byte, then appended
    old = tmp_path / "old_season.duckdb"
    shutil.copy(base["prev"], old)
    con = duckdb.connect(str(old))
    con.execute("UPDATE leagues SET season = season - 1")
    con.close()
    work2 = tmp_path / "job2"
    work2.mkdir()
    fresh = build_league_file(work2, global_path=base["global"], previous=old, payloads=fetch_all(FakeEspnClient()),
                              owner_swid=None, stamp="2026-10-21T13:00:00")
    stamps = {r[4] for r in rows(fresh.league_path)}                       # captured_at
    assert len(stamps) == 1 and not set(prior) & set(rows(fresh.league_path))


def test_rebuilding_from_unchanged_payloads_appends_nothing(base, tmp_path):  # noqa: F811
    """Review #346: each sync has new input stamps (roster and projection sync times); they are provenance, not
    content, so a rebuild whose recommendations are unchanged appends no capture."""
    previous, kept = base["prev"], None
    for i in range(3):
        work = tmp_path / f"job{i}"
        work.mkdir()
        built = build_league_file(work, global_path=base["global"], previous=previous,
                                  payloads=fetch_all(FakeEspnClient()), owner_swid=None, stamp=f"2026-10-21T1{i}:00:00")
        now = rows(built.league_path)
        if kept is not None:
            assert now == kept                         # byte-identical: the earlier rows, and nothing appended
        kept, previous = now, built.league_path


def test_a_league_file_never_carries_another_leagues_captures(e2e, tmp_path):
    db = tmp_path / "two.duckdb"
    shutil.copy(e2e, db)
    con = duckdb.connect(str(db))
    con.execute(f"INSERT INTO {TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, snapshot_id) "
                f"VALUES (424242, {E2E_SEASON}, 3, 1, TIMESTAMP '2026-09-01 00:00:00', 1, '{MARK}')")
    con.close()
    out = split_database(db, tmp_path / "l.duckdb", tmp_path / "g.duckdb", stamp="2026-10-21T12:00:00")
    assert rows(out.league_path, "WHERE league_id = 424242") == [] and rows(out.league_path)


def _league_with_capture(src, path, marker):
    """A copy of the fixture league with Wednesday waivers and one week-2 capture of team 2 well before the cutoff."""
    shutil.copy(src, path)
    con = duckdb.connect(str(path))
    lid = con.execute("SELECT league_id FROM leagues").fetchone()[0]
    con.execute("""UPDATE leagues SET waiver_process_days = '["WEDNESDAY"]'""")
    con.execute(f"INSERT INTO {TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, rank, "
                f"recommendation, availability_status, snapshot_id, content_sha256) VALUES (?, ?, 2, 2, ?, 201, 1, "
                f"'CLAIM', 'FREEAGENT', ?, 'h')", [lid, int(E2E_SEASON), datetime(2026, 8, 20, tzinfo=UTC).replace(tzinfo=None), marker])
    con.close()
    return path


def test_v2_serves_each_leagues_own_captures_for_the_viewers_team_only(pg, served, client, e2e, tmp_path):  # noqa: F811
    a = publish_local(pg, _league_with_capture(e2e, tmp_path / "a.duckdb", "A-capture"), served, tmp_path / "a",
                      visibility="private", name="A")
    b = publish_local(pg, _league_with_capture(e2e, tmp_path / "b.duckdb", f"{MARK}-capture"), served, tmp_path / "b",
                      visibility="private", name="B", stamp="2026-10-20T13:00:00", external_id="1998")
    with pg.connect() as con:
        seed_user(con, U1)
        for lg in (a, b):
            seed_membership(con, U1, lg.league_id, "viewer")
            con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, '2')",
                        [U1, lg.league_id])
        con.commit()
    path = "/api/weekly-recap/waiver-feedback?week=2"
    got = {}
    for name, lg in (("a", a), ("b", b)):
        r = client.get(f"/v2/leagues/{lg.league_id}{path}", headers=bearer(U1))
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["team"]["team_id"] == 2
        got[name] = (body, json.dumps(body))
    assert got["a"][0]["weeks"][0]["capture"]["snapshot_id"] == "A-capture" and MARK not in got["a"][1]
    assert got["b"][0]["weeks"][0]["capture"]["snapshot_id"] == f"{MARK}-capture"
    named = client.get(f"/v2/leagues/{a.league_id}{path}&team_id=1", headers=bearer(U1))
    unknown = client.get(f"/v2/leagues/lg_{'Z' * 22}{path}", headers=bearer(U1))
    assert named.status_code == unknown.status_code == 404 and named.json() == unknown.json()

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
from app.analytics.waiver_archive import ROSTER_TABLE, TABLE
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


def rows(path, where="", params=(), table=TABLE):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(f"SELECT * FROM {table} {where} ORDER BY ALL", list(params)).fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("table", [TABLE, ROSTER_TABLE])
def test_the_archive_is_a_league_table_and_never_derived(table):
    assert table in LEAGUE_TABLES and table not in GLOBAL_TABLES | BOTH_TABLES
    assert table not in DERIVED_TABLES, "derived tables are rebuilt, not carried forward: the archive would be lost"


@pytest.mark.parametrize("table", [TABLE, ROSTER_TABLE])
def test_a_schema_upgrade_never_drops_the_archive(table):
    from app.db.connection import _REBUILD
    assert all(table not in tables for tables in _REBUILD.values())


def test_no_code_path_updates_or_deletes_archived_rows():
    names = f"({TABLE}|{ROSTER_TABLE})"
    sql = re.compile(rf"(UPDATE|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE|INSERT\s+OR\s+REPLACE\s+INTO)\s+(main\.)?{names}\b",
                     re.IGNORECASE)
    rewrite = re.compile(rf"replace_where\(\s*con\s*,\s*(\"{names}\"|(ROSTER_)?TABLE\b)")  # the constants or the names
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
        before = [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in (TABLE, ROSTER_TABLE)]
        assert all(before)
        assert run_analytics(con, int(E2E_SEASON))["waiver_archive"]["rows"] == 0
        assert [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in (TABLE, ROSTER_TABLE)] == before
        orphans = con.execute(f"""SELECT count(*) FROM {ROSTER_TABLE} r ANTI JOIN {TABLE} c USING (snapshot_id)""")
        assert orphans.fetchone() == (0,)                         # every archived roster belongs to a capture
        k = con.execute(f"SELECT max(rank), min(archive_k), min(horizon_weeks) FROM {TABLE}").fetchone()
        assert k[0] <= k[1] == 25 and k[2] == 3
    finally:
        con.close()


def test_a_build_carries_the_previous_captures_forward_and_a_new_season_starts_empty(base, tmp_path):  # noqa: F811
    prior, prior_rosters = rows(base["prev"]), rows(base["prev"], table=ROSTER_TABLE)
    assert prior and prior_rosters, "the fixture's previous league file has captures"
    work = tmp_path / "job"
    work.mkdir()
    built = build_league_file(work, global_path=base["global"], previous=base["prev"],
                              payloads=fetch_all(FakeEspnClient()), owner_swid=None, stamp="2026-10-21T12:00:00")
    after = rows(built.league_path)
    assert set(prior) <= set(after) and len(after) > len(prior)          # carried byte for byte, then appended
    assert set(prior_rosters) < set(rows(built.league_path, table=ROSTER_TABLE))
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
    assert not set(prior_rosters) & set(rows(fresh.league_path, table=ROSTER_TABLE))


def test_rebuilding_from_unchanged_payloads_appends_nothing(base, tmp_path):  # noqa: F811
    """Review #346: each sync has new input stamps (roster and projection sync times); they are provenance, not
    content, so a rebuild whose recommendations are unchanged appends no capture."""
    previous, kept = base["prev"], None
    for i in range(3):
        work = tmp_path / f"job{i}"
        work.mkdir()
        built = build_league_file(work, global_path=base["global"], previous=previous,
                                  payloads=fetch_all(FakeEspnClient()), owner_swid=None, stamp=f"2026-10-21T1{i}:00:00")
        now = (rows(built.league_path), rows(built.league_path, table=ROSTER_TABLE))
        if kept is not None:
            assert now == kept                         # byte-identical: the earlier rows, and nothing appended
        kept, previous = now, built.league_path


def test_the_hash_ignores_sync_stamps_and_the_unarchived_evidence():
    """Codex #353: K/D/ST streaming evidence carries projection_synced_at and a synced_at per week inside components.
    The archive keeps no components (the grading never reads them, and they were most of a row's bytes), so a capture
    whose only change is those stamps, or the input stamps, hashes the same; a changed score does not."""
    from app.analytics.waiver_archive import content_sha256

    def row(stamp, score=3.0):
        return {"rank": 1, "espn_player_id": 9, "recommendation": "CLAIM", "waiver_score": score,
                "projection_version": stamp, "roster_synced_at": stamp, "components": {"decision": {"streaming": {
                    "projection_synced_at": stamp, "weeks": [{"week": 5, "value": 1.0, "synced_at": stamp}]}}}}
    assert content_sha256([row("2026-10-01 10:00:00")]) == content_sha256([row("2026-10-02 11:00:00")])
    assert content_sha256([row("2026-10-01 10:00:00")]) != content_sha256([row("2026-10-01 10:00:00", score=4.0)])
    roster = [{"espn_player_id": 1, "lineup_slot_id": 0}, {"espn_player_id": 2, "lineup_slot_id": 20}]
    assert content_sha256([row("x")], roster) == content_sha256([row("x")], roster[::-1])
    assert content_sha256([row("x")], roster) != content_sha256([row("x")], roster[:1])   # a roster change is new content
    cols = {r[0] for r in duckdb.connect().execute(
        f"{(APP / 'db' / 'schema.sql').read_text()}; DESCRIBE {TABLE}").fetchall()}
    assert not cols & {"components", "reasons"}


def test_a_league_file_never_carries_another_leagues_captures(e2e, tmp_path):
    db = tmp_path / "two.duckdb"
    shutil.copy(e2e, db)
    con = duckdb.connect(str(db))
    con.execute(f"INSERT INTO {TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, snapshot_id) "
                f"VALUES (424242, {E2E_SEASON}, 3, 1, TIMESTAMP '2026-09-01 00:00:00', 1, '{MARK}')")
    con.execute(f"INSERT INTO {ROSTER_TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, snapshot_id) "
                f"VALUES (424242, {E2E_SEASON}, 3, 1, TIMESTAMP '2026-09-01 00:00:00', 1, '{MARK}')")
    con.close()
    out = split_database(db, tmp_path / "l.duckdb", tmp_path / "g.duckdb", stamp="2026-10-21T12:00:00")
    for table in (TABLE, ROSTER_TABLE):
        assert rows(out.league_path, "WHERE league_id = 424242", table=table) == [] and rows(out.league_path, table=table)


def _league_with_capture(src, path, marker):
    """A copy of the fixture league with Wednesday waivers and one week-2 capture of team 2 well before the cutoff."""
    shutil.copy(src, path)
    con = duckdb.connect(str(path))
    lid = con.execute("SELECT league_id FROM leagues").fetchone()[0]
    con.execute("""UPDATE leagues SET waiver_process_days = '["WEDNESDAY"]'""")
    con.execute(f"INSERT INTO {TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, rank, "
                f"recommendation, availability_status, snapshot_id, content_sha256) VALUES (?, ?, 2, 2, ?, 201, 1, "
                f"'CLAIM', 'FREEAGENT', ?, 'h')", [lid, int(E2E_SEASON), datetime(2026, 8, 20, tzinfo=UTC).replace(tzinfo=None), marker])
    con.execute(f"INSERT INTO {ROSTER_TABLE} (league_id, season, week, team_id, captured_at, espn_player_id, "
                f"lineup_slot_id, snapshot_id) VALUES (?, ?, 2, 2, ?, 202, 0, ?)",
                [lid, int(E2E_SEASON), datetime(2026, 8, 20, tzinfo=UTC).replace(tzinfo=None), marker])
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
    assert got["a"][0]["weeks"][0]["roster"]["snapshot_id"] == f"{ROSTER_TABLE}:A-capture"
    assert got["b"][0]["weeks"][0]["capture"]["snapshot_id"] == f"{MARK}-capture"
    named = client.get(f"/v2/leagues/{a.league_id}{path}&team_id=1", headers=bearer(U1))
    unknown = client.get(f"/v2/leagues/lg_{'Z' * 22}{path}", headers=bearer(U1))
    assert named.status_code == unknown.status_code == 404 and named.json() == unknown.json()

"""Injury retrieval log + freshness diagnostics. Synthetic temp DuckDB only; the network fetch is mocked."""

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from app.analytics import injury_freshness as inj
from app.db.connection import connect, init_db, open_db

UTC = timezone.utc
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

# Week 7: BUF hosts MIA on Thursday night, KC hosts DEN on Sunday afternoon.
THU = datetime(2026, 10, 23, 0, 15, tzinfo=UTC)      # Thursday 20:15 ET
SUN = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)
KICKOFFS = {"BUF": THU, "MIA": THU, "KC": SUN, "DEN": SUN}
WED_NOON = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)  # 32h before Thursday, 97h before Sunday


@pytest.fixture
def db(tmp_path):
    c = connect(tmp_path / "inj.duckdb")
    init_db(c)
    yield c
    c.close()


def obs(source=inj.SOURCE_NFLVERSE, team="BUF", week=7, retrieved=WED_NOON, sha="a" * 64, rows=3, pub=None,
        run="r1", scope=None):
    scope = scope or (inj.SCOPE_TEAM_WEEK if source == inj.SOURCE_NFLVERSE else inj.SCOPE_TEAM_CURRENT)
    return inj.Observation(source, scope, 2026, None if source == inj.SOURCE_ESPN else week, team, retrieved, sha,
                           rows, 1 if pub else 0, pub, retrieved - timedelta(seconds=30), run)


def put(db, *observations):
    """Log observations, registering the successful nfl sync that wrote each nflverse one (as run_nfl_sync does)."""
    for o in observations:
        if o.source == inj.SOURCE_NFLVERSE and o.sync_run_id and not db.execute(
                "SELECT count(*) FROM sync_runs WHERE run_id = ?", [o.sync_run_id]).fetchone()[0]:
            at = naive(inj.naive_utc(o.retrieved_at))
            db.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, stats) "
                       "VALUES (?, 'nfl', ?, ?, 'ok', ?)",
                       [o.sync_run_id, at - timedelta(minutes=1), at + timedelta(minutes=10),
                        json.dumps({"season": o.season})])
    return inj.record_observations(db, list(observations))


def injuries(db, rows):
    """rows: (team, week, gsis, report_status, practice_status, date_modified)."""
    db.execute("""CREATE TABLE IF NOT EXISTS nfl_injuries (season INTEGER, game_type VARCHAR, team VARCHAR, week INTEGER,
                  gsis_id VARCHAR, report_status VARCHAR, practice_status VARCHAR, date_modified TIMESTAMP)""")
    for team, week, gsis, rep, prac, mod in rows:
        db.execute("INSERT INTO nfl_injuries VALUES (2026, 'REG', ?, ?, ?, ?, ?, ?)", [team, week, gsis, rep, prac, mod])


def status_of(db, now, team, **kw):
    rows = inj.team_freshness(db, 2026, 7, now, kickoffs=KICKOFFS, **kw)
    return next(r for r in rows if r["team"] == team)


def naive(dt):
    return dt.replace(tzinfo=None)


# ----------------------------------------------------------------------------- the log


def test_table_is_created_by_init_db_and_is_rerunnable(db):
    init_db(db)
    cols = {r[0] for r in db.execute("SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name = 'injury_retrievals'").fetchall()}
    assert {"retrieval_id", "source", "retrieved_at", "content_sha", "row_count", "provider_published_at"} <= cols


def test_same_retrieval_id_is_write_once(db):
    first = obs()
    assert put(db, first) == {"inserted": 1, "ignored": 0}
    # The same observation again (e.g. a retried hook) is ignored, not duplicated or rewritten.
    assert put(db, first) == {"inserted": 0, "ignored": 1}
    assert db.execute("SELECT count(*) FROM injury_retrievals").fetchone()[0] == 1


def test_identical_content_refetch_is_a_new_observation_and_never_overwrites(db):
    a = obs(retrieved=WED_NOON, run="r1")
    b = obs(retrieved=WED_NOON + timedelta(hours=20), run="r2")      # same content sha, later fetch
    put(db, a)
    before = db.execute("SELECT * FROM injury_retrievals").fetchall()
    put(db, b)
    rows = db.execute("SELECT content_sha, retrieved_at, sync_run_id FROM injury_retrievals "
                      "ORDER BY retrieved_at").fetchall()
    assert [r[2] for r in rows] == ["r1", "r2"] and rows[0][0] == rows[1][0]
    assert db.execute("SELECT * FROM injury_retrievals ORDER BY retrieved_at").fetchall()[0] == before[0]


def test_module_only_ever_inserts_into_the_log():
    src = Path(inj.__file__).read_text().upper()
    for forbidden in ("UPDATE INJURY_RETRIEVALS", "DELETE FROM INJURY_RETRIEVALS", "DROP TABLE INJURY_RETRIEVALS",
                      "INSERT OR REPLACE", "ON CONFLICT (RETRIEVAL_ID) DO UPDATE"):
        assert forbidden not in src
    assert "UPDATE {TABLE}" not in src and "DELETE FROM {TABLE}" not in src


def test_publication_time_is_null_when_the_provider_gives_none():
    rows = [{"season": 2026, "week": 7, "team": "BUF", "gsis_id": "g1", "report_status": "Out",
             "date_modified": None, "game_type": "REG"}]
    team = [o for o in inj.nflverse_observations(rows, 2026, retrieved_at=WED_NOON) if o.team == "BUF"][0]
    assert team.provider_published_at is None and team.rows_with_publication_time == 0
    rows.append({**rows[0], "gsis_id": "g2", "date_modified": datetime(2026, 10, 21, 13, 0, tzinfo=UTC)})
    rows.append({**rows[0], "gsis_id": "g3", "date_modified": datetime(2026, 10, 21, 15, 0, tzinfo=UTC)})
    team = [o for o in inj.nflverse_observations(rows, 2026, retrieved_at=WED_NOON) if o.team == "BUF"][0]
    assert team.provider_published_at == naive(datetime(2026, 10, 21, 15, 0, tzinfo=UTC))   # the latest, not imputed
    assert (team.row_count, team.rows_with_publication_time) == (3, 2)
    assert team.retrieved_at == WED_NOON.replace(tzinfo=None) and team.retrieved_at != team.provider_published_at


def test_espn_status_can_never_carry_a_publication_time():
    with pytest.raises(ValueError):
        inj.Observation(inj.SOURCE_ESPN, inj.SCOPE_TEAM_CURRENT, 2026, None, "BUF", WED_NOON, "x", 1, 1,
                        WED_NOON).validate()
    espn = inj.espn_status_observations([{"espn_player_id": 1, "pro_team": "BUF", "injury_status": "OUT"}], 2026,
                                        retrieved_at=WED_NOON)
    assert espn[0].provider_published_at is None and espn[0].week is None


def test_a_missing_retrieval_time_is_refused():
    with pytest.raises(ValueError):
        inj.Observation(inj.SOURCE_NFLVERSE, inj.SCOPE_FEED, 2026, None, None, None, "x", 0).validate()


def test_content_sha_ignores_row_order_and_changes_with_content():
    a = [{"x": 1}, {"x": 2}]
    assert inj.content_sha(a) == inj.content_sha(a[::-1]) != inj.content_sha([{"x": 1}, {"x": 3}])


# ----------------------------------------------------------------------------- sync hooks (mocked fetch)


def _factory(path, drop_log=False):
    @contextmanager
    def open_():
        with open_db(path) as con:
            if drop_log:
                con.execute("DROP TABLE injury_retrievals")
            yield con
    return open_


def _payload():
    from app.nfl.sync import NflPayloads

    t0, t1 = datetime(2026, 10, 21, 15, 59, 20), datetime(2026, 10, 21, 15, 59, 40)
    df = pl.DataFrame({"season": [2026] * 3, "game_type": ["REG"] * 3, "team": ["BUF", "BUF", "KC"],
                       "week": [7, 7, 7], "gsis_id": ["g1", "g2", "g3"], "report_status": ["Out", None, "Questionable"],
                       "date_modified": [datetime(2026, 10, 21, 15, 0, tzinfo=UTC), None, None]},
                      schema_overrides={"date_modified": pl.Datetime("us", "UTC")})
    return NflPayloads(season=2026, frames={"nfl_injuries": df}, fetch_times={"nfl_injuries": (t0, t1)}), t0, t1


def test_nfl_sync_records_the_download_time_not_the_write_time(tmp_path, monkeypatch):
    from app.nfl import sync

    payload, t0, t1 = _payload()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    stats = sync.run_nfl_sync(_factory(tmp_path / "a.duckdb"), 2026)
    assert stats["injury_retrievals"] == {"inserted": 3, "ignored": 0}      # feed + BUF + KC
    con = connect(tmp_path / "a.duckdb", read_only=True)
    rows = con.execute("SELECT scope, team, retrieved_at, fetch_started_at, row_count, provider_published_at, "
                       "sync_run_id FROM injury_retrievals ORDER BY scope, team").fetchall()
    assert {r[2] for r in rows} == {t1} and {r[3] for r in rows} == {t0}     # the fetch's own clock
    by = {(r[0], r[1]): r for r in rows}
    assert by[("team_week", "BUF")][4] == 2 and by[("team_week", "BUF")][5] == naive(datetime(2026, 10, 21, 15, 0, tzinfo=UTC))
    assert by[("team_week", "KC")][5] is None                                # provider supplied none
    assert by[("feed", None)][4] == 3 and all(r[6].startswith("nfl-") for r in rows)
    assert con.execute("SELECT count(*) FROM nfl_injuries").fetchone()[0] == 3   # existing behaviour untouched
    con.close()


def test_resync_of_identical_content_appends_without_overwrite(tmp_path, monkeypatch):
    from app.nfl import sync

    payload, t0, t1 = _payload()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    sync.run_nfl_sync(_factory(tmp_path / "b.duckdb"), 2026)
    later = (t0 + timedelta(hours=6), t1 + timedelta(hours=6))
    payload.fetch_times = {"nfl_injuries": later}
    sync.run_nfl_sync(_factory(tmp_path / "b.duckdb"), 2026)
    con = connect(tmp_path / "b.duckdb", read_only=True)
    assert con.execute("SELECT count(*), count(DISTINCT content_sha), count(DISTINCT retrieved_at) "
                       "FROM injury_retrievals WHERE team = 'BUF'").fetchone() == (2, 1, 2)
    assert con.execute("SELECT min(retrieved_at), max(retrieved_at) FROM injury_retrievals "
                       "WHERE scope = 'feed'").fetchone() == (t1, later[1])
    con.close()


def test_a_log_failure_never_fails_the_sync(tmp_path, monkeypatch):
    from app.nfl import sync

    payload, *_ = _payload()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    stats = sync.run_nfl_sync(_factory(tmp_path / "c.duckdb", drop_log=True), 2026)
    assert any("injury retrieval log failed" in w for w in stats["warnings"])
    con = connect(tmp_path / "c.duckdb", read_only=True)
    assert con.execute("SELECT count(*) FROM nfl_injuries").fetchone()[0] == 3
    con.close()


def test_a_sync_that_dies_after_its_commit_leaves_a_running_row_and_the_view_fails_closed(tmp_path, monkeypatch):
    from app.nfl import sync

    payload, t0, t1 = _payload()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    factory = _factory(tmp_path / "e.duckdb")
    sync.run_nfl_sync(factory, 2026)                                         # run A: ok, logged
    capture, kick = datetime(2030, 1, 1, tzinfo=UTC), {"BUF": datetime(2031, 1, 1, tzinfo=UTC)}
    con = connect(tmp_path / "e.duckdb")
    assert set(inj.manifest_view(con, 2026, 7, capture, kickoffs=kick)["teams"]) == {"BUF"}
    con.close()

    def die(*a, **k):
        raise KeyboardInterrupt          # the process dies after write_all committed; finish('ok') never runs

    monkeypatch.setattr(sync, "record_injury_retrievals", die)
    with pytest.raises(KeyboardInterrupt):
        sync.run_nfl_sync(factory, 2026)                                     # run B
    con = connect(tmp_path / "e.duckdb")
    assert sorted(r[0] for r in con.execute("SELECT status FROM sync_runs WHERE source = 'nfl'").fetchall()) == \
        ["ok", "running"]
    view = inj.manifest_view(con, 2026, 7, capture, kickoffs=kick)
    assert view["teams"] == {} and view["log"].startswith("unverifiable: unfinished nfl sync nfl-")
    con.close()


def test_failed_injury_download_records_nothing(tmp_path, monkeypatch):
    from app.nfl import sync

    payload, *_ = _payload()
    payload.frames.clear()
    payload.fetch_times.clear()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    stats = sync.run_nfl_sync(_factory(tmp_path / "d.duckdb"), 2026)
    assert stats["injury_retrievals"] is None


def test_real_nflverse_sync_output_is_readable_by_team_freshness(tmp_path, monkeypatch):
    """The sync stores the UTC polars Datetime as TIMESTAMP WITH TIME ZONE; fetching that into Python needs
    pytz (not installed). The module must read it as naive UTC in SQL instead."""
    from app.nfl import sync

    payload, _, t1 = _payload()
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    sync.run_nfl_sync(_factory(tmp_path / "tz.duckdb"), 2026)
    con = connect(tmp_path / "tz.duckdb")
    assert con.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'nfl_injuries' "
                       "AND column_name = 'date_modified'").fetchone()[0] == "TIMESTAMP WITH TIME ZONE"
    rows = {r["team"]: r for r in inj.team_freshness(con, 2026, 7, WED_NOON, kickoffs=KICKOFFS)}
    assert rows["BUF"]["source_used"] == "nflverse" and rows["BUF"]["nflverse_designation_synced"]
    assert rows["BUF"]["status"] == inj.FRESH and rows["KC"]["source_used"] == "nflverse"
    assert inj.diagnostics(con, 2026, 7, WED_NOON, kickoffs=KICKOFFS)["summary"]["by_source"]["nflverse"] == 2
    assert inj.plan_refresh(con, 2026, 7, WED_NOON, kickoffs=KICKOFFS)["season"] == 2026
    con.close()


def test_date_modified_after_kickoff_is_post_kickoff_under_a_non_utc_session_timezone(tmp_path, monkeypatch):
    from app.nfl import sync
    from app.nfl.sync import NflPayloads

    t0, t1 = datetime(2026, 10, 21, 15, 59, 20), datetime(2026, 10, 21, 15, 59, 40)
    # BUF modified 3h AFTER its UTC kickoff (a Los Angeles-local reading, UTC-7, would put it 4h before);
    # KC modified 3h BEFORE its UTC kickoff (a local reading would still be before: control).
    df = pl.DataFrame({"season": [2026] * 2, "game_type": ["REG"] * 2, "team": ["BUF", "KC"], "week": [7, 7],
                       "gsis_id": ["g1", "g3"], "report_status": ["Out", "Questionable"],
                       "date_modified": [THU + timedelta(hours=3), SUN - timedelta(hours=3)]},
                      schema_overrides={"date_modified": pl.Datetime("us", "UTC")})
    payload = NflPayloads(season=2026, frames={"nfl_injuries": df}, fetch_times={"nfl_injuries": (t0, t1)})
    monkeypatch.setattr(sync, "fetch_all", lambda season: payload)
    sync.run_nfl_sync(_factory(tmp_path / "la.duckdb"), 2026)
    con = connect(tmp_path / "la.duckdb")
    for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
        con.execute(f"SET TimeZone = '{zone}'")
        rows = {r["team"]: r for r in inj.team_freshness(con, 2026, 7, THU + timedelta(hours=4), kickoffs=KICKOFFS)}
        assert rows["BUF"]["nflverse_report"]["post_kickoff_designations"] == 1, zone
        assert rows["BUF"]["nflverse_report"]["designations"] == 0 and not rows["BUF"]["nflverse_designation_synced"], zone
        assert rows["KC"]["nflverse_report"]["designations"] == 1 and rows["KC"]["source_used"] == "nflverse", zone
    con.close()


def test_espn_sync_records_per_team_status_with_null_publication(db):
    from app.espn.sync import fetch_all, record_injury_status_retrieval, write_all
    from espn_fixture import MY_SWID, FakeEspnClient

    payloads = fetch_all(FakeEspnClient())
    assert payloads.fetch_started_at <= payloads.fetched_at
    write_all(db, payloads, swid=MY_SWID, my_team_id=None)
    res = record_injury_status_retrieval(db, payloads, "espn-run")
    assert res["inserted"] >= 1
    rows = db.execute("SELECT source, scope, week, team, retrieved_at, provider_published_at, rows_with_publication_time "
                      "FROM injury_retrievals").fetchall()
    assert {r[0] for r in rows} == {"espn_status"} and {r[1] for r in rows} == {"team_current"}
    assert {r[2] for r in rows} == {None} and {r[5] for r in rows} == {None} and {r[6] for r in rows} == {0}
    assert {r[4] for r in rows} == {payloads.fetched_at}
    teams = {x[0] for x in db.execute("SELECT DISTINCT pro_team FROM fantasy_players "
                                      "WHERE pro_team IS NOT NULL").fetchall()}
    assert {r[3] for r in rows} == teams and teams


def test_incomplete_espn_snapshot_is_not_logged_and_never_proves_freshness(db):
    """When the pro_players fetch fails, `players` holds only the FA pool and roster rows. Logging a
    per-team retrieval for that would let a team look fresh although most statuses were never fetched."""
    from app.espn.sync import fetch_all, record_injury_status_retrieval, write_all
    from espn_fixture import MY_SWID, FakeEspnClient

    class NoProPlayers(FakeEspnClient):
        def pro_players(self):
            raise RuntimeError("pro players endpoint down")

    payloads = fetch_all(NoProPlayers())
    assert not payloads.pro_players and any("pro players" in w for w in payloads.warnings)
    stats = write_all(db, payloads, swid=MY_SWID, my_team_id=None)
    assert any("injury status retrieval not logged" in w for w in stats["warnings"])
    assert record_injury_status_retrieval(db, payloads, "espn-run") is None
    assert db.execute("SELECT count(*) FROM injury_retrievals").fetchone()[0] == 0
    team = db.execute("SELECT pro_team FROM fantasy_players WHERE pro_team IS NOT NULL LIMIT 1").fetchone()[0]
    row = next(r for r in inj.team_freshness(db, 2026, 7, WED_NOON, kickoffs={team: SUN}) if r["team"] == team)
    assert row["source_used"] == inj.SOURCE_NONE and row["status"] == inj.MISSING

    # control: the same fetch with the pro-player base logs one retrieval per team
    full = fetch_all(FakeEspnClient())
    write_all(db, full, swid=MY_SWID, my_team_id=None)
    assert record_injury_status_retrieval(db, full, "espn-run")["inserted"] >= 1


def test_espn_write_all_alone_logs_nothing(db):
    from app.espn.sync import fetch_all, record_injury_status_retrieval, write_all
    from espn_fixture import MY_SWID, FakeEspnClient

    payloads = fetch_all(FakeEspnClient())
    payloads.fetched_at = None          # a fetch that never finished has no verified retrieval time
    write_all(db, payloads, swid=MY_SWID, my_team_id=None)
    assert record_injury_status_retrieval(db, payloads) is None
    assert db.execute("SELECT count(*) FROM injury_retrievals").fetchone()[0] == 0


# ----------------------------------------------------------------------------- classification


def synced_nflverse(db, team, retrieved, **kw):
    injuries(db, [(team, 7, f"{team}-1", "Out", "Did Not Participate In Practice", naive(retrieved) - timedelta(hours=1))])
    put(db, obs(team=team, retrieved=retrieved, **kw))


def test_thursday_game_is_stale_but_sunday_game_is_fresh_for_the_same_old_retrieval(db):
    old = WED_NOON - timedelta(hours=30)         # retrieved 30h before "now"
    for team in ("BUF", "KC"):
        synced_nflverse(db, team, old, sha=team)
    thu, sun = status_of(db, WED_NOON, "BUF"), status_of(db, WED_NOON, "KC")
    assert (thu["status"], thu["hours_to_kickoff"]) == (inj.STALE, 32.25) and thu["hours_since_retrieval"] == 30.0
    assert sun["status"] == inj.FRESH and sun["reason"] == "kickoff_beyond_window"
    # the same Sunday team becomes stale once kickoff is inside the window
    assert status_of(db, WED_NOON + timedelta(days=1, hours=3), "KC")["status"] == inj.STALE


def test_recent_retrieval_is_fresh_and_thresholds_are_configurable(db):
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=5))
    assert status_of(db, WED_NOON, "BUF")["status"] == inj.FRESH
    assert status_of(db, WED_NOON, "BUF", thresholds=inj.Thresholds(stale_after_hours=4))["status"] == inj.STALE


def test_team_without_any_report_is_missing_never_healthy(db):
    row = status_of(db, WED_NOON, "MIA")
    assert (row["status"], row["source_used"]) == (inj.MISSING, inj.SOURCE_NONE)
    assert row["evidence_retrieved_at"] is None and row["hours_since_retrieval"] is None
    assert inj.summarize(inj.team_freshness(db, 2026, 7, WED_NOON, kickoffs=KICKOFFS))["teams_without_any_report"] == 4


def test_practice_only_rows_are_not_a_designation(db):
    injuries(db, [("MIA", 7, "m1", None, "Limited Participation in Practice", naive(WED_NOON))])
    put(db, obs(team="MIA", retrieved=WED_NOON))     # downloaded, but no designation yet
    row = status_of(db, WED_NOON, "MIA")
    assert row["status"] == inj.MISSING and row["reason"].startswith("practice_report_only")
    assert row["nflverse_report"]["practice_only"] == 1 and row["latest_nflverse_retrieval"]


def test_espn_fallback_is_labeled_espn_status_and_nflverse_wins_when_synced(db):
    put(db, obs(inj.SOURCE_ESPN, team="MIA", retrieved=WED_NOON - timedelta(hours=2)),
        obs(inj.SOURCE_ESPN, team="BUF", retrieved=WED_NOON - timedelta(hours=2)))
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=10))
    mia, buf = status_of(db, WED_NOON, "MIA"), status_of(db, WED_NOON, "BUF")
    assert (mia["source_used"], mia["status"]) == ("espn_status", inj.FRESH)
    assert mia["provider_published_at"] is None and mia["provider_publication_time"] == "null"
    assert buf["source_used"] == "nflverse" and buf["hours_since_retrieval"] == 10.0   # not the fresher ESPN one
    assert buf["espn_fresher_than_unused_nflverse"] is True
    s = inj.summarize(inj.team_freshness(db, 2026, 7, WED_NOON, kickoffs=KICKOFFS))
    assert s["espn_fallback_teams"] == ["MIA"] and s["espn_fallback_without_designations"] == ["MIA"]
    assert s["publication_time"] == {"used_evidence": 2, "with_provider_time": 0, "null_provider_time": 2}


def test_espn_fallback_counts_mapped_designations(db):
    db.execute("INSERT INTO fantasy_players (espn_player_id, pro_team, injury_status) VALUES (1, 'MIA', 'OUT'), "
               "(2, 'MIA', 'QUESTIONABLE'), (3, 'MIA', 'ACTIVE')")
    db.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (1, 'g1', 'high'), "
               "(2, 'g2', 'medium'), (3, 'g3', 'high')")
    put(db, obs(inj.SOURCE_ESPN, team="MIA", retrieved=WED_NOON))
    row = status_of(db, WED_NOON, "MIA")
    assert row["espn_designated_players"] == 1 and row["fallback_adds_no_designations"] is False


def test_espn_designations_exclude_conflicting_crosswalks(db):
    db.execute("INSERT INTO fantasy_players (espn_player_id, pro_team, injury_status) VALUES (1, 'MIA', 'OUT'), "
               "(2, 'MIA', 'OUT'), (3, 'MIA', 'QUESTIONABLE')")
    db.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence, notes) VALUES "
               "(1, 'g1', 'high', 'Conflicting crosswalks'), (2, 'g2', 'high', NULL), (3, 'g3', 'high', 'name match')")
    put(db, obs(inj.SOURCE_ESPN, team="MIA", retrieved=WED_NOON))
    assert status_of(db, WED_NOON, "MIA")["espn_designated_players"] == 2


def test_published_before_kickoff_but_retrieved_after_is_post_kickoff_only(db):
    after = THU + timedelta(hours=2)
    synced_nflverse(db, "BUF", after)               # report dated pregame, but we only fetched it after kickoff
    row = status_of(db, after + timedelta(hours=1), "BUF")
    assert row["status"] == inj.POST_KICKOFF_ONLY and row["pregame_label"] is None
    assert inj.label_pregame(after, THU) == "post_kickoff" and inj.label_pregame(THU - timedelta(minutes=1), THU) == "pregame"
    assert inj.label_pregame(THU, THU) == "post_kickoff" and inj.label_pregame(after, None) == "unknown_kickoff"


def test_designations_modified_after_kickoff_do_not_count_as_a_report(db):
    injuries(db, [("BUF", 7, "b1", "Out", "Did Not Participate In Practice", naive(THU) + timedelta(hours=3))])
    put(db, obs(team="BUF", retrieved=THU + timedelta(hours=3)))
    row = status_of(db, THU + timedelta(hours=4), "BUF")
    assert row["nflverse_report"]["post_kickoff_designations"] == 1 and not row["nflverse_designation_synced"]
    assert row["status"] == inj.POST_KICKOFF_ONLY


def test_started_game_uses_the_last_retrieval_before_kickoff(db):
    synced_nflverse(db, "BUF", THU - timedelta(hours=3))
    put(db, obs(team="BUF", retrieved=THU + timedelta(hours=5), run="late"))      # later re-fetch is ignored
    row = status_of(db, THU + timedelta(hours=6), "BUF")
    assert row["game_started"] and row["status"] == inj.FRESH and row["hours_since_retrieval"] == 3.0
    assert row["pregame_label"] == "pregame"
    assert row["latest_nflverse_retrieval"].startswith("2026-10-23T05:15")        # the late one is visible, not used
    # evidence 30h before kickoff was stale AT kickoff
    synced_nflverse(db, "MIA", THU - timedelta(hours=30))
    assert status_of(db, THU + timedelta(hours=6), "MIA")["status"] == inj.STALE


def test_report_without_logged_retrieval_is_stale_unverified(db):
    injuries(db, [("KC", 7, "k1", "Questionable", "Limited Participation in Practice", naive(WED_NOON))])
    row = status_of(db, WED_NOON, "KC")       # legacy data synced before the log existed
    assert row["status"] == inj.STALE and row["reason"] == "nflverse_report_without_verified_retrieval"


def test_unknown_kickoff_is_never_pregame(db):
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=2))
    row = inj.team_freshness(db, 2026, 7, WED_NOON, kickoffs={"BUF": None})[0]
    assert row["kickoff"] is None and row["status"] == inj.FRESH and row["pregame_label"] == "unknown_kickoff"
    synced_nflverse(db, "KC", WED_NOON - timedelta(hours=40), sha="old")
    assert inj.team_freshness(db, 2026, 7, WED_NOON, kickoffs={"KC": None})[0]["status"] == inj.STALE


# ----------------------------------------------------------------------------- diagnostics + refresh


def schedule(db):
    db.execute("""CREATE TABLE nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, gameday VARCHAR,
                  gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)""")
    db.execute("INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', '2026-10-22', '20:15', 'BUF', 'MIA'), "
               "(2026, 7, 'REG', '2026-10-25', '13:00', 'KC', 'DEN'), (2026, 7, 'REG', '2026-10-25', NULL, 'SEA', 'LV')")


def test_scheduled_kickoffs_use_eastern_clock_and_leave_unknown_as_none(db):
    schedule(db)
    ko = inj.scheduled_kickoffs(db, 2026, 7)
    assert ko["BUF"] == ko["MIA"] == THU and ko["KC"] == SUN and ko["SEA"] is None and "NYJ" not in ko    # bye


def test_diagnostics_lists_upcoming_games_with_missing_or_stale_evidence(db):
    schedule(db)
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=30), sha="s")
    rep = inj.diagnostics(db, 2026, 7, WED_NOON)
    assert rep["log_present"] and rep["summary"]["teams"] == 6
    issues = {i["team"]: i for i in rep["upcoming_issues"]}
    assert issues["BUF"]["status"] == inj.STALE and issues["BUF"]["opponent"] == "MIA"
    assert issues["MIA"]["status"] == inj.MISSING and "SEA" in issues and "KC" in issues
    assert [i["team"] for i in rep["upcoming_issues"]][:2] == ["BUF", "MIA"]      # earliest kickoff first
    assert rep["summary"]["unknown_kickoff"] == ["LV", "SEA"]
    json.dumps(rep)                                                                # serialisable


def test_diagnostics_is_read_only(tmp_path):
    path = tmp_path / "ro.duckdb"
    c = connect(path)
    init_db(c)
    schedule(c)
    c.close()
    ro = connect(path, read_only=True)
    assert inj.diagnostics(ro, 2026, 7, WED_NOON)["summary"]["teams"] == 6
    assert inj.plan_refresh(ro, 2026, 7, WED_NOON, kickoffs=KICKOFFS)["refreshable"]
    ro.close()


def test_diagnostics_without_a_log_table_says_so(tmp_path):
    c = connect(tmp_path / "old.duckdb")
    rep = inj.diagnostics(c, 2026, 7, WED_NOON, kickoffs=KICKOFFS)
    assert rep["log_present"] is False and {t["status"] for t in rep["teams"]} == {inj.MISSING}
    c.close()


def test_refresh_plan_excludes_teams_that_already_kicked_off(db):
    now = THU + timedelta(hours=1)                  # Thursday game is under way
    plan = inj.plan_refresh(db, 2026, 7, now, kickoffs=KICKOFFS)
    assert {r["team"] for r in plan["refreshable"]} == {"KC", "DEN"}
    assert {b["team"] for b in plan["not_refreshable"]} == {"BUF", "MIA"}
    assert all("kicked_off" in b["reason"] for b in plan["not_refreshable"])
    # no ESPN retrieval and no designations: refresh both sources for the missing teams
    assert plan["sources"] == ["espn_status", "nflverse"]


def test_refresh_plan_skips_fresh_teams_and_only_adds_espn_without_designations(db):
    synced_nflverse(db, "KC", WED_NOON - timedelta(hours=2))
    plan = inj.plan_refresh(db, 2026, 7, WED_NOON, kickoffs={"KC": SUN, "BUF": THU})
    assert plan["up_to_date"] == ["KC"] and [r["team"] for r in plan["refreshable"]] == ["BUF"]
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=40), sha="old")
    plan = inj.plan_refresh(db, 2026, 7, WED_NOON, kickoffs={"BUF": THU})
    assert plan["refreshable"][0]["sources"] == ["nflverse"] and plan["sources"] == ["nflverse"]


def test_execute_refresh_calls_only_planned_syncs_and_refuses_when_nothing_to_do(db):
    calls = []
    clock = iter([WED_NOON, WED_NOON + timedelta(minutes=2)])
    synced_nflverse(db, "BUF", WED_NOON - timedelta(hours=40), sha="old")
    plan = inj.plan_refresh(db, 2026, 7, WED_NOON, kickoffs={"BUF": THU})        # nflverse only
    out = inj.execute_refresh(plan, sync_nfl=lambda: calls.append("nfl"), sync_espn=lambda: calls.append("espn"),
                              now_fn=lambda: next(clock))
    assert calls == ["nfl"] and out["labels"] == {"BUF": "pregame"}
    # a refresh that overruns kickoff is reported as post_kickoff, never pregame
    clock = iter([THU - timedelta(minutes=1), THU + timedelta(minutes=3)])
    out = inj.execute_refresh(plan, sync_nfl=lambda: None, sync_espn=lambda: None, now_fn=lambda: next(clock))
    assert out["labels"] == {"BUF": "post_kickoff"}
    done = inj.plan_refresh(db, 2026, 7, THU + timedelta(hours=1), kickoffs={"BUF": THU})
    with pytest.raises(inj.RefreshRefused):
        inj.execute_refresh(done, sync_nfl=lambda: calls.append("x"), sync_espn=lambda: calls.append("x"))
    assert "x" not in calls


def test_execute_refresh_runs_each_source_independently_and_reports_errors(db):
    calls = []

    def nfl_boom():
        calls.append("nfl")
        raise RuntimeError("nflverse down")

    plan = inj.plan_refresh(db, 2026, 7, WED_NOON, kickoffs={"MIA": THU})        # missing: nflverse + espn
    assert plan["sources"] == ["espn_status", "nflverse"]
    clock = iter([WED_NOON, WED_NOON + timedelta(minutes=2)])
    out = inj.execute_refresh(plan, sync_nfl=nfl_boom, sync_espn=lambda: calls.append("espn"),
                              now_fn=lambda: next(clock))
    assert calls == ["nfl", "espn"]                                              # ESPN still ran
    assert out["ran"] == {"nflverse": "error", "espn_status": "ran"}
    assert out["errors"] == {"nflverse": "RuntimeError: nflverse down"}
    assert out["labels"] == {"MIA": "refresh_failed"}                            # never pregame without the source
    calls.clear()
    out = inj.execute_refresh(plan, sync_nfl=lambda: calls.append("nfl"), sync_espn=lambda: 1 / 0)
    assert calls == ["nfl"] and out["errors"] == {"espn_status": "ZeroDivisionError: division by zero"}


# ----------------------------------------------------------------------------- CLI


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("injury_freshness_cli", SCRIPTS / "injury_freshness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli_db(tmp_path):
    path = tmp_path / "cli.duckdb"
    c = connect(path)
    init_db(c)
    schedule(c)
    synced_nflverse(c, "BUF", datetime(2026, 10, 20, 10, 0, tzinfo=UTC), sha="cli")
    c.close()
    return path


def test_cli_report_text_and_json(cli, cli_db, capsys):
    now = "2026-10-21T16:00:00+00:00"
    assert cli.main(["report", "--season", "2026", "--week", "7", "--db", str(cli_db), "--now", now]) == 0
    text = capsys.readouterr().out
    assert "BUF" in text and "stale" in text and "missing" in text and "Upcoming games" in text
    assert cli.main(["report", "--season", "2026", "--week", "7", "--db", str(cli_db), "--now", now, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["teams"] == 6 and data["teams"][0]["team"] == "BUF"


def test_cli_refresh_is_a_dry_run_by_default_and_never_syncs(cli, cli_db, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a sync was called in dry-run mode")

    monkeypatch.setattr("app.nfl.sync.run_nfl_sync", boom)
    monkeypatch.setattr("app.espn.sync.run_espn_sync", boom)
    now = "2026-10-21T16:00:00+00:00"
    assert cli.main(["refresh", "--season", "2026", "--week", "7", "--db", str(cli_db), "--now", now]) == 0
    out = capsys.readouterr().out
    assert "Refresh plan" in out and "dry run" in out
    assert cli.main(["refresh", "--dry-run", "--season", "2026", "--week", "7", "--db", str(cli_db), "--now", now]) == 0


def test_cli_execute_calls_only_the_existing_sync_functions(cli, cli_db, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr("app.nfl.sync.run_nfl_sync", lambda factory, season: calls.append(("nfl", season)))
    monkeypatch.setattr("app.espn.sync.run_espn_sync", lambda factory, settings: calls.append(("espn",)))
    clock = [datetime(2026, 10, 21, 16, 0, tzinfo=UTC)]       # --now is refused with --execute: pin the clock
    monkeypatch.setattr(cli, "_now", lambda args: clock[0])
    assert cli.main(["refresh", "--execute", "--season", "2026", "--week", "7", "--db", str(cli_db)]) == 0
    assert ("nfl", 2026) in calls and ("espn",) in calls
    # after every kickoff nothing is refreshable and --execute refuses
    calls.clear()
    c = connect(cli_db)
    c.execute("UPDATE nfl_schedules SET gametime = '16:25' WHERE gametime IS NULL")     # no unknown kickoffs left
    c.close()
    clock[0] = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    assert cli.main(["refresh", "--execute", "--season", "2026", "--week", "7", "--db", str(cli_db)]) == 2
    assert calls == []


def test_cli_missing_database_exits_with_a_message(cli, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["report", "--week", "7", "--db", str(tmp_path / "nope.duckdb")])


def test_cli_execute_refuses_now_and_runs_nothing(cli, cli_db, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a sync was called")

    monkeypatch.setattr("app.nfl.sync.run_nfl_sync", boom)
    monkeypatch.setattr("app.espn.sync.run_espn_sync", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["refresh", "--execute", "--season", "2026", "--week", "7", "--db", str(cli_db),
                  "--now", "2026-10-21T16:00:00+00:00"])
    assert exc.value.code == 2
    assert "--execute cannot be combined with --now" in capsys.readouterr().err


def test_cli_execute_reports_per_source_errors_and_still_runs_the_other_sync(cli, cli_db, capsys, monkeypatch):
    calls = []

    def nfl_boom(factory, season):
        calls.append("nfl")
        raise RuntimeError("nflverse down")

    monkeypatch.setattr("app.nfl.sync.run_nfl_sync", nfl_boom)
    monkeypatch.setattr("app.espn.sync.run_espn_sync", lambda factory, settings: calls.append("espn"))
    # kickoffs are real-time relative: pin the clock the plan and the labels use
    monkeypatch.setattr(cli, "_now", lambda args: datetime(2026, 10, 21, 16, 0, tzinfo=UTC))
    rc = cli.main(["refresh", "--execute", "--season", "2026", "--week", "7", "--db", str(cli_db)])
    captured = capsys.readouterr()
    assert rc == 1 and calls == ["nfl", "espn"]
    assert "nflverse down" in captured.err and '"errors"' in captured.out


# ----------------------------------------------------------------------------- manifest view / readiness notes


def sync_run(db, run, finished, status="ok", season=2026):
    db.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, stats) "
               "VALUES (?, 'nfl', ?, ?, ?, ?)",
               [run, naive(finished) - timedelta(minutes=5), naive(finished), status, json.dumps({"season": season})])


def test_manifest_view_records_only_pregame_retrievals_of_the_latest_sync_before_the_capture(db):
    put(db, obs(team="BUF", retrieved=WED_NOON - timedelta(hours=5), sha="1" * 64, run="r1"),
        obs(team="BUF", retrieved=WED_NOON, sha="2" * 64, run="r2", pub=WED_NOON - timedelta(hours=2)),
        obs(team="KC", retrieved=WED_NOON, sha="3" * 64, run="r2"),
        obs(team="DEN", retrieved=WED_NOON, sha="4" * 64, run="r2"),
        obs(team="MIA", retrieved=WED_NOON - timedelta(hours=5), sha="5" * 64, run="r1"))   # r2 did not list MIA
    ko = {**KICKOFFS, "KC": WED_NOON, "DEN": None}                   # KC kicks off exactly at the retrieval
    view = inj.manifest_view(db, 2026, 7, WED_NOON + timedelta(minutes=30), kickoffs=ko)
    assert view["log"] == "present" and set(view["teams"]) == {"BUF"}   # not KC (at kickoff), DEN (unknown), MIA (old sync)
    e = view["teams"]["BUF"]
    assert (e["source"], e["content_sha"]) == ("nflverse", "2" * 64) and e["retrieved_at"] == WED_NOON.isoformat()
    assert e["provider_published_at"] == (WED_NOON - timedelta(hours=2)).isoformat()
    assert e["retrieval_id"] == db.execute("SELECT retrieval_id FROM injury_retrievals WHERE content_sha = ?",
                                           ["2" * 64]).fetchone()[0]
    # a capture before the latest sync finished cannot contain its content: nothing is embedded (not r1's rows)
    assert inj.manifest_view(db, 2026, 7, WED_NOON + timedelta(minutes=5), kickoffs=KICKOFFS)["teams"] == {}
    # a failed sync does not count as the one that wrote the table; r2 stays the latest successful one
    sync_run(db, "r3", WED_NOON + timedelta(hours=1), status="error")
    assert set(inj.manifest_view(db, 2026, 7, WED_NOON + timedelta(hours=2), kickoffs=KICKOFFS)["teams"]) == \
        {"BUF", "KC", "DEN"}
    # a newer successful sync whose log write failed (no rows): no older retrieval is attached to its content
    sync_run(db, "r4", WED_NOON + timedelta(hours=3))
    assert inj.manifest_view(db, 2026, 7, WED_NOON + timedelta(hours=4), kickoffs=KICKOFFS)["teams"] == {}


def test_manifest_view_is_read_only_and_marks_an_absent_table(db):
    put(db, obs())
    before = db.execute("SELECT * FROM injury_retrievals").fetchall()
    inj.manifest_view(db, 2026, 7, WED_NOON + timedelta(hours=1), kickoffs=KICKOFFS)
    assert db.execute("SELECT * FROM injury_retrievals").fetchall() == before
    db.execute("DROP TABLE injury_retrievals")
    assert inj.manifest_view(db, 2026, 7, WED_NOON, kickoffs=KICKOFFS) == {
        "schema": "injury-retrievals/v1", "log": "unavailable", "teams": {}}


def test_readiness_notes_report_issues_post_kickoff_and_fallback_as_warnings(db):
    injuries(db, [("BUF", 7, "g1", "Out", None, None)])
    put(db, obs(team="BUF", retrieved=WED_NOON - timedelta(hours=30)),       # stale, Thursday game
        obs(inj.SOURCE_ESPN, team="KC", retrieved=WED_NOON, sha="5" * 64))   # ESPN fallback, no mapped designation
    db.execute("CREATE TABLE IF NOT EXISTS nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, "
               "gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    db.execute("INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', '2026-10-22', '20:15', 'BUF', 'MIA'), "
               "(2026, 7, 'REG', '2026-10-25', '13:00', 'KC', 'DEN')")
    notes = inj.readiness_notes(db, 2026, 7, WED_NOON)
    text = " | ".join(notes["warnings"])
    assert notes["log_available"] and "stale injury evidence" in text and "BUF" in text
    assert notes["fallback_adds_no_designations"] == ["KC"] and "ESPN status fallback adds no designations" in text
    assert "no injury evidence retrieved" in text and "MIA" in text and "DEN" in text
    after = inj.readiness_notes(db, 2026, 7, SUN + timedelta(hours=1))        # everything kicked off
    assert after["upcoming_issues"] == [] and "KC" not in " ".join(w for w in after["warnings"] if "stale" in w)


def test_readiness_notes_absent_table_or_no_connection(db):
    db.execute("DROP TABLE injury_retrievals")
    for con in (db, None):
        notes = inj.readiness_notes(con, 2026, 7, WED_NOON)
        assert notes["log_available"] is False and notes["upcoming_issues"] == []
        assert notes["warnings"] and notes["warnings"][0].startswith("injury retrieval log unavailable")


def test_newer_sync_without_log_rows_makes_older_retrieval_not_fresh_and_warns(db):
    db.execute("CREATE TABLE IF NOT EXISTS nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, "
               "gameday VARCHAR, gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    db.execute("INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', '2026-10-25', '13:00', 'KC', 'DEN')")
    synced_nflverse(db, "KC", WED_NOON - timedelta(hours=2))                  # sync A: designation + logged retrieval
    assert status_of(db, WED_NOON, "KC")["status"] == inj.FRESH
    # sync B (newer, ok) rewrote nfl_injuries but its log write failed: no retrievals at all
    sync_run(db, "rB", WED_NOON - timedelta(hours=1))
    row = status_of(db, WED_NOON, "KC")
    assert row["status"] == inj.STALE and row["reason"] == "nflverse_report_without_verified_retrieval"
    assert row["evidence_retrieved_at"] is None
    notes = inj.readiness_notes(db, 2026, 7, WED_NOON)
    assert notes["nflverse_writer"] == {"run_id": "rB", "verified": True, "reason": None}   # B is the writer ...
    assert any("stale injury evidence" in w for w in notes["warnings"])                      # ... with no retrievals
    # a newer sync of ANOTHER season is not the writer: A's retrieval still vouches for the table
    db.execute("DELETE FROM sync_runs WHERE run_id = 'rB'")
    sync_run(db, "rOther", WED_NOON - timedelta(hours=1), season=2025)
    assert status_of(db, WED_NOON, "KC")["status"] == inj.FRESH
    # an unfinished sync after the writer makes the table unverifiable: never fresh, and the warning says why
    sync_run(db, "rDead", WED_NOON - timedelta(hours=1), status="running")
    row = status_of(db, WED_NOON, "KC")
    assert row["status"] == inj.STALE
    notes = inj.readiness_notes(db, 2026, 7, WED_NOON)
    assert not notes["nflverse_writer"]["verified"]
    assert any("cannot be tied to a logged retrieval" in w and "rDead" in w for w in notes["warnings"])


def test_no_successful_sync_of_the_season_is_never_fresh(db):
    injuries(db, [("KC", 7, "k1", "Out", None, None)])
    put(db, obs(team="KC", retrieved=WED_NOON - timedelta(hours=1), run="r1"))
    db.execute("DELETE FROM sync_runs")
    assert status_of(db, WED_NOON, "KC")["status"] == inj.STALE
    assert not inj.nfl_table_writer(db, 2026)["verified"]

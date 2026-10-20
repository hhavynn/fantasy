"""The drain's maintenance step: scheduled refreshes, visibility probes, recomputes after a new global file, and the
deletion of objects whose retention ran out (spec §2, §3.2, §3.7)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.db.league_publish import DirWriter
from app.sync import maintenance
from cp_seed import seed_league, seed_snapshot

GV_OLD = "20261020T100000Z-" + "a" * 24 + "-100"
GV_NEW = "20261021T100000Z-" + "b" * 24 + "-100"
ET = ZoneInfo("America/New_York")


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def jobs(pg):
    with pg.connect() as su:
        return sorted(su.execute("SELECT league_id, kind FROM fi.sync_jobs WHERE status = 'queued'").fetchall())


def set_global(pg, gv):
    with sync(pg) as con:
        con.execute("INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s) ON CONFLICT (id) "
                    "DO UPDATE SET global_version = EXCLUDED.global_version", [gv, "c" * 24])
        con.commit()


def committed(pg, lid, hours_ago):
    with pg.connect() as su:
        su.execute("UPDATE fi.league_snapshots SET committed_at = now() - %s * interval '1 hour' WHERE league_id = %s",
                   [hours_ago, lid])
        su.commit()


@pytest.mark.parametrize("weekday, interval", [(datetime(2026, 10, 22, 12, tzinfo=ET), 1),    # Thursday
                                               (datetime(2026, 10, 25, 12, tzinfo=ET), 1),    # Sunday
                                               (datetime(2026, 10, 21, 12, tzinfo=ET), 6)])   # Wednesday
def test_refresh_interval_is_shorter_on_game_days(weekday, interval):
    assert maintenance.refresh_interval(weekday).total_seconds() == interval * 3600


def test_due_leagues_get_a_scheduled_refresh_once(pg, monkeypatch):
    monkeypatch.setattr(maintenance, "refresh_interval", lambda now=None: timedelta(hours=6))
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        due, fresh, dormant = seed_league(su), seed_league(su), seed_league(su, status="dormant")
        seed_league(su)                               # no version yet: its initial job is the user's, not ours
        for lid in (due, fresh, dormant):
            seed_snapshot(su, lid, data_version=f"v{lid[-4:]}", global_version=GV_OLD)
        su.commit()
    committed(pg, due, 7)
    committed(pg, dormant, 7)
    with sync(pg) as con:
        first, again = maintenance.enqueue_due(con), maintenance.enqueue_due(con)
    assert jobs(pg) == [(due, "scheduled")]
    assert first["scheduled"] == 1 and again["scheduled"] == 0, "idempotent"


def test_public_leagues_are_probed_every_six_hours(pg):
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        old = seed_league(su, checked_at="7 hours")
        recent = seed_league(su, checked_at="1 hour")
        never = seed_league(su, checked_at=None)
        private = seed_league(su, visibility="private", checked_at="30 hours")
        su.commit()
    with sync(pg) as con:
        maintenance.enqueue_due(con)
    assert jobs(pg) == sorted([(old, "probe"), (never, "probe")])


def test_a_new_global_file_queues_a_recompute_for_each_active_league(pg):
    set_global(pg, GV_OLD)
    with pg.connect() as su:
        a, b = seed_league(su, checked_at="now()"), seed_league(su, checked_at="now()")
        for lid in (a, b):
            seed_snapshot(su, lid, data_version=f"v{lid[-4:]}", global_version=GV_OLD)
        su.commit()
    with sync(pg) as con:
        assert maintenance.enqueue_due(con)["recompute"] == 0
    set_global(pg, GV_NEW)
    with sync(pg) as con:
        assert maintenance.enqueue_due(con)["recompute"] == 2
    assert jobs(pg) == sorted([(a, "recompute"), (b, "recompute")])


def test_expired_leagues_lose_their_objects(pg, tmp_path):
    objects = DirWriter(tmp_path / "objects")
    (tmp_path / "f").write_bytes(b"x")
    with pg.connect() as su:
        gone = seed_league(su, deleted=True)
        kept = seed_league(su)
        su.execute("UPDATE fi.leagues SET deleted_at = now() - interval '8 days' WHERE id = %s", [gone])
        seed_snapshot(su, gone)
        su.commit()
    for lid in (gone, kept):
        objects.put(f"leagues/{lid}/v1.duckdb", tmp_path / "f")
        objects.put(f"raw/{lid}/v1.json.gz", tmp_path / "f")
    with sync(pg) as con:
        assert maintenance.purge_expired(con, objects) == [gone]
        assert maintenance.purge_expired(con, objects) == []
    assert objects.keys(f"leagues/{gone}/") == [] and objects.keys(f"raw/{gone}/") == []
    assert objects.keys(f"leagues/{kept}/") == [f"leagues/{kept}/v1.duckdb"]

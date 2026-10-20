"""Publishing a league: split, upload immutable objects, move the fenced pointer, keep three versions (Phase 2)."""
import shutil

import duckdb
import psycopg
import pytest

from app.db.league_files import global_key, league_key
from app.db import league_publish
from app.db.league_publish import R2Writer, publish_league
from cp_seed import seed_league
from fake_r2 import FakeR2
from golden.golden import build_e2e_db


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    return build_e2e_db(tmp_path_factory.mktemp("pub") / "e2e.duckdb")


@pytest.fixture
def r2():
    fake = FakeR2()
    yield fake
    fake.close()


@pytest.fixture
def league(pg):
    with pg.connect() as con:
        lid = seed_league(con, visibility="private", external_id="999")
        con.commit()
    return lid


def sync_con(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def pointer(pg, lid):
    with pg.connect() as con:
        return con.execute("SELECT object_key, data_version, fence, global_version FROM fi.league_snapshots "
                           "WHERE league_id = %s", [lid]).fetchone()


def touched(src, dst, n):
    shutil.copy(src, dst)
    con = duckdb.connect(str(dst))
    con.execute("UPDATE fantasy_teams SET name = name || ?", [f" v{n}"])
    con.close()
    return dst


def test_first_publish_uploads_both_files_and_inserts_the_pointer(pg, r2, league, e2e):
    with sync_con(pg) as con:
        res = publish_league(e2e, R2Writer(r2.config), con, league, stamp="2026-10-20T12:00:00")
    assert res.applied and res.fence == 1
    assert pointer(pg, league) == (league_key(league, res.data_version), res.data_version, 1, res.global_version)
    assert {league_key(league, res.data_version), global_key(res.global_version)} <= set(r2.objects)
    with pg.connect() as su:
        assert su.execute("SELECT global_version FROM fi.global_snapshot").fetchone() == (res.global_version,), \
            "the laptop publish names the current global file for sync jobs"


def test_republishing_moves_the_pointer_and_keeps_three_versions(pg, r2, league, e2e, tmp_path):
    versions = []
    with sync_con(pg) as con:
        for n in range(5):
            res = publish_league(touched(e2e, tmp_path / f"v{n}.duckdb", n), R2Writer(r2.config), con, league,
                                 stamp=f"2026-10-20T12:0{n}:00")
            versions.append(res.data_version)
    assert pointer(pg, league)[1] == versions[-1] and pointer(pg, league)[2] == 5
    kept = sorted(k for k in r2.objects if k.startswith(f"leagues/{league}/"))
    assert kept == sorted(league_key(league, v) for v in versions[-3:])
    with pg.connect() as su:
        assert su.execute("SELECT count(*) FROM fi.league_snapshot_history WHERE league_id = %s", [league]).fetchone() == (3,)
    globals_ = [k for k in r2.objects if k.startswith("global/")]
    assert len(globals_) == 1, "unchanged global data is one object, uploaded once"


def test_a_superseded_publisher_changes_nothing_visible(pg, r2, league, e2e, tmp_path):
    with sync_con(pg) as con:
        first = publish_league(e2e, R2Writer(r2.config), con, league, stamp="2026-10-20T12:00:00")
    with pg.connect() as su:                 # a newer worker already published with a higher fence
        su.execute("UPDATE fi.league_snapshots SET fence = 99 WHERE league_id = %s", [league])
        su.execute("UPDATE fi.leagues SET sync_fence = 1 WHERE id = %s", [league])   # stale counter on this side
        su.commit()
    with sync_con(pg) as con:
        stale = publish_league(touched(e2e, tmp_path / "late.duckdb", 1), R2Writer(r2.config), con, league,
                               stamp="2026-10-20T12:05:00")
    assert not stale.applied
    assert pointer(pg, league)[1] == first.data_version
    assert league_key(league, stale.data_version) not in r2.objects, "the orphan is collected"


def test_the_publisher_role_cannot_touch_people_tables_and_unknown_leagues_are_refused(pg, r2, e2e):
    with sync_con(pg) as con:
        with pytest.raises(ValueError, match="not registered"):
            publish_league(e2e, R2Writer(r2.config), con, "lg_" + "q" * 24, stamp="2026-10-20T12:00:00")
    with sync_con(pg) as con, pytest.raises(psycopg.errors.InsufficientPrivilege):
        con.execute("SELECT count(*) FROM fi.league_memberships")



def test_a_recent_unreferenced_global_is_kept_and_an_old_one_is_collected(pg, r2, league, e2e):
    """Another publisher's freshly uploaded global is unreferenced until its pointer moves: never collect it early."""
    from datetime import timedelta
    from app.db.connection import utcnow
    fmt = "%Y%m%dT%H%M%SZ"
    now = utcnow()
    other_new = global_key((now - timedelta(hours=2)).strftime(fmt) + "-" + "a" * 24 + "-100")   # recent, not newest
    other_old = global_key((now - timedelta(days=3)).strftime(fmt) + "-" + "b" * 24 + "-100")
    r2.objects[other_new] = b"x" * 100
    r2.objects[other_old] = b"y" * 100
    with sync_con(pg) as con:     # this publish's own global is stamped now: the newest key
        publish_league(e2e, R2Writer(r2.config), con, league, stamp=now.isoformat(timespec="seconds"))
    assert other_new in r2.objects and other_old not in r2.objects


def test_an_overlapping_older_publisher_never_overwrites_a_newer_one(pg, r2, league, e2e, tmp_path, monkeypatch):
    """The fence is reserved when a publish starts: a publisher that started first and finishes last is stale."""
    real_split = league_publish.split_database
    newer = {}

    def split_then_let_a_newer_publish_finish(*args, **kwargs):
        if not newer:
            newer["src"] = touched(e2e, tmp_path / "newer.duckdb", 2)
            monkeypatch.setattr(league_publish, "split_database", real_split)
            with sync_con(pg) as other:
                newer["res"] = publish_league(newer["src"], R2Writer(r2.config), other, league,
                                              stamp="2026-10-20T12:01:00")
        return real_split(*args, **kwargs)

    monkeypatch.setattr(league_publish, "split_database", split_then_let_a_newer_publish_finish)
    with sync_con(pg) as con:
        older = publish_league(touched(e2e, tmp_path / "older.duckdb", 1), R2Writer(r2.config), con, league,
                               stamp="2026-10-20T12:00:00")
    assert newer["res"].applied and not older.applied
    assert older.fence < newer["res"].fence
    assert pointer(pg, league)[1] == newer["res"].data_version
    assert league_key(league, older.data_version) not in r2.objects, "the stale publisher removes its own object"
    assert league_key(league, newer["res"].data_version) in r2.objects


def test_an_unreferenced_league_object_newer_than_the_pointer_is_kept(pg, r2, league, e2e):
    """Another publisher's league object is unreferenced until its pointer moves: collect only older ones."""
    with sync_con(pg) as con:
        res = publish_league(e2e, R2Writer(r2.config), con, league, stamp="2026-10-20T12:00:00")
    in_flight = league_key(league, "20261020T120500Z-" + "c" * 24 + "-100")
    stale = league_key(league, "20261020T115500Z-" + "d" * 24 + "-100")
    r2.objects[in_flight] = r2.objects[stale] = b"z" * 100
    with sync_con(pg) as con:
        league_publish._collect(R2Writer(r2.config), con, league)
    assert in_flight in r2.objects and stale not in r2.objects
    assert league_key(league, res.data_version) in r2.objects

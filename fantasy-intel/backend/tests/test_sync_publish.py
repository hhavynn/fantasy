"""A job's publish is the fenced pointer swap with the fence it got at claim time (spec §3.4): stale fences publish
nothing, a new league's first publish inserts its pointer, racing first publishes keep the higher fence, and a
failure before the swap changes nothing visible."""
import psycopg
import pytest

from app.db.league_files import league_key
from app.db.league_publish import DirWriter
from app.sync import queue
from app.sync.build import Built
from app.sync.publish import publish_job, raw_key
from cp_seed import seed_league

GV = "20261021T100000Z-" + "a" * 24 + "-100"


def sync(pg):
    return psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None)


def pointer(pg, lid):
    with pg.connect() as su:
        return su.execute("SELECT data_version, fence FROM fi.league_snapshots WHERE league_id = %s", [lid]).fetchone()


def fake_built(tmp_path, n: int) -> Built:
    path = tmp_path / f"league{n}.duckdb"
    path.write_bytes(b"x" * (100 + n))
    return Built(path, f"20261021T12000{n}Z-{n:024x}-{100 + n}", 999, 2026, "League")


@pytest.fixture
def lg(pg):
    with pg.connect() as su:
        lid = seed_league(su)
        su.commit()
    return lid


def claim_one(con, lid, kind="initial"):
    queue.enqueue(con, lid, kind, key=f"{kind}:{lid}")
    return queue.claim(con, "w1")


def test_a_new_leagues_first_publish_inserts_its_pointer_and_raw_payloads(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        c = claim_one(con, lg)
        built = fake_built(tmp_path, 1)
        applied, _ = publish_job(writer, con, c, built, raw=b"payloads", global_version=GV)
    assert applied and pointer(pg, lg) == (built.league_version, c.fence)
    assert (tmp_path / "objects" / league_key(lg, built.league_version)).exists()
    assert (tmp_path / "objects" / raw_key(lg, built.league_version)).read_bytes() == b"payloads"


def test_a_stale_fence_publishes_nothing_and_removes_its_objects(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        old = claim_one(con, lg)
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [old.job_id])
        con.commit()
        new = queue.claim(con, "w2")
        first = fake_built(tmp_path, 2)
        assert publish_job(writer, con, new, first, raw=b"new", global_version=GV)[0]
        late = fake_built(tmp_path, 3)
        applied, _ = publish_job(writer, con, old, late, raw=b"old", global_version=GV)
    assert not applied and pointer(pg, lg) == (first.league_version, new.fence)
    assert not (tmp_path / "objects" / league_key(lg, late.league_version)).exists()
    assert not (tmp_path / "objects" / raw_key(lg, late.league_version)).exists()


def test_racing_first_publishes_keep_the_higher_fence(pg, lg, tmp_path):
    """Two workers both hold an initial job's fence (the first lost its lease mid-build); whichever commits last,
    the pointer ends on the higher fence."""
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        low = claim_one(con, lg)
        con.execute("UPDATE fi.sync_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [low.job_id])
        con.commit()
        high = queue.claim(con, "w2")
        assert high.fence > low.fence
        hb = fake_built(tmp_path, 4)
        lb = fake_built(tmp_path, 5)
        assert publish_job(writer, con, high, hb, raw=None, global_version=GV)[0]
        assert not publish_job(writer, con, low, lb, raw=None, global_version=GV)[0]
    assert pointer(pg, lg) == (hb.league_version, high.fence)


def test_a_failure_before_the_swap_leaves_the_pointer_unchanged(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        c = claim_one(con, lg)
        first = fake_built(tmp_path, 6)
        publish_job(writer, con, c, first, raw=None, global_version=GV)
        queue.complete(con, c)
        c2 = claim_one(con, lg, "manual")

    class Broken(DirWriter):
        def put(self, key, path):
            raise OSError("disk full")

    with sync(pg) as con, pytest.raises(OSError):
        publish_job(Broken(tmp_path / "objects"), con, c2, fake_built(tmp_path, 7), raw=b"r", global_version=GV)
    assert pointer(pg, lg) == (first.league_version, c.fence)


def test_raw_payloads_follow_their_league_version_out(pg, lg, tmp_path):
    writer = DirWriter(tmp_path / "objects")
    with sync(pg) as con:
        versions = []
        for n in range(5):
            c = claim_one(con, lg, "manual")
            b = fake_built(tmp_path, n)
            publish_job(writer, con, c, b, raw=b"r%d" % n, global_version=GV)
            queue.complete(con, c)
            versions.append(b.league_version)
    kept = sorted(p.name for p in (tmp_path / "objects" / "raw" / lg).iterdir())
    assert kept == sorted(f"{v}.json.gz" for v in versions[-3:])

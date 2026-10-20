"""The per-instance cache of league and global files: verified, atomic, downloaded once, byte-bounded."""
import hashlib
import threading

import pytest

from app.db.league_files import DirStore, LeagueFileCache, ObjectKeyError, R2Store, global_key, league_key
from app.db.r2 import R2Error
from fake_r2 import FakeR2

LG = "lg_" + "a" * 24


def blob(n: int, seed: int) -> bytes:
    return (hashlib.sha256(str(seed).encode()).digest() * (n // 32 + 1))[:n]


def version_of(data: bytes, stamp="20261020T120000Z") -> str:
    return f"{stamp}-{hashlib.sha256(data).hexdigest()[:24]}-{len(data)}"


@pytest.fixture
def r2():
    fake = FakeR2()
    yield fake
    fake.close()


def put(fake, key, data):
    fake.objects[key] = data


def test_keys_are_derived_only_from_validated_ids_and_versions():
    v = version_of(b"x" * 10)
    assert league_key(LG, v) == f"leagues/{LG}/{v}.duckdb"
    assert global_key(v) == f"global/{v}.duckdb"
    for bad_lg, bad_v in ((LG + "/../x", v), ("lg_short", v), (LG, "../../etc"), (LG, v + "/x"), (LG, "2026-x")):
        with pytest.raises(ObjectKeyError):
            league_key(bad_lg, bad_v)


def test_downloads_once_verifies_and_serves_the_same_path(r2, tmp_path):
    data = blob(5000, 1)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=1 << 20)
    p1 = cache.league_file(LG, v)
    p2 = cache.league_file(LG, v)
    assert p1 == p2 and p1.read_bytes() == data
    assert [m for m, k, _ in r2.requests if m == "GET"] == ["GET"]
    assert not list((tmp_path / "c").glob("*.part"))


def test_concurrent_first_requests_download_once(r2, tmp_path):
    data = blob(200_000, 2)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=10 << 20)
    paths, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        paths.append(cache.league_file(LG, v))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(paths)) == 1 and len([1 for m, k, _ in r2.requests if m == "GET"]) == 1


@pytest.mark.parametrize("tamper", ["content", "size"])
def test_a_corrupt_or_wrong_size_object_is_rejected_and_nothing_is_cached(r2, tmp_path, tamper):
    data = blob(5000, 3)
    v = version_of(data)
    put(r2, league_key(LG, v), data[:-1] + b"!" if tamper == "content" else data + b"extra")
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=1 << 20)
    with pytest.raises(R2Error):
        cache.league_file(LG, v)
    assert list((tmp_path / "c").iterdir()) == [] and cache.bytes_used == 0


def test_the_lru_never_exceeds_its_bound_under_a_20_league_mix(r2, tmp_path):
    import random
    rng = random.Random(7)
    leagues = []
    for i in range(20):
        lg = f"lg_{i:024d}"
        data = blob(rng.randint(30_000, 90_000), i)
        v = version_of(data)
        put(r2, league_key(lg, v), data)
        leagues.append((lg, v))
    bound = 300_000
    evicted = []
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=bound, on_evict=evicted.append)
    for _ in range(200):
        lg, v = rng.choice(leagues)
        p = cache.league_file(lg, v)
        assert p.exists()
        on_disk = sum(f.stat().st_size for f in (tmp_path / "c").iterdir())
        assert on_disk <= bound and cache.bytes_used == on_disk
    assert evicted, "the mix is larger than the bound, so something must have been evicted"


def test_a_file_larger_than_the_whole_cache_is_refused(r2, tmp_path):
    data = blob(5000, 4)
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    with pytest.raises(ValueError, match="larger than the cache"):
        LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=4000).league_file(LG, v)


def test_dir_store_serves_local_objects_with_the_same_checks(tmp_path):
    data = blob(4000, 5)
    v = version_of(data)
    src = tmp_path / "objects" / f"global/{v}.duckdb"
    src.parent.mkdir(parents=True)
    src.write_bytes(data)
    cache = LeagueFileCache(DirStore(tmp_path / "objects"), tmp_path / "c", max_bytes=1 << 20)
    assert cache.global_file(v).read_bytes() == data
    src.write_bytes(data[:-1] + b"?")
    bad = version_of(b"other")
    with pytest.raises(R2Error):
        cache.global_file(bad)

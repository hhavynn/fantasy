"""The per-instance cache of league and global files: verified, atomic, downloaded once, byte-bounded."""
import hashlib
import threading

import pytest

from app.db.league_files import CacheFull, DirStore, LeagueFileCache, ObjectKeyError, R2Store, global_key, league_key
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
    assert not list(cache.cache_dir.glob("*.part"))


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
    assert list(cache.cache_dir.iterdir()) == [] and cache.bytes_used == 0


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
        on_disk = sum(f.stat().st_size for f in cache.cache_dir.iterdir())
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



class SlowStore:
    """Writes the object, then waits for a release, so concurrent downloads overlap."""
    def __init__(self, objects, gate):
        self.objects, self.gate, self.peak, self.cache = objects, gate, 0, None

    def fetch(self, key, dest, *, sha256_prefix, size):
        dest.with_name(dest.name + ".part").write_bytes(self.objects[key])
        self.peak = max(self.peak, sum(f.stat().st_size for f in self.cache.cache_dir.iterdir()))
        self.gate.wait(5)
        dest.with_name(dest.name + ".part").replace(dest)


def test_concurrent_first_downloads_stay_under_the_bound_together(tmp_path):
    objects, keys = {}, []
    for i in range(4):
        data = blob(40_000, 10 + i)
        v = version_of(data)
        objects[league_key(f"lg_{i:024d}", v)] = data
        keys.append((f"lg_{i:024d}", v))
    gate = threading.Event()
    store = SlowStore(objects, gate)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=100_000)
    store.cache = cache
    errors = []

    def go(lg, v):
        try:
            cache.league_file(lg, v)
        except CacheFull:
            errors.append(lg)

    threads = [threading.Thread(target=go, args=k) for k in keys]
    [t.start() for t in threads]
    import time
    time.sleep(0.3)
    assert cache.bytes_used <= 100_000, "reservations must keep in-flight downloads under the bound"
    gate.set()
    [t.join() for t in threads]
    assert store.peak <= 100_000 and cache.bytes_used <= 100_000
    assert errors, "with every slot reserved, the extra downloads are refused rather than overshooting"


def test_a_pinned_file_is_never_evicted_and_a_full_cache_refuses(r2, tmp_path):
    datas = [blob(60_000, 20 + i) for i in range(3)]
    keys = []
    for i, data in enumerate(datas):
        v = version_of(data)
        put(r2, league_key(f"lg_{i:024d}", v), data)
        keys.append((f"lg_{i:024d}", v))
    evicted = []
    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=130_000, on_evict=evicted.append)
    a = cache.pin_league(*keys[0])
    cache.league_file(*keys[1])
    cache.league_file(*keys[2])                       # needs room: evicts b, never the pinned a
    assert a.exists() and evicted and a not in evicted
    b = cache.pin_league(*keys[1])                    # re-download b, pinned
    with pytest.raises(CacheFull):
        cache.pin_league(*keys[2])                    # a and b pinned (120k), c (60k) cannot fit
    cache.release(a)
    assert cache.pin_league(*keys[2]).exists()
    cache.release(b)


def test_processes_sharing_a_directory_keep_their_own_files(tmp_path):
    import os
    alive = tmp_path / "c" / f"p{os.getppid()}"      # another live process's directory
    alive.mkdir(parents=True)
    (alive / "x.duckdb").write_bytes(b"theirs")
    dead = tmp_path / "c" / "p999999"                 # no such process
    dead.mkdir()
    (dead / "y.duckdb").write_bytes(b"stale")
    LeagueFileCache(DirStore(tmp_path / "objects"), tmp_path / "c", max_bytes=1 << 20)
    assert (alive / "x.duckdb").exists() and not dead.exists()


def test_a_key_being_evicted_is_not_fetched_again_until_its_file_is_gone(r2, tmp_path):
    """A request for a key mid-eviction waits; otherwise its fresh copy (same local path) would be unlinked."""
    datas = [blob(60_000, 40 + i) for i in range(2)]
    keys = []
    for i, data in enumerate(datas):
        v = version_of(data)
        put(r2, league_key(f"lg_{i:024d}", v), data)
        keys.append((f"lg_{i:024d}", v))
    seen = {}

    def refetch():
        try:
            seen["path"] = cache.pin_league(*keys[0])
        except CacheFull:
            seen["full"] = True

    def on_evict(path):
        t = seen["thread"] = threading.Thread(target=refetch)
        t.start()
        t.join(0.3)
        seen["waited"] = t.is_alive()             # still waiting for this eviction to finish

    cache = LeagueFileCache(R2Store(r2.config), tmp_path / "c", max_bytes=130_000, on_evict=on_evict)
    cache.league_file(*keys[0])
    b = blob(80_000, 50)
    put(r2, league_key("lg_" + "b" * 24, version_of(b)), b)
    cache.league_file("lg_" + "b" * 24, version_of(b))      # 60k + 80k > 130k: evicts a
    seen["thread"].join(5)
    assert seen["waited"]
    assert seen.get("full") or seen["path"].exists(), "never a pinned path that was unlinked"
    assert cache.bytes_used <= 130_000


def test_concurrent_downloads_of_one_object_by_two_caches_never_share_a_temporary_file(r2, tmp_path):
    """Two processes (or a cache inherited across a fork) fetching the same object at once: each download has its
    own temporary name, so neither truncates or deletes the other's partial file."""
    import threading
    data = blob(40_000_000, 99)                     # large enough for parallel ranged parts
    v = version_of(data)
    put(r2, league_key(LG, v), data)
    dest = tmp_path / "same.duckdb"
    errors, barrier = [], threading.Barrier(4)

    def fetch():
        try:
            barrier.wait()
            R2Store(r2.config).fetch(league_key(LG, v), dest, sha256_prefix=v.split("-")[1], size=len(data))
        except Exception as err:   # noqa: BLE001 - collected below
            errors.append(repr(err))

    threads = [threading.Thread(target=fetch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and dest.read_bytes() == data


def test_the_file_cache_and_layered_databases_are_per_process(monkeypatch, tmp_path):
    """A forked worker must not reuse its parent's cache directory or DuckDB instances."""
    from app.api import deps, league_scope
    monkeypatch.setenv("LEAGUE_FILES_DIR", str(tmp_path / "objects"))
    monkeypatch.setattr(league_scope, "_CACHE", {})
    monkeypatch.setattr(deps, "_LAYERED", {})
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    parent_cache, parent_dbs = league_scope.league_file_cache(), deps.layered_databases()
    real_pid = __import__("os").getpid()
    monkeypatch.setattr("os.getpid", lambda: real_pid + 1)
    child_cache, child_dbs = league_scope.league_file_cache(), deps.layered_databases()
    assert child_cache is not parent_cache and child_cache.cache_dir != parent_cache.cache_dir
    assert child_dbs is not parent_dbs

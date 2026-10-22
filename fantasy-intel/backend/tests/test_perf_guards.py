"""Issue #201 performance guards without Postgres: request-path R2 retry, the budgeted degraded read (a league falls
back only to its own previous files), concurrent loads of two leagues never bleed, response single-flight, and
Server-Timing / the sampled perf log carrying no league id."""
import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pytest

from app.db.league_conn import LayeredDatabases
from app.db.league_files import LeagueFileCache, R2Store, global_key, league_key
from app.db.r2 import R2Error
from app.monitor import timing as perf
from fake_r2 import FakeR2

A = "lg_" + "a" * 24
B = "lg_" + "b" * 24


def blob(n: int, seed: int) -> bytes:
    return (hashlib.sha256(str(seed).encode()).digest() * (n // 32 + 1))[:n]


def version_of(data: bytes, stamp="20261020T120000Z") -> str:
    return f"{stamp}-{hashlib.sha256(data).hexdigest()[:24]}-{len(data)}"


class MemStore:
    """Objects in memory; `gate` (an Event per key) holds a fetch until set, `fail` makes a key's fetch raise."""

    def __init__(self):
        self.objects, self.gate, self.fail, self.calls = {}, {}, set(), []
        self.lock = threading.Lock()

    def put(self, key, data):
        self.objects[key] = data

    def fetch(self, key, dest, *, sha256_prefix, size):
        with self.lock:
            self.calls.append(key)
        if key in self.gate:
            self.gate[key].wait(10)
        if key in self.fail or key not in self.objects:
            raise R2Error(None, "URLError", key)
        dest.write_bytes(self.objects[key])


# ---- request-path R2 retry ------------------------------------------------------------------------------------------

@pytest.fixture
def r2():
    fake = FakeR2()
    yield fake
    fake.close()


def test_r2_store_retries_a_transient_failure_once(r2, tmp_path):
    data = blob(4000, 1)
    v = version_of(data)
    r2.objects[league_key(A, v)] = data
    r2.fail_next["GET"] = 503
    pauses = []
    cache = LeagueFileCache(R2Store(r2.config, sleep=pauses.append), tmp_path / "c", max_bytes=1 << 20)
    assert cache.league_file(A, v).read_bytes() == data
    assert [m for m, _, _ in r2.requests] == ["GET", "GET"]
    assert len(pauses) == 1 and 0.2 <= pauses[0] <= 0.5


@pytest.mark.parametrize("status", [403, 404])
def test_r2_store_never_retries_a_definite_answer(r2, tmp_path, status):
    data = blob(4000, 2)
    v = version_of(data)
    r2.objects[league_key(A, v)] = data
    r2.fail_next["GET"] = status
    cache = LeagueFileCache(R2Store(r2.config, sleep=lambda s: None), tmp_path / "c", max_bytes=1 << 20)
    with pytest.raises(R2Error):
        cache.league_file(A, v)
    assert len(r2.requests) == 1


def test_r2_store_never_retries_a_checksum_mismatch(r2, tmp_path):
    data = blob(4000, 3)
    v = version_of(data)
    r2.objects[league_key(A, v)] = blob(4000, 4)          # right size, wrong content
    cache = LeagueFileCache(R2Store(r2.config, sleep=lambda s: None), tmp_path / "c", max_bytes=1 << 20)
    with pytest.raises(R2Error):
        cache.league_file(A, v)
    assert len(r2.requests) == 1


# ---- budgeted degraded read ------------------------------------------------------------------------------------------

def two_versions(store, league, seed):
    old, new, glob = blob(3000, seed), blob(3000, seed + 1), blob(2000, 999)
    vo, vn, vg = version_of(old), version_of(new, "20261021T120000Z"), version_of(glob)
    store.put(league_key(league, vo), old)
    store.put(league_key(league, vn), new)
    store.put(global_key(vg), glob)
    return vo, vn, vg


def test_a_cached_pair_is_served_at_once_and_remembered(tmp_path):
    store = MemStore()
    vo, _, vg = two_versions(store, A, 10)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    first = cache.pin_pair(A, vo, vg)
    assert first.cold and first.degraded is None
    cache.release_pair(first)
    again = cache.pin_pair(A, vo, vg)
    assert not again.cold and again.degraded is None and again.league_path == first.league_path
    assert cache.last_good(A) == (vo, vg)


def test_without_an_older_pair_a_failed_load_still_raises(tmp_path):
    store = MemStore()
    vo, _, vg = two_versions(store, A, 20)
    store.fail.add(league_key(A, vo))
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    with pytest.raises(R2Error):
        cache.pin_pair(A, vo, vg)


def test_a_slow_new_pair_serves_the_previous_one_within_the_budget_then_the_new_one(tmp_path):
    store = MemStore()
    vo, vn, vg = two_versions(store, A, 30)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    cache.release_pair(cache.pin_pair(A, vo, vg))
    store.gate[league_key(A, vn)] = gate = threading.Event()
    t0 = time.monotonic()
    stale = cache.pin_pair(A, vn, vg, budget_s=0.2)
    assert time.monotonic() - t0 < 2
    assert (stale.degraded, stale.data_version) == ("slow", vo)
    assert stale.league_path.read_bytes() == store.objects[league_key(A, vo)]
    cache.release_pair(stale)
    # The budget counts from the background fetch's start: a later request does not wait it all again.
    t1 = time.monotonic()
    assert cache.pin_pair(A, vn, vg, budget_s=0.2).degraded == "slow"
    assert time.monotonic() - t1 < 0.15
    gate.set()
    for _ in range(100):                          # the background fetch lands
        fresh = cache.pin_pair(A, vn, vg, budget_s=0.2)
        if fresh.degraded is None:
            break
        cache.release_pair(fresh)
        time.sleep(0.02)
    assert (fresh.degraded, fresh.data_version) == (None, vn)
    assert fresh.league_path.read_bytes() == store.objects[league_key(A, vn)]
    assert store.calls.count(league_key(A, vn)) == 1          # fetched once, in the background


def test_an_unavailable_new_pair_serves_the_previous_one_and_is_not_retried_at_once(tmp_path):
    store = MemStore()
    vo, vn, vg = two_versions(store, A, 40)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    cache.release_pair(cache.pin_pair(A, vo, vg))
    store.fail.add(league_key(A, vn))
    for _ in range(3):
        pair = cache.pin_pair(A, vn, vg, budget_s=2)
        assert (pair.degraded, pair.data_version) == ("unavailable", vo)
        cache.release_pair(pair)
    assert store.calls.count(league_key(A, vn)) == 1          # R2 down: one attempt, not one per request
    assert cache.last_good(A) == (vo, vg)                      # a degraded answer never becomes the last good pair


def test_a_league_never_falls_back_to_another_leagues_files(tmp_path):
    store = MemStore()
    va, _, vg = two_versions(store, A, 50)
    vb_old, vb_new, _ = two_versions(store, B, 60)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    cache.release_pair(cache.pin_pair(A, va, vg))              # A has a cached pair; B has none yet
    store.fail.add(league_key(B, vb_new))
    with pytest.raises(R2Error):                                # B has nothing of its own to fall back to
        cache.pin_pair(B, vb_new, vg, budget_s=0.1)
    assert cache.last_good(B) is None
    cache.release_pair(cache.pin_pair(B, vb_old, vg))
    pair = cache.pin_pair(B, vb_new, vg, budget_s=0.1)
    assert (pair.degraded, pair.data_version) == ("unavailable", vb_old)
    assert f"leagues__{B}__" in pair.league_path.name and A not in pair.league_path.name


# ---- concurrent loads of two leagues: no bleed -----------------------------------------------------------------------

def league_db(path, marker):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE fantasy_teams (team_id INTEGER, name VARCHAR, is_mine BOOLEAN)")
    con.execute("INSERT INTO fantasy_teams VALUES (1, ?, false), (2, ?, false)", [f"{marker} one", f"{marker} two"])
    con.close()
    return path.read_bytes()


def test_concurrent_cold_loads_of_two_leagues_each_read_their_own_data(tmp_path):
    store = MemStore()
    data = {A: league_db(tmp_path / "a.duckdb", "Alpha"), B: league_db(tmp_path / "b.duckdb", "Bravo")}
    gcon = duckdb.connect(str(tmp_path / "g.duckdb"))
    gcon.execute("CREATE TABLE players (player_id INTEGER)")
    gcon.close()
    gdata = (tmp_path / "g.duckdb").read_bytes()
    vg = version_of(gdata)
    store.put(global_key(vg), gdata)
    versions = {}
    for lg, d in data.items():
        versions[lg] = version_of(d)
        store.put(league_key(lg, versions[lg]), d)
    dbs = LayeredDatabases()
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 22, on_evict=dbs.forget)
    for key in list(store.objects):                              # every first load waits, then all start together
        store.gate[key] = threading.Event()
    barrier = threading.Barrier(16)

    def read(i):
        lg = (A, B)[i % 2]
        barrier.wait()
        pair = cache.pin_pair(lg, versions[lg], vg)
        try:
            cur = dbs.cursor(pair.league_path, pair.global_path, perspective=i % 3 or None)
            try:
                return lg, sorted(r[0] for r in cur.execute("SELECT name FROM fantasy_teams").fetchall())
            finally:
                cur.close()
        finally:
            cache.release_pair(pair)

    with ThreadPoolExecutor(16) as pool:
        futures = [pool.submit(read, i) for i in range(64)]
        time.sleep(0.2)
        for gate in store.gate.values():
            gate.set()
        results = [f.result(timeout=30) for f in futures]
    for lg, names in results:
        expected = "Alpha" if lg == A else "Bravo"
        assert names == [f"{expected} one", f"{expected} two"], (lg, names)
    assert sorted(store.calls) == sorted(store.objects)         # each object fetched once despite 64 concurrent reads


# ---- response single flight -------------------------------------------------------------------------------------------

def test_identical_concurrent_requests_share_one_computation():
    from app.api.league_scope import ResponseCache
    cache = ResponseCache(1 << 20)
    computed = []

    async def request(key):
        if cache.get(key) is not None:
            return "hit"
        flight = cache.begin(key)
        if flight is not None:
            await asyncio.wait_for(flight.wait(), 5)
            hit = cache.get(key)
            return "shared" if hit is not None else "own"
        try:
            computed.append(key)
            await asyncio.sleep(0.05)
            cache.put(key, 200, key.encode(), "application/json")
            return "computed"
        finally:
            cache.land(key)

    async def main():
        return await asyncio.gather(*(request(k) for k in ["a"] * 5 + ["b"] * 3))

    out = asyncio.run(main())
    assert computed == ["a", "b"]
    assert sorted(out) == sorted(["computed"] * 2 + ["shared"] * 6)
    assert cache.get("a")[1] == b"a" and cache.get("b")[1] == b"b"     # each key keeps its own answer
    assert not cache._flights


def test_a_failed_leader_wakes_waiters_to_compute_their_own():
    from app.api.league_scope import ResponseCache
    cache = ResponseCache(1 << 20)

    async def main():
        assert cache.begin("k") is None
        waiter = cache.begin("k")
        assert waiter is not None
        cache.land("k")                          # the leader's answer was not cacheable (an error)
        await asyncio.wait_for(waiter.wait(), 1)
        assert cache.get("k") is None
        assert cache.begin("k") is None          # the next one leads afresh
        cache.land("k")

    asyncio.run(main())


# ---- Server-Timing and the perf log -------------------------------------------------------------------------------------

def test_timing_header_names_phases_and_codes_only():
    t = perf.Timing()
    t.add("auth", 0.0123)
    t.add("files", 0.5, "cold")
    t.add("files", 0.1, 'x"; drop')                 # a code that is not a short fixed shape is dropped
    t.add("Bad Name", 1)                            # so is a name
    t.size = perf.size_bucket(3 << 20)
    header = t.header(0.75)
    assert header == 'auth;dur=12.3, files;dur=500.0;desc="cold", files;dur=100.0, app;dur=750.0;desc="s"'


@pytest.mark.parametrize("nbytes,bucket", [(None, "unknown"), (10, "xs"), (2 << 20, "s"), (20 << 20, "m"),
                                           (100 << 20, "l"), (300 << 20, "xl")])
def test_size_buckets_are_coarse(nbytes, bucket):
    assert perf.size_bucket(nbytes) == bucket


def test_version_bytes_reads_the_byte_count_only():
    assert perf.version_bytes(version_of(b"x" * 1234)) == 1234
    assert perf.version_bytes("nonsense") is None


def test_the_perf_log_is_off_unless_sampled_or_slow(monkeypatch):
    monkeypatch.delenv("FI_PERF_SAMPLE", raising=False)
    monkeypatch.delenv("FI_PERF_SLOW_MS", raising=False)
    assert not perf.should_log(99999, rand=lambda: 0.0)
    monkeypatch.setenv("FI_PERF_SAMPLE", "0.1")
    assert perf.should_log(1, rand=lambda: 0.05) and not perf.should_log(1, rand=lambda: 0.5)
    monkeypatch.setenv("FI_PERF_SAMPLE", "garbage")
    monkeypatch.setenv("FI_PERF_SLOW_MS", "800")
    assert perf.should_log(900, rand=lambda: 0.0) and not perf.should_log(100, rand=lambda: 0.0)


def test_the_middleware_adds_server_timing_and_logs_the_route_template_not_the_league(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/v2/leagues/{league_id}/api/overview")
    def overview(league_id: str, request: Request):
        perf.request_timing(request).add("files", 0.002, "warm")
        return {"ok": True}

    app.add_middleware(perf.ServerTimingMiddleware)
    lines = []

    class Collect:
        def info(self, line):
            lines.append(line)

    monkeypatch.setattr(perf, "_logger", lambda: Collect())
    monkeypatch.setenv("FI_PERF_SAMPLE", "1")
    r = TestClient(app).get(f"/v2/leagues/{A}/api/overview?team_id=3")
    assert r.status_code == 200
    assert r.headers["server-timing"].startswith('files;dur=2.0;desc="warm", app;dur=')
    assert len(lines) == 1
    assert lines[0].startswith("perf GET /v2/leagues/{league_id}/api/overview status=200 total_ms=")
    assert A not in lines[0] and "team_id" not in lines[0]

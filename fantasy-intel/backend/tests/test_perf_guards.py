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
        role, flight = cache.begin(key)
        if role == "wait":
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
        assert cache.begin("k") == ("lead", None)
        role, waiter = cache.begin("k")
        assert role == "wait"
        cache.land("k")                          # the leader's answer was not cacheable (an error)
        await asyncio.wait_for(waiter.wait(), 1)
        assert cache.get("k") is None
        assert cache.begin("k") == ("lead", None)          # the next one leads afresh
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


# ---- budgets -----------------------------------------------------------------------------------------------------------

def test_budgets_reuse_the_monitor_quotas_and_the_cache_settings():
    from app.db import league_files
    from app.monitor import budgets
    q = budgets.free_tier_quotas()
    assert q["r2_class_a_ops"] == (1_000_000, "requests") and q["r2_class_b_ops"] == (10_000_000, "requests")
    assert q["r2_storage_bytes"][0] == 10e9 and q["supabase_db_bytes"][0] == 500e6
    assert q["vercel_invocations"][0] == 1_000_000 and q["vercel_active_cpu_hours"][0] == 4
    assert budgets.COLD_LOAD_BUDGET_S == league_files.DEFAULT_COLD_BUDGET_S
    assert budgets.r2_gets_per_download(5 << 20) == 1 and budgets.r2_gets_per_download(95 << 20) == 8
    assert budgets.r2_gets_per_download(1, retried=True) == 2
    assert budgets.within_budget("overview", "warm", 1000) and not budgets.within_budget("overview", "cached", 1000)
    assert budgets.page_budget_ms("trades-discover", "warm") > budgets.page_budget_ms("waivers", "warm")


# ---- more leagues at once than the cache holds ---------------------------------------------------------------------------

def test_more_concurrent_leagues_than_the_cache_holds_wait_for_room_instead_of_failing(tmp_path):
    """25 leagues of 16.5 MB at once exceed the 350 MB /tmp bound (issue #201 baseline): the newcomers wait for running
    requests to release their files, the bound is never exceeded, and nobody gets CacheFull."""
    store = MemStore()
    leagues = [f"lg_{i:024d}" for i in range(8)]
    versions = {}
    for i, lg in enumerate(leagues):
        data = blob(30_000, 100 + i)
        versions[lg] = version_of(data)
        store.put(league_key(lg, versions[lg]), data)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=95_000, room_wait_s=5)     # three files fit
    peak, barrier = [0], threading.Barrier(len(leagues))

    def use(lg):
        barrier.wait()
        path = cache.pin_league(lg, versions[lg])
        try:
            peak[0] = max(peak[0], cache.bytes_used)
            assert path.read_bytes() == store.objects[league_key(lg, versions[lg])]
            time.sleep(0.05)                               # the request runs, holding its file
        finally:
            cache.release(path)
        return lg

    with ThreadPoolExecutor(len(leagues)) as pool:
        assert sorted(pool.map(use, leagues)) == leagues
    assert peak[0] <= 95_000 and cache.bytes_used <= 95_000


def test_a_cache_that_stays_full_still_refuses_after_the_wait(tmp_path):
    from app.db.league_files import CacheFull
    store = MemStore()
    a, b = blob(60_000, 1), blob(60_000, 2)
    va, vb = version_of(a), version_of(b)
    store.put(league_key(A, va), a)
    store.put(league_key(B, vb), b)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=100_000, room_wait_s=0.3)
    held = cache.pin_league(A, va)
    t0 = time.monotonic()
    with pytest.raises(CacheFull):
        cache.pin_league(B, vb)
    assert 0.25 <= time.monotonic() - t0 < 3
    cache.release(held)
    assert cache.pin_league(B, vb).exists()


# ---- review fixes: rollback never serves a newer pair; foreign-loop flights; bounded fallback wait --------------------

def test_after_a_rollback_a_newer_cached_pair_is_never_served_in_place_of_the_pointer(tmp_path):
    """The pointer moved back V2 -> V1 (operator rollback). The instance still holds V2; V1 cannot be fetched. The
    answer is the error (a 503), never the retracted V2 marked as "data may be older"."""
    store = MemStore()
    v_old, v_new, vg = two_versions(store, B, 70)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    cache.release_pair(cache.pin_pair(B, v_new, vg))          # V2 served and cached
    store.fail.add(league_key(B, v_old))
    for _ in range(2):
        with pytest.raises(R2Error):
            cache.pin_pair(B, v_old, vg, budget_s=0.1)
    assert cache.last_good(B) is None                          # forgotten: the rolled-back pair is never a fallback
    store.fail.discard(league_key(B, v_old))
    pair = cache.pin_pair(B, v_old, vg, budget_s=0.1)
    assert (pair.degraded, pair.data_version) == (None, v_old)
    cache.release_pair(pair)
    assert cache.last_good(B) == (v_old, vg)


def test_a_pair_with_a_newer_global_file_is_not_older():
    from app.db.league_files import _older
    d1, d2 = version_of(b"a"), version_of(b"b", "20261021T120000Z")
    g1, g2 = version_of(b"g"), version_of(b"h", "20261021T120000Z")
    assert _older((d1, g1), (d2, g2)) and _older((d1, g1), (d2, g1))
    assert not _older((d2, g1), (d1, g1))                      # rollback
    assert not _older((d1, g2), (d2, g1))                      # its global file is newer than the pointer's
    assert not _older((d1, g1), (d1, g1))                      # the same pair


def test_a_request_on_another_event_loop_computes_alone_and_never_ends_the_first_loops_flight():
    from app.api.league_scope import ResponseCache
    cache = ResponseCache(1 << 20)
    owner_ready, other_done = threading.Event(), threading.Event()
    seen = {}

    async def owner():
        seen["owner"] = cache.begin("k")
        owner_ready.set()
        await asyncio.get_running_loop().run_in_executor(None, other_done.wait, 5)
        seen["still_owned"] = "k" in cache._flights
        cache.land("k")

    async def other():
        owner_ready.wait(5)
        seen["other"] = cache.begin("k")
        cache.land("k")                     # not its flight: must not remove or wake the owner's
        other_done.set()

    t = threading.Thread(target=lambda: asyncio.run(owner()))
    t.start()
    asyncio.run(other())
    t.join(5)
    assert seen["owner"] == ("lead", None) and seen["other"] == ("solo", None)
    assert seen["still_owned"] is True and not cache._flights


def test_the_fallback_wait_for_a_new_pair_is_bounded(tmp_path, monkeypatch):
    """The older pair vanished while the new one is still downloading: the request waits a bounded time, then fails."""
    from app.db import league_files
    monkeypatch.setattr(league_files, "_FALLBACK_WAIT_S", 0.3)
    store = MemStore()
    vo, vn, vg = two_versions(store, A, 80)
    cache = LeagueFileCache(store, tmp_path / "c", max_bytes=1 << 20)
    cache.release_pair(cache.pin_pair(A, vo, vg))
    store.gate[league_key(A, vn)] = gate = threading.Event()
    monkeypatch.setattr(cache, "_pin_pair_cached", lambda *a: None)     # the older pair is gone
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        cache.pin_pair(A, vn, vg, budget_s=0.1)
    assert time.monotonic() - t0 < 3
    gate.set()

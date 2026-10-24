"""Cold-load gate (app/api/league_scope.py `_pin_cold`, issue #201 risk 2): a request whose files are not cached waits
on the event loop, not in a thread-pool slot. Per pair one leader loads, under at most LEAGUE_COLD_LOADS permits per
event loop; the others pin from the cache once it is done, or answer its error. `_pin` is replaced by a fake that
records how many loads run in threads at once."""
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import league_scope as ls
from app.monitor.timing import Timing


class Pair(SimpleNamespace):
    def __init__(self, key, degraded=None):
        super().__init__(key=key, degraded=degraded)


def access(league):
    return SimpleNamespace(league_id=league, snapshot=SimpleNamespace(data_version=f"d-{league}", global_version="g"))


class FakePin:
    """`_pin(access, team, timing, wait=...)`: `wait=True` loads (sleeps `load_s`, then the pair is cached); `wait=False`
    returns the pair only once loaded. Counts loads running at once and the threads busy in loads."""

    def __init__(self, load_s=0.05, fail=None, degraded=None):
        self.load_s, self.fail, self.degraded = load_s, fail or set(), degraded or set()
        self.lock = threading.Lock()
        self.cached, self.loads, self.running, self.peak = set(), [], 0, 0

    def __call__(self, acc, team, timing, *, wait=True, since=None):
        key = acc.league_id
        if not wait:
            return (f"scope-{key}", Pair(key)) if key in self.cached else None
        if key in self.degraded and self.loads.count(key) >= 1:
            return f"scope-{key}", Pair(key, "slow")            # a later request reads the same previous files
        with self.lock:
            self.loads.append(key)
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.load_s)
            if key in self.fail:
                raise HTTPException(503, "League data is unavailable.", headers={"X-Leader": "secret"})
            if key in self.degraded:
                return f"scope-{key}", Pair(key, "slow")        # the previous files; the new pair is not cached
            with self.lock:
                self.cached.add(key)
            return f"scope-{key}", Pair(key)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def fake(monkeypatch):
    f = FakePin()
    f.fallback = set()                                        # leagues whose previous files are cached
    monkeypatch.setattr(ls, "_pin", f)
    monkeypatch.setattr(ls, "league_file_cache",
                        lambda: SimpleNamespace(has_cached_fallback=lambda lg, dv, gv: lg in f.fallback))
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "2")
    ls._COLD.clear()
    yield f
    ls._COLD.clear()


def run(coro):
    return asyncio.run(coro)


def test_one_load_per_pair_and_the_others_pin_from_the_cache(fake):
    async def go():
        return await asyncio.gather(*(ls._pin_cold(access("a"), None, Timing()) for _ in range(6)))
    results = run(go())
    assert fake.loads == ["a"] and [(sc, p.key) for sc, p in results] == [("scope-a", "a")] * 6


def test_at_most_the_permits_load_at_once_across_leagues(fake):
    async def go():
        return await asyncio.gather(*(ls._pin_cold(access(f"l{i}"), None, Timing()) for i in range(7)))
    results = run(go())
    assert sorted(fake.loads) == sorted(f"l{i}" for i in range(7)) and fake.peak == 2
    assert [(sc, p.key) for sc, p in results] == [(f"scope-l{i}", f"l{i}") for i in range(7)]


def test_a_failed_load_is_every_waiters_answer_without_loading_again(fake):
    fake.fail = {"bad"}

    async def go():
        return await asyncio.gather(*(ls._pin_cold(access("bad"), None, Timing()) for _ in range(5)),
                                    return_exceptions=True)
    results = run(go())
    assert fake.loads == ["bad"]                              # one wait for room per pair, never one after another
    assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)
    followers = results[1:]                                   # a constant answer: nothing of the leader's crosses over
    assert all(r.headers is None and r.detail == ls.FILES_UNAVAILABLE for r in followers)


def test_a_leader_served_the_previous_files_lets_the_others_read_them_without_leading(fake):
    fake.degraded = {"a"}

    async def go():
        return await asyncio.gather(*(ls._pin_cold(access("a"), None, Timing()) for _ in range(6)))
    results = run(go())
    assert fake.loads == ["a"]                                # one gated load, not one round per waiter
    assert all(p.degraded == "slow" for _, p in results)


def test_a_cancelled_leader_hands_the_load_to_a_waiter(fake):
    fake.load_s = 0.2

    async def go():
        lead = asyncio.ensure_future(ls._pin_cold(access("a"), None, Timing()))
        await asyncio.sleep(0.01)
        follower = asyncio.ensure_future(ls._pin_cold(access("a"), None, Timing()))
        await asyncio.sleep(0.01)
        lead.cancel()
        return await follower
    sc, p = run(go())
    assert (sc, p.key) == ("scope-a", "a")


def test_waiting_requests_hold_no_thread(fake):
    """20 leagues cold at once with 2 permits: never more than 2 threads busy loading, while the loop stays free to run
    other work (here a tick counter) the whole time."""
    fake.load_s = 0.03
    ticks = []

    async def ticker(stop):
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.005)

    async def go():
        stop = asyncio.Event()
        t = asyncio.ensure_future(ticker(stop))
        await asyncio.gather(*(ls._pin_cold(access(f"l{i}"), None, Timing()) for i in range(20)))
        stop.set()
        await t
    run(go())
    assert fake.peak == 2 and len(fake.loads) == 20
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.1                                    # the loop was never blocked by a waiting load


def test_each_event_loop_has_its_own_gate(fake):
    async def gate():
        return ls._cold_state()[0]
    first, second = run(gate()), run(gate())
    assert first is not second and len([lp for lp in ls._COLD if not lp.is_closed()]) <= 1


def test_the_permit_count_is_configurable(monkeypatch):
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "7")
    assert ls.cold_loads_per_loop() == 7
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "0")
    assert ls.cold_loads_per_loop() == 1
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "x")
    assert ls.cold_loads_per_loop() == ls.COLD_LOADS


def test_waiting_for_a_permit_or_a_leader_ends_with_a_503(fake, monkeypatch):
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "1")
    monkeypatch.setattr(ls, "COLD_WAIT_S", 0.2)
    fake.load_s = 0.6                                         # one load holds the only permit past the deadline

    async def go():
        return await asyncio.gather(ls._pin_cold(access("a"), None, Timing()),      # the leader: loads
                                    ls._pin_cold(access("a"), None, Timing()),      # waits for it: times out
                                    ls._pin_cold(access("b"), None, Timing()),      # waits for the permit: times out
                                    return_exceptions=True)
    lead, follower, other = run(go())
    assert lead[1].key == "a"
    assert isinstance(follower, HTTPException) and follower.status_code == 503
    assert isinstance(other, HTTPException) and other.status_code == 503 and fake.loads == ["a"]


def test_a_league_with_its_previous_files_cached_never_waits_for_a_permit(fake, monkeypatch):
    """Codex #343: while every permit is held by slow unrelated downloads, a league whose pointer moved but whose
    previous files are cached is still served (pin_pair's stale fallback), not a 503 after COLD_WAIT_S."""
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "1")
    monkeypatch.setattr(ls, "COLD_WAIT_S", 0.2)
    fake.load_s = 0.6
    fake.fallback = {"moved"}

    async def go():
        slow = asyncio.ensure_future(ls._pin_cold(access("other"), None, Timing()))   # holds the only permit
        await asyncio.sleep(0.02)
        t0 = time.perf_counter()
        moved = await ls._pin_cold(access("moved"), None, Timing())
        return moved, time.perf_counter() - t0, await slow
    moved, waited, slow = run(go())
    assert moved[1].key == "moved" and slow[1].key == "other"
    assert waited < 0.9 and fake.peak == 2                    # loaded beside the permit holder, not after it


# ---- the real _pin over a fake cache: isolation and timing ----------------------------------------------------------

class FakeCache:
    """pin_pair loads (sleeps), then the pair is cached; pins are counted per key."""

    def __init__(self, load_s=0.1):
        from app.db.league_files import PinnedPair
        self.PinnedPair, self.load_s = PinnedPair, load_s
        self.cached, self.pins, self.lock = set(), 0, threading.Lock()

    def _pair(self, league, dv, gv, cold):
        with self.lock:
            self.pins += 1
        return self.PinnedPair(Path(f"/x/{league}-{dv}"), Path(f"/x/{gv}"), dv, gv, cold=cold)

    def pin_pair_if_cached(self, league, dv, gv):
        return self._pair(league, dv, gv, False) if (league, dv, gv) in self.cached else None

    def has_cached_fallback(self, league, dv, gv):
        return False

    def pin_pair(self, league, dv, gv, *, budget_s):
        time.sleep(self.load_s)
        self.cached.add((league, dv, gv))
        return self._pair(league, dv, gv, True)


def viewer(team, epoch):
    return SimpleNamespace(league_id="lg", provider="espn", visibility_epoch=epoch, perspective_team_id=team,
                           snapshot=SimpleNamespace(data_version="d1", global_version="g1"))


def test_each_waiter_builds_its_own_scope_and_records_its_wait(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(ls, "league_file_cache", lambda: cache)
    ls._COLD.clear()
    timings = [Timing(), Timing(), Timing()]

    async def go():
        return await asyncio.gather(ls._pin_cold(viewer(3, 1), None, timings[0]),
                                    ls._pin_cold(viewer(None, 2), "5", timings[1]),
                                    ls._pin_cold(viewer(None, 7), None, timings[2]))
    (s1, _), (s2, _), (s3, _) = run(go())
    assert (s1.perspective, s2.perspective, s3.perspective) == (3, 5, None)
    assert len({s1.cache_key, s2.cache_key, s3.cache_key}) == 3           # epochs and perspectives stay apart
    assert cache.pins == 3                                                # each request pins its own pair
    for t in timings:                                                     # followers report the wait, as cold
        files = [p for p in t.phases if p[0] == "files"]
        assert len(files) == 1 and files[0][2] == "cold" and files[0][1] >= 90
    ls._COLD.clear()

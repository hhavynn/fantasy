"""Cold-load gate (app/api/league_scope.py `_pin_cold`, issue #201 risk 2): a request whose files are not cached waits
on the event loop, not in a thread-pool slot. Per pair one leader loads, under at most LEAGUE_COLD_LOADS permits per
event loop; the others pin from the cache once it is done, or answer its error. `_pin` is replaced by a fake that
records how many loads run in threads at once."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import league_scope as ls


def access(league):
    return SimpleNamespace(league_id=league, snapshot=SimpleNamespace(data_version=f"d-{league}", global_version="g"))


class FakePin:
    """`_pin(access, team, timing, wait=...)`: `wait=True` loads (sleeps `load_s`, then the pair is cached); `wait=False`
    returns the pair only once loaded. Counts loads running at once and the threads busy in loads."""

    def __init__(self, load_s=0.05, fail=None):
        self.load_s, self.fail = load_s, fail or set()
        self.lock = threading.Lock()
        self.cached, self.loads, self.running, self.peak = set(), [], 0, 0

    def __call__(self, acc, team, timing, *, wait=True):
        key = acc.league_id
        if not wait:
            return (f"scope-{key}", f"pair-{key}") if key in self.cached else None
        with self.lock:
            self.loads.append(key)
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.load_s)
            if key in self.fail:
                raise HTTPException(503, "League data is unavailable.")
            with self.lock:
                self.cached.add(key)
            return f"scope-{key}", f"pair-{key}"
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def fake(monkeypatch):
    f = FakePin()
    monkeypatch.setattr(ls, "_pin", f)
    monkeypatch.setenv("LEAGUE_COLD_LOADS", "2")
    ls._COLD.clear()
    yield f
    ls._COLD.clear()


def run(coro):
    return asyncio.run(coro)


def test_one_load_per_pair_and_the_others_pin_from_the_cache(fake):
    async def go():
        return await asyncio.gather(*(ls._pin_cold(access("a"), None, None) for _ in range(6)))
    results = run(go())
    assert fake.loads == ["a"] and results == [("scope-a", "pair-a")] * 6


def test_at_most_the_permits_load_at_once_across_leagues(fake):
    async def go():
        return await asyncio.gather(*(ls._pin_cold(access(f"l{i}"), None, None) for i in range(7)))
    results = run(go())
    assert sorted(fake.loads) == sorted(f"l{i}" for i in range(7)) and fake.peak == 2
    assert results == [(f"scope-l{i}", f"pair-l{i}") for i in range(7)]


def test_a_failed_load_is_every_waiters_answer_without_loading_again(fake):
    fake.fail = {"bad"}

    async def go():
        return await asyncio.gather(*(ls._pin_cold(access("bad"), None, None) for _ in range(5)),
                                    return_exceptions=True)
    results = run(go())
    assert fake.loads == ["bad"]                              # one wait for room per pair, never one after another
    assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)


def test_a_cancelled_leader_hands_the_load_to_a_waiter(fake):
    fake.load_s = 0.2

    async def go():
        lead = asyncio.ensure_future(ls._pin_cold(access("a"), None, None))
        await asyncio.sleep(0.01)
        follower = asyncio.ensure_future(ls._pin_cold(access("a"), None, None))
        await asyncio.sleep(0.01)
        lead.cancel()
        return await follower
    assert run(go()) == ("scope-a", "pair-a")


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
        await asyncio.gather(*(ls._pin_cold(access(f"l{i}"), None, None) for i in range(20)))
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

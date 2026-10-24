"""GET /v2/action-center (issue #166): one signed-in user's decisions across their own leagues.

Cross-tenant guarantees first: the league list is the caller's own (memberships and pins) after authorization, so
another user's private league, a revoked membership and an expired public pin contribute nothing, not even a name.
Then the honesty rules: a league without data, without a saved team or read from older files says so and is never
shown as a clean roster; the fan-out is capped with the rest listed; the cache is per user, league and version."""
from datetime import datetime, timedelta, timezone

import pytest

from cp_seed import U1, U2, U3, seed_league, seed_membership, seed_user
from league_fixtures import publish_local
from test_league_scope import MARK, client, e2e, e2e_b, my_team, served  # noqa: F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: F401  (fixture)

URL = "/v2/action-center"


def _select_team(pg, uid, league_id, team):
    with pg.connect() as con:
        con.execute("INSERT INTO fi.team_selections (user_id, league_id, team_external_id) VALUES (%s, %s, %s)",
                    [uid, league_id, str(team)])
        con.commit()


def _member(pg, uid, league_id, role="viewer", *, revoked=False):
    with pg.connect() as con:
        seed_user(con, uid)
        seed_membership(con, uid, league_id, role, revoked=revoked)
        con.commit()


def _pin(pg, uid, league_id):
    with pg.connect() as con:
        seed_user(con, uid)
        con.execute("INSERT INTO fi.league_pins (user_id, league_id) VALUES (%s, %s)", [uid, league_id])
        con.commit()


def _ids(body):
    return [s["league"]["league_id"] for s in body["leagues"]]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    from app.api import action_center
    monkeypatch.setattr(action_center, "_CACHES", {})
    monkeypatch.setattr(action_center, "_POOLS", {})          # a slow league from another test never holds a worker
    monkeypatch.setattr(action_center, "_FLIGHTS", {})
    monkeypatch.setattr(action_center, "_HOLDERS", {})
    monkeypatch.setattr(action_center, "_ADMISSION", {})


# ---- identity -------------------------------------------------------------------------------------------------------

def test_anonymous_is_401_and_a_bad_token_is_401(client):
    assert (client.get(URL).status_code, client.get(URL).json()) == (401, {"detail": "Sign in to use this endpoint."})
    bad = {"Authorization": "Bearer " + "x" * 40 + "." + "y" * 40 + "." + "z" * 40}
    assert client.get(URL, headers=bad).status_code == 401


def test_flag_off_is_a_plain_404(client, monkeypatch):
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "0")
    assert client.get(URL, headers=bearer(U1)).status_code == 404


def test_the_league_list_cannot_be_supplied_by_the_client(pg, served, client, e2e, tmp_path):
    """Query parameters naming a league are ignored: only the caller's own leagues are read."""
    other = publish_local(pg, e2e, served, tmp_path / "o", visibility="private", name="Someone Else's")
    _member(pg, U2, other.league_id)
    with pg.connect() as con:
        seed_user(con, U1)
        con.commit()
    r = client.get(f"{URL}?league_id={other.league_id}&leagues={other.league_id}", headers=bearer(U1))
    assert r.status_code == 200
    assert r.json()["leagues"] == [] and other.league_id not in r.text and "Someone Else" not in r.text


# ---- isolation ----------------------------------------------------------------------------------------------------------

def test_user_a_never_sees_user_bs_private_league(pg, served, client, e2e, e2e_b, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="Tenant Aleph")
    b = publish_local(pg, e2e_b, served, tmp_path / "b", visibility="private", name="Tenant Beth", external_id="1999")
    _member(pg, U1, a.league_id)
    _member(pg, U2, b.league_id)
    _select_team(pg, U1, a.league_id, my_team(e2e))
    _select_team(pg, U2, b.league_id, my_team(e2e_b))
    ra, rb = client.get(URL, headers=bearer(U1)), client.get(URL, headers=bearer(U2))
    assert ra.status_code == rb.status_code == 200
    assert _ids(ra.json()) == [a.league_id] and _ids(rb.json()) == [b.league_id]
    # nothing of the other league: no id, no name, no team name (B's team names carry MARK)
    assert b.league_id not in ra.text and "Tenant Beth" not in ra.text and MARK not in ra.text
    assert a.league_id not in rb.text and "Tenant Aleph" not in rb.text
    assert ra.json()["leagues"][0]["state"] == rb.json()["leagues"][0]["state"] == "ready"
    assert MARK in rb.text                                  # B's own data is B's: the files were not swapped


def test_a_revoked_membership_disappears(pg, served, client, e2e, tmp_path):
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="private", name="Tenant Aleph")
    _member(pg, U1, a.league_id)
    assert _ids(client.get(URL, headers=bearer(U1)).json()) == [a.league_id]
    with pg.connect() as con:
        con.execute("UPDATE fi.league_memberships SET revoked_at = now() WHERE user_id = %s AND league_id = %s",
                    [U1, a.league_id])
        con.commit()
    r = client.get(URL, headers=bearer(U1))
    assert r.json()["leagues"] == [] and a.league_id not in r.text and "Tenant Aleph" not in r.text


def test_a_pinned_league_that_went_private_disappears_even_with_a_cached_summary(pg, served, client, e2e, tmp_path):
    """Authorization runs on every request before the cache: a cached section never outlives the access."""
    pub = publish_local(pg, e2e, served, tmp_path / "p", visibility="public", name="Was Public")
    _pin(pg, U1, pub.league_id)
    _select_team(pg, U1, pub.league_id, my_team(e2e))
    assert client.get(URL, headers=bearer(U1)).json()["leagues"][0]["state"] == "ready"      # now cached
    with pg.connect() as con:
        con.execute("UPDATE fi.leagues SET visibility = 'private', visibility_epoch = visibility_epoch + 1 WHERE id = %s",
                    [pub.league_id])
        con.commit()
    r = client.get(URL, headers=bearer(U1))
    assert r.json()["leagues"] == [] and "Was Public" not in r.text


def test_public_leagues_follow_the_visibility_rules(pg, served, client, e2e, tmp_path):
    """A pinned public league with a fresh probe is shown (a non-member's view); once the last probe is older than
    24 h it is gone, as on GET /v2/leagues, and no probe or sync is started from here."""
    fresh = publish_local(pg, e2e, served, tmp_path / "f", visibility="public", name="Fresh Public")
    old = publish_local(pg, e2e, served, tmp_path / "o", visibility="public", name="Old Public", external_id="2888",
                        checked_at="25 hours")
    for lg in (fresh, old):
        _pin(pg, U3, lg.league_id)
    with pg.connect() as con:
        jobs_before = con.execute("SELECT count(*) FROM fi.sync_jobs").fetchone()[0]
    body = client.get(URL, headers=bearer(U3)).json()
    assert _ids(body) == [fresh.league_id]
    assert body["leagues"][0]["league"]["role"] is None
    assert body["leagues"][0]["state"] == "needs_team"         # no saved team: no lineup is invented
    assert "Old Public" not in str(body)
    with pg.connect() as con:
        assert con.execute("SELECT count(*) FROM fi.sync_jobs").fetchone()[0] == jobs_before


# ---- data not ready ------------------------------------------------------------------------------------------------------

def test_every_league_is_shown_with_an_explicit_state(pg, served, client, e2e, tmp_path):
    ready = publish_local(pg, e2e, served, tmp_path / "r", visibility="private", name="Ready")
    noteam = publish_local(pg, e2e, served, tmp_path / "n", visibility="private", name="No Team", external_id="2777")
    gone = publish_local(pg, e2e, served, tmp_path / "g", visibility="private", name="Gone Team", external_id="2666")
    with pg.connect() as con:
        blank = seed_league(con, visibility="private", checked_at=None, name="Blank")
        con.commit()
    for lg in (ready.league_id, noteam.league_id, gone.league_id, blank):
        _member(pg, U1, lg)
    _select_team(pg, U1, ready.league_id, my_team(e2e))
    _select_team(pg, U1, gone.league_id, 987)                   # a saved team the data does not have
    r = client.get(URL, headers=bearer(U1))
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    by = {s["league"]["name"]: s for s in r.json()["leagues"]}
    assert set(by) == {"Ready", "No Team", "Gone Team", "Blank"}
    assert by["Blank"]["state"] == "not_ready" and by["Blank"]["reason"]
    assert by["No Team"]["state"] == "needs_team" and by["Gone Team"]["state"] == "needs_team"
    for name in ("Blank", "No Team", "Gone Team"):      # never a clean roster
        assert by[name]["overview"] is None and by[name]["start_sit"] is None, name
    s = by["Ready"]
    assert s["state"] == "ready" and s["stale"] is False
    assert s["versions"] == {"data_version": ready.data_version, "global_version": ready.global_version}
    assert s["team"]["team_id"] == my_team(e2e) and s["week"]
    assert any(p["is_starter"] for p in s["overview"]["roster"])
    assert s["overview"]["provenance"]["contract"] == "signal-provenance.v1"
    assert s["start_sit"]["provenance"]["contract"] == "signal-provenance.v1"
    league_signal = next(x for x in s["start_sit"]["provenance"]["signals"] if x["signal"] == "league")
    assert league_signal["snapshot_version"] == ready.data_version
    assert set(s["lock"]) == {"next_kickoff_utc", "starters", "players", "rule"}
    assert ready.league_id not in str(s["overview"]["provenance"])


def test_a_degraded_read_is_marked_not_fresh_and_not_cached(pg, served, client, e2e, tmp_path):
    import psycopg
    from app.db.league_files import league_key
    from app.db.league_publish import DirWriter, publish_league
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Slow")
    _member(pg, U1, pub.league_id)
    _select_team(pg, U1, pub.league_id, my_team(e2e))
    first = client.get(URL, headers=bearer(U1))
    assert first.json()["leagues"][0]["stale"] is False and "x-fi-degraded" not in first.headers
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        res = publish_league(e2e, DirWriter(served), sync, pub.league_id, stamp="2026-10-21T12:00:00")
    (served / league_key(pub.league_id, res.data_version)).unlink()     # R2 cannot deliver the new file
    r = client.get(URL, headers=bearer(U1))
    s = r.json()["leagues"][0]
    assert s["state"] == "ready" and s["stale"] is True and s["degraded"]
    assert s["versions"]["data_version"] == pub.data_version            # the files actually read, not the pointer
    assert s["league"]["data"]["data_version"] == res.data_version
    assert r.headers["x-fi-degraded"] == "stale-files"


# ---- the cap, the budget and the cache -------------------------------------------------------------------------------------

def test_fan_out_is_capped_and_the_rest_are_listed(pg, client, served, monkeypatch):
    from app.api import action_center
    monkeypatch.setenv("ACTION_CENTER_MAX_LEAGUES", "2")
    seen = []
    real = action_center.evaluate_league
    monkeypatch.setattr(action_center, "evaluate_league", lambda p, a, now: seen.append(a.league_id) or real(p, a, now))
    with pg.connect() as con:
        ids = [seed_league(con, visibility="private", checked_at=None, name=f"L{i}") for i in range(4)]
        con.commit()
    for lg in ids:
        _member(pg, U1, lg)
    body = client.get(URL, headers=bearer(U1)).json()
    assert body["limit"] == 2 and len(body["leagues"]) == 2
    assert body["more"]["count"] == 2 and len(body["more"]["leagues"]) == 2
    shown = _ids(body) + [x["league_id"] for x in body["more"]["leagues"]]
    assert sorted(shown) == sorted(ids)
    assert sorted(seen) == sorted(_ids(body))                  # the extra leagues were never evaluated


def test_a_league_past_the_budget_is_reported_not_dropped(pg, client, served, monkeypatch):
    import time
    from app.api import action_center
    monkeypatch.setenv("ACTION_CENTER_BUDGET_S", "1")
    real = action_center.evaluate_league
    slow_id = []

    def maybe_slow(p, a, now):
        if a.league_id in slow_id:
            time.sleep(2.5)
        return real(p, a, now)

    monkeypatch.setattr(action_center, "evaluate_league", maybe_slow)
    with pg.connect() as con:
        quick = seed_league(con, visibility="private", checked_at=None, name="Quick")
        slow = seed_league(con, visibility="private", checked_at=None, name="Slow")
        con.commit()
    slow_id.append(slow)
    for lg in (quick, slow):
        _member(pg, U1, lg)
    by = {s["league"]["name"]: s for s in client.get(URL, headers=bearer(U1)).json()["leagues"]}
    assert by["Slow"]["state"] == "not_ready" and by["Slow"]["reason"] == action_center.TOO_SLOW
    assert by["Quick"]["state"] == "not_ready" and by["Quick"]["reason"] != action_center.TOO_SLOW


def test_the_cache_is_keyed_by_user_league_and_version(pg, served, client, e2e, tmp_path, monkeypatch):
    from app.api import action_center
    calls = []
    real = action_center._read
    monkeypatch.setattr(action_center, "_read", lambda scope, team: calls.append((scope.league_id, team)) or real(scope, team))
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Shared")
    team = my_team(e2e)
    for uid in (U1, U2):
        _member(pg, uid, pub.league_id)
        _select_team(pg, uid, pub.league_id, team)
    one = client.get(URL, headers=bearer(U1)).json()
    again = client.get(URL, headers=bearer(U1)).json()
    assert len(calls) == 1 and one["leagues"][0]["overview"] == again["leagues"][0]["overview"]
    client.get(URL, headers=bearer(U2))                         # another user: never served U1's entry
    assert len(calls) == 2
    keys = list(action_center.summary_cache()._entries)
    assert {k[0] for k in keys} == {U1, U2} and all(k[1] == pub.league_id and k[2] == pub.data_version for k in keys)


def test_summary_cache_is_bounded_and_expires():
    from app.api.action_center import SummaryCache
    clock = [0.0]
    cache = SummaryCache(max_entries=2, ttl_s=10, clock=lambda: clock[0])
    now = datetime(2026, 10, 25, 17, tzinfo=timezone.utc)
    cache.put(("u", "a"), {"x": 1}, None)
    cache.put(("u", "b"), {"x": 2}, now + timedelta(hours=1))
    cache.put(("u", "c"), {"x": 3}, None)
    assert len(cache) == 2 and cache.get(("u", "a"), now) is None                  # least recently used went first
    assert cache.get(("u", "b"), now) == {"x": 2}
    assert cache.get(("u", "b"), now + timedelta(hours=1)) is None                # a kickoff passed: lock states moved
    clock[0] = 11
    assert cache.get(("u", "c"), now) is None                                     # older than the TTL


def test_lock_summary_reads_start_sit_without_inferring():
    from app.api.action_center import lock_summary
    ss = {"actual_lineup": [
        {"player_id": 1, "lock_status": "LOCKED", "kickoff_utc": "2026-10-25T17:00:00+00:00"},
        {"player_id": 2, "lock_status": "UNLOCKED", "kickoff_utc": "2026-10-26T00:20:00+00:00"},
        {"player_id": 3, "lock_status": "UNKNOWN", "kickoff_utc": None},
        {"player_id": 4, "lock_status": "BYE", "kickoff_utc": None}],
        "current_bench": [
        {"player_id": 5, "lock_status": "UNLOCKED", "kickoff_utc": "2026-10-25T20:05:00+00:00", "on_ir": False},
        {"player_id": 6, "lock_status": "UNLOCKED", "kickoff_utc": "2026-10-25T18:00:00+00:00", "on_ir": True}]}
    out = lock_summary(ss, {"value": None, "display": None, "reason": "not synced"})
    assert out["next_kickoff_utc"] == "2026-10-25T20:05:00+00:00"           # the IR player cannot be moved in
    assert out["starters"] == {"unlocked": 1, "locked": 1, "unknown": 1, "no_game": 1}
    assert "6" not in out["players"] and out["players"]["3"]["kickoff_utc"] is None
    assert lock_summary({}, None)["next_kickoff_utc"] is None


def test_the_trimmed_start_sit_keeps_the_engines_all_locked_flag():
    """#165: the card's decisions read Start/Sit's own all_locked flag, which incomplete_data hides from the status."""
    from app.api.action_center import _trim_start_sit
    summary = {"starters_count": 9, "status": "incomplete_data", "projection_complete": False, "all_locked": True,
               "bench_count": 6}
    assert _trim_start_sit({"summary": summary})["summary"] == {
        "starters_count": 9, "status": "incomplete_data", "projection_complete": False, "all_locked": True}


def test_a_request_never_has_more_than_its_concurrency_in_flight(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    from app.api import action_center
    monkeypatch.setenv("ACTION_CENTER_CONCURRENCY", "2")
    lock, live, peak = threading.Lock(), [0], [0]

    def fake(principal, access, now):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"league": {"league_id": access.league_id}, "stale": False}

    monkeypatch.setattr(action_center, "evaluate_league", fake)
    leagues = [SimpleNamespace(league_id=f"lg_{i:024d}", to_json=lambda: {}) for i in range(6)]
    import asyncio
    body, degraded = asyncio.run(action_center.build(SimpleNamespace(user_id=U1), leagues))
    assert [s["league"]["league_id"] for s in body["leagues"]] == [a.league_id for a in leagues]
    assert peak[0] == 2 and degraded is False


# ---- review fixes: admission, single flight, cold loads, partial results, one failing league ---------------------------

def _fire(client, uids, n_each=1):
    """Concurrent requests (one thread each); returns [(uid, status, seconds, response)] in completion order."""
    import threading
    import time
    out, lock = [], threading.Lock()

    def one(uid):
        t0 = time.monotonic()
        r = client.get(URL, headers=bearer(uid))
        with lock:
            out.append((uid, r.status_code, time.monotonic() - t0, r))

    threads = [threading.Thread(target=one, args=(u,)) for u in uids for _ in range(n_each)]
    for t in threads:
        t.start()
    return threads, out


def _slow_for(monkeypatch, slow_ids, seconds):
    """evaluate_league that sleeps for `slow_ids` and counts every evaluation per league."""
    import threading
    import time
    from app.api import action_center
    calls, lock = {}, threading.Lock()
    real = action_center.evaluate_league

    def fake(p, a, now):
        with lock:
            calls[a.league_id] = calls.get(a.league_id, 0) + 1
        if a.league_id in slow_ids:
            time.sleep(seconds)
        return real(p, a, now)

    monkeypatch.setattr(action_center, "evaluate_league", fake)
    return calls


def test_one_users_burst_shares_one_build_and_never_starves_another_user(pg, client, served, monkeypatch):
    import time
    from app.api import action_center
    with pg.connect() as con:
        mine = [seed_league(con, visibility="private", checked_at=None, name=f"Mine {i}") for i in range(2)]
        theirs = seed_league(con, visibility="private", checked_at=None, name="Theirs")
        con.commit()
    for lg in mine:
        _member(pg, U1, lg)
    _member(pg, U2, theirs)
    calls = _slow_for(monkeypatch, set(mine), 3.0)
    threads, out = _fire(client, [U1], n_each=2 * action_center.POOL_WORKERS)
    time.sleep(0.5)                                         # U1's build is running
    t0 = time.monotonic()
    other = client.get(URL, headers=bearer(U2))
    elapsed = time.monotonic() - t0
    for t in threads:
        t.join()
    assert other.status_code == 200 and elapsed < 2.0, (other.status_code, elapsed)    # not queued behind U1
    assert [s["league"]["league_id"] for s in other.json()["leagues"]] == [theirs]
    assert all(status == 200 for _, status, _, _ in out), [s for _, s, _, _ in out]
    assert all(calls[lg] == 1 for lg in mine), calls        # every U1 request joined the one build
    bodies = {tuple(s["league"]["league_id"] for s in r.json()["leagues"]) for _, _, _, r in out}
    assert len(bodies) == 1 and set(next(iter(bodies))) == set(mine)


def test_past_the_admission_slots_the_answer_is_503_after_a_short_wait(pg, client, served, monkeypatch):
    import time
    from app.api import action_center
    monkeypatch.setenv("ACTION_CENTER_CONCURRENCY", "6")    # 6 workers // 6 = one build at a time in this process
    with pg.connect() as con:
        mine = seed_league(con, visibility="private", checked_at=None, name="Mine")
        theirs = seed_league(con, visibility="private", checked_at=None, name="Theirs")
        con.commit()
    _member(pg, U1, mine)
    _member(pg, U2, theirs)
    _slow_for(monkeypatch, {mine}, 4.0)
    threads, out = _fire(client, [U1])
    time.sleep(0.5)
    t0 = time.monotonic()
    busy = client.get(URL, headers=bearer(U2))
    elapsed = time.monotonic() - t0
    for t in threads:
        t.join()
    assert busy.status_code == 503 and busy.headers["retry-after"], busy.status_code
    assert action_center.ADMISSION_WAIT_S - 0.2 < elapsed < action_center.ADMISSION_WAIT_S + 1.0, elapsed
    assert "Theirs" not in busy.text and "Mine" not in busy.text
    assert out[0][1] == 200
    assert client.get(URL, headers=bearer(U2)).status_code == 200                  # the slot is free again


def test_a_waiting_user_gets_the_slot_that_frees_within_the_window(pg, client, served, monkeypatch):
    import time
    monkeypatch.setenv("ACTION_CENTER_CONCURRENCY", "6")    # one slot
    with pg.connect() as con:
        mine = seed_league(con, visibility="private", checked_at=None, name="Mine")
        theirs = seed_league(con, visibility="private", checked_at=None, name="Theirs")
        con.commit()
    _member(pg, U1, mine)
    _member(pg, U3, theirs)
    _slow_for(monkeypatch, {mine}, 1.0)
    threads, out = _fire(client, [U1])
    time.sleep(0.4)
    third = client.get(URL, headers=bearer(U3))             # waits ~0.6 s for U1's slot, then builds
    for t in threads:
        t.join()
    assert third.status_code == 200 and _ids(third.json()) == [theirs]
    assert out[0][1] == 200


def test_two_users_with_the_same_leagues_never_share_a_build(pg, client, served, monkeypatch):
    with pg.connect() as con:
        shared = seed_league(con, visibility="private", checked_at=None, name="Shared")
        con.commit()
    _member(pg, U1, shared)
    _member(pg, U2, shared)
    calls = _slow_for(monkeypatch, {shared}, 1.0)
    threads, out = _fire(client, [U1, U2])
    for t in threads:
        t.join()
    assert sorted(status for _, status, _, _ in out) == [200, 200]
    assert calls[shared] == 2                                # each user's own evaluation, never joined


def test_one_user_never_holds_two_slots_even_for_a_different_league_set(pg, client, served, monkeypatch):
    import threading
    import time
    from app.api import action_center
    with pg.connect() as con:
        a = seed_league(con, visibility="private", checked_at=None, name="A")
        b = seed_league(con, visibility="private", checked_at=None, name="B")
        con.commit()
    _member(pg, U1, a)
    _slow_for(monkeypatch, {a, b}, 1.0)
    live, peak, lock = [0], [0], threading.Lock()
    real_build = action_center.build

    async def counted(principal, granted, now=None):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            return await real_build(principal, granted, now)
        finally:
            with lock:
                live[0] -= 1

    monkeypatch.setattr(action_center, "build", counted)
    first, out1 = _fire(client, [U1])
    time.sleep(0.4)
    _member(pg, U1, b)                                       # the second request is authorized for {a, b}
    second, out2 = _fire(client, [U1])
    for t in first + second:
        t.join()
    assert peak[0] == 1
    assert out1[0][1] == out2[0][1] == 200
    assert set(_ids(out2[0][3].json())) == {a, b}            # it waited, then built its own (not the other's) result


def test_a_cancelled_build_leaves_nothing_queued(monkeypatch):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace
    from app.api import action_center
    monkeypatch.setattr(action_center, "POOL_WORKERS", 1)  # the second league waits in the pool's queue
    monkeypatch.setenv("ACTION_CENTER_CONCURRENCY", "2")
    started, lock = [], threading.Lock()

    def fake(principal, access, now):
        with lock:
            started.append(access.league_id)
        time.sleep(0.5)
        return {"league": {"league_id": access.league_id}, "stale": False}

    monkeypatch.setattr(action_center, "evaluate_league", fake)
    leagues = [SimpleNamespace(league_id=f"lg_{i:024d}", to_json=lambda: {}) for i in range(2)]

    async def run():
        task = asyncio.ensure_future(action_center.build(SimpleNamespace(user_id=U1), leagues))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    time.sleep(1.0)                                          # the running one finishes; the queued one must not start
    assert started == [leagues[0].league_id]


def test_a_cold_league_never_downloads_in_a_pool_thread_past_the_budget(pg, served, client, e2e, tmp_path, monkeypatch):
    import time
    from app.api import action_center
    from app.api.league_scope import league_file_cache
    from app.db.league_files import DirStore
    monkeypatch.setenv("LEAGUE_COLD_LOAD_BUDGET_S", "0.3")
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Cold")
    _member(pg, U1, pub.league_id)
    _select_team(pg, U1, pub.league_id, my_team(e2e))
    real_fetch = DirStore.fetch
    monkeypatch.setattr(DirStore, "fetch", lambda self, *a, **k: (time.sleep(3), real_fetch(self, *a, **k))[1])
    t0 = time.monotonic()
    r = client.get(URL, headers=bearer(U1))
    elapsed = time.monotonic() - t0
    s = r.json()["leagues"][0]
    assert r.status_code == 200 and elapsed < 2.0, elapsed
    assert s["state"] == "not_ready" and s["reason"] == action_center.TOO_SLOW and s["overview"] is None
    assert league_file_cache()._pins == {}                  # nothing is left pinned by an abandoned evaluation


def test_a_partial_read_is_not_cached(pg, served, client, e2e, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app.api import start_sit
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Partial")
    _member(pg, U1, pub.league_id)
    _select_team(pg, U1, pub.league_id, my_team(e2e))
    real, failed = start_sit.get_start_sit, []

    def flaky(**kw):
        if not failed:
            failed.append(1)
            raise HTTPException(503, "Projections are being rebuilt.")
        return real(**kw)

    monkeypatch.setattr(start_sit, "get_start_sit", flaky)
    first = client.get(URL, headers=bearer(U1)).json()["leagues"][0]
    assert first["state"] == "ready" and first["start_sit"] is None and first["errors"]["start_sit"]
    second = client.get(URL, headers=bearer(U1)).json()["leagues"][0]
    assert second["start_sit"] is not None and second["errors"] == {}


def test_one_league_failing_unexpectedly_never_fails_the_others(pg, served, client, e2e, tmp_path, monkeypatch):
    from app.api import provider_copy
    monkeypatch.setenv("SLEEPER_SOURCE", "live")
    espn = publish_local(pg, e2e, served, tmp_path / "e", visibility="private", name="Fine League")
    sleeper = publish_local(pg, e2e, served, tmp_path / "s", visibility="private", name="Broken League",
                            provider="sleeper", external_id="5555")
    for lg in (espn, sleeper):
        _member(pg, U1, lg.league_id)
        _select_team(pg, U1, lg.league_id, my_team(e2e))

    def boom(*a, **k):
        raise RuntimeError("secret detail that must not leak")

    monkeypatch.setattr(provider_copy, "rewrite", boom)
    r = client.get(URL, headers=bearer(U1))
    assert r.status_code == 200
    by = {s["league"]["name"]: s for s in r.json()["leagues"]}
    assert by["Broken League"]["state"] == "unavailable" and by["Broken League"]["overview"] is None
    assert by["Fine League"]["state"] == "ready" and by["Fine League"]["overview"]["roster"]
    assert "secret detail" not in r.text


def test_a_deterministic_error_is_cached_briefly_and_a_transient_one_is_not(pg, served, client, e2e, tmp_path,
                                                                             monkeypatch):
    from fastapi import HTTPException
    from app.api import action_center, start_sit
    pub = publish_local(pg, e2e, served, tmp_path / "w", visibility="private", name="Offseason")
    _member(pg, U1, pub.league_id)
    _select_team(pg, U1, pub.league_id, my_team(e2e))
    calls = []

    def offseason(**kw):
        calls.append(1)
        raise HTTPException(422, "Week 19 is outside valid fantasy season (1-17)")

    monkeypatch.setattr(start_sit, "get_start_sit", offseason)
    one = client.get(URL, headers=bearer(U1)).json()["leagues"][0]
    two = client.get(URL, headers=bearer(U1)).json()["leagues"][0]
    assert len(calls) == 1 and one["errors"]["start_sit"] and two == one
    cache = action_center.summary_cache()
    (expires, _, _), = cache._entries.values()
    assert expires - cache.clock() <= action_center.ERROR_TTL_S
    assert action_center.deterministic(HTTPException(503, "This league's data (schema v6) is older than v8."))
    assert not action_center.deterministic(HTTPException(503, "Projections are being rebuilt."))
    assert not action_center.deterministic(HTTPException(500, "boom"))


def test_one_user_never_holds_two_slots_across_kinds(monkeypatch):
    """#300 review: the portfolio shares the action center's admission slots with its own flight per kind. A user's
    action center and portfolio requests at once must not take both slots (2 by default): the second kind waits for the
    first, and another user still gets a slot at once. No Postgres: serve() with stand-in builds."""
    import asyncio
    from types import SimpleNamespace
    from app.api import action_center
    monkeypatch.setattr(action_center, "POOL_WORKERS", 6)
    monkeypatch.setenv("ACTION_CENTER_CONCURRENCY", "3")
    monkeypatch.setattr(action_center, "ADMISSION_WAIT_S", 0.2)   # far below a build: a taken slot means Busy
    assert action_center.admission_slots() == 2
    running: dict[str, int] = {}
    peak: dict[str, int] = {}

    def slow(tag):
        async def build(principal, granted):
            running[principal.user_id] = running.get(principal.user_id, 0) + 1
            peak[principal.user_id] = max(peak.get(principal.user_id, 0), running[principal.user_id])
            await asyncio.sleep(0.6)
            running[principal.user_id] -= 1
            return {"kind": tag, "who": principal.user_id}
        return build

    alice, bob = SimpleNamespace(user_id="u-alice"), SimpleNamespace(user_id="u-bob")
    granted = [SimpleNamespace(league_id="lg_aaaaaaaaaaaaaaaa")]

    async def run():
        a1 = asyncio.ensure_future(action_center.serve(alice, granted, build_fn=slow("action-center")))
        a2 = asyncio.ensure_future(action_center.serve(alice, granted, kind="portfolio", build_fn=slow("portfolio"),
                                                       budget_s=5))
        await asyncio.sleep(0.1)
        b = await action_center.serve(bob, granted, build_fn=slow("action-center"))      # never Busy
        return await a1, await a2, b

    first, second, other = asyncio.run(run())
    assert first["kind"] == "action-center" and second["kind"] == "portfolio" and other["who"] == "u-bob"
    assert peak == {"u-alice": 1, "u-bob": 1}                        # alice's two kinds ran one after the other
    assert action_center._HOLDERS == {} and action_center._FLIGHTS == {}


def test_a_failed_or_cancelled_slot_holder_leaves_no_entry_and_the_waiting_kind_builds(monkeypatch):
    """#300 review: the per-user slot holder is released in `finally` whether its build raises or is cancelled, so the
    same user's other kind then builds and nothing stays registered."""
    import asyncio
    from types import SimpleNamespace
    from app.api import action_center
    monkeypatch.setattr(action_center, "_HOLDERS", {})
    monkeypatch.setattr(action_center, "_FLIGHTS", {})
    monkeypatch.setattr(action_center, "_ADMISSION", {})
    alice = SimpleNamespace(user_id="u-alice")
    granted = [SimpleNamespace(league_id="lg_aaaaaaaaaaaaaaaa")]

    async def boom(principal, granted):
        await asyncio.sleep(0.2)
        raise RuntimeError("build failed")

    async def hang(principal, granted):
        await asyncio.sleep(30)

    async def ok(principal, granted):
        return {"built": True}

    async def run():
        first = asyncio.ensure_future(action_center.serve(alice, granted, build_fn=boom))
        await asyncio.sleep(0.05)
        second = await action_center.serve(alice, granted, kind="portfolio", build_fn=ok, budget_s=5)
        with pytest.raises(RuntimeError):
            await first
        assert second == {"built": True} and action_center._HOLDERS == {}
        held = asyncio.ensure_future(action_center.serve(alice, granted, build_fn=hang))
        await asyncio.sleep(0.05)
        held.cancel()
        with pytest.raises(asyncio.CancelledError):
            await held
        assert action_center._HOLDERS == {} and action_center._FLIGHTS == {}
        assert await action_center.serve(alice, granted, kind="portfolio", build_fn=ok, budget_s=5) == {"built": True}

    asyncio.run(run())

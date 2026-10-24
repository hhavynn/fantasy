"""Trade discovery lets its league files go before the in-memory scan (issue #331): everything the build reads from the
files (the data version and every opponent's manager context) is read first, the connection is closed, the request's
pin is released (deps.release_league_files), and only then does the search run, so 25 concurrent scans no longer pin
their files for the whole search. The answer is unchanged; the pair is released exactly once."""
import pytest

from app.api import deps
from test_league_scope import client, e2e, served  # noqa: F401  (fixtures)
from test_leagues_api import env  # noqa: F401  (fixture)
from league_fixtures import publish_local


def _timeless(x):
    """The answer without the search's own wall-clock `seconds` (they differ on every run)."""
    if isinstance(x, dict):
        return {k: _timeless(v) for k, v in x.items() if k != "seconds"}
    if isinstance(x, list):
        return [_timeless(v) for v in x]
    return x


@pytest.mark.parametrize("path", ["/api/trades/discover/summary?team_id=1&mode=mutual_upgrades",
                                  "/api/trades/discover?team_id=1&mode=mutual_upgrades"])
def test_the_scan_runs_with_no_file_pinned_and_the_pair_is_released_once(pg, served, client, e2e, tmp_path,
                                                                         monkeypatch, path):
    from app.api import league_scope
    from app.api import trade_discovery as api
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    url = f"/v2/leagues/{a.league_id}{path}"
    expected = client.get(url)                          # a memo miss through the whole route, as before
    assert expected.status_code == 200, expected.text[:300]

    cache = league_scope.league_file_cache()
    pinned_during_scan, released = [], []
    real_search, real_release = api.TradeSearch, cache.release_pair

    def search(*args, **kwargs):
        with cache._lock:
            pinned_during_scan.append(dict(cache._pins))
        return real_search(*args, **kwargs)

    def release_pair(pair):
        released.append(pair)
        real_release(pair)

    monkeypatch.setattr(api, "TradeSearch", search)
    monkeypatch.setattr(cache, "release_pair", release_pair)
    api._CACHE.clear()
    api._VIEWS.clear()
    monkeypatch.setattr(league_scope, "_RESPONSES", {})
    again = client.get(url)
    assert again.status_code == 200 and _timeless(again.json()) == _timeless(expected.json())    # the same answer
    assert pinned_during_scan == [{}]                   # the search ran with the league's and global files unpinned
    assert len(released) == 1                           # early, and never again at the end of the request
    with cache._lock:
        assert cache._pins == {}


def test_evidence_after_a_released_summary_still_reads_its_files(pg, served, client, e2e, tmp_path):
    """The row's evidence is a separate request: it pins and reads the files again (its ratings), from the view the
    summary stored."""
    from app.api import trade_discovery as api
    a = publish_local(pg, e2e, served, tmp_path / "a", visibility="public", name="A")
    api._CACHE.clear()
    api._VIEWS.clear()
    q = "team_id=1&mode=mutual_upgrades"
    summary = client.get(f"/v2/leagues/{a.league_id}/api/trades/discover/summary?{q}")
    assert summary.status_code == 200
    rows = [x for items in summary.json()["sections"].values() for x in items]
    assert rows
    ev = client.get(f"/v2/leagues/{a.league_id}/api/trades/discover/evidence?{q}&key={rows[0]['key']}")
    assert ev.status_code == 200 and ev.json()["key"] == rows[0]["key"]


def test_files_are_released_once_whoever_releases_first():
    calls = []
    held = deps.HeldFiles(lambda: calls.append(1))
    token = deps.LEAGUE_FILES.set(held)
    try:
        deps.release_league_files()
        deps.release_league_files()
    finally:
        deps.LEAGUE_FILES.reset(token)
    held.release()                                      # the end of the request
    assert calls == [1] and held.released


def test_db_refuses_released_files_before_opening_them(monkeypatch):
    opened = []
    monkeypatch.setattr(deps, "layered_databases", lambda: opened.append(1))
    scope = deps.LeagueScope("lg", "/nowhere/l.duckdb", "/nowhere/g.duckdb", "d", "g", 1, None)
    held = deps.HeldFiles(lambda: None)
    held.release()
    tokens = deps.LEAGUE_SCOPE.set(scope), deps.LEAGUE_FILES.set(held)
    try:
        with pytest.raises(RuntimeError, match="release_league_files"):
            with deps.db():
                pass
    finally:
        deps.LEAGUE_FILES.reset(tokens[1])
        deps.LEAGUE_SCOPE.reset(tokens[0])
    assert opened == []


def test_outside_a_league_request_releasing_does_nothing():
    assert deps.LEAGUE_FILES.get() is None
    deps.release_league_files()

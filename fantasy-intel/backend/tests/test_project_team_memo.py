"""The trade-discovery scan computes each lineup projection once (issue #331): `team_lineups.project_team_memo` keeps one
scan's project_team results, for the scan's own projections object only. The answers are identical with the memo on and
off, no result or projection changes while it is shared, and the memo is empty once the scan is over."""
import contextlib
import copy
import json
import typing

import pytest
from fastapi.testclient import TestClient
from golden.golden import E2E_SEASON, build_e2e_db

from app.analytics import team_lineups as tl
from app.api import trade_discovery as api

QUERIES = [f"team_id={t}&mode={m}" for t in (1, 2) for m in ("practical", "mutual_upgrades", "all", "targeted")] + [
    "team_id=1&mode=practical&include_kdst=true", "team_id=1&mode=practical&kdst_streaming=false",
    "team_id=1&mode=practical&phase=playoff", "team_id=1&mode=targeted&need=weakest",
    "team_id=1&mode=targeted&need=bye", "team_id=1&mode=practical&shapes=1-for-1,2-for-1,1-for-2,2-for-2,3-for-1",
    "team_id=3&mode=practical&strictness=strict"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = build_e2e_db(tmp_path_factory.mktemp("memo") / "e2e.duckdb")
    mp = pytest.MonkeyPatch()
    mp.setenv("FANTASY_DB_PATH", str(db))
    mp.setenv("ESPN_SEASON", E2E_SEASON)
    from app.main import app
    yield TestClient(app)
    mp.undo()


def _timeless(x):
    """The answer without the search's own wall-clock `seconds` (they differ on every run)."""
    if isinstance(x, dict):
        return {k: _timeless(v) for k, v in x.items() if k != "seconds"}
    if isinstance(x, list):
        return [_timeless(v) for v in x]
    return x


def _discover(client, q):
    api._CACHE.clear()
    api._VIEWS.clear()
    r = client.get(f"/api/trades/discover?{q}")
    assert r.status_code == 200, r.text[:300]
    return _timeless(r.json())


def test_every_answer_is_identical_with_the_memo_off(client, monkeypatch):
    on = {q: _discover(client, q) for q in QUERIES}
    monkeypatch.setattr(api, "project_team_memo", lambda *a, **k: contextlib.nullcontext())
    off = {q: _discover(client, q) for q in QUERIES}
    for q in QUERIES:
        assert on[q] == off[q], q


STREAM_ATTRS = ("stream", "stream_reference", "stream_rank")   # StreamingProjections' baseline, beside the dict


class _Watched(tl.TeamMemo):
    """Snapshots each result when stored and the projections when the scan starts, to check nothing changes them."""
    made: typing.ClassVar[list] = []

    def __init__(self, proj, scope):
        super().__init__(proj, scope)
        self.proj_at_start = copy.deepcopy(proj)
        self.stream_at_start = {k: copy.deepcopy(getattr(proj, k, None)) for k in STREAM_ATTRS}
        self.snapshots, self.hits = {}, 0
        _Watched.made.append(self)
        entries, snapshots, memo = self.entries, self.snapshots, self

        class Entries(dict):
            def get(self, key, default=None):
                hit = super().get(key, default)
                memo.hits += hit is not None
                return hit

            def __setitem__(self, key, value):
                snapshots[key] = json.dumps(value, sort_keys=True, default=str)
                super().__setitem__(key, value)

            def clear(self):
                memo.unchanged = all(json.dumps(v, sort_keys=True, default=str) == snapshots[k] for k, v in self.items())
                first = memo.projs[0]
                memo.proj_unchanged = first == memo.proj_at_start and all(
                    getattr(first, k, None) == v for k, v in memo.stream_at_start.items())
                memo.size_at_end = len(self)
                super().clear()
        self.entries = Entries(entries)


@pytest.mark.parametrize("q", ["team_id=1&mode=mutual_upgrades", "team_id=2&mode=practical&include_kdst=true"])
def test_shared_results_and_the_projections_never_change_and_the_memo_ends_empty(client, monkeypatch, q):
    _Watched.made = []
    monkeypatch.setattr(tl, "TeamMemo", _Watched)
    _discover(client, q)
    (memo,) = _Watched.made                                  # one memo per scan
    assert memo.size_at_end > 0 and memo.hits > memo.size_at_end      # it was used: most calls repeat
    assert memo.unchanged and memo.proj_unchanged            # no caller mutated a shared result or the projections
    assert memo.entries == {}                                # emptied when the scan ends


def test_a_search_cached_by_an_earlier_request_is_memoized_too(client, monkeypatch):
    """Review #352: with the trade search cached and the view not (another filter), the scan works on the cached
    search's projections object; the memo admits it, and the answer is still the one computed without the memo."""
    _discover(client, "team_id=1&mode=mutual_upgrades")              # caches the search
    q = "team_id=1&mode=practical"
    api._VIEWS.clear()
    _Watched.made = []
    monkeypatch.setattr(tl, "TeamMemo", _Watched)
    warm = client.get(f"/api/trades/discover?{q}")
    (memo,) = _Watched.made
    assert len(memo.projs) == 2 and memo.hits > 0 and memo.unchanged
    monkeypatch.setattr(api, "project_team_memo", lambda *a, **k: contextlib.nullcontext())
    api._VIEWS.clear()
    assert _timeless(warm.json()) == _timeless(client.get(f"/api/trades/discover?{q}").json())


def test_only_the_scans_own_projections_are_memoized():
    players = [tl.RosterPlayer(1, "QB", pro_team="KC")]
    window = tl.Window((5, 6), 14)
    proj, other = {1: {5: 20.0, 6: 18.0}}, {1: {5: 20.0, 6: 18.0}}
    with tl.project_team_memo(proj, ("v1", True)) as memo:
        a = tl.project_team(players, ["QB"], window, proj)
        assert tl.project_team(players, ["QB"], window, proj) is a          # the same inputs: the stored result
        assert tl.project_team(players, ["QB"], window, other) is not a     # an equal but other object: computed
        two = players + [tl.RosterPlayer(2, "QB")]
        b = tl.project_team(two, ["QB"], window, proj)
        assert tl.project_team(list(reversed(two)), ["QB"], window, proj) is not b   # the order is part of the key
        assert len(memo.entries) == 3
    assert memo.entries == {} and tl.project_team(players, ["QB"], window, proj) is not a   # off after the block


def test_slot_lists_with_other_unsupported_slots_never_share_a_result():
    """Codex #352: lineup_slots({'QB': 1}) and lineup_slots({'QB': 1, 'LB': 1}) are both ['QB'], but the second carries
    LB as unsupported, so its totals are partial: the memo must keep them apart."""
    from app.analytics.lineup import lineup_slots
    players = [tl.RosterPlayer(1, "QB", pro_team="KC")]
    window = tl.Window((5, 6), 14)
    proj = {1: {5: 20.0, 6: 18.0}}
    plain, with_idp = lineup_slots({"QB": 1}), lineup_slots({"QB": 1, "LB": 1})
    assert list(plain) == list(with_idp)
    with tl.project_team_memo(proj, ("v1", True)):
        a = tl.project_team(players, plain, window, proj)
        b = tl.project_team(players, with_idp, window, proj)
    assert b is not a and b == tl.project_team(players, with_idp, window, proj)    # the uncached answer

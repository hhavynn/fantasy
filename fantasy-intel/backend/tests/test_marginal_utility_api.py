"""GET /api/my-team/value/marginal (#216 PR 2): the merged marginal-utility evaluator behind a read-only route on the
team value router. The answer is the evaluator's own on the same state, the waiver pool is only free agents whose
availability is synced (none synced: unknown, never a free replacement), and nothing is written."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.analytics import playoff_race
from app.analytics.marginal_utility import marginal_utility
from app.analytics.team_lineups import source_label
from app.analytics.trades import with_streaming
from app.db.connection import connect

from test_trade_data import _raw_snapshot, synced

PATH = "/api/my-team/value/marginal"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "mu.duckdb"
    con = synced(db_path)
    raw = _raw_snapshot(con)
    con.close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    client = TestClient(app)
    yield client, db_path
    assert db_path.stat().st_mtime_ns == mtime                     # nothing written
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    con.close()


def _state(db_path):
    con = connect(db_path, read_only=True)
    race = playoff_race.load_race_data(con)
    con.close()
    return race, with_streaming(race.state)


def _mine(race):
    return next(t["team_id"] for t in race.teams if t["is_mine"])


def test_the_route_is_the_evaluator_on_the_team_value_state(api):
    client, db_path = api
    race, state = _state(db_path)
    tid = _mine(race)
    roster = state.rosters[tid]
    ids = [p.espn_player_id for p in roster if not p.reserved][:2]
    res = client.get(PATH, params={"ids": ids})
    assert res.status_code == 200, res.text
    body = res.json()
    direct = marginal_utility(roster, state.slots, state.window(), state.projections, ids,
                              projection_source=source_label(state.projection_source, state.projections))
    assert body["contract"] == "roster-utility/v1" and body["team"]["team_id"] == tid and body["pool"] == "bench"
    assert body["change"] == direct["change"] and body["removal"] == direct["removal"]
    assert body["nonadditive"] == direct["nonadditive"] and "backfill" in body["nonadditive_note"]
    assert body["projection_source"] and body["horizon"]["weeks"] == list(state.window().weeks)
    assert {str(i) for i in ids} <= set(body["names"]) and body["provenance"]


def test_another_team_and_a_single_player(api):
    client, db_path = api
    race, state = _state(db_path)
    other = next(t["team_id"] for t in race.teams if not t["is_mine"])
    pid = state.rosters[other][0].espn_player_id
    body = client.get(PATH, params={"ids": [pid], "team_id": other}).json()
    assert body["team"]["team_id"] == other and body["asset_ids"] == [pid] and "nonadditive" not in body


def test_the_waiver_pool_takes_only_free_agents_whose_availability_is_synced(api, monkeypatch):
    client, db_path = api
    race, state = _state(db_path)
    pid = next(p.espn_player_id for p in state.rosters[_mine(race)] if not p.reserved)
    body = client.get(PATH, params={"ids": [pid], "pool": "waiver"}).json()
    known = [p for p in state.free_agents if state.free_agent_status.get(p.espn_player_id) in ("FREEAGENT", "WAIVERS")]
    if known:
        assert body["status"] in ("evaluated", "partial_pool") and body["change"] is not None
        assert body["waiver_availability"]["availability_known"] == len(known)
    else:
        assert body["status"] == "unknown_pool"
    # No availability synced for anyone: the pool is unknown, never a free replacement.
    real = playoff_race.load_race_data
    monkeypatch.setattr("app.api.team_value.load_race_data",
                        lambda con: replace(r := real(con), state=replace(r.state, free_agent_status={})))
    blind = client.get(PATH, params={"ids": [pid], "pool": "waiver"}).json()
    assert blind["status"] == "unknown_pool" and blind["change"] is None and blind["replacement"] is None
    if state.free_agents:
        assert blind["waiver_availability"]["excluded_unknown_availability"] == len(state.free_agents)


def test_bad_requests_are_refused(api):
    client, db_path = api
    race, state = _state(db_path)
    ids = [p.espn_player_id for p in state.rosters[_mine(race)]]
    assert client.get(PATH).status_code == 422                                  # no player named
    assert client.get(PATH, params={"ids": ids[:4]}).status_code == 422         # more than three
    assert client.get(PATH, params={"ids": ids[:1], "pool": "trade"}).status_code == 422
    assert client.get(PATH, params={"ids": ids[:2], "pool": "waiver"}).status_code == 422   # one player at a time
    assert client.get(PATH, params={"ids": [123456789]}).status_code == 422     # not on this roster
    assert client.get(PATH, params={"ids": ids[:1], "team_id": 999}).status_code == 404


def test_a_waiver_replacement_keeps_the_league_position_limits(api, monkeypatch):
    """The fixture's best free agent for a removed WR is an RB (209, through FLEX). With the RB limit already reached,
    removing a WR frees no RB spot, so that RB is not a legal replacement and is never chosen."""
    client, db_path = api
    race, state = _state(db_path)
    roster = state.rosters[_mine(race)]
    wr = next(p for p in roster if p.position == "WR" and not p.reserved)
    free = client.get(PATH, params={"ids": [wr.espn_player_id], "pool": "waiver"}).json()
    assert free["replacement"] and free["replacement"]["position"] == "RB"        # the case the limit must change
    rbs = sum(1 for p in roster if p.position == "RB")
    real = playoff_race.load_race_data
    monkeypatch.setattr("app.api.team_value.load_race_data", lambda con: replace(
        r := real(con), state=replace(r.state, position_limits={"RB": rbs})))
    capped = client.get(PATH, params={"ids": [wr.espn_player_id], "pool": "waiver"}).json()
    assert capped["replacement"] is None or capped["replacement"]["position"] != "RB"
    assert capped["replacement"] is None or capped["replacement"]["espn_player_id"] != free["replacement"]["espn_player_id"]
    assert capped["waiver_availability"]["excluded_position_limit"] >= 1

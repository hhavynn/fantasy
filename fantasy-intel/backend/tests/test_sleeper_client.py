"""Sleeper HTTP client: league id validation, 429 / 5xx retry, TTL cache, players-file freshness.
No network: every response comes from tests/fixtures/sleeper via a scripted transport."""

import json

import pytest

from app.sleeper.client import (PLAYERS_FILE, InvalidLeagueId, SleeperClient, SleeperHTTPError, SleeperRateLimited)
from app.sleeper.league import fetch_league
from sleeper_fixtures import LEAGUE_ID, Clock, FakeTransport, body, league_routes, make_client, ok, status

DAY = 24 * 3600


# ------------------------------------------------------------------ league ids --

@pytest.mark.parametrize("bad", [1180000000000000001, 1.18e18, None])
def test_league_id_must_be_a_string_and_is_never_converted(tmp_path, bad):
    client, transport, _ = make_client(tmp_path)
    with pytest.raises(TypeError, match="strings"):
        client.league(bad)
    assert transport.calls == []


@pytest.mark.parametrize("bad", ["", "  ", "abc", "1180-0001", "../state/nfl"])
def test_malformed_league_id_rejected_before_any_request(tmp_path, bad):
    client, transport, _ = make_client(tmp_path)
    with pytest.raises(InvalidLeagueId) as e:
        client.league(bad)
    assert "digits" in e.value.reason
    assert transport.calls == []


def test_unknown_league_404_is_a_clear_invalid_league_error(tmp_path):
    client, _, _ = make_client(tmp_path, {"/league/123": status(404)})
    with pytest.raises(InvalidLeagueId, match="no league with this id") as e:
        client.league("123")
    assert e.value.league_id == "123"
    assert "sleeper.com/leagues" in str(e.value)


def test_unknown_league_null_body_is_invalid(tmp_path):
    client, _, _ = make_client(tmp_path, {"/league/123": ok("null"), "/league/123/rosters": status(404, payload=b"null")})
    with pytest.raises(InvalidLeagueId):
        client.league("123")
    with pytest.raises(InvalidLeagueId):
        client.league_rosters("123")


def test_non_nfl_league_rejected(tmp_path):
    client, _, _ = make_client(tmp_path, {"/league/55": ok(json.dumps({"league_id": "55", "sport": "nba"}))})
    with pytest.raises(InvalidLeagueId, match="nba"):
        client.league("55")


def test_fetch_league_stops_before_rosters_for_an_invalid_league(tmp_path):
    client, transport, _ = make_client(tmp_path, {"/league/77": status(404)})
    with pytest.raises(InvalidLeagueId):
        fetch_league(client, "77")
    assert transport.calls == ["/league/77"]


# ---------------------------------------------------------------------- retry --

def test_429_honours_retry_after_then_succeeds(tmp_path):
    client, transport, sleeps = make_client(tmp_path, {"/state/nfl": [
        status(429, {"Retry-After": "2"}), status(429, {"retry-after": "0.5"}), ok(body("state.json"))]})
    assert client.nfl_state()["week"] == 6
    assert sleeps == [2.0, 0.5]
    assert transport.count("/state/nfl") == 3


def test_429_without_retry_after_backs_off_exponentially(tmp_path):
    client, _, sleeps = make_client(tmp_path, {"/state/nfl": [status(429), status(429), ok(body("state.json"))]})
    client.nfl_state()
    assert sleeps == [1.0, 2.0]


def test_429_retry_after_is_capped(tmp_path):
    client, _, sleeps = make_client(tmp_path, {"/state/nfl": [status(429, {"Retry-After": "3600"}), ok(body("state.json"))]})
    client.nfl_state()
    assert sleeps == [30.0]


def test_persistent_429_raises_rate_limited_after_max_retries(tmp_path):
    client, transport, sleeps = make_client(tmp_path, {"/state/nfl": status(429)}, max_retries=2)
    with pytest.raises(SleeperRateLimited):
        client.nfl_state()
    assert transport.count("/state/nfl") == 3
    assert len(sleeps) == 2


def test_5xx_and_network_errors_are_retried(tmp_path):
    client, transport, _ = make_client(tmp_path, {"/state/nfl": [
        status(503), OSError("connection reset"), ok(body("state.json"))]})
    assert client.nfl_state()["season"] == "2026"
    assert transport.count("/state/nfl") == 3


def test_other_http_errors_are_not_retried(tmp_path):
    client, transport, _ = make_client(tmp_path, {"/state/nfl": status(403)})
    with pytest.raises(SleeperHTTPError) as e:
        client.nfl_state()
    assert e.value.status == 403
    assert transport.count("/state/nfl") == 1


# ---------------------------------------------------------------------- cache --

def test_league_calls_cached_for_ttl_then_refetched(tmp_path):
    clock = Clock()
    client, transport, _ = make_client(tmp_path, clock=clock, ttl=60)
    path = f"/league/{LEAGUE_ID}/rosters"
    client.league_rosters(LEAGUE_ID)
    clock.t += 59
    client.league_rosters(LEAGUE_ID)
    assert transport.count(path) == 1
    clock.t += 2
    client.league_rosters(LEAGUE_ID)
    assert transport.count(path) == 2


def test_cache_is_per_league(tmp_path):
    routes = {**league_routes(), "/league/42/rosters": ok("[]")}
    client, transport, _ = make_client(tmp_path, routes)
    client.league_rosters(LEAGUE_ID)
    assert client.league_rosters("42") == []
    assert transport.count("/league/42/rosters") == 1


def test_not_found_is_not_cached(tmp_path):
    client, transport, _ = make_client(tmp_path, {"/league/9": [status(404), ok(json.dumps({"league_id": "9", "sport": "nfl"}))]})
    with pytest.raises(InvalidLeagueId):
        client.league("9")
    assert client.league("9")["league_id"] == "9"
    assert transport.count("/league/9") == 2


# --------------------------------------------------------------- players file --

def _write_players_file(tmp_path, fetched_at: float, players: dict):
    d = tmp_path / "sleeper"
    d.mkdir(parents=True, exist_ok=True)
    (d / PLAYERS_FILE).write_text(json.dumps({"fetched_at": fetched_at, "players": players}))


def test_players_fetched_once_and_stored_on_disk(tmp_path):
    clock = Clock()
    client, transport, _ = make_client(tmp_path, clock=clock)
    players = client.players()
    assert client.players_source == "network"
    assert "DET" in players and all(isinstance(k, str) for k in players)
    clock.t += DAY - 1
    client.players()
    assert client.players_source == "memory"
    assert transport.count("/players/nfl") == 1
    doc = json.loads((tmp_path / "sleeper" / PLAYERS_FILE).read_text())
    assert doc["fetched_at"] == 1_800_000_000.0 and doc["players"].keys() == players.keys()


def test_fresh_disk_copy_is_reused_by_a_new_client(tmp_path):
    clock = Clock()
    _write_players_file(tmp_path, clock.t - (DAY - 60), {"1": {"player_id": "1"}})
    client, transport, _ = make_client(tmp_path, clock=clock)
    assert client.players() == {"1": {"player_id": "1"}}
    assert client.players_source == "disk"
    assert transport.count("/players/nfl") == 0


def test_copy_older_than_24h_is_refreshed(tmp_path):
    clock = Clock()
    _write_players_file(tmp_path, clock.t - DAY, {"1": {"player_id": "1"}})      # exactly 24h: stale
    client, transport, _ = make_client(tmp_path, clock=clock)
    assert "4046" in client.players()
    assert client.players_source == "network"
    assert transport.count("/players/nfl") == 1
    assert json.loads((tmp_path / "sleeper" / PLAYERS_FILE).read_text())["fetched_at"] == clock.t


def test_in_memory_copy_expires_after_24h(tmp_path):
    clock = Clock()
    client, transport, _ = make_client(tmp_path, clock=clock)
    client.players()
    clock.t += DAY + 1
    client.players()
    assert transport.count("/players/nfl") == 2


def test_failed_refresh_falls_back_to_the_stale_copy(tmp_path):
    clock = Clock()
    _write_players_file(tmp_path, clock.t - 3 * DAY, {"1": {"player_id": "1"}})
    client, _, _ = make_client(tmp_path, {"/players/nfl": status(503)}, clock=clock, max_retries=1)
    assert client.players() == {"1": {"player_id": "1"}}
    assert client.players_source == "stale_disk"


def test_failed_fetch_without_any_copy_raises(tmp_path):
    client, _, _ = make_client(tmp_path, {"/players/nfl": status(429)}, max_retries=0)
    with pytest.raises(SleeperRateLimited):
        client.players()


def test_force_refresh_ignores_a_fresh_copy(tmp_path):
    clock = Clock()
    _write_players_file(tmp_path, clock.t, {"1": {"player_id": "1"}})
    client, transport, _ = make_client(tmp_path, clock=clock)
    assert "4046" in client.players(force_refresh=True)
    assert transport.count("/players/nfl") == 1


def test_corrupt_players_file_is_treated_as_missing(tmp_path):
    (tmp_path / "sleeper").mkdir()
    (tmp_path / "sleeper" / PLAYERS_FILE).write_text("{not json")
    client, transport, _ = make_client(tmp_path)
    assert "4046" in client.players()
    assert transport.count("/players/nfl") == 1


# ----------------------------------------------------------------- end to end --

def test_fetch_league_end_to_end_keeps_ids_as_strings(tmp_path):
    client, transport, _ = make_client(tmp_path)
    data = fetch_league(client, LEAGUE_ID)
    assert data.league.league_id == LEAGUE_ID
    assert data.league.bracket_id == "1404207029957996999"          # JSON number > 2^53, exact
    assert {r.roster_id for r in data.rosters} == {1, 2, 3, 4}
    assert all(isinstance(r.owner_id, str) for r in data.rosters if r.owner_id)
    assert all(isinstance(pid, str) for r in data.rosters for pid in r.players)
    assert all(isinstance(u.user_id, str) for u in data.users)
    assert "1111" not in data.players                                # only rostered players are parsed
    assert {"DET", "LAR", "4046"} <= data.players.keys()
    assert "99999" not in data.players                               # rostered, but unknown to Sleeper
    assert transport.count("/players/nfl") == 1


def test_default_transport_is_requests(tmp_path):
    client = SleeperClient(cache_dir=tmp_path)
    assert callable(client.transport)


def test_fake_transport_rejects_unscripted_paths():
    t = FakeTransport({})
    with pytest.raises(AssertionError):
        t("https://api.sleeper.app/v1/league/1", 1)

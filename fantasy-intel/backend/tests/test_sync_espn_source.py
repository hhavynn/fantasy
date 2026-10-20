"""ESPN access for sync jobs: cookies reach only the league's own read endpoint; the control request and the
visibility probe carry none; raw payloads round-trip with every member SWID scrubbed."""
import gzip
import json

import pytest
import requests

from app.espn.parse import anon_member
from app.sync import espn_source, raw_store
from app.sync.espn_source import FANTASY, cookie_allowed

LG, SEASON = 4242, 2026
CRED = {"espn_s2": "S2SECRETVALUE", "swid": "{AAAA-BBBB}"}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, {}

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, headers=None, cookies=None, timeout=None, **kw):
        seen.append({"url": url, "cookies": cookies, "timeout": timeout})
        return Resp(200, {"id": LG, "seasonId": SEASON, "players": []} if "/leagues/" in url else [])

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


@pytest.mark.parametrize("url, ok", [
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}", True),
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}/communication/", True),
    (f"{FANTASY}/leagueHistory/{LG}?seasonId={SEASON}", True),
    (f"{FANTASY}/seasons/{SEASON}/segments/0/leagues/{LG}9", False),          # another league sharing a prefix
    (f"{FANTASY}/seasons/{SEASON + 1}/segments/0/leagues/{LG}", False),       # another season
    (f"{FANTASY}/seasons/{SEASON}/players", False),                            # pro players: public, no cookies
    (f"https://evil.example/apis/v3/games/ffl/seasons/{SEASON}/segments/0/leagues/{LG}", False),
])
def test_the_cookie_allowlist(url, ok):
    assert cookie_allowed(url, LG, SEASON) is ok


def test_a_private_client_sends_cookies_only_to_the_league_endpoint(calls):
    with espn_source.espn_client(LG, SEASON, CRED) as client:
        client.league()
        client.pro_players()
    league, players = calls
    assert "/leagues/4242" in league["url"] and league["cookies"] == {"espn_s2": "S2SECRETVALUE", "SWID": "{AAAA-BBBB}"}
    assert players["url"].endswith("/players") and players["cookies"] is None
    assert all(c["timeout"] for c in calls), "every ESPN request has a timeout"


def test_the_scope_is_removed_after_the_job(calls):
    from espn_api.requests import espn_requests

    from app.espn import client as client_mod
    with espn_source.espn_client(LG, SEASON, None):
        pass
    assert espn_requests.requests is requests and client_mod.requests is requests


def test_control_and_probe_never_carry_cookies(calls, monkeypatch):
    assert espn_source.control_request(SEASON) is True
    assert espn_source.probe(LG, SEASON) == "public"
    assert all(c["cookies"] is None for c in calls)
    for status, verdict in ((401, "private"), (403, "private"), (404, "missing"), (500, "unknown")):
        monkeypatch.setattr(requests, "get", lambda *a, status=status, **k: Resp(status))
        assert espn_source.probe(LG, SEASON) == verdict

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    assert espn_source.probe(LG, SEASON) == "unknown" and espn_source.control_request(SEASON) is False


def test_member_ids_hash_once():
    once = anon_member("{AAAA-BBBB}")
    assert once.startswith("m_") and anon_member(once) == once


def test_raw_payloads_round_trip_with_swids_scrubbed():
    from datetime import datetime

    from app.espn.sync import EspnPayloads
    p = EspnPayloads(league={"id": LG, "members": [{"id": "{AAAA-BBBB}"}], "teams": [{"owners": ["{AAAA-BBBB}"]}]},
                     rosters={1: {"r": 1}}, transactions={2: {"t": 2}}, activity=[{"a": 1}], projections={3: {"p": 3}},
                     projection_weeks=[3], projection_failures={4: "ESPN projections week 4 failed"},
                     fetch_started_at=datetime(2026, 10, 21, 12, 0), fetched_at=datetime(2026, 10, 21, 12, 1))
    blob = raw_store.dump_payloads(p)
    assert b"AAAA-BBBB" not in gzip.decompress(blob)
    back = raw_store.load_payloads(blob)
    assert back.league["members"][0]["id"] == anon_member("{AAAA-BBBB}")
    assert (back.rosters, back.transactions, back.projections) == ({1: {"r": 1}}, {2: {"t": 2}}, {3: {"p": 3}})
    assert back.projection_failures == {4: "ESPN projections week 4 failed"} and back.projection_weeks == [3]
    assert back.fetched_at == datetime(2026, 10, 21, 12, 1) and back.warnings == []
    assert json.loads(gzip.decompress(blob))["format"] == raw_store.FORMAT

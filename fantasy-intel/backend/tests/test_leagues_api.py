"""/v2 API end to end against a fake LeagueProvider (the real registry is injected by dependency override)."""

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.leagues_v2 import get_tenancy_store, provider_registry
from app.leagues.auth import HEADER, sign_user_assertion
from app.leagues.capabilities import (
    Capability, CapabilityState, CapabilityUnavailable, ProviderCapabilities, Support,
)
from app.leagues.context import AccessMethod, LeagueKey, Provider
from app.leagues.credentials import EspnCookies
from app.leagues.identity import ProviderPlayerId
from app.leagues.models import (
    LeagueSettings, LineupRules, LineupSlot, PlayoffSettings, Provenance, Roster, RosterEntry, ScoringRules,
    SlotKind, Sourced, Team, TradeSettings, WaiverSettings,
)
from app.leagues.providers.base import (
    LeagueNotFound, LeagueProvider, ProviderError, ProviderUnavailable,
)
from app.leagues.store import TenancyStore

SECRET = "x" * 48
ESPN_S2 = "AEBsupersecretcookievalue0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"
SWID = "{B1A2C3D4-0000-4321-8765-0123456789AB}"


class FakeProvider(LeagueProvider):
    """Programmable adapter: public/private league, failures, fixture vs live source, call log."""

    def __init__(self, provider: Provider, *, enabled=True, reason=None, supported=True):
        self.provider = provider
        self.enabled, self.reason, self.supported = enabled, reason, supported
        self.is_public: bool | None = True
        self.source = "fake:/league"
        self.error: Exception | None = None
        self.calls: list[tuple[str, AccessMethod, object]] = []
        self.team_ids = ("1", "2")

    def capabilities(self):
        sup = CapabilityState(Support.SUPPORTED if self.supported else Support.UNSUPPORTED,
                              None if self.supported else "not built yet")
        return ProviderCapabilities(self.provider, self.enabled, self.reason,
                                    {c: sup for c in (Capability.LEAGUE_SETTINGS, Capability.TEAMS, Capability.ROSTERS)})

    def _go(self, what, key, access):
        self.calls.append((what, access.method, access.credential))
        if self.error:
            raise self.error
        return Provenance(self.provider, self.source, datetime(2026, 9, 1, 12), datetime(2026, 9, 1, 11))

    def fetch_settings(self, key, access):
        prov = self._go("settings", key, access)
        lineup = LineupRules((LineupSlot("QB", 1, SlotKind.STARTER, frozenset({"QB"}), "QB"),
                              LineupSlot("SUPERFLEX", 1, SlotKind.STARTER, frozenset({"QB", "RB", "WR", "TE"}), "OP"),
                              LineupSlot("BN", 6, SlotKind.BENCH, frozenset(), "BN")))
        s = LeagueSettings(key, f"{self.provider.value} league {key.external_league_id}", 2, lineup,
                           ScoringRules({"rec": 1.0}, {"rec": 1.0}, ()), WaiverSettings("faab", 100),
                           TradeSettings(), PlayoffSettings(), "dynasty", self.is_public)
        return Sourced(s, prov)

    def fetch_teams(self, key, access):
        prov = self._go("teams", key, access)
        return Sourced([Team(key, t, f"Team {t}", (f"{self.provider.value}:m{t}",)) for t in self.team_ids], prov)

    def fetch_rosters(self, key, access):
        prov = self._go("rosters", key, access)
        entry = RosterEntry(ProviderPlayerId(self.provider, "4046"), SlotKind.STARTER, "QB", name="A QB", position="QB")
        return Sourced([Roster(key, t, (entry,)) for t in self.team_ids], prov)

    def _no(self, *a, **k):
        raise CapabilityUnavailable(self.provider, Capability.MATCHUPS, CapabilityState(Support.UNSUPPORTED))

    discover_leagues = fetch_scoring = fetch_matchups = fetch_standings = _no
    fetch_free_agents = fetch_transactions = fetch_projections = _no


class FakeRegistry:
    def __init__(self, *providers: FakeProvider):
        self.providers = {p.provider: p for p in providers}

    def get_provider(self, provider):
        if provider not in self.providers:
            raise ProviderUnavailable("yahoo is not enabled in this deployment")
        return self.providers[provider]

    def all_capabilities(self):
        return [p.capabilities() for p in self.providers.values()]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", str(tmp_path / "tenancy.duckdb"))
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    monkeypatch.delenv("FANTASY_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("ESPN_S2", raising=False)
    monkeypatch.delenv("SWID", raising=False)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)


@pytest.fixture
def providers(env):
    return {Provider.SLEEPER: FakeProvider(Provider.SLEEPER), Provider.ESPN: FakeProvider(Provider.ESPN)}


@pytest.fixture
def client(providers):
    from app.main import app
    registry = FakeRegistry(*providers.values())
    app.dependency_overrides[provider_registry] = lambda: registry
    yield TestClient(app)
    app.dependency_overrides.clear()


def as_user(user: str, **kw) -> dict:
    return {HEADER: sign_user_assertion(user, SECRET, time.time(), **kw)}


def connect(client, user="alice", provider="sleeper", ext="12345", season=2026):
    return client.post("/v2/leagues", json={"provider": provider, "external_league_id": ext, "season": season},
                       headers=as_user(user))


ALL_ROUTES = [
    ("GET", "/v2/providers"), ("GET", "/v2/leagues"), ("POST", "/v2/leagues"),
    ("GET", "/v2/leagues/lg_" + "a" * 24), ("GET", "/v2/leagues/lg_" + "a" * 24 + "/settings"),
    ("GET", "/v2/leagues/lg_" + "a" * 24 + "/teams"), ("PUT", "/v2/leagues/lg_" + "a" * 24 + "/my-team"),
    ("DELETE", "/v2/leagues/lg_" + "a" * 24),
]


# ---- feature flag ------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["", "0", "false", None])
@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_flag_off_means_404_on_every_v2_route(client, monkeypatch, flag, method, path):
    if flag is None:
        monkeypatch.delenv("MULTILEAGUE_ENABLED")
    else:
        monkeypatch.setenv("MULTILEAGUE_ENABLED", flag)
    # even a garbage assertion must not turn into a 401 that reveals the feature exists
    for headers in ({}, {HEADER: "garbage"}, as_user("alice")):
        r = client.request(method, path, headers=headers, json={"provider": "sleeper"})
        assert r.status_code == 404, (method, path, r.text)


def test_flag_off_leaves_existing_routes_alone(client, monkeypatch):
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "0")
    assert client.get("/health").json() == {"ok": True}
    # a legacy route still answers as before (503: no analytics database in this test), not 404
    assert client.get("/api/overview").status_code != 404


def test_flag_on_does_not_change_legacy_routes_or_service_auth(client, monkeypatch):
    assert client.get("/health").json() == {"ok": True}
    monkeypatch.setenv("FANTASY_API_KEY", "k" * 32)
    assert client.get("/v2/providers").status_code == 401                       # the service key gate still applies
    assert client.get("/v2/providers", headers={"X-API-Key": "k" * 32}).status_code == 200
    # ... and the service key is not a user
    assert client.get("/v2/leagues", headers={"X-API-Key": "k" * 32}).status_code == 401


# ---- identity ----------------------------------------------------------------------------------

def test_providers_endpoint_is_public(client):
    body = client.get("/v2/providers").json()
    assert {p["provider"] for p in body["providers"]} == {"sleeper", "espn"}
    assert body["providers"][0]["capabilities"]["teams"]["support"] == "supported"


def test_anonymous_gets_401_on_list_and_connect_but_404_on_league_routes(client):
    assert client.get("/v2/leagues").status_code == 401
    assert client.post("/v2/leagues", json={"provider": "sleeper", "external_league_id": "1", "season": 2026}
                       ).status_code == 401
    owned = connect(client).json()["league_id"]
    for method, path in [("GET", ""), ("GET", "/settings"), ("GET", "/teams"), ("PUT", "/my-team"), ("DELETE", "")]:
        r = client.request(method, f"/v2/leagues/{owned}{path}", json={"external_team_id": "1"})
        assert r.status_code == 404, (method, path)
    assert client.get(f"/v2/leagues/{owned}").json() == {"detail": "League not found."}


@pytest.mark.parametrize("make", [
    lambda: "garbage",
    lambda: sign_user_assertion("alice", SECRET, time.time() - 5000),                 # expired
    lambda: sign_user_assertion("alice", "y" * 48, time.time()),                      # wrong key
    lambda: sign_user_assertion("alice", SECRET, time.time()).replace("v1.", "v1.A", 1),  # tampered payload
])
def test_bad_assertions_are_401_everywhere(client, make):
    for method, path in [("GET", "/v2/leagues"), ("GET", "/v2/leagues/lg_" + "a" * 24), ("GET", "/v2/providers")]:
        assert client.request(method, path, headers={HEADER: make()}).status_code == 401


def test_wrong_audience_is_401(client):
    import base64, hashlib, hmac, json
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    now = int(time.time())
    body = b64(json.dumps({"sub": "alice", "aud": "other", "iat": now, "exp": now + 60}).encode())
    sig = b64(hmac.new(SECRET.encode(), f"v1.{body}".encode(), hashlib.sha256).digest())
    assert client.get("/v2/leagues", headers={HEADER: f"v1.{body}.{sig}"}).status_code == 401


def test_assertion_without_a_configured_secret_is_401(client, monkeypatch):
    headers = as_user("alice")
    monkeypatch.delenv("FANTASY_USER_ASSERTION_SECRET")
    assert client.get("/v2/leagues", headers=headers).status_code == 401


# ---- connect -----------------------------------------------------------------------------------

def test_connect_returns_a_league_view_and_lists_it(client, providers):
    r = connect(client)
    assert r.status_code == 201, r.text
    v = r.json()
    assert set(v) == {"league_id", "provider", "external_league_id", "season", "name", "role", "access",
                      "my_team_external_id", "sync", "data_origin", "missing_capabilities", "capabilities"}
    assert (v["provider"], v["external_league_id"], v["season"]) == ("sleeper", "12345", 2026)
    assert (v["role"], v["access"], v["data_origin"]) == ("owner", "public", "live")
    assert v["league_id"].startswith("lg_") and "12345" not in v["league_id"]
    assert v["sync"] == {"status": "ok", "last_synced_at": "2026-09-01T12:00:00Z",
                         "source_updated_at": "2026-09-01T11:00:00Z", "last_error": None}
    assert client.get("/v2/leagues", headers=as_user("alice")).json() == {"leagues": [client.get(
        f"/v2/leagues/{v['league_id']}", headers=as_user("alice")).json()]}
    assert [c[1] for c in providers[Provider.SLEEPER].calls] == [AccessMethod.PUBLIC] * 3


def test_fixture_source_marks_data_origin(client, providers):
    providers[Provider.SLEEPER].source = "fixture:sleeper/league.json"
    assert connect(client).json()["data_origin"] == "fixture"


def test_reads_serve_snapshots_without_calling_the_provider(client, providers):
    lid = connect(client).json()["league_id"]
    calls = len(providers[Provider.SLEEPER].calls)
    s = client.get(f"/v2/leagues/{lid}/settings", headers=as_user("alice")).json()
    t = client.get(f"/v2/leagues/{lid}/teams", headers=as_user("alice")).json()
    assert len(providers[Provider.SLEEPER].calls) == calls
    assert s["league_id"] == lid and s["name"] == "sleeper league 12345" and s["league_type"] == "dynasty"
    assert s["lineup"]["is_superflex"] is True and s["lineup"]["superflex_slots"] == 1
    assert s["lineup"]["slots"][1]["eligible"] == ["QB", "RB", "TE", "WR"]
    assert s["scoring"]["points_per_reception"] == 1.0
    assert s["provenance"]["source"] == "fake:/league"
    assert [x["external_team_id"] for x in t["teams"]] == ["1", "2"]
    assert t["teams"][0]["scoped_id"] == "sleeper:12345:2026:team:1"


@pytest.mark.parametrize("body", [
    {"external_league_id": "1", "season": 2026},                                     # no provider
    {"provider": "myspace", "external_league_id": "1", "season": 2026},
    {"provider": "sleeper", "season": 2026},                                         # no id
    {"provider": "sleeper", "external_league_id": 12345, "season": 2026},            # id must be a string
    {"provider": "sleeper", "external_league_id": "abc", "season": 2026},            # sleeper ids are digits
    {"provider": "sleeper", "external_league_id": "1"},                              # season required
    {"provider": "sleeper", "external_league_id": "1", "season": "2026"},
    {"provider": "sleeper", "external_league_id": "1", "season": True},
    {"provider": "sleeper", "external_league_id": "1", "season": 1850},
    {"provider": "sleeper", "external_league_id": "1", "season": 2026, "espn_s2": ESPN_S2},   # cookies never accepted
    {"provider": "espn", "external_league_id": "1", "season": 2026, "swid": SWID, "espn_s2": ESPN_S2},
    {"provider": "sleeper", "external_league_id": "1", "season": 2026, "user_id": "bob"},
])
def test_bad_connect_bodies_are_400_and_never_echo_cookies(client, providers, body):
    r = client.post("/v2/leagues", json=body, headers=as_user("alice"))
    assert r.status_code == 400, r.text
    assert ESPN_S2 not in r.text and SWID not in r.text
    assert providers[Provider.SLEEPER].calls == [] and providers[Provider.ESPN].calls == []


@pytest.mark.parametrize("raw", [b"{not json", b"[]", b'"x"', b""])
def test_non_object_bodies_are_400(client, raw):
    r = client.post("/v2/leagues", content=raw, headers={**as_user("alice"), "content-type": "application/json"})
    assert r.status_code == 400


def test_private_league_is_refused_with_422_and_nothing_is_stored(client, providers):
    for visibility in (False, None):
        providers[Provider.SLEEPER].is_public = visibility
        r = connect(client)
        assert r.status_code == 422
        assert r.json()["detail"] == ("This league is private; private leagues need an authorized connection "
                                      "method that is not available yet.")
    assert client.get("/v2/leagues", headers=as_user("alice")).json() == {"leagues": []}
    assert [c[0] for c in providers[Provider.SLEEPER].calls] == ["settings", "settings"]   # stopped before teams/rosters


def test_disabled_provider_is_501_with_its_reason(client, providers):
    fake = providers[Provider.SLEEPER]
    fake.enabled, fake.reason = False, "Sleeper is switched off in this deployment."
    r = connect(client)
    assert r.status_code == 501 and r.json()["detail"] == "Sleeper is switched off in this deployment."
    assert fake.calls == []


def test_unsupported_capability_is_501_not_a_fake_success(client, providers):
    providers[Provider.SLEEPER].supported = False
    r = connect(client)
    assert r.status_code == 501 and "league_settings" in r.json()["detail"] and "not built yet" in r.json()["detail"]
    assert client.get("/v2/leagues", headers=as_user("alice")).json() == {"leagues": []}


def test_provider_missing_from_the_registry_is_501(client):
    r = connect(client, provider="yahoo", ext="449.l.1")
    assert r.status_code == 501 and "not enabled" in r.json()["detail"]


def test_provider_errors_are_502_503_404_with_sanitized_messages(client, providers):
    fake = providers[Provider.SLEEPER]
    leak = f"request failed espn_s2={ESPN_S2} SWID={SWID} Bearer {ESPN_S2}"
    for err, status in [(ProviderError(leak), 502), (ProviderUnavailable(leak), 503), (LeagueNotFound(leak), 404),
                        (RuntimeError(leak), 502), (TimeoutError(leak), 502)]:
        fake.error = err
        r = connect(client)
        assert r.status_code == status, (err, r.text)
        assert ESPN_S2 not in r.text and SWID not in r.text and "redacted" in r.text


def test_failure_marks_only_that_league_and_only_for_its_members(client, providers):
    sleeper, espn = providers[Provider.SLEEPER], providers[Provider.ESPN]
    sl = connect(client, "alice", "sleeper").json()["league_id"]
    es = connect(client, "alice", "espn").json()["league_id"]
    sleeper.error = ProviderUnavailable(f"down espn_s2={ESPN_S2}")
    assert connect(client, "alice", "sleeper").status_code == 503                    # refresh by a member
    assert connect(client, "bob", "sleeper").status_code == 503                      # a stranger's failed attempt ...
    failed = client.get(f"/v2/leagues/{sl}", headers=as_user("alice")).json()["sync"]
    assert failed["status"] == "failed" and ESPN_S2 not in str(failed) and failed["last_synced_at"]
    ok = client.get(f"/v2/leagues/{es}", headers=as_user("alice")).json()["sync"]
    assert ok["status"] == "ok" and ok["last_error"] is None
    assert client.get("/v2/leagues", headers=as_user("bob")).json() == {"leagues": []}
    # snapshots from the last good sync are still served for the failed league
    assert client.get(f"/v2/leagues/{sl}/teams", headers=as_user("alice")).status_code == 200


def test_same_external_id_on_two_providers_gives_two_leagues(client):
    a = connect(client, "alice", "sleeper", "12345").json()
    b = connect(client, "alice", "espn", "12345").json()
    assert a["league_id"] != b["league_id"] and (a["provider"], b["provider"]) == ("sleeper", "espn")
    assert {v["league_id"] for v in client.get("/v2/leagues", headers=as_user("alice")).json()["leagues"]} == {
        a["league_id"], b["league_id"]}


def test_two_users_connecting_the_same_league_share_one_id_but_not_access(client):
    a = connect(client, "alice").json()
    b = connect(client, "bob").json()
    assert a["league_id"] == b["league_id"]
    client.delete(f"/v2/leagues/{a['league_id']}", headers=as_user("alice"))
    assert client.get(f"/v2/leagues/{a['league_id']}", headers=as_user("alice")).status_code == 404
    assert client.get(f"/v2/leagues/{a['league_id']}", headers=as_user("bob")).status_code == 200


# ---- owner env credentials ---------------------------------------------------------------------

def test_owner_uses_server_credentials_on_espn_and_nobody_else_does(client, providers, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    monkeypatch.setenv("ESPN_S2", ESPN_S2)
    monkeypatch.setenv("SWID", SWID)
    espn = providers[Provider.ESPN]
    espn.is_public = False

    mine = connect(client, "alice", "espn", "777")
    assert mine.status_code == 201 and mine.json()["access"] == "owner_env"
    method, cred = espn.calls[0][1], espn.calls[0][2]
    assert method is AccessMethod.OWNER_ENV and isinstance(cred, EspnCookies)
    assert cred.espn_s2 == ESPN_S2 and ESPN_S2 not in repr(cred) + str(cred) + f"{cred}"
    assert ESPN_S2 not in mine.text and SWID not in mine.text

    espn.calls.clear()
    other = connect(client, "bob", "espn", "777")
    assert other.status_code == 422                                                    # private, and bob has no way in
    assert all(c[1] is AccessMethod.PUBLIC and c[2] is None for c in espn.calls)
    assert client.get("/v2/leagues", headers=as_user("bob")).json() == {"leagues": []}


def test_owner_without_server_credentials_falls_back_to_public(client, providers, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    r = connect(client, "alice", "espn", "888")
    assert r.status_code == 201 and r.json()["access"] == "public"


def test_owner_env_never_applies_to_sleeper(client, providers, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    monkeypatch.setenv("ESPN_S2", ESPN_S2)
    monkeypatch.setenv("SWID", SWID)
    assert connect(client, "alice", "sleeper").json()["access"] == "public"
    assert providers[Provider.SLEEPER].calls[0][2] is None


# ---- cross-user isolation ----------------------------------------------------------------------

@pytest.fixture
def alices_league(client):
    return connect(client, "alice").json()["league_id"]


def test_user_b_cannot_read_modify_or_delete_user_as_league(client, alices_league):
    bob = as_user("bob")
    base = f"/v2/leagues/{alices_league}"
    for path in ("", "/settings", "/teams"):
        r = client.get(base + path, headers=bob)
        assert r.status_code == 404 and r.json() == {"detail": "League not found."}, path
    r = client.put(base + "/my-team", json={"external_team_id": "1"}, headers=bob)
    assert r.status_code == 404
    assert client.delete(base, headers=bob).status_code == 404
    # nothing changed for alice
    v = client.get(base, headers=as_user("alice")).json()
    assert v["my_team_external_id"] is None and v["league_id"] == alices_league
    assert client.get("/v2/leagues", headers=bob).json() == {"leagues": []}


@pytest.mark.parametrize("league_id", ["lg_" + "z" * 24, "nope", "12345", "lg_", "lg_" + "a" * 100])
def test_unknown_and_malformed_ids_are_the_same_404(client, alices_league, league_id):
    r = client.get(f"/v2/leagues/{league_id}", headers=as_user("alice"))
    assert r.status_code == 404 and r.json() == {"detail": "League not found."}


# ---- my team / delete --------------------------------------------------------------------------

def test_my_team_must_belong_to_the_league(client, alices_league):
    h = as_user("alice")
    url = f"/v2/leagues/{alices_league}/my-team"
    r = client.put(url, json={"external_team_id": "2"}, headers=h)
    assert r.status_code == 200 and r.json()["my_team_external_id"] == "2"
    assert client.get(f"/v2/leagues/{alices_league}", headers=h).json()["my_team_external_id"] == "2"
    assert client.put(url, json={"external_team_id": "99"}, headers=h).status_code == 422
    for bad in ({}, {"external_team_id": 2}, {"external_team_id": "1", "user_id": "bob"}, []):
        assert client.put(url, json=bad, headers=h).status_code == 400
    assert client.get(f"/v2/leagues/{alices_league}", headers=h).json()["my_team_external_id"] == "2"


def test_delete_revokes_and_the_old_link_stops_working(client, alices_league):
    h = as_user("alice")
    assert client.delete(f"/v2/leagues/{alices_league}", headers=h).status_code == 204
    for path in ("", "/settings", "/teams"):
        assert client.get(f"/v2/leagues/{alices_league}{path}", headers=h).status_code == 404
    assert client.delete(f"/v2/leagues/{alices_league}", headers=h).status_code == 404
    assert client.get("/v2/leagues", headers=h).json() == {"leagues": []}
    assert connect(client, "alice").json()["league_id"] == alices_league           # reconnect works


def test_responses_never_contain_credentials(client, providers, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    monkeypatch.setenv("ESPN_S2", ESPN_S2)
    monkeypatch.setenv("SWID", SWID)
    providers[Provider.ESPN].is_public = False
    lid = connect(client, "alice", "espn", "777").json()["league_id"]
    h = as_user("alice")
    for path in ("", "/settings", "/teams"):
        text = client.get(f"/v2/leagues/{lid}{path}", headers=h).text
        assert ESPN_S2 not in text and SWID not in text and SECRET not in text
    assert ESPN_S2 not in client.get("/v2/leagues", headers=h).text
    assert ESPN_S2 not in client.get("/v2/providers").text


def test_missing_registry_module_is_a_503_not_a_crash(env, monkeypatch):
    import sys
    import app.leagues.providers as providers_pkg
    from app.main import app
    monkeypatch.setitem(sys.modules, "app.leagues.providers.registry", None)         # simulate "not installed"
    monkeypatch.delattr(providers_pkg, "registry", raising=False)
    c = TestClient(app)
    assert c.get("/v2/providers").status_code == 503
    assert c.get("/v2/leagues", headers=as_user("alice")).json() == {"leagues": []}  # store does not need it


def test_store_dependency_is_cached_per_database_file(env):
    assert get_tenancy_store() is get_tenancy_store()
    assert isinstance(get_tenancy_store(), TenancyStore)


# ---- against the real provider registry (fixture-backed Sleeper) ---------------------------------

def test_real_registry_with_sleeper_fixtures_end_to_end(env, monkeypatch):
    from app.main import app
    monkeypatch.setenv("SLEEPER_SOURCE", "fixture")
    c = TestClient(app)
    listed = c.get("/v2/providers").json()["providers"]
    assert {p["provider"] for p in listed} == {"espn", "sleeper", "yahoo"}
    r = c.post("/v2/leagues", json={"provider": "sleeper", "external_league_id": "1180000000000000001",
                                    "season": 2026}, headers=as_user("alice"))
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["data_origin"] == "fixture" and v["capabilities"]["provider"] == "sleeper"
    assert c.get(f"/v2/leagues/{v['league_id']}/teams", headers=as_user("alice")).json()["teams"]
    assert c.get(f"/v2/leagues/{v['league_id']}/teams", headers=as_user("bob")).status_code == 404
    # Yahoo and (without the import flag) ESPN are truthfully unavailable, never faked
    assert c.post("/v2/leagues", json={"provider": "yahoo", "external_league_id": "449.l.1", "season": 2026},
                  headers=as_user("alice")).status_code == 501
    assert c.post("/v2/leagues", json={"provider": "espn", "external_league_id": "1", "season": 2026},
                  headers=as_user("alice")).status_code == 501

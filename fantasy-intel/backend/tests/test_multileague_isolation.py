"""Adversarial, cross-cutting isolation tests for the multi-league platform (Phase 1).

These drive the REAL FastAPI app (and the real tenancy store) the way a hostile second tenant would. They
complement, and deliberately do not repeat, the unit-level tests in tests/test_leagues_*.py and
tests/test_league_providers*.py. Numbering follows the security checklist:

   1  user A cannot reach user B's private league            6  superflex and FLEX stay distinct
   2  same external id on different providers is two leagues  7  missing capabilities are explicit
   3  same team id in different leagues is two teams          8  anonymous access follows the rules
   4  same player name never auto-matches                     9  one provider failure stays in its own tenant
   5  league-specific scoring is preserved                   10  an ESPN-only deployment is unchanged

(11, credentials, lives in tests/test_no_credential_leaks.py; the global-selector ratchet in
tests/test_league_scope_audit.py.)

Tests named `test_secN_...` reproduce findings of the Phase 1 adversarial review (docs/multileague/
threat-model.md, "Review findings"). All were fixed in this PR; the tests now guard against regressions.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.api.leagues_v2 import get_tenancy_store, provider_registry
from app.config import get_settings
from app.leagues.auth import HEADER, InvalidUserAssertion, sign_user_assertion, verify_user_assertion
from app.leagues.cache import TenantScopedCache, scoped_cache_key
from app.leagues.capabilities import (
    Capability, CapabilityState, CapabilityUnavailable, ProviderCapabilities, Support,
)
from app.leagues.context import (
    AccessMethod, LeagueAccessDenied, LeagueContext, LeagueKey, Principal, Provider, check_external_league_id,
    check_internal_league_id, issue_context,
)
from app.leagues.credentials import CredentialAccessDenied, EspnCookies, OwnerEnvCredentials
from app.leagues.identity import (
    Confidence, IdentityLink, IdentityMethod, ProviderPlayerId, link_from_espn_map_row, same_athlete,
)
from app.leagues.models import (
    LeagueSettings, LineupRules, LineupSlot, PlayoffSettings, Provenance, Roster, RosterEntry, ScoringRules,
    SlotKind, Sourced, Team, TradeSettings, WaiverSettings,
)
from app.leagues.providers import registry as real_registry
from app.leagues.providers.base import LeagueAccess, LeagueProvider, ProviderUnavailable
from app.leagues.providers.fixtures import FIXTURE_DIR, FixtureTransport, load_fixture_id_map
from app.leagues.providers.sleeper import SleeperProvider
from app.leagues.store import TenancyStore
from app.leagues.views import league_view

SECRET = "t" * 48
FAKE_S2 = "AEBfakeespns2cookie%2Bvalue0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop0123456789"
FAKE_SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
L1, L2 = "1180000000000000001", "1180000000000000002"     # Sleeper fixture leagues (superflex dynasty / FLEX-only)
SEASON = 2026
ALICE, BOB, CAROL = Principal("alice"), Principal("bob"), Principal("carol")

TENANCY_TABLES = {"app_users", "league_registry", "league_connections", "league_sync_status", "league_snapshots",
                  "credential_refs"}
PROVIDER_ENV = ("FANTASY_OWNER_USER_ID", "ESPN_S2", "SWID", "FANTASY_API_KEY", "SLEEPER_SOURCE",
                "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID", "MULTILEAGUE_UI_ENABLED")


# ---- harness -----------------------------------------------------------------------------------

@pytest.fixture

def env(tmp_path, monkeypatch):
    """Multi-league on, tenancy DB in tmp_path, every provider flag and credential cleared."""
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", str(tmp_path / "tenancy.duckdb"))
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    real_registry.reset_providers()
    yield monkeypatch
    real_registry.reset_providers()


@pytest.fixture
def store(tmp_path):
    return TenancyStore(tmp_path / "store.duckdb", capabilities=lambda: None)


def hdr(user: str, **kw) -> dict:
    return {HEADER: sign_user_assertion(user, SECRET, time.time(), **kw)}


def post_connect(client, user, provider, ext, season=SEASON):
    return client.post("/v2/leagues", json={"provider": provider, "external_league_id": ext, "season": season},
                       headers=hdr(user))


def rows(path, sql, params=()):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def tenancy_path() -> Path:
    return get_settings().tenancy_db_path


class FakeProvider(LeagueProvider):
    """Programmable adapter: visibility, per-league failures, calls log. Never touches a network."""

    def __init__(self, provider: Provider, *, enabled=True, reason=None):
        self.provider = provider
        self.enabled, self.reason = enabled, reason
        self.is_public: bool | None = True
        self.fail_for: dict[str, Exception] = {}
        self.wrong_league: bool = False
        self.calls: list[tuple[str, str, AccessMethod, object]] = []
        self.team_ids = ("1", "2")

    def capabilities(self):
        ok = CapabilityState(Support.SUPPORTED)
        return ProviderCapabilities(self.provider, self.enabled, self.reason,
                                    {c: ok for c in (Capability.LEAGUE_SETTINGS, Capability.TEAMS,
                                                     Capability.ROSTERS)})

    def _go(self, what, key, access):
        self.calls.append((what, key.external_league_id, access.method, access.credential))
        if key.external_league_id in self.fail_for:
            raise self.fail_for[key.external_league_id]
        return Provenance(self.provider, "fake:/league", datetime(2026, 9, 1, 12), datetime(2026, 9, 1, 11))

    def fetch_settings(self, key, access):
        prov = self._go("settings", key, access)
        lineup = LineupRules((LineupSlot("QB", 1, SlotKind.STARTER, frozenset({"QB"}), "QB"),
                              LineupSlot("BN", 4, SlotKind.BENCH, frozenset(), "BN")))
        shown = LeagueKey(self.provider, "999", key.season) if self.wrong_league else key
        s = LeagueSettings(shown, f"{self.provider.value} league {key.external_league_id}", len(self.team_ids),
                           lineup, ScoringRules({"rec": 1.0}, {"rec": 1.0}, ()), WaiverSettings("faab", 100),
                           TradeSettings(), PlayoffSettings(), "dynasty", self.is_public)
        return Sourced(s, prov)

    def fetch_teams(self, key, access):
        prov = self._go("teams", key, access)
        return Sourced([Team(key, t, f"Team {t} of {key.external_league_id}", (f"{self.provider.value}:m{t}",))
                        for t in self.team_ids], prov)

    def fetch_rosters(self, key, access):
        prov = self._go("rosters", key, access)
        entry = RosterEntry(ProviderPlayerId(self.provider, "4046"), SlotKind.STARTER, "QB", name="A QB", position="QB")
        return Sourced([Roster(key, t, (entry,)) for t in self.team_ids], prov)

    def _no(self, *a, **k):
        raise CapabilityUnavailable(self.provider, Capability.MATCHUPS, CapabilityState(Support.UNSUPPORTED))

    discover_leagues = fetch_scoring = fetch_matchups = fetch_standings = _no
    fetch_free_agents = fetch_transactions = fetch_projections = _no


class FakeRegistry:
    def __init__(self, *providers: LeagueProvider):
        self.providers = {p.provider: p for p in providers}

    def get_provider(self, provider):
        if provider not in self.providers:
            raise ProviderUnavailable(f"{provider.value} is not enabled in this deployment")
        return self.providers[provider]

    def all_capabilities(self):
        return [p.capabilities() for p in self.providers.values()]


@pytest.fixture
def fakes(env):
    return {Provider.SLEEPER: FakeProvider(Provider.SLEEPER), Provider.ESPN: FakeProvider(Provider.ESPN)}


@pytest.fixture
def fake_client(fakes):
    from app.main import app
    registry = FakeRegistry(*fakes.values())
    app.dependency_overrides[provider_registry] = lambda: registry
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def real_client(env):
    """The real app with the real provider registry; Sleeper served from the recorded fixtures (no network)."""
    from app.main import app
    env.setenv("SLEEPER_SOURCE", "fixture")
    real_registry.reset_providers()
    app.dependency_overrides.clear()
    return TestClient(app)


# =================================================================================================
# 1. User A cannot access user B's private league
# =================================================================================================

def _private_league_of_bob(client, fakes, env) -> str:
    """Bob is the deployment owner; his ESPN league is private and was connected with the server's cookies."""
    env.setenv("FANTASY_OWNER_USER_ID", "bob")
    env.setenv("ESPN_S2", FAKE_S2)
    env.setenv("SWID", FAKE_SWID)
    fakes[Provider.ESPN].is_public = False
    r = post_connect(client, "bob", "espn", "777")
    assert r.status_code == 201, r.text
    assert r.json()["access"] == "owner_env"
    return r.json()["league_id"]


def _bobs_world(client, league_id) -> dict:
    h = hdr("bob")
    return {"list": client.get("/v2/leagues", headers=h).json(),
            "league": client.get(f"/v2/leagues/{league_id}", headers=h).json(),
            "settings": client.get(f"/v2/leagues/{league_id}/settings", headers=h).json(),
            "teams": client.get(f"/v2/leagues/{league_id}/teams", headers=h).json()}


def test_user_a_cannot_read_modify_or_delete_user_bs_private_league(fake_client, fakes, env):
    bobs_league = _private_league_of_bob(fake_client, fakes, env)
    assert fake_client.put(f"/v2/leagues/{bobs_league}/my-team", json={"external_team_id": "2"},
                           headers=hdr("bob")).status_code == 200
    before = _bobs_world(fake_client, bobs_league)
    assert before["league"]["my_team_external_id"] == "2"

    alice = hdr("alice")
    attempts = [("GET", ""), ("GET", "/settings"), ("GET", "/teams"),
                ("PUT", "/my-team"), ("DELETE", "")]
    for method, suffix in attempts:
        r = fake_client.request(method, f"/v2/leagues/{bobs_league}{suffix}", headers=alice,
                                json={"external_team_id": "1"})
        # 404, never 403: indistinguishable from a league that does not exist
        assert r.status_code == 404, (method, suffix, r.status_code, r.text)
        assert r.json() == {"detail": "League not found."}
        assert "bob" not in r.text and "777" not in r.text

    # the same attempts against a well-formed id that does not exist produce the identical answer
    ghost = "lg_" + "z" * 24
    for method, suffix in attempts:
        assert fake_client.request(method, f"/v2/leagues/{ghost}{suffix}", headers=alice,
                                   json={"external_team_id": "1"}).json() == {"detail": "League not found."}

    # Alice cannot connect Bob's private league by id either: not public, so no connection is created
    r = post_connect(fake_client, "alice", "espn", "777")
    assert r.status_code == 422, r.text
    assert fake_client.get("/v2/leagues", headers=alice).json() == {"leagues": []}

    # Bob's data is exactly as before
    assert _bobs_world(fake_client, bobs_league) == before
    active = rows(tenancy_path(), "SELECT user_id FROM league_connections WHERE revoked_at IS NULL")
    assert active == [("bob",)]


def _reissue(ctx, **changes):
    """A context minted with issue_context() but no authorization check: what a buggy internal caller could
    build. dataclasses.replace() can no longer forge one (SEC-5); the store must still refuse these."""
    fields = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx) if f.name != "_issuer"}
    return issue_context(**{**fields, **changes})


def test_store_authorize_refuses_a_stranger_and_every_forged_context(store, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "bob")
    bob_key, alice_key = LeagueKey(Provider.ESPN, "777", SEASON), LeagueKey(Provider.SLEEPER, "888", SEASON)
    bobs = store.connect(BOB, bob_key, AccessMethod.OWNER_ENV, name="B", is_public=False, data_origin="live")
    alices = store.connect(ALICE, alice_key, AccessMethod.PUBLIC, name="A", is_public=True, data_origin="fixture")
    prov = Provenance(Provider.ESPN, "espn:x", datetime(2026, 9, 1))
    store.save_snapshot(bobs, "settings", {"name": "bob's league"}, prov)
    store.save_snapshot(bobs, "teams", [{"external_team_id": "1"}], prov)

    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, bobs.league_id)
    with pytest.raises(LeagueAccessDenied):
        store.authorize(Principal.anonymous(), bobs.league_id)

    # Hand-built contexts are refused outright ...
    with pytest.raises(RuntimeError):
        LeagueContext(league_id=bobs.league_id, key=bob_key, principal=ALICE, role=bobs.role,
                      access=AccessMethod.PUBLIC, name=None, my_team_external_id=None, freshness=bobs.freshness)
    # ... and a context for Alice's own league re-pointed at Bob's league is useless to every store method,
    # because each one re-checks the connection of ctx.principal to ctx.league_id inside its own transaction.
    for repointed in (_reissue(alices, league_id=bobs.league_id),
                      _reissue(alices, league_id=bobs.league_id, key=bob_key)):
        with pytest.raises(LeagueAccessDenied):
            store.load_snapshot(repointed, "settings")
        with pytest.raises(LeagueAccessDenied):
            store.save_snapshot(repointed, "settings", {"name": "pwned"},
                                Provenance(repointed.key.provider, "x", datetime(2026, 9, 1)))
        with pytest.raises(LeagueAccessDenied):
            store.set_my_team(repointed, "1")
        with pytest.raises(LeagueAccessDenied):
            store.league_meta(repointed)
        with pytest.raises(LeagueAccessDenied):
            store.record_sync_failure(repointed, "boom")
        store.revoke(repointed)                       # filters on ctx.principal: must not touch Bob's connection
    # Bob's league is untouched
    ctx = store.authorize(BOB, bobs.league_id)
    assert store.load_snapshot(ctx, "settings")[0] == {"name": "bob's league"}
    assert ctx.my_team_external_id is None and ctx.freshness.last_error is None


def test_a_context_cannot_be_turned_into_another_tenants_by_swapping_the_principal(store):
    """Tenant id for cache keys and connection checks comes from ctx.principal, so swapping it must not work."""
    key = LeagueKey(Provider.SLEEPER, "888", SEASON)
    alices = store.connect(ALICE, key, AccessMethod.PUBLIC, name="A", is_public=True, data_origin="fixture")
    swapped = _reissue(alices, principal=BOB)                       # replace() itself is refused (SEC-5)
    with pytest.raises(LeagueAccessDenied):
        store.load_snapshot(swapped, "settings")                    # Bob holds no connection: the store refuses


@pytest.mark.parametrize("visibility", [False, None])
def test_public_access_to_a_private_or_unknown_visibility_league_is_refused(fake_client, fakes, visibility):
    fakes[Provider.SLEEPER].is_public = visibility
    r = post_connect(fake_client, "alice", "sleeper", "4242")
    assert r.status_code == 422, r.text
    assert fake_client.get("/v2/leagues", headers=hdr("alice")).json() == {"leagues": []}
    assert rows(tenancy_path(), "SELECT count(*) FROM league_registry") == [(0,)]
    assert rows(tenancy_path(), "SELECT count(*) FROM league_snapshots") == [(0,)]


def test_owner_env_credentials_are_never_used_for_anyone_but_the_configured_owner(fake_client, fakes, env):
    env.setenv("FANTASY_OWNER_USER_ID", "bob")
    env.setenv("ESPN_S2", FAKE_S2)
    env.setenv("SWID", FAKE_SWID)
    espn = fakes[Provider.ESPN]
    ra = post_connect(fake_client, "alice", "espn", "555")           # a public league; Alice is not the owner
    assert ra.status_code == 201 and ra.json()["access"] == "public"
    assert all(access is AccessMethod.PUBLIC and cred is None for _, _, access, cred in espn.calls)
    espn.calls.clear()
    rb = post_connect(fake_client, "bob", "espn", "555")
    assert rb.json()["access"] == "owner_env"
    assert any(isinstance(cred, EspnCookies) for *_, cred in espn.calls)
    espn.calls.clear()
    rs = post_connect(fake_client, "bob", "sleeper", "555")          # the owner flag is ESPN-only
    assert rs.json()["access"] == "public"
    assert all(cred is None for *_, cred in fakes[Provider.SLEEPER].calls)


def test_store_refuses_owner_env_to_non_owners_other_providers_and_unconfigured_owner(store, monkeypatch):
    espn, sleeper = LeagueKey(Provider.ESPN, "1", SEASON), LeagueKey(Provider.SLEEPER, "1", SEASON)
    monkeypatch.delenv("FANTASY_OWNER_USER_ID", raising=False)
    with pytest.raises(PermissionError):
        store.connect(ALICE, espn, AccessMethod.OWNER_ENV, name=None, is_public=False, data_origin="live")
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    with pytest.raises(PermissionError):
        store.connect(BOB, espn, AccessMethod.OWNER_ENV, name=None, is_public=False, data_origin="live")
    with pytest.raises(PermissionError):
        store.connect(ALICE, sleeper, AccessMethod.OWNER_ENV, name=None, is_public=False, data_origin="live")
    ctx = store.connect(ALICE, espn, AccessMethod.OWNER_ENV, name=None, is_public=False, data_origin="live")
    # credentials follow the same rule at release time, not only at connect time
    monkeypatch.setenv("ESPN_S2", FAKE_S2)
    monkeypatch.setenv("SWID", FAKE_SWID)
    assert isinstance(OwnerEnvCredentials().for_context(ctx), EspnCookies)
    with pytest.raises(CredentialAccessDenied):
        OwnerEnvCredentials().for_owner(BOB, Provider.ESPN)
    public = store.connect(BOB, sleeper, AccessMethod.PUBLIC, name=None, is_public=True, data_origin="fixture")
    with pytest.raises(CredentialAccessDenied):
        OwnerEnvCredentials().for_context(public)


def test_connect_endpoint_refuses_a_provider_answer_for_a_different_league(fake_client, fakes):
    fakes[Provider.SLEEPER].wrong_league = True
    r = post_connect(fake_client, "alice", "sleeper", "4242")
    assert r.status_code == 502, r.text
    assert fake_client.get("/v2/leagues", headers=hdr("alice")).json() == {"leagues": []}
    assert rows(tenancy_path(), "SELECT count(*) FROM league_snapshots") == [(0,)]


@pytest.mark.parametrize("bad", ["12 34", "12\n34", "１２３４", "١٢٣٤", "1" * 65, "../x", "12;34", "", " "])
def test_external_ids_are_ascii_digit_strings_for_digit_platforms(bad):
    for provider in (Provider.ESPN, Provider.SLEEPER):
        with pytest.raises(ValueError):
            check_external_league_id(provider, bad)


@pytest.mark.parametrize("bad", ["lg_short", "lg_" + "a" * 65, "LG_" + "a" * 24, "lg_" + "a" * 23 + "/", "lg_" + "é" * 24,
                                 "lg_" + "a" * 23 + "\n", "../" + "a" * 24, 12345, None])
def test_internal_ids_that_are_not_ours_are_the_same_denial(bad):
    with pytest.raises(LeagueAccessDenied):
        check_internal_league_id(bad)


# =================================================================================================
# 2. Same external league id on different providers does not collide
# =================================================================================================

def test_same_external_id_on_every_provider_is_a_separate_league_everywhere(store):
    keys = {p: LeagueKey(p, "12345", SEASON) for p in Provider}
    ctxs = {
        Provider.SLEEPER: store.connect(ALICE, keys[Provider.SLEEPER], AccessMethod.PUBLIC, name="sleeper one",
                                        is_public=True, data_origin="fixture"),
        Provider.ESPN: store.connect(ALICE, keys[Provider.ESPN], AccessMethod.PUBLIC, name="espn one",
                                     is_public=True, data_origin="live"),
        # Yahoo is disabled in the registry, so it is constructed at store level only (a stored OAuth grant).
        Provider.YAHOO: store.connect(ALICE, keys[Provider.YAHOO], AccessMethod.OAUTH, name="yahoo one",
                                      is_public=False, data_origin="live", credential_ref="yahoo-ref-0001"),
    }
    # registry rows, ids, keys
    assert rows(store.path, "SELECT count(DISTINCT league_id), count(*) FROM league_registry") == [(3, 3)]
    assert len({c.league_id for c in ctxs.values()}) == 3
    assert len({k.namespaced for k in keys.values()}) == 3
    assert len(set(keys.values())) == 3 and len({hash(k) for k in keys.values()}) == 3
    assert {c.provider for c in ctxs.values()} == set(Provider)
    assert {c.name for c in ctxs.values()} == {"sleeper one", "espn one", "yahoo one"}
    assert all(c.key.external_league_id == "12345" for c in ctxs.values())
    assert store.context_for_key(ALICE, keys[Provider.ESPN]).league_id == ctxs[Provider.ESPN].league_id
    # per-league snapshots do not bleed
    for p, ctx in ctxs.items():
        store.save_snapshot(ctx, "settings", {"who": p.value}, Provenance(p, "x:y", datetime(2026, 9, 1))
                            ) if p is not Provider.YAHOO else None
    assert store.load_snapshot(ctxs[Provider.SLEEPER], "settings")[0] == {"who": "sleeper"}
    assert store.load_snapshot(ctxs[Provider.ESPN], "settings")[0] == {"who": "espn"}
    assert store.load_snapshot(ctxs[Provider.YAHOO], "settings") is None
    with pytest.raises(ValueError):                                   # a snapshot cannot carry another provider's label
        store.save_snapshot(ctxs[Provider.ESPN], "settings", {}, Provenance(Provider.SLEEPER, "x", datetime(2026, 9, 1)))
    # cache keys
    cache_keys = {scoped_cache_key(c, "roster", "1", data_version="v1") for c in ctxs.values()}
    assert len(cache_keys) == 3
    # revoking one leaves the others
    store.revoke(ctxs[Provider.SLEEPER])
    assert {c.provider for c in store.list_contexts(ALICE)} == {Provider.ESPN, Provider.YAHOO}


def test_same_external_id_is_separate_through_the_api(fake_client):
    ids = {p: post_connect(fake_client, "alice", p, "12345").json()["league_id"] for p in ("sleeper", "espn")}
    assert len(set(ids.values())) == 2
    h = hdr("alice")
    views = {p: fake_client.get(f"/v2/leagues/{lid}", headers=h).json() for p, lid in ids.items()}
    assert {v["provider"] for v in views.values()} == {"sleeper", "espn"}
    assert {v["external_league_id"] for v in views.values()} == {"12345"}
    assert fake_client.delete(f"/v2/leagues/{ids['sleeper']}", headers=h).status_code == 204
    assert fake_client.get(f"/v2/leagues/{ids['espn']}", headers=h).status_code == 200
    assert fake_client.get(f"/v2/leagues/{ids['sleeper']}", headers=h).status_code == 404


def test_cross_provider_and_cross_season_keys_are_never_equal_and_ids_stay_strings():
    assert LeagueKey(Provider.ESPN, "12345", 2026) != LeagueKey(Provider.SLEEPER, "12345", 2026)
    assert LeagueKey(Provider.ESPN, "12345", 2026) != LeagueKey(Provider.ESPN, "12345", 2025)
    assert LeagueKey(Provider.ESPN, "12345", 2026).namespaced == "espn:12345:2026"
    with pytest.raises(TypeError):
        LeagueKey(Provider.SLEEPER, 1180000000000000001, 2026)        # an int has already lost precision in JS
    big = LeagueKey(Provider.SLEEPER, L1, 2026)
    assert big.external_league_id == L1 and big.namespaced == f"sleeper:{L1}:2026"


# =================================================================================================
# 3. Same team id in different leagues does not collide
# =================================================================================================

def test_team_scoped_ids_differ_across_leagues_providers_and_seasons():
    teams = [Team(LeagueKey(Provider.SLEEPER, "100", 2026), "1", "a", ()),
             Team(LeagueKey(Provider.SLEEPER, "101", 2026), "1", "b", ()),
             Team(LeagueKey(Provider.ESPN, "100", 2026), "1", "c", ()),
             Team(LeagueKey(Provider.SLEEPER, "100", 2025), "1", "d", ()),
             Team(LeagueKey(Provider.YAHOO, "449.l.100", 2026), "449.l.100.t.1", "e", ())]
    assert len({t.scoped_id for t in teams}) == len(teams)
    assert all(t.external_team_id in t.scoped_id for t in teams)


def test_set_my_team_in_one_league_does_not_touch_another_through_the_api(real_client):
    h = hdr("alice")
    one = post_connect(real_client, "alice", "sleeper", L1).json()
    two = post_connect(real_client, "alice", "sleeper", L2).json()
    teams = {lid: real_client.get(f"/v2/leagues/{lid}/teams", headers=h).json()["teams"]
             for lid in (one["league_id"], two["league_id"])}
    ids = [{t["external_team_id"] for t in ts} for ts in teams.values()]
    assert "1" in ids[0] and "1" in ids[1]                                    # the same team id exists in both
    scoped = [{t["scoped_id"] for t in ts} for ts in teams.values()]
    assert not scoped[0] & scoped[1]                                          # but the scoped ids are disjoint

    r = real_client.put(f"/v2/leagues/{one['league_id']}/my-team", json={"external_team_id": "1"}, headers=h)
    assert r.status_code == 200 and r.json()["my_team_external_id"] == "1"
    assert real_client.get(f"/v2/leagues/{two['league_id']}", headers=h).json()["my_team_external_id"] is None
    r = real_client.put(f"/v2/leagues/{two['league_id']}/my-team", json={"external_team_id": "2"}, headers=h)
    assert r.status_code == 200
    assert real_client.get(f"/v2/leagues/{one['league_id']}", headers=h).json()["my_team_external_id"] == "1"
    # another tenant picking "1" in the shared public league does not change Alice's pick
    post_connect(real_client, "bob", "sleeper", L2)
    bob_lid = next(v["league_id"] for v in real_client.get("/v2/leagues", headers=hdr("bob")).json()["leagues"])
    assert bob_lid == two["league_id"]                                        # one registry row, two connections
    assert real_client.put(f"/v2/leagues/{bob_lid}/my-team", json={"external_team_id": "1"},
                           headers=hdr("bob")).json()["my_team_external_id"] == "1"
    assert real_client.get(f"/v2/leagues/{two['league_id']}", headers=h).json()["my_team_external_id"] == "2"


def test_snapshots_and_rosters_of_team_1_are_stored_per_league(real_client):
    one = post_connect(real_client, "alice", "sleeper", L1).json()["league_id"]
    two = post_connect(real_client, "alice", "sleeper", L2).json()["league_id"]
    store = get_tenancy_store()
    rosters = {}
    for lid in (one, two):
        ctx = store.authorize(ALICE, lid)
        payload, prov = store.load_snapshot(ctx, "rosters")
        assert prov.provider is Provider.SLEEPER and prov.source.startswith("fixture:")
        rosters[lid] = {r["external_team_id"]: r for r in payload}
        assert all(r["key"]["external_league_id"] == ctx.key.external_league_id for r in payload)
    assert "1" in rosters[one] and "1" in rosters[two]
    assert rosters[one]["1"]["player_ids"] != rosters[two]["1"]["player_ids"]
    assert rosters[one]["1"]["key"] != rosters[two]["1"]["key"]


def test_cache_entries_for_team_1_are_per_league_and_per_tenant(store):
    a1 = store.connect(ALICE, LeagueKey(Provider.SLEEPER, "100", SEASON), AccessMethod.PUBLIC, name="x", is_public=True, data_origin="fixture")
    a2 = store.connect(ALICE, LeagueKey(Provider.SLEEPER, "101", SEASON), AccessMethod.PUBLIC, name="y", is_public=True, data_origin="fixture")
    b1 = store.connect(BOB, LeagueKey(Provider.SLEEPER, "100", SEASON), AccessMethod.PUBLIC, name="x", is_public=True, data_origin="fixture")
    assert a1.league_id == b1.league_id                                       # same public league, two tenants
    cache = TenantScopedCache()
    cache.put(a1, "team", "1", data_version="v1", value="alice/100/team1")
    assert cache.get(a2, "team", "1", data_version="v1") is None              # other league, same team id
    assert cache.get(b1, "team", "1", data_version="v1") is None              # other tenant, same league and team id
    assert cache.get(a1, "team", "1", data_version="v2") is None              # other data version
    assert cache.get(a1, "team", "1", data_version="v1") == "alice/100/team1"
    assert len({scoped_cache_key(c, "team", "1", data_version="v1") for c in (a1, a2, b1)}) == 3


# =================================================================================================
# 4. The same player name never auto-matches
# =================================================================================================

def _link(provider, raw, gsis=None, team=None, method=IdentityMethod.ID_MAP):
    p = ProviderPlayerId(provider, raw)
    if gsis or team:
        return IdentityLink(p, gsis, team, method, Confidence.HIGH)
    return IdentityLink(p, None, None, IdentityMethod.UNMATCHED, Confidence.NONE, "no id-based evidence")


def test_players_with_the_same_name_are_never_matched_by_name():
    # Three different "Mike Williams": two resolved to different GSIS ids, one unresolved
    pit = _link(Provider.SLEEPER, "7001", gsis="00-0030001")
    nyj = _link(Provider.SLEEPER, "7002", gsis="00-0030002")
    buf = _link(Provider.SLEEPER, "7003")
    assert not same_athlete(pit, nyj)
    assert not same_athlete(pit, buf) and not same_athlete(buf, pit)
    assert same_athlete(buf, buf) and same_athlete(pit, pit)
    # the same raw id on two providers is two different players until an id source says otherwise
    espn_4046, sleeper_4046 = _link(Provider.ESPN, "4046"), _link(Provider.SLEEPER, "4046")
    assert not same_athlete(espn_4046, sleeper_4046)
    assert ProviderPlayerId(Provider.ESPN, "4046") != ProviderPlayerId(Provider.SLEEPER, "4046")
    assert ProviderPlayerId(Provider.ESPN, "4046").namespaced != ProviderPlayerId(Provider.SLEEPER, "4046").namespaced
    with pytest.raises(ValueError):
        ProviderPlayerId.parse("4046")                                       # bare ids are refused
    # id-based evidence is what links: two providers resolved to the same GSIS id ARE the same athlete
    assert same_athlete(_link(Provider.ESPN, "3139477", gsis="00-0033873"),
                        _link(Provider.SLEEPER, "4046", gsis="00-0033873", method=IdentityMethod.PROVIDER_GSIS))
    # two unresolved players with identical names/positions/teams stay distinct
    assert not same_athlete(_link(Provider.ESPN, "1"), _link(Provider.SLEEPER, "1"))
    assert not same_athlete(_link(Provider.ESPN, "1"), _link(Provider.ESPN, "2"))


def test_sleeper_fixture_players_named_mike_williams_stay_unlinked_by_name(env):
    env.setenv("SLEEPER_SOURCE", "fixture")
    real_registry.reset_providers()
    provider = real_registry.get_provider(Provider.SLEEPER)
    key = LeagueKey(Provider.SLEEPER, L2, SEASON)
    rosters = provider.fetch_rosters(key, LeagueAccess(AccessMethod.PUBLIC)).value
    williams = [e for r in rosters for e in r.entries if e.name == "Mike Williams"]
    assert {e.player.raw_id for e in williams} == {"7001", "7002", "7003"}
    by_id = {e.player.raw_id: e.identity for e in williams}
    assert by_id["7001"].gsis_id != by_id["7002"].gsis_id and None not in (by_id["7001"].gsis_id, by_id["7002"].gsis_id)
    assert by_id["7003"].method is IdentityMethod.UNMATCHED and by_id["7003"].gsis_id is None
    gsis = [i.gsis_id for i in by_id.values() if i.gsis_id]
    assert len(gsis) == len(set(gsis))
    for a in by_id.values():
        for b in by_id.values():
            assert same_athlete(a, b) == (a.player == b.player)


@pytest.mark.parametrize("row,expected_resolved", [
    ({"espn_player_id": 1, "gsis_id": "00-0033873", "match_method": "name_position_team", "match_confidence": "medium"}, False),
    ({"espn_player_id": 2, "gsis_id": "00-0033873", "match_method": "unmatched", "match_confidence": "none"}, False),
    ({"espn_player_id": 3, "gsis_id": None, "match_method": "espn_id:ff_playerids"}, False),
    ({"espn_player_id": 4, "gsis_id": "00-0033873", "match_method": "espn_id:ff_playerids", "match_confidence": "high"}, True),
    ({"espn_player_id": 5, "gsis_id": "00-0033873", "match_method": "espn_id:nfl_players", "match_confidence": "high"}, True),
])
def test_legacy_espn_name_matches_are_unresolved_identities(row, expected_resolved):
    link = link_from_espn_map_row(row)
    assert link.resolved is expected_resolved
    if not expected_resolved:
        assert link.gsis_id is None and link.method is IdentityMethod.UNMATCHED and link.confidence is Confidence.NONE
        assert link.reason
    if row["match_method"] == "name_position_team":
        assert "name" in link.reason.lower()
        # the very same GSIS id on a Sleeper player (id evidence) must NOT make the name-matched ESPN player equal
        sleeper = _link(Provider.SLEEPER, "4046", gsis="00-0033873", method=IdentityMethod.PROVIDER_GSIS)
        assert not same_athlete(link, sleeper)


# =================================================================================================
# 5. League-specific scoring is preserved
# =================================================================================================

def test_two_sleeper_leagues_keep_their_own_scoring_through_the_api(real_client):
    h = hdr("alice")
    stored = {}
    for ext in (L1, L2):
        lid = post_connect(real_client, "alice", "sleeper", ext).json()["league_id"]
        stored[ext] = real_client.get(f"/v2/leagues/{lid}/settings", headers=h).json()["scoring"]
    assert stored[L1] != stored[L2]
    # exactly what each league's own settings say, verbatim: nothing borrowed from the other, nothing defaulted
    for ext, scoring in stored.items():
        raw = json.loads((FIXTURE_DIR / "leagues" / ext / "league.json").read_text())["scoring_settings"]
        assert scoring["provider_rules"] == {k: float(v) for k, v in raw.items()}
    assert set(stored[L1]["provider_rules"]) == {"rec", "pass_td", "rush_yd"}
    assert "pass_yd" not in stored[L1]["provider_rules"] and "pass_yd" in stored[L2]["provider_rules"]
    assert "rec_te_bonus" not in stored[L1]["canonical"] and stored[L2]["canonical"]["rec_te_bonus"] == 0.5
    assert stored[L2]["unmapped"] == ["bonus_pass_yd_300", "def_sack"] and stored[L1]["unmapped"] == []
    assert stored[L1]["points_per_reception"] == 0.5 == stored[L2]["points_per_reception"]


def test_points_per_reception_is_none_when_the_league_does_not_say(env, tmp_path):
    """A league whose scoring_settings has no `rec` key reports None, never 0, 0.5 or 1."""
    from app.main import app
    from app.sleeper.client import SleeperClient

    fx = tmp_path / "fx"
    shutil.copytree(FIXTURE_DIR, fx)
    league_file = fx / "leagues" / L2 / "league.json"
    doc = json.loads(league_file.read_text())
    del doc["scoring_settings"]["rec"]
    league_file.write_text(json.dumps(doc))
    provider = SleeperProvider(client=SleeperClient(transport=FixtureTransport(fx), cache_dir=tmp_path / "cache"),
                               idmap=load_fixture_id_map(), source="fixture")
    app.dependency_overrides[provider_registry] = lambda: FakeRegistry(provider)
    try:
        client, h = TestClient(app), hdr("alice")
        r = post_connect(client, "alice", "sleeper", L2)
        assert r.status_code == 201, r.text
        scoring = client.get(f"/v2/leagues/{r.json()['league_id']}/settings", headers=h).json()["scoring"]
        assert scoring["points_per_reception"] is None
        assert "rec" not in scoring["provider_rules"] and "rec" not in scoring["canonical"]
        assert ScoringRules({}, {}, ()).points_per_reception is None
    finally:
        app.dependency_overrides.clear()


# =================================================================================================
# 6. Superflex and regular FLEX stay distinct
# =================================================================================================

def test_superflex_and_flex_are_distinct_in_the_api_settings(real_client):
    h = hdr("alice")
    lineups = {}
    for ext in (L1, L2):
        lid = post_connect(real_client, "alice", "sleeper", ext).json()["league_id"]
        lineups[ext] = real_client.get(f"/v2/leagues/{lid}/settings", headers=h).json()["lineup"]
    dynasty, redraft = lineups[L1], lineups[L2]
    slots = {s["code"]: s for s in dynasty["slots"]}
    assert dynasty["is_superflex"] is True and dynasty["superflex_slots"] == 1 and dynasty["flex_slots"] == 1
    assert slots["FLEX"]["eligible"] == ["RB", "TE", "WR"]
    assert slots["SUPERFLEX"]["eligible"] == ["QB", "RB", "TE", "WR"] and slots["SUPERFLEX"]["provider_code"] == "SUPER_FLEX"
    assert "QB" in slots["SUPERFLEX"]["eligible"] and "QB" not in slots["FLEX"]["eligible"]
    assert redraft["is_superflex"] is False and redraft["superflex_slots"] == 0 and redraft["flex_slots"] == 1
    assert "SUPERFLEX" not in {s["code"] for s in redraft["slots"]}


def test_espn_slot_23_is_flex_and_slot_7_is_superflex():
    from app.leagues.providers.espn import lineup_rules
    both = lineup_rules({"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "7": 1, "20": 6})
    by_code = {s.code: s for s in both.slots}
    assert by_code["FLEX"].eligible == frozenset({"RB", "WR", "TE"})
    assert by_code["SUPERFLEX"].eligible == frozenset({"QB", "RB", "WR", "TE"})
    assert by_code["SUPERFLEX"].provider_code != by_code["FLEX"].provider_code
    assert both.is_superflex and both.superflex_slots == 1 and both.flex_slots == 1
    flex_only = lineup_rules({"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "20": 6})
    assert not flex_only.is_superflex and flex_only.superflex_slots == 0 and flex_only.flex_slots == 1
    op_only = lineup_rules({"0": 1, "7": 1})
    assert op_only.is_superflex and op_only.flex_slots == 0
    # ESPN's OP and Sleeper's SUPER_FLEX are the same kind of slot, by eligibility
    sleeper_sf = {s.code: s for s in lineup_rules_for_sleeper(["QB", "SUPER_FLEX", "FLEX"]).slots}["SUPERFLEX"]
    assert sleeper_sf.eligible == by_code["SUPERFLEX"].eligible


def lineup_rules_for_sleeper(positions):
    from app.leagues.providers.sleeper import lineup_rules
    return lineup_rules(SimpleNamespace(roster_positions=positions, settings={}))


def test_slot_kind_follows_eligibility_not_the_label():
    """A slot LABELLED superflex whose eligibility excludes QB is not superflex; a slot labelled FLEX that
    admits a QB is."""
    mislabelled = LineupRules((LineupSlot("SUPERFLEX", 1, SlotKind.STARTER, frozenset({"RB", "WR", "TE"}), "X"),))
    assert not mislabelled.is_superflex and mislabelled.flex_slots == 1
    sneaky = LineupRules((LineupSlot("FLEX", 1, SlotKind.STARTER, frozenset({"QB", "RB", "WR", "TE"}), "X"),))
    assert sneaky.is_superflex and sneaky.flex_slots == 0
    with pytest.raises(ValueError):
        LineupSlot("SUPERFLEX", 1, SlotKind.STARTER, frozenset(), "OP")      # a starter slot must say who may fill it


# =================================================================================================
# 7. Missing capabilities are explicit
# =================================================================================================

def _registry_configurations(env):
    """(label, apply) pairs covering every provider both disabled and enabled."""
    return [("all off", {}), ("sleeper fixture + espn public + yahoo id",
                              {"SLEEPER_SOURCE": "fixture", "ESPN_PUBLIC_IMPORT_ENABLED": "1", "YAHOO_CLIENT_ID": "abc"})]


def test_every_provider_declares_a_state_for_every_capability(env):
    for label, flags in _registry_configurations(env):
        for name in ("SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID"):
            env.delenv(name, raising=False)
        for k, v in flags.items():
            env.setenv(k, v)
        real_registry.reset_providers()
        caps = real_registry.all_capabilities()
        assert {c.provider for c in caps} == set(Provider), label
        for c in caps:
            assert set(c.states) == set(Capability), f"{label}: {c.provider.value} leaves capabilities undeclared"
            shown = c.as_dict()
            assert set(shown["capabilities"]) == {cap.value for cap in Capability}
            for cap in Capability:
                st = c.state(cap)
                assert isinstance(st.support, Support)
                if not st.available:
                    assert st.note and st.note.strip(), f"{label}: {c.provider.value}.{cap.value} is {st.support.value} with no reason"
            if not c.enabled:
                assert c.reason and all(c.state(cap).support is Support.DISABLED for cap in Capability)
        assert next(c for c in caps if c.provider is Provider.YAHOO).enabled is False   # OAuth is not implemented


def test_disabled_providers_raise_capability_unavailable_for_every_operation(env):
    real_registry.reset_providers()
    access, week = LeagueAccess(AccessMethod.PUBLIC), 1
    for provider in Provider:
        adapter = real_registry.get_provider(provider)
        assert adapter.capabilities().enabled is False
        key = LeagueKey(provider, "12345" if provider is not Provider.YAHOO else "449.l.12345", SEASON)
        calls = [lambda: adapter.discover_leagues(access, user_handle="someone", season=SEASON),
                 lambda: adapter.fetch_settings(key, access), lambda: adapter.fetch_teams(key, access),
                 lambda: adapter.fetch_rosters(key, access), lambda: adapter.fetch_scoring(key, access),
                 lambda: adapter.fetch_matchups(key, access, week=week), lambda: adapter.fetch_standings(key, access),
                 lambda: adapter.fetch_free_agents(key, access), lambda: adapter.fetch_transactions(key, access, week=week),
                 lambda: adapter.fetch_projections(key, access, week=week)]
        for call in calls:
            with pytest.raises(CapabilityUnavailable) as err:
                call()
            assert err.value.state.support is Support.DISABLED and err.value.provider is provider


def test_unsupported_operations_on_an_enabled_provider_raise_instead_of_returning_empty_data(env):
    env.setenv("SLEEPER_SOURCE", "fixture")
    real_registry.reset_providers()
    sleeper = real_registry.get_provider(Provider.SLEEPER)
    key = LeagueKey(Provider.SLEEPER, L1, SEASON)
    with pytest.raises(CapabilityUnavailable) as err:
        sleeper.fetch_projections(key, LeagueAccess(AccessMethod.PUBLIC), week=1)
    assert err.value.state.support is Support.UNSUPPORTED


def test_league_view_missing_capabilities_names_every_unavailable_capability(env, tmp_path):
    env.setenv("SLEEPER_SOURCE", "fixture")
    env.setenv("YAHOO_CLIENT_ID", "abc")
    real_registry.reset_providers()
    capmap = {c.provider: c for c in real_registry.all_capabilities()}
    store = TenancyStore(tmp_path / "s.duckdb", capabilities=lambda: capmap)
    yahoo = store.connect(ALICE, LeagueKey(Provider.YAHOO, "449.l.1", SEASON), AccessMethod.OAUTH, name="y",
                          is_public=False, data_origin="live", credential_ref="yahoo-ref-0002")
    view = league_view(yahoo, data_origin="live")
    assert view["missing_capabilities"] == [c.value for c in Capability]          # disabled provider: all of them
    assert all(v["support"] == "disabled" and v["note"] for v in view["capabilities"]["capabilities"].values())
    sleeper = store.connect(ALICE, LeagueKey(Provider.SLEEPER, L1, SEASON), AccessMethod.PUBLIC, name="s",
                            is_public=True, data_origin="fixture")
    sv = league_view(sleeper, data_origin="fixture")
    states = sv["capabilities"]["capabilities"]
    assert set(sv["missing_capabilities"]) == {n for n, s in states.items() if s["support"] not in ("supported", "derived")}
    assert {"projections", "private_leagues"} <= set(sv["missing_capabilities"])
    assert "teams" not in sv["missing_capabilities"] and "standings" not in sv["missing_capabilities"]   # derived counts as available


def test_league_view_through_the_api_lists_what_the_provider_cannot_do(real_client):
    view = post_connect(real_client, "alice", "sleeper", L1).json()
    states = view["capabilities"]["capabilities"]
    assert set(states) == {c.value for c in Capability}
    assert set(view["missing_capabilities"]) == {n for n, s in states.items() if s["support"] not in ("supported", "derived")}
    assert "projections" in view["missing_capabilities"] and states["projections"]["support"] == "unsupported"
    assert states["standings"]["support"] == "derived" and states["standings"]["note"]


@pytest.mark.parametrize("provider,ext,extra_env", [
    ("yahoo", "449.l.12345", {}),
    ("yahoo", "449.l.12345", {"YAHOO_CLIENT_ID": "client-id-present"}),
    ("espn", "12345", {}),
])
def test_yahoo_and_espn_connect_are_501_with_a_reason_and_never_201(real_client, env, provider, ext, extra_env):
    for k, v in extra_env.items():
        env.setenv(k, v)
    real_registry.reset_providers()
    r = post_connect(real_client, "alice", provider, ext)
    assert r.status_code == 501, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and len(detail) > 20 and "client-id-present" not in detail
    assert real_client.get("/v2/leagues", headers=hdr("alice")).json() == {"leagues": []}
    assert not tenancy_path().exists() or rows(tenancy_path(), "SELECT count(*) FROM league_registry") == [(0,)]


def test_espn_with_only_the_owner_cookies_configured_still_refuses_everyone_else(real_client, env):
    env.setenv("FANTASY_OWNER_USER_ID", "bob")
    env.setenv("ESPN_S2", FAKE_S2)
    env.setenv("SWID", FAKE_SWID)
    real_registry.reset_providers()
    r = post_connect(real_client, "alice", "espn", "12345")        # public import is off: no cookieless ESPN reads
    assert r.status_code in (501, 503), r.text
    assert FAKE_S2 not in r.text and FAKE_SWID not in r.text
    assert real_client.get("/v2/leagues", headers=hdr("alice")).json() == {"leagues": []}


# =================================================================================================
# 8. Anonymous access follows the authorization rules
# =================================================================================================

ROUTES = [("GET", "/v2/providers"), ("GET", "/v2/leagues"), ("POST", "/v2/leagues"),
          ("GET", "/v2/leagues/{id}"), ("GET", "/v2/leagues/{id}/settings"), ("GET", "/v2/leagues/{id}/teams"),
          ("PUT", "/v2/leagues/{id}/my-team"), ("DELETE", "/v2/leagues/{id}")]


def _call(client, method, path, headers=None, league_id="lg_" + "q" * 24):
    return client.request(method, path.format(id=league_id), headers=headers or {},
                          json={"provider": "sleeper", "external_league_id": L1, "season": SEASON,
                                "external_team_id": "1"} if method in ("POST", "PUT") else None)


def test_anonymous_callers_get_public_info_only(real_client):
    providers = real_client.get("/v2/providers")
    assert providers.status_code == 200
    assert {p["provider"] for p in providers.json()["providers"]} == {"espn", "sleeper", "yahoo"}
    assert real_client.get("/v2/leagues").status_code == 401
    assert real_client.post("/v2/leagues", json={"provider": "sleeper", "external_league_id": L1,
                                                 "season": SEASON}).status_code == 401
    owned = post_connect(real_client, "alice", "sleeper", L1).json()["league_id"]
    for method, path in ROUTES[3:]:
        assert _call(real_client, method, path, league_id=owned).status_code == 404, (method, path)
    # nothing about the league leaks to an anonymous caller, even though it exists and is public data
    assert "Fixture" not in real_client.get(f"/v2/leagues/{owned}").text


def test_the_service_api_key_is_not_a_user(real_client, env):
    env.setenv("FANTASY_API_KEY", "k" * 40)
    owned = None
    key = {"X-API-Key": "k" * 40}
    assert real_client.get("/v2/providers", headers=key).status_code == 200
    assert real_client.get("/v2/leagues", headers=key).status_code == 401
    assert real_client.get(f"/v2/leagues/{owned or 'lg_' + 'a' * 24}", headers=key).status_code == 404
    # a user header without the service key never gets past the service gate
    assert real_client.get("/v2/leagues", headers=hdr("alice")).status_code == 401
    assert real_client.get("/v2/leagues", headers={**key, **hdr("alice")}).status_code == 200


def _forged(payload: dict, secret: str = SECRET) -> str:
    import base64, hashlib, hmac
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    body = b64(json.dumps(payload).encode())
    return f"v1.{body}.{b64(hmac.new(secret.encode(), f'v1.{body}'.encode(), hashlib.sha256).digest())}"


def _claims(**over):
    now = int(time.time())
    return {"sub": "alice", "aud": "fantasy-intel-api", "iat": now, "exp": now + 300, **over}


BAD_ASSERTIONS = {
    "garbage": lambda: "garbage",
    "empty": lambda: "",
    "alg-none": lambda: _forged(_claims()).rsplit(".", 1)[0] + ".",
    "unsigned": lambda: _forged(_claims()).rsplit(".", 1)[0],
    "wrong secret": lambda: _forged(_claims(), secret="u" * 48),
    "expired beyond skew": lambda: _forged(_claims(iat=int(time.time()) - 1000, exp=int(time.time()) - 400)),
    "lives too long": lambda: _forged(_claims(exp=int(time.time()) + 601)),
    "from the future": lambda: _forged(_claims(iat=int(time.time()) + 3600, exp=int(time.time()) + 3900)),
    "wrong audience": lambda: _forged(_claims(aud="somebody-else")),
    "no subject": lambda: _forged({k: v for k, v in _claims().items() if k != "sub"}),
    "subject with a newline": lambda: _forged(_claims(sub="alice\n")),
    "subject with unicode": lambda: _forged(_claims(sub="alicé")),
    "float timestamps": lambda: _forged(_claims(iat=1.5e9, exp=1.5e9 + 60)),
    "boolean timestamps": lambda: _forged(_claims(iat=True, exp=True)),
    "oversized": lambda: _forged(_claims(sub="a" * 128)) + "A" * 3000,
    "extra segment": lambda: _forged(_claims()) + ".x",
    "uppercase version": lambda: "V1" + _forged(_claims())[2:],
    "leading space": lambda: " " + _forged(_claims()),
}


@pytest.mark.parametrize("name", sorted(BAD_ASSERTIONS))
def test_a_bad_assertion_is_401_never_anonymous_and_never_a_user(real_client, name):
    headers = {HEADER: BAD_ASSERTIONS[name]()}
    for method, path in ROUTES:
        r = _call(real_client, method, path, headers)
        assert r.status_code == 401, (name, method, path, r.status_code)
        assert r.json() == {"detail": "Invalid or expired user assertion."}        # never echoes why or what


def test_a_signed_assertion_is_accepted_but_only_inside_its_lifetime(real_client):
    now = time.time()
    assert real_client.get("/v2/leagues", headers={HEADER: sign_user_assertion("alice", SECRET, now)}).status_code == 200
    # still valid inside the 60 s skew, refused once past it
    just_expired = sign_user_assertion("alice", SECRET, now - 300 - 30, ttl=300)
    assert real_client.get("/v2/leagues", headers={HEADER: just_expired}).status_code == 200
    long_expired = sign_user_assertion("alice", SECRET, now - 300 - 90, ttl=300)
    assert real_client.get("/v2/leagues", headers={HEADER: long_expired}).status_code == 401


def test_the_verifier_rejects_structural_variants_even_with_a_valid_mac():
    now = time.time()
    good = sign_user_assertion("alice", SECRET, now)
    assert verify_user_assertion(good, SECRET, now).user_id == "alice"
    version, body, sig = good.split(".")
    for variant in (f"{version}.{body}.{sig}=", f"{version}.{body}=.{sig}", f"{version}.{body}.{sig}\n",
                    f"{version}.{body}\n.{sig}", f"{version}.{body}.{sig[:-1]}", f"{version}..{sig}",
                    f"{version}.{body}.{sig.swapcase()}", f"v2.{body}.{sig}", f"{body}.{sig}", f"{version}.{body}.é"):
        with pytest.raises(InvalidUserAssertion):
            verify_user_assertion(variant, SECRET, now)
    # duplicate JSON keys signed by the real secret still resolve to ONE well-formed subject (the last one)
    import base64, hashlib, hmac
    raw = ('{"sub":"mallory","sub":"alice","aud":"fantasy-intel-api","iat":%d,"exp":%d}' % (now, now + 60)).encode()
    body2 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig2 = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"v1.{body2}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert verify_user_assertion(f"v1.{body2}.{sig2}", SECRET, now).user_id == "alice"
    # a secret that is too short never verifies, whoever signed
    with pytest.raises(InvalidUserAssertion):
        verify_user_assertion(good, "short", now)


@pytest.mark.parametrize("flag", [None, "", "0", "false", "no", "off"])
def test_flag_off_makes_every_v2_route_404_even_with_a_valid_assertion(real_client, env, flag):
    owned = post_connect(real_client, "alice", "sleeper", L1).json()["league_id"]
    if flag is None:
        env.delenv("MULTILEAGUE_ENABLED")
    else:
        env.setenv("MULTILEAGUE_ENABLED", flag)
    for headers in ({}, hdr("alice"), {HEADER: "garbage"}):
        for method, path in ROUTES:
            r = _call(real_client, method, path, headers, league_id=owned)
            assert r.status_code == 404, (flag, method, path, r.status_code)
    assert real_client.get("/health").json() == {"ok": True}


# =================================================================================================
# 9. Provider failures do not affect unrelated tenants
# =================================================================================================

def test_a_provider_failure_marks_only_that_league_and_leaves_other_tenants_untouched(fake_client, fakes):
    sleeper = fakes[Provider.SLEEPER]
    a_lid = post_connect(fake_client, "alice", "sleeper", "111").json()["league_id"]
    b_lid = post_connect(fake_client, "bob", "sleeper", "222").json()["league_id"]
    ha, hb = hdr("alice"), hdr("bob")

    def snapshot(client, headers, lid):
        return {"view": client.get(f"/v2/leagues/{lid}", headers=headers).json(),
                "teams": client.get(f"/v2/leagues/{lid}/teams", headers=headers).json(),
                "settings": client.get(f"/v2/leagues/{lid}/settings", headers=headers).json()}

    alice_before, bob_before = snapshot(fake_client, ha, a_lid), snapshot(fake_client, hb, b_lid)
    assert alice_before["view"]["sync"]["status"] == "ok"

    # Bob's league starts failing; the exception text carries things that must never be shown or stored
    sleeper.fail_for["222"] = RuntimeError(f"GET failed: Cookie: espn_s2={FAKE_S2}; SWID={FAKE_SWID} Bearer abc.def.ghi")
    r = post_connect(fake_client, "bob", "sleeper", "222")
    assert r.status_code == 502
    for secret in (FAKE_S2, FAKE_SWID, "abc.def.ghi"):
        assert secret not in r.text
    bob_after = snapshot(fake_client, hb, b_lid)
    assert bob_after["view"]["sync"]["status"] == "failed" and bob_after["view"]["sync"]["last_error"]
    assert FAKE_S2 not in json.dumps(bob_after) and FAKE_SWID not in json.dumps(bob_after)
    assert bob_after["teams"] == bob_before["teams"] and bob_after["settings"] == bob_before["settings"]   # last good data kept

    # Alice is untouched in every way
    assert snapshot(fake_client, ha, a_lid) == alice_before
    assert fake_client.get("/v2/leagues", headers=ha).json()["leagues"][0]["sync"]["status"] == "ok"

    # A stranger who fails against Alice's or Bob's league cannot change what their members see
    sleeper.fail_for["111"] = ProviderUnavailable("Sleeper could not be reached")
    assert post_connect(fake_client, "carol", "sleeper", "111").status_code == 503
    assert post_connect(fake_client, "carol", "sleeper", "222").status_code == 502
    assert snapshot(fake_client, ha, a_lid) == alice_before
    assert snapshot(fake_client, hb, b_lid)["view"]["sync"] == bob_after["view"]["sync"]
    assert fake_client.get("/v2/leagues", headers=hdr("carol")).json() == {"leagues": []}

    # Alice's own failure marks her league only
    assert post_connect(fake_client, "alice", "sleeper", "111").status_code == 503
    assert fake_client.get(f"/v2/leagues/{a_lid}", headers=ha).json()["sync"]["status"] == "failed"
    assert snapshot(fake_client, hb, b_lid)["view"]["sync"] == bob_after["view"]["sync"]

    # recovery is per league too
    sleeper.fail_for.clear()
    assert post_connect(fake_client, "bob", "sleeper", "222").status_code == 201
    assert fake_client.get(f"/v2/leagues/{b_lid}", headers=hb).json()["sync"] ["status"] == "ok"
    assert fake_client.get(f"/v2/leagues/{a_lid}", headers=ha).json()["sync"]["status"] == "failed"


def test_one_providers_outage_does_not_change_another_providers_leagues(fake_client, fakes):
    a_sleeper = post_connect(fake_client, "alice", "sleeper", "111").json()["league_id"]
    a_espn = post_connect(fake_client, "alice", "espn", "111").json()["league_id"]
    fakes[Provider.ESPN].fail_for["111"] = ProviderUnavailable("ESPN could not be reached")
    assert post_connect(fake_client, "alice", "espn", "111").status_code == 503
    h = hdr("alice")
    assert fake_client.get(f"/v2/leagues/{a_espn}", headers=h).json()["sync"]["status"] == "failed"
    assert fake_client.get(f"/v2/leagues/{a_sleeper}", headers=h).json()["sync"]["status"] == "ok"
    assert fake_client.get(f"/v2/leagues/{a_sleeper}/teams", headers=h).status_code == 200


def test_reads_never_call_a_provider(fake_client, fakes):
    lid = post_connect(fake_client, "alice", "sleeper", "111").json()["league_id"]
    for p in fakes.values():
        p.calls.clear()
        p.fail_for["111"] = RuntimeError("provider is down")
    h = hdr("alice")
    for path in ("", "/settings", "/teams"):
        assert fake_client.get(f"/v2/leagues/{lid}{path}", headers=h).status_code == 200
    assert fake_client.get("/v2/leagues", headers=h).status_code == 200
    assert all(not p.calls for p in fakes.values())


# =================================================================================================
# 10. The existing ESPN-only deployment is unchanged
# =================================================================================================

NEW_ENV = ("MULTILEAGUE_ENABLED", "MULTILEAGUE_UI_ENABLED", "FANTASY_TENANCY_DB_PATH", "FANTASY_USER_ASSERTION_SECRET",
           "FANTASY_OWNER_USER_ID", "SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID")
BACKEND = Path(__file__).resolve().parent.parent


def test_settings_defaults_with_all_new_environment_unset_are_off(monkeypatch):
    for name in NEW_ENV:
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert s.multileague_enabled is False
    assert s.user_assertion_secret is None and s.owner_user_id is None and s.yahoo_client_id is None
    assert s.sleeper_source == "disabled" and s.espn_public_import_enabled is False
    assert s.tenancy_db_path.name == "tenancy.duckdb" and s.tenancy_db_path != s.db_path
    # a short secret is treated as unset rather than accepted
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", "s" * 31)
    assert get_settings().user_assertion_secret is None
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", "s" * 32)
    assert get_settings().user_assertion_secret == "s" * 32


@pytest.fixture
def espn_only_db(tmp_path, monkeypatch):
    """The owner's single-league analytics database, built the way tests/test_api.py builds it."""
    from app.analytics.pipeline import run_analytics
    from app.db.connection import connect, open_db
    from app.espn.sync import run_espn_sync
    from espn_fixture import MY_SWID, SEASON as ESPN_SEASON, FakeEspnClient

    db_path = tmp_path / "api.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(db_path)
    run_analytics(con, ESPN_SEASON)
    con.close()
    for name in NEW_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FANTASY_API_KEY", raising=False)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    return db_path


LEGACY_GETS = ("/health", "/api/overview", "/api/managers", "/api/waivers", "/api/status")


def test_espn_only_deployment_serves_existing_routes_and_404s_v2(espn_only_db, tmp_path, monkeypatch):
    from app.main import app
    app.dependency_overrides.clear()
    tenancy = tmp_path / "must-not-be-created.duckdb"
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", str(tenancy))        # even pointing at one, the flag decides
    client = TestClient(app)
    legacy_before = {p: client.get(p) for p in LEGACY_GETS}
    assert all(r.status_code == 200 for r in legacy_before.values()), {p: r.status_code for p, r in legacy_before.items()}
    assert len(legacy_before["/api/overview"].json()["roster"]) == 12

    for method, path in ROUTES:
        for headers in ({}, {HEADER: sign_user_assertion("alice", SECRET, time.time())}):
            assert _call(client, method, path, headers).status_code == 404, (method, path)
    assert not tenancy.exists(), "a flag-off /v2 request must not even create the tenancy database"

    # turning the feature on adds /v2 and changes nothing about the legacy answers
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    assert client.get("/v2/providers").status_code == 200
    for path in LEGACY_GETS:
        assert client.get(path).json() == legacy_before[path].json(), path


def test_the_analytics_database_never_receives_tenancy_tables_and_vice_versa(espn_only_db, tmp_path, monkeypatch):
    from app.main import app
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    monkeypatch.setenv("FANTASY_USER_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("FANTASY_TENANCY_DB_PATH", str(tmp_path / "tenancy.duckdb"))
    monkeypatch.setenv("SLEEPER_SOURCE", "fixture")
    real_registry.reset_providers()
    app.dependency_overrides.clear()

    def tables(path):
        return {r[0] for r in rows(path, "SELECT table_name FROM duckdb_tables()")}

    analytics_before = tables(espn_only_db)
    assert not analytics_before & TENANCY_TABLES
    client = TestClient(app)
    assert post_connect(client, "alice", "sleeper", L1).status_code == 201
    assert client.get("/api/overview").status_code == 200
    assert tables(espn_only_db) == analytics_before
    assert tables(tmp_path / "tenancy.duckdb") == TENANCY_TABLES            # and no analytics table leaked the other way
    real_registry.reset_providers()


def test_publish_never_references_the_tenancy_database():
    publish = (BACKEND / "app" / "db" / "publish.py").read_text()
    assert not re.search(r"tenanc|app\.leagues|\.\.leagues|league_registry|league_connections|league_snapshots|"
                         r"credential_refs|app_users", publish, re.IGNORECASE)
    imported = set()
    for node in ast.walk(ast.parse(publish)):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("leagues" in name for name in imported), imported
    # the one place that decides which file is published takes the analytics path
    scripts = [p for p in (BACKEND / "scripts").glob("*.py") if "publish" in p.read_text() and "tenancy" in p.read_text().lower()]
    assert not scripts, f"scripts mixing publishing with the tenancy database: {scripts}"


def test_schema_sql_has_no_tenancy_tables_and_the_schema_version_is_unchanged():
    from app.db import connection
    schema = (BACKEND / "app" / "db" / "schema.sql").read_text()
    created = set(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", schema, re.IGNORECASE))
    assert created, "expected to parse CREATE TABLE statements"
    assert not created & TENANCY_TABLES
    assert not re.search(r"tenanc|credential|user_assertion", schema, re.IGNORECASE)
    assert connection.SCHEMA_VERSION == 8 and connection.MIN_READ_SCHEMA_VERSION == 7
    tenancy_schema = (BACKEND / "app" / "leagues" / "tenancy_schema.sql").read_text()
    assert set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", tenancy_schema)) == TENANCY_TABLES


# =================================================================================================
# Findings from the adversarial review (fixed; these assert the safe behaviour as regression guards)
# =================================================================================================

def test_sec1_public_connection_stops_authorizing_when_the_league_turns_private(store, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    key = LeagueKey(Provider.ESPN, "777", SEASON)
    bob = store.connect(BOB, key, AccessMethod.PUBLIC, name="L", is_public=True, data_origin="live")
    owner = store.connect(ALICE, key, AccessMethod.OWNER_ENV, name="L", is_public=False, data_origin="live")
    store.save_snapshot(owner, "rosters", [{"private": "roster fetched with the owner's cookies"}],
                        Provenance(Provider.ESPN, "espn:mRoster", datetime(2026, 9, 1)))
    with pytest.raises(LeagueAccessDenied):
        store.authorize(BOB, bob.league_id)
    # nor can the stale context read the private snapshot
    with pytest.raises(LeagueAccessDenied):
        store.load_snapshot(bob, "rosters")


def test_sec2_owner_env_connection_ends_when_the_user_is_no_longer_the_owner(store, monkeypatch):
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "alice")
    ctx = store.connect(ALICE, LeagueKey(Provider.ESPN, "777", SEASON), AccessMethod.OWNER_ENV, name="L",
                        is_public=False, data_origin="live")
    monkeypatch.setenv("FANTASY_OWNER_USER_ID", "carol")             # ownership handed over (or the variable removed)
    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, ctx.league_id)
    monkeypatch.delenv("FANTASY_OWNER_USER_ID")
    with pytest.raises(LeagueAccessDenied):
        store.authorize(ALICE, ctx.league_id)


@pytest.mark.parametrize("method,path", [("POST", "/v2/providers"), ("DELETE", "/v2/providers"), ("PATCH", "/v2/leagues"),
                                         ("GET", "/v2/leagues/"), ("OPTIONS", "/v2/leagues"),
                                         ("GET", "/v2/leagues/lg_aaaaaaaaaaaaaaaaaaaaaaaa/my-team")])
def test_sec3_flag_off_is_404_for_every_method_and_path_shape(env, method, path):
    from app.main import app
    env.setenv("MULTILEAGUE_ENABLED", "0")
    r = TestClient(app, follow_redirects=False).request(method, path)
    assert r.status_code == 404, (method, path, r.status_code)


def test_sec5_a_context_cannot_be_forged_by_replacing_its_principal_or_league(store):
    """Impact if it were allowed: replace(bob_ctx, principal=ALICE) reads Alice's entries in the tenant-scoped
    cache (which trusts the context) for the same league, because cache keys derive from ctx.principal."""
    ctx = store.connect(ALICE, LeagueKey(Provider.SLEEPER, "888", SEASON), AccessMethod.PUBLIC, name="A",
                        is_public=True, data_origin="fixture")
    with pytest.raises(Exception):
        dataclasses.replace(ctx, principal=BOB)
    with pytest.raises(Exception):
        dataclasses.replace(ctx, league_id="lg_" + "b" * 24)
    with pytest.raises(Exception):
        dataclasses.replace(ctx, key=LeagueKey(Provider.SLEEPER, "999", SEASON))


def test_sec6_an_assertion_secret_equal_to_the_service_api_key_is_treated_as_unset(env):
    shared = "shared-secret-" + "x" * 40
    env.setenv("FANTASY_API_KEY", shared)
    env.setenv("FANTASY_USER_ASSERTION_SECRET", shared)
    assert get_settings().user_assertion_secret is None

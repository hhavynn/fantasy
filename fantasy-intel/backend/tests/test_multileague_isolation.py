"""Adversarial, cross-cutting isolation tests for the multi-league platform.

Authorization moved to the Supabase control plane in Phase 1 of the platform plan (docs/multileague/ADR-0002): its
matrix lives in tests/test_v2_authorization_matrix.py, RLS in tests/test_control_plane_rls.py, claims isolation in
tests/test_control_plane_pg.py. This file keeps the provider- and identity-level guarantees that do not depend on
where authorization happens:

   1  league ids: external ids are digit strings; internal ids that are not ours are the same 404
   2  the same external id on different providers or seasons is a different league
   3  the same team id in different leagues is a different team
   4  players with the same name are never matched by name
   5  league-specific scoring is preserved (points per reception is None when a league does not say)
   6  superflex and FLEX stay distinct, by eligibility not label
   7  missing capabilities are explicit, never empty stand-ins
  10  with every new flag off, an ESPN-only deployment is unchanged and /v2 is a plain 404 that opens no Postgres

(11, credentials, lives in tests/test_no_credential_leaks.py; the global-selector ratchet in
tests/test_league_scope_audit.py.)
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.control_plane.authz import LeagueNotFound, check_league_id
from app.leagues.capabilities import Capability, CapabilityUnavailable, Support
from app.leagues.context import AccessMethod, LeagueKey, Provider, check_external_league_id
from app.leagues.identity import (
    Confidence, IdentityLink, IdentityMethod, ProviderPlayerId, link_from_espn_map_row, same_athlete,
)
from app.leagues.models import LineupRules, LineupSlot, ScoringRules, SlotKind, Team
from app.leagues.providers import registry as real_registry
from app.leagues.providers.base import LeagueAccess
from app.leagues.providers.fixtures import FIXTURE_DIR, FixtureTransport, load_fixture_id_map
from app.leagues.providers.sleeper import SleeperProvider

L1, L2 = "1180000000000000001", "1180000000000000002"     # Sleeper fixture leagues (superflex dynasty / FLEX-only)
SEASON = 2026
BACKEND = Path(__file__).resolve().parent.parent
NEW_ENV = ("MULTILEAGUE_ENABLED", "MULTILEAGUE_UI_ENABLED", "SUPABASE_URL", "SUPABASE_DB_URL", "CREDENTIALS_PUBKEY_V1",
           "CREDENTIALS_WRAP_KEY_VERSION", "SLEEPER_SOURCE", "ESPN_PUBLIC_IMPORT_ENABLED", "YAHOO_CLIENT_ID")
# fi.* tables (fantasy-intel/supabase/migrations). `leagues` is left out: the analytics schema has its own, unrelated
# `leagues` table, and the two never share a database.
CONTROL_PLANE_TABLES = {"app_users", "league_memberships", "league_pins", "team_selections", "league_invites",
                        "league_credentials", "sync_jobs", "provider_slots", "league_snapshots", "league_snapshot_history",
                        "audit_log"}


@pytest.fixture
def env(monkeypatch):
    """Multi-league on, no control plane configured, every provider flag and credential cleared."""
    for name in (*NEW_ENV, "ESPN_S2", "SWID", "FANTASY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    real_registry.reset_providers()
    yield monkeypatch
    real_registry.reset_providers()


# =================================================================================================
# 1. League ids
# =================================================================================================

@pytest.mark.parametrize("bad", ["12 34", "12\n34", "１２３４", "١٢٣٤", "1" * 65, "../x", "12;34", "", " "])
def test_external_ids_are_ascii_digit_strings_for_digit_platforms(bad):
    for provider in (Provider.ESPN, Provider.SLEEPER):
        with pytest.raises(ValueError):
            check_external_league_id(provider, bad)


@pytest.mark.parametrize("bad", ["lg_short", "lg_" + "a" * 65, "LG_" + "a" * 24, "lg_" + "a" * 23 + "/", "lg_" + "\u00e9" * 24,
                                 "lg_" + "a" * 23 + "\n", "../" + "a" * 24, 12345, None])
def test_internal_ids_that_are_not_ours_are_the_same_denial(bad):
    with pytest.raises(LeagueNotFound):
        check_league_id(bad)


# =================================================================================================
# 2-3. Same external id on different providers / seasons; same team id in different leagues
# =================================================================================================

def test_cross_provider_and_cross_season_keys_are_never_equal_and_ids_stay_strings():
    assert LeagueKey(Provider.ESPN, "12345", 2026) != LeagueKey(Provider.SLEEPER, "12345", 2026)
    assert LeagueKey(Provider.ESPN, "12345", 2026) != LeagueKey(Provider.ESPN, "12345", 2025)
    assert LeagueKey(Provider.ESPN, "12345", 2026).namespaced == "espn:12345:2026"
    with pytest.raises(TypeError):
        LeagueKey(Provider.SLEEPER, 1180000000000000001, 2026)        # an int has already lost precision in JS
    big = LeagueKey(Provider.SLEEPER, L1, 2026)
    assert big.external_league_id == L1 and big.namespaced == f"sleeper:{L1}:2026"


def test_team_scoped_ids_differ_across_leagues_providers_and_seasons():
    teams = [Team(LeagueKey(Provider.SLEEPER, "100", 2026), "1", "a", ()),
             Team(LeagueKey(Provider.SLEEPER, "101", 2026), "1", "b", ()),
             Team(LeagueKey(Provider.ESPN, "100", 2026), "1", "c", ()),
             Team(LeagueKey(Provider.SLEEPER, "100", 2025), "1", "d", ()),
             Team(LeagueKey(Provider.YAHOO, "449.l.100", 2026), "449.l.100.t.1", "e", ())]
    assert len({t.scoped_id for t in teams}) == len(teams)
    assert all(t.external_team_id in t.scoped_id for t in teams)


# =================================================================================================
# 4. Same player name never auto-matches
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

def test_points_per_reception_is_none_when_the_league_does_not_say(env, tmp_path):
    """A league whose scoring_settings has no `rec` key reports None, never 0, 0.5 or 1."""
    from app.sleeper.client import SleeperClient

    fx = tmp_path / "fx"
    shutil.copytree(FIXTURE_DIR, fx)
    league_file = fx / "leagues" / L2 / "league.json"
    doc = json.loads(league_file.read_text())
    del doc["scoring_settings"]["rec"]
    league_file.write_text(json.dumps(doc))
    provider = SleeperProvider(client=SleeperClient(transport=FixtureTransport(fx), cache_dir=tmp_path / "cache"),
                               idmap=load_fixture_id_map(), source="fixture")
    scoring = provider.fetch_settings(LeagueKey(Provider.SLEEPER, L2, SEASON), LeagueAccess(AccessMethod.PUBLIC)).value.scoring
    assert scoring.points_per_reception is None
    assert "rec" not in scoring.provider_rules and "rec" not in scoring.canonical
    assert ScoringRules({}, {}, ()).points_per_reception is None


# =================================================================================================
# 6. Superflex and FLEX stay distinct
# =================================================================================================

def lineup_rules_for_sleeper(positions):
    from app.leagues.providers.sleeper import lineup_rules
    return lineup_rules(SimpleNamespace(roster_positions=positions, settings={}))


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


# =================================================================================================
# 10. An ESPN-only deployment is unchanged with every new flag off
# =================================================================================================

def test_settings_defaults_with_all_new_environment_unset_are_off(monkeypatch):
    for name in NEW_ENV:
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert s.multileague_enabled is False
    assert s.supabase_url is None and s.supabase_db_url is None and s.supabase_issuer is None
    assert s.credential_public_keys == {} and s.credential_wrap_key_version is None
    assert s.sleeper_source == "disabled" and s.espn_public_import_enabled is False and s.yahoo_client_id is None


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
V2_ROUTES = [("GET", "/v2/providers"), ("GET", "/v2/me"), ("GET", "/v2/leagues"), ("GET", "/v2/action-center"),
             ("GET", "/v2/portfolio"),
             ("GET", "/v2/leagues/{id}"),
             ("PUT", "/v2/leagues/{id}/pin"), ("DELETE", "/v2/leagues/{id}/pin"),
             ("PUT", "/v2/leagues/{id}/perspective"), ("DELETE", "/v2/leagues/{id}/perspective")]


def test_espn_only_deployment_serves_existing_routes_and_404s_v2(espn_only_db, monkeypatch):
    from app.control_plane import pg as cp_pg
    from app.main import app
    app.dependency_overrides.clear()
    monkeypatch.setattr(cp_pg, "_PLANES", {})
    # even pointing at a database, the flag decides: nothing may connect
    monkeypatch.setenv("SUPABASE_DB_URL", "host=/nonexistent user=fi_api dbname=nope")
    client = TestClient(app)
    legacy_before = {p: client.get(p) for p in LEGACY_GETS}
    assert all(r.status_code == 200 for r in legacy_before.values()), {p: r.status_code for p, r in legacy_before.items()}
    assert len(legacy_before["/api/overview"].json()["roster"]) == 12

    for method, path in V2_ROUTES:
        for headers in ({}, {"Authorization": "Bearer " + "x" * 40 + "." + "y" * 40 + "." + "z" * 40}):
            r = client.request(method, path.format(id="lg_" + "q" * 24), headers=headers,
                               json={"team_external_id": "1"} if method == "PUT" else None)
            assert r.status_code == 404, (method, path)
    assert cp_pg._PLANES == {}, "a flag-off /v2 request must not even create a control-plane pool"

    # turning the feature on adds /v2 and changes nothing about the legacy answers
    monkeypatch.setenv("MULTILEAGUE_ENABLED", "1")
    assert client.get("/v2/providers").status_code == 200
    for path in LEGACY_GETS:
        assert client.get(path).json() == legacy_before[path].json(), path


def test_publishers_never_reference_the_control_plane():
    """The R2 snapshot (and the legacy MotherDuck copy) carry only the analytics file: nothing that publishes may
    import the Postgres driver or the control plane."""
    for rel in ("app/db/publish.py", "app/db/snapshot_publish.py", "app/db/snapshot.py", "../scripts/publish_snapshot.py",
                "../scripts/publish_motherduck.py", "../scripts/refresh_all.py"):
        path = BACKEND / rel
        if path.exists():
            text = path.read_text()
            assert "psycopg" not in text and "control_plane" not in text and "SUPABASE" not in text, rel


def test_schema_sql_has_no_control_plane_tables_and_the_schema_version_is_unchanged():
    from app.db import connection
    schema = (BACKEND / "app" / "db" / "schema.sql").read_text()
    created = set(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", schema, re.IGNORECASE))
    assert created, "expected to parse CREATE TABLE statements"
    assert not created & CONTROL_PLANE_TABLES, created & CONTROL_PLANE_TABLES
    assert not re.search(r"tenanc|credential|user_assertion|\bfi\.", schema, re.IGNORECASE)
    assert connection.SCHEMA_VERSION == 8 and connection.MIN_READ_SCHEMA_VERSION == 7


@pytest.mark.parametrize("method,path", [("POST", "/v2/providers"), ("DELETE", "/v2/providers"), ("PATCH", "/v2/leagues"),
                                         ("GET", "/v2/leagues/"), ("OPTIONS", "/v2/leagues"),
                                         ("GET", "/v2/leagues/lg_aaaaaaaaaaaaaaaaaaaaaaaa/perspective")])
def test_sec3_flag_off_is_404_for_every_method_and_path_shape(env, method, path):
    from app.main import app
    env.setenv("MULTILEAGUE_ENABLED", "0")
    r = TestClient(app, follow_redirects=False).request(method, path)
    assert r.status_code == 404, (method, path, r.status_code)

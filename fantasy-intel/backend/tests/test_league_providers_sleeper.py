"""SleeperProvider: the recorded Sleeper fixtures translated into the provider-neutral contract."""

import dataclasses
from datetime import datetime

import pytest

from app.leagues.capabilities import Capability
from app.leagues.context import Provider
from app.leagues.identity import Confidence, IdentityMethod, ProviderPlayerId, same_athlete
from app.leagues.models import FLEX_POSITIONS, SUPERFLEX_POSITIONS, SlotKind
from app.leagues.providers.base import LeagueNotFound, ProviderUnavailable
from app.leagues.providers.fixtures import FixtureTransport
from app.leagues.providers.sleeper import (SleeperProvider, canonical_position, lineup_rules, scoring_rules,
                                           settings_from)
from app.sleeper.client import SleeperClient
from app.sleeper.models import parse_league
from sleeper_fixtures import FakeTransport, league_routes, ok
from test_league_providers_support import PUBLIC, SLEEPER_L1, SLEEPER_L2, SEASON, clean_env, no_network, skey

pytestmark = pytest.mark.usefixtures("no_network", "clean_env")


@pytest.fixture
def prov():
    return SleeperProvider(source="fixture")


def by_code(rules):
    return {s.code: s for s in rules.slots}


# ----------------------------------------------------------------------------- offline proof --

def test_fixture_mode_runs_through_the_real_client_on_the_fixture_transport(prov):
    assert prov.source == "fixture" and prov.data_origin == "fixture"
    assert isinstance(prov.client, SleeperClient)
    assert isinstance(prov.client.transport, FixtureTransport) and prov.client.transport.is_fixture
    prov.fetch_settings(skey(), PUBLIC)
    prov.fetch_rosters(skey(), PUBLIC)
    assert prov.client.transport.calls == [f"/league/{SLEEPER_L1}", f"/league/{SLEEPER_L1}/rosters", "/players/nfl"]
    assert "sleeper-fixture-" in prov.client.cache_dir.name, "fixture mode never touches the live cache dir"


def test_fixture_provenance_is_prefixed_and_live_provenance_is_not(prov, tmp_path):
    s = prov.fetch_settings(skey(), PUBLIC)
    assert s.provenance.provider is Provider.SLEEPER
    assert s.provenance.source == f"fixture:sleeper:/league/{SLEEPER_L1}"
    assert isinstance(s.provenance.fetched_at, datetime) and s.provenance.fetched_at.tzinfo is None
    transport = FakeTransport(league_routes())
    live = SleeperProvider(client=SleeperClient(cache_dir=tmp_path, transport=transport), source="live")
    assert live.data_origin == "live"
    assert live.fetch_settings(skey(), PUBLIC).provenance.source == f"sleeper:/league/{SLEEPER_L1}"
    assert transport.calls == [f"/league/{SLEEPER_L1}"], "live mode goes through the injected transport only"


def test_live_mode_builds_a_real_client_but_makes_no_request_until_asked(no_network, monkeypatch):
    import socket
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[1:2] or a[:1])
        raise OSError("network access attempted in an offline provider test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    p = SleeperProvider(source="live")
    assert p.capabilities().enabled and p.data_origin == "live"
    assert attempts == []                      # building the provider and reading capabilities is offline
    with pytest.raises(ProviderUnavailable) as exc:
        p.fetch_settings(skey(), PUBLIC)       # the blocked socket proves the live path is the real client
    assert attempts and exc.value.__cause__ is None and exc.value.__context__ is None


# -------------------------------------------------------------------------------- settings --

def test_flex_and_superflex_are_distinct_slots_with_explicit_eligibility(prov):
    lineup = prov.fetch_settings(skey(SLEEPER_L1), PUBLIC).value.lineup
    slots = by_code(lineup)
    assert slots["FLEX"].eligible == FLEX_POSITIONS == frozenset({"RB", "WR", "TE"})
    assert slots["SUPERFLEX"].eligible == SUPERFLEX_POSITIONS == frozenset({"QB", "RB", "WR", "TE"})
    assert slots["FLEX"].eligible != slots["SUPERFLEX"].eligible
    assert (slots["FLEX"].provider_code, slots["SUPERFLEX"].provider_code) == ("FLEX", "SUPER_FLEX")
    assert lineup.flex_slots == 1 and lineup.superflex_slots == 1 and lineup.is_superflex
    assert slots["DST"].provider_code == "DEF" and slots["DST"].eligible == frozenset({"DST"})
    assert slots["BN"].kind is SlotKind.BENCH and slots["BN"].count == 3 and not slots["BN"].eligible
    assert slots["IR"].kind is SlotKind.INJURED_RESERVE and slots["IR"].provider_code == "reserve"
    assert slots["TAXI"].kind is SlotKind.TAXI and slots["TAXI"].count == 1
    assert [s.code for s in lineup.starters()] == ["QB", "RB", "WR", "TE", "FLEX", "SUPERFLEX", "K", "DST"]


def test_a_flex_only_league_is_not_superflex(prov):
    lineup = prov.fetch_settings(skey(SLEEPER_L2), PUBLIC).value.lineup
    assert "SUPERFLEX" not in by_code(lineup)
    assert lineup.flex_slots == 1 and lineup.superflex_slots == 0 and not lineup.is_superflex


def test_other_flex_shapes_and_idp_slots_keep_explicit_eligibility():
    raw = {"league_id": "5", "roster_positions": ["QB", "WRRB_FLEX", "REC_FLEX", "REC_FLEX", "SUPER_FLEX", "DL", "LB",
                                                  "DB", "IDP_FLEX", "K", "DEF", "WEIRD_NEW_SLOT", "BN"],
           "settings": {"reserve_slots": 2, "taxi_slots": 0}}
    slots = by_code(lineup_rules(parse_league(raw)))
    assert slots["WRRB_FLEX"].eligible == {"RB", "WR"} and slots["REC_FLEX"].eligible == {"WR", "TE"}
    assert slots["REC_FLEX"].count == 2
    assert slots["IDP_FLEX"].eligible == {"DL", "LB", "DB"} and slots["DL"].eligible == {"DL"}
    assert slots["WEIRD_NEW_SLOT"].eligible == {"WEIRD_NEW_SLOT"}, "unknown slots stay visible under their own label"
    assert slots["IR"].count == 2 and "TAXI" not in slots
    rules = lineup_rules(parse_league(raw))
    assert rules.superflex_slots == 1 and rules.flex_slots == 3      # WRRB 1 + REC_FLEX 2; SUPER_FLEX is counted apart


def test_scoring_is_preserved_verbatim_and_never_defaults_to_ppr(prov):
    sc = prov.fetch_scoring(skey(SLEEPER_L2), PUBLIC).value
    assert sc.points_per_reception == 0.5, "a half-PPR league stays 0.5"
    assert sc.provider_rules["rec"] == 0.5 and sc.provider_rules["bonus_rec_te"] == 0.5
    assert sc.canonical["rec_te_bonus"] == 0.5 and sc.canonical["rec"] == 0.5
    assert sc.provider_rules["pass_yd"] == 0.04 and sc.canonical["pass_yd"] == 0.04
    assert sc.canonical["fg_made"] == 3.0 and sc.canonical["xp_made"] == 1.0 and sc.canonical["fum_lost"] == -2.0
    assert set(sc.unmapped) == {"bonus_pass_yd_300", "def_sack"}, "unmapped rules are reported, not dropped"
    assert sc.provider_rules["bonus_pass_yd_300"] == 1.0 and sc.provider_rules["def_sack"] == 1.0
    # a league whose rules say nothing about receptions has no PPR value - not 0, 0.5 or 1
    bare = scoring_rules(parse_league({"league_id": "6", "scoring_settings": {"pass_td": 4}}))
    assert bare.points_per_reception is None and "rec" not in bare.provider_rules
    assert scoring_rules(parse_league({"league_id": "6"})).provider_rules == {}


def test_scoring_ignores_non_numeric_values_without_inventing_numbers():
    sc = scoring_rules(parse_league({"league_id": "7", "scoring_settings": {"rec": "1", "pass_td": True, "x": 2}}))
    assert sc.provider_rules == {"x": 2.0} and set(sc.unmapped) == {"rec", "pass_td", "x"} and not sc.canonical


def test_waiver_trade_playoff_and_league_type(prov):
    s1 = prov.fetch_settings(skey(SLEEPER_L1), PUBLIC).value
    assert (s1.waivers.kind, s1.waivers.faab_budget) == ("faab", 100)
    assert s1.trades.deadline_week == 11 and s1.trades.deadline_at is None
    assert (s1.playoffs.teams, s1.playoffs.start_week, s1.playoffs.last_regular_season_week) == (4, 15, 14)
    assert s1.playoffs.week_per_round == 1
    assert s1.league_type == "dynasty" and s1.team_count == 4 and s1.name == "Fixture Dynasty League"
    assert s1.is_public is True
    s2 = prov.fetch_settings(skey(SLEEPER_L2), PUBLIC).value
    assert (s2.waivers.kind, s2.waivers.faab_budget, s2.waivers.clear_days) == ("rolling", None, 2)
    assert s2.league_type == "redraft" and s2.trades.deadline_week == 10
    unknown = settings_from(skey(), parse_league({"league_id": "8"}))
    assert unknown.waivers.kind is None and unknown.league_type is None and unknown.playoffs.teams is None
    assert unknown.trades.deadline_week is None


def test_scoped_to_the_requested_league_key(prov):
    assert prov.fetch_settings(skey(SLEEPER_L2), PUBLIC).value.key == skey(SLEEPER_L2)
    assert prov.fetch_settings(skey(SLEEPER_L2), PUBLIC).value.name == "Fixture Redraft Half-PPR TE Premium"


# ---------------------------------------------------------------------------------- teams --

def test_teams_ids_are_strings_scoped_and_managers_namespaced(prov):
    t1 = {t.external_team_id: t for t in prov.fetch_teams(skey(SLEEPER_L1), PUBLIC).value}
    t2 = {t.external_team_id: t for t in prov.fetch_teams(skey(SLEEPER_L2), PUBLIC).value}
    assert set(t1) == {"1", "2", "3", "4"} and set(t2) == {"1", "2"}
    assert all(isinstance(k, str) for k in t1)
    assert t1["1"].scoped_id == f"sleeper:{SLEEPER_L1}:2026:team:1"
    assert t1["1"].scoped_id != t2["1"].scoped_id, "roster 1 of two leagues never collides"
    assert t1["1"].name == "Alpha Squad" and t2["1"].name == "Alpha Redraft"
    assert t1["1"].manager_ids == ("sleeper:700000000000000101",)
    assert t1["2"].manager_ids == ("sleeper:700000000000000102", "sleeper:700000000000000103"), "co-owners included"
    assert t1["2"].name == "bravo_mgr" and t1["2"].manager_display_names == ("bravo_mgr", "charlie_co")
    assert t1["3"].manager_ids == () and t1["3"].name == "Team 3", "an orphaned roster has no manager"
    assert t1["4"].manager_ids == ("sleeper:700000000000009999",) and t1["4"].manager_display_names == ()
    assert (t1["1"].wins, t1["1"].losses, t1["1"].points_for) == (4, 1, 725.64)
    assert t1["4"].points_for is None, "missing stays None, never 0"


# -------------------------------------------------------------------------------- rosters --

def entries(prov, league, team):
    rosters = {r.external_team_id: r for r in prov.fetch_rosters(skey(league), PUBLIC).value}
    return {e.player.namespaced: e for e in rosters[team].entries}


def test_roster_entries_use_namespaced_ids_slots_and_canonical_positions(prov):
    e = entries(prov, SLEEPER_L1, "1")
    assert all(isinstance(x.player, ProviderPlayerId) and x.player.provider is Provider.SLEEPER for x in e.values())
    assert e["sleeper:4046"].slot_kind is SlotKind.STARTER and e["sleeper:4046"].slot_code == "QB"
    assert e["sleeper:DET"].slot_code == "DST" and e["sleeper:DET"].position == "DST"
    assert e["sleeper:5850"].slot_kind is SlotKind.INJURED_RESERVE
    assert e["sleeper:12500"].slot_kind is SlotKind.TAXI
    assert e["sleeper:4046"].name == "Quinn Arbor" and e["sleeper:4046"].nfl_team == "KC"
    assert e["sleeper:99999"].name is None and e["sleeper:99999"].position is None, "unknown player kept, not dropped"
    assert len(e) == 9 and sum(1 for x in e.values() if x.slot_kind is SlotKind.STARTER) == 7
    e2 = entries(prov, SLEEPER_L1, "2")
    assert e2["sleeper:6801"].nfl_team == "LV", "OAK normalised to nflverse LV"
    assert e2["sleeper:6801"].slot_code == "RB" and e2["sleeper:9509"].slot_code == "WR"
    assert canonical_position("DEF") == "DST" and canonical_position(None) is None


def test_identity_links_follow_the_id_map_evidence(prov):
    e1, e2 = entries(prov, SLEEPER_L1, "1"), entries(prov, SLEEPER_L1, "2")
    hi = e1["sleeper:4046"].identity
    assert (hi.method, hi.confidence, hi.gsis_id) == (IdentityMethod.ID_MAP, Confidence.HIGH, "00-0033873")
    assert hi.player == e1["sleeper:4046"].player
    # the ID map's value beats Sleeper's own conflicting gsis_id, and says so
    d = e1["sleeper:8130"].identity
    assert d.gsis_id == "00-0036100" and d.method is IdentityMethod.ID_MAP and "differs" in d.reason
    med = e2["sleeper:9509"].identity
    assert (med.method, med.confidence, med.gsis_id) == (IdentityMethod.PROVIDER_GSIS, Confidence.MEDIUM, "00-0038000")
    team = e1["sleeper:DET"].identity
    assert (team.method, team.nfl_team, team.gsis_id) == (IdentityMethod.TEAM_DEFENSE, "DET", None)
    assert e2["sleeper:LAR"].identity.nfl_team == "LA", "team defence mapped to the nflverse abbreviation"


def test_unresolved_identities_are_kept_with_reasons(prov):
    e1, e2 = entries(prov, SLEEPER_L1, "1"), entries(prov, SLEEPER_L1, "2")
    conflict = e2["sleeper:2133"].identity
    assert conflict.method is IdentityMethod.CONFLICT and conflict.gsis_id is None and not conflict.resolved
    assert "00-0031000" in conflict.reason and "00-0031001" in conflict.reason
    unknown = e1["sleeper:99999"].identity
    assert unknown.method is IdentityMethod.UNMATCHED and "not in sleeper's /players/nfl" in unknown.reason.lower()
    unmatched = e2["sleeper:11632"].identity
    assert unmatched.method is IdentityMethod.UNMATCHED and unmatched.confidence is Confidence.NONE
    assert "ID map" in unmatched.reason
    no_gsis = e2["sleeper:10229"].identity
    assert no_gsis.method is IdentityMethod.UNMATCHED and "without a gsis_id" in no_gsis.reason
    for team in ("1", "2"):
        for x in entries(prov, SLEEPER_L1, team).values():
            assert x.identity is not None and x.identity.player == x.player, "every entry carries a link"


def test_same_name_different_sleeper_ids_are_never_linked(prov):
    e = {**entries(prov, SLEEPER_L2, "1"), **entries(prov, SLEEPER_L2, "2")}
    a, b, c = e["sleeper:7001"], e["sleeper:7002"], e["sleeper:7003"]
    assert a.name == b.name == c.name == "Mike Williams"
    assert a.identity.gsis_id == "00-0030001" and b.identity.gsis_id == "00-0030002"
    assert not same_athlete(a.identity, b.identity)
    assert c.identity.method is IdentityMethod.UNMATCHED and c.identity.gsis_id is None
    assert not same_athlete(a.identity, c.identity) and not same_athlete(c.identity, b.identity)
    assert c.identity.player != a.identity.player
    assert same_athlete(a.identity, a.identity)


def test_a_malformed_gsis_never_becomes_a_link():
    from app.leagues.providers.sleeper import identity_link
    from app.sleeper.id_map import PlayerMapping

    link = identity_link(ProviderPlayerId(Provider.SLEEPER, "1"),
                         PlayerMapping("1", "X", "WR", "KC", "not-a-gsis", None, "ff_playerids", "high"))
    assert link.method is IdentityMethod.UNMATCHED and link.gsis_id is None and "malformed" in link.reason


def test_starters_beyond_the_slot_list_and_empty_slots_are_handled(prov):
    e = entries(prov, SLEEPER_L2, "2")
    assert set(e) == {"sleeper:7002", "sleeper:7003", "sleeper:7564"}, "empty ('0') slots create no entries"
    assert e["sleeper:7002"].slot_code == "WR" and e["sleeper:7564"].slot_code == "WR"
    assert e["sleeper:7003"].slot_kind is SlotKind.BENCH and e["sleeper:7003"].slot_code is None


# ------------------------------------------------------------- matchups, standings, the rest --

def test_matchups_pair_teams_and_handle_byes(prov):
    m = prov.fetch_matchups(skey(SLEEPER_L1), PUBLIC, week=6).value
    assert [(x.external_matchup_id, x.team_ids, x.points) for x in m] == [
        ("1", ("1", "2"), (101.5, 98.25)), ("2", ("3", "4"), (80.0, 75.5))]
    bye = prov.fetch_matchups(skey(SLEEPER_L1), PUBLIC, week=7).value
    assert {x.external_matchup_id: x.team_ids for x in bye} == {"1": ("1", "2"), "unpaired-3": ("3",), "2": ("4",)}
    assert prov.fetch_matchups(skey(SLEEPER_L1), PUBLIC, week=12).value == [], "no data that week is an empty list"
    assert all(isinstance(t, str) for x in m for t in x.team_ids)


def test_standings_are_derived_from_rosters_in_a_stated_order(prov):
    st = prov.fetch_standings(skey(SLEEPER_L1), PUBLIC).value
    assert [s.external_team_id for s in st] == ["1", "4", "2", "3"]
    assert [s.rank for s in st] == [1, 2, 3, 4]
    assert prov.capabilities().states[Capability.STANDINGS].note.startswith("Sleeper has no standings endpoint")


def test_transactions_are_completed_only_and_namespaced(prov):
    tx = prov.fetch_transactions(skey(SLEEPER_L1), PUBLIC, week=6).value
    assert [t.kind for t in tx] == ["free_agent", "waiver", "trade"], "the failed waiver claim is not an add"
    assert tx[0].adds == {"sleeper:4984": "1"} and tx[0].drops == {"sleeper:12500": "1"}
    assert tx[1].drops == {} and tx[1].team_ids == ("2",)
    assert tx[2].team_ids == ("1", "2") and tx[2].adds["sleeper:9509"] == "1" and tx[2].drops["sleeper:9509"] == "2"
    assert all(t.week == 6 and t.external_id.isdigit() for t in tx)
    assert prov.fetch_transactions(skey(SLEEPER_L1), PUBLIC, week=3).value == []


def test_free_agents_are_derived_excluding_every_rostered_player(prov):
    ids = {f.player for f in prov.fetch_free_agents(skey(SLEEPER_L1), PUBLIC).value}
    fa = prov.fetch_free_agents(skey(SLEEPER_L1), PUBLIC).value
    assert "sleeper:1111" in ids, "an active, unrostered QB is available"
    rostered = {"sleeper:4046", "sleeper:6794", "sleeper:99999", "sleeper:7564", "sleeper:4984", "sleeper:8130",
                "sleeper:DET", "sleeper:LAR", "sleeper:5850", "sleeper:12500", "sleeper:9509", "sleeper:2133"}
    assert not ids & rostered, "every roster of the league is subtracted (starters, IR, taxi, bench)"
    assert "sleeper:10229" not in ids and "sleeper:11632" not in ids, "inactive / team-less players are not offered"
    assert [f.player for f in fa] == sorted(ids) and all(f.position for f in fa)
    # availability is derived per league: 7001 is free in league 1 and rostered in league 2
    ids2 = {f.player for f in prov.fetch_free_agents(skey(SLEEPER_L2), PUBLIC).value}
    assert "sleeper:7001" in ids and "sleeper:7001" not in ids2 and "sleeper:9509" in ids2


def test_discover_leagues_lists_the_users_leagues_for_the_season(prov):
    res = prov.discover_leagues(PUBLIC, user_handle="alpha_mgr", season=SEASON)
    assert [s.key.external_league_id for s in res.value] == [SLEEPER_L1, SLEEPER_L2]
    assert all(s.key.provider is Provider.SLEEPER and s.key.season == SEASON and s.is_public for s in res.value)
    assert [s.team_count for s in res.value] == [4, 2]
    assert res.provenance.source == "fixture:sleeper:/user/700000000000000101/leagues/nfl/2026"
    assert prov.client.transport.calls == ["/user/alpha_mgr", "/user/700000000000000101/leagues/nfl/2026"]
    assert prov.discover_leagues(PUBLIC, user_handle="700000000000000101", season=SEASON).value
    assert prov.discover_leagues(PUBLIC, user_handle="alpha_mgr", season=2020).value == []


def test_results_are_immutable_value_objects(prov):
    s = prov.fetch_settings(skey(), PUBLIC)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.value.name = "x"       # type: ignore[misc]


def test_credentials_are_ignored_not_stored(prov):
    from types import SimpleNamespace

    from app.leagues.context import AccessMethod
    from app.leagues.providers.base import LeagueAccess

    a = LeagueAccess(AccessMethod.OAUTH, SimpleNamespace(token="tok-123"))
    prov.fetch_settings(skey(), a)
    assert "tok-123" not in repr(vars(prov)) + repr(vars(prov.client))


def test_unknown_league_in_fixture_mode_is_not_found(prov):
    with pytest.raises(LeagueNotFound):
        prov.fetch_rosters(skey("1180000000000000077"), PUBLIC)

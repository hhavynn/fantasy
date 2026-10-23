"""EspnProvider: payloads from tests/espn_fixture.py (never the network) translated into the contract models."""

import copy
import re

import pytest

from app.leagues.capabilities import Capability, CapabilityUnavailable
from app.leagues.context import AccessMethod, LeagueKey, Provider
from app.leagues.identity import ProviderPlayerId
from app.leagues.models import FLEX_POSITIONS, SUPERFLEX_POSITIONS, SlotKind
from app.leagues.providers.base import LeagueAccess, LeagueNotFound
from app.leagues.providers.espn import EspnProvider, lineup_rules, scoring_rules
from espn_fixture import LEAGUE_ID, MY_SWID, SEASON, UNIVERSE_SIZE, FakeEspnClient, league_payload
from test_league_providers_support import (PUBLIC, S2_SECRET, SWID_SECRET, clean_env, no_network, owner_access)

pytestmark = pytest.mark.usefixtures("no_network", "clean_env")

KEY = LeagueKey(Provider.ESPN, str(LEAGUE_ID), SEASON)
OTHER_KEY = LeagueKey(Provider.ESPN, "1234", SEASON)


class Recorder(FakeEspnClient):
    """The fixture client plus a record of how it was built and a league payload override."""

    def __init__(self, league=None, **kw):
        super().__init__(**kw)
        self._league = league
        self.calls = []

    def league(self):
        self.calls.append("league")
        return copy.deepcopy(self._league) if self._league is not None else super().league()


def make(league=None, *, public=True, owner=False, **kw):
    built = []

    def factory(key, access):
        client = Recorder(league, **kw)
        built.append((key, access, client))
        return client

    return EspnProvider(factory, public_import_enabled=public, private_access_enabled=owner), built


def with_op_slot(count=1):
    p = league_payload()
    p["settings"]["rosterSettings"]["lineupSlotCounts"]["7"] = count
    return p


# ---------------------------------------------------------------------------- capabilities --

def test_enabled_only_for_the_encrypted_credential_path_or_the_public_flag():
    assert not EspnProvider(public_import_enabled=False, private_access_enabled=False).capabilities().enabled
    reason = EspnProvider(public_import_enabled=False, private_access_enabled=False).capabilities().reason
    assert "no documented public fantasy API" in reason and "set up on this deployment yet" in reason
    # Visitors read this on the /leagues host card: plain words, never the settings, keys or versions behind it.
    assert not re.search(r"CREDENTIALS_|PUBKEY|ESPN_PUBLIC_IMPORT|_ENABLED|V<n>|[A-Z]{2,}_[A-Z]{2,}", reason)
    assert EspnProvider(public_import_enabled=True, private_access_enabled=False).capabilities().enabled
    assert EspnProvider(public_import_enabled=False, private_access_enabled=True).capabilities().enabled


def test_espn_is_not_enabled_by_environment_defaults(clean_env):
    assert not EspnProvider().capabilities().enabled


# ------------------------------------------------------------------------------- settings --

def test_op_slot_becomes_superflex_and_flex_stays_flex():
    p, _ = make(with_op_slot())
    lineup = p.fetch_settings(KEY, PUBLIC).value.lineup
    slots = {s.code: s for s in lineup.slots}
    assert slots["FLEX"].eligible == FLEX_POSITIONS and slots["FLEX"].provider_code == "FLEX"
    assert slots["SUPERFLEX"].eligible == SUPERFLEX_POSITIONS and slots["SUPERFLEX"].provider_code == "OP"
    assert slots["FLEX"].eligible != slots["SUPERFLEX"].eligible
    assert lineup.is_superflex and lineup.superflex_slots == 1 and lineup.flex_slots == 1
    assert {s.code for s in lineup.starters()} == {"QB", "RB", "WR", "TE", "FLEX", "SUPERFLEX", "K", "DST"}
    assert slots["BN"].kind is SlotKind.BENCH and slots["BN"].count == 3 and slots["BN"].provider_code == "BE"
    assert slots["IR"].kind is SlotKind.INJURED_RESERVE and slots["DST"].provider_code == "D/ST"


def test_the_fixture_league_without_an_op_slot_is_not_superflex():
    p, _ = make()
    lineup = p.fetch_settings(KEY, PUBLIC).value.lineup
    assert "SUPERFLEX" not in {s.code for s in lineup.slots} and not lineup.is_superflex
    assert lineup.flex_slots == 1


def test_other_espn_slot_ids():
    rules = lineup_rules({"3": 1, "5": 2, "8": 1, "13": 1, "15": 1, "18": 1, "22": 1, "99": 1, "0": 0})
    slots = {s.code: s for s in rules.slots}
    assert slots["WRRB_FLEX"].eligible == {"RB", "WR"} and slots["REC_FLEX"].count == 2
    assert slots["DT"].eligible == {"DL"} and slots["S"].eligible == {"DB"}
    assert slots["IDP_FLEX"].eligible == {"DL", "LB", "DB"}
    assert slots["P"].eligible == {"P"} and slots["slot99"].eligible == {"slot99"}
    assert slots["UNKNOWN_22"].kind is SlotKind.BENCH
    assert "QB" not in slots, "a zero count is not a slot"


def test_settings_scoring_waivers_trades_playoffs():
    p, _ = make()
    s = p.fetch_settings(KEY, PUBLIC)
    st = s.value
    assert s.provenance.source == "espn:mSettings" and s.provenance.provider is Provider.ESPN
    assert st.name == "Fixture League" and st.team_count == 4 and st.key == KEY
    assert st.scoring.points_per_reception == 1.0 and st.scoring.provider_rules == {"53": 1.0}
    assert (st.waivers.kind, st.waivers.faab_budget) == ("faab", 100)
    assert st.trades.deadline_at is not None and st.trades.deadline_week is None
    assert (st.playoffs.teams, st.playoffs.last_regular_season_week, st.playoffs.start_week) == (2, 14, 15)
    assert st.is_public is None and st.league_type is None, "what ESPN does not say stays None"


def test_scoring_keeps_rules_verbatim_by_stat_id_and_reports_unmapped():
    sc = scoring_rules([
        {"statId": 53, "points": 0.5, "pointsOverrides": {"6": 1.0}},      # PPR with a TE override
        {"statId": 4, "points": 4}, {"statId": 20, "points": -2}, {"statId": 24, "points": 0.1},
        {"statId": 74, "points": 5}, {"statId": 95, "points": 2},
        {"statId": 3, "points": None}, {"statId": "x", "points": 1},
    ])
    assert sc.points_per_reception == 0.5
    assert sc.canonical == {"rec": 0.5, "pass_td": 4.0, "pass_int": -2.0, "rush_yd": 0.1}
    assert sc.provider_rules["53@slot6"] == 1.0 and "53@slot6" in sc.unmapped, "overrides never silently dropped"
    assert {"74", "95"} <= set(sc.unmapped)
    assert scoring_rules([]).points_per_reception is None and scoring_rules(None).provider_rules == {}


def test_a_league_with_no_reception_rule_has_no_ppr_value():
    payload = league_payload()
    payload["settings"]["scoringSettings"]["scoringItems"] = [{"statId": 4, "points": 4}]
    p, _ = make(payload)
    assert p.fetch_scoring(KEY, PUBLIC).value.points_per_reception is None


# -------------------------------------------------------------------------- teams, rosters --

def test_teams_are_scoped_and_managers_are_hashed_never_swids():
    p, _ = make()
    teams = p.fetch_teams(KEY, PUBLIC).value
    assert [t.external_team_id for t in teams] == ["1", "2", "3", "4"]
    assert teams[0].scoped_id == f"espn:{LEAGUE_ID}:{SEASON}:team:1"
    other = LeagueKey(Provider.SLEEPER, str(LEAGUE_ID), SEASON)
    assert teams[0].scoped_id != type(teams[0])(other, "1", None, ()).scoped_id, "same id, other provider"
    mine = teams[1]
    assert mine.manager_display_names == ("me",) and len(mine.manager_ids) == 1
    assert mine.manager_ids[0].startswith("espn:sha256:") and len(mine.manager_ids[0]) == len("espn:sha256:") + 12
    blob = repr(teams)
    assert MY_SWID.lower() not in blob.lower() and "{OWNER-1}" not in blob and "abcd-1234" not in blob.lower()
    assert (teams[0].wins, teams[0].losses, teams[0].points_for) == (3, 0, 400.0)


def test_rosters_use_namespaced_ids_slot_kinds_and_leave_identity_to_the_caller():
    p, _ = make()
    rosters = {r.external_team_id: r for r in p.fetch_rosters(KEY, PUBLIC).value}
    assert set(rosters) == {"1", "2", "3", "4"}
    e = {x.player.namespaced: x for x in rosters["1"].entries}
    assert e["espn:101"].slot_kind is SlotKind.STARTER and e["espn:101"].slot_code == "QB"
    assert e["espn:203"].slot_code == "FLEX"
    assert e["espn:-16012"].slot_code == "DST" and e["espn:-16012"].position == "DST"
    assert e["espn:303"].slot_kind is SlotKind.BENCH and e["espn:303"].slot_code is None
    assert e["espn:101"].name == "Alpha QB" and e["espn:101"].nfl_team == "KC" and e["espn:101"].position == "QB"
    assert all(x.identity is None for r in rosters.values() for x in r.entries), "no analytics DB here"
    assert all(isinstance(x.player, ProviderPlayerId) and x.player.provider is Provider.ESPN
               for r in rosters.values() for x in r.entries)
    ir = {x.player.namespaced: x for x in rosters["2"].entries}
    assert ir["espn:207"].slot_kind is SlotKind.INJURED_RESERVE
    assert rosters["1"].player_ids()[0] == "espn:101"


def test_op_slot_players_get_the_superflex_slot_code():
    payload = with_op_slot()
    payload["teams"][0]["roster"]["entries"][0]["lineupSlotId"] = 7      # QB 101 into OP
    p, _ = make(payload)
    r = {x.player.namespaced: x for x in p.fetch_rosters(KEY, PUBLIC).value[0].entries}
    assert r["espn:101"].slot_code == "SUPERFLEX"


# ------------------------------------------------------------------- matchups, standings ... --

def test_matchups_standings_free_agents_transactions_projections():
    p, _ = make()
    m = p.fetch_matchups(KEY, PUBLIC, week=1).value
    assert [(x.external_matchup_id, x.team_ids, x.points) for x in m] == [("1", ("1", "2"), (110.5, 99.0))]
    assert p.fetch_matchups(KEY, PUBLIC, week=2).value == []

    st = p.fetch_standings(KEY, PUBLIC).value
    assert [(s.external_team_id, s.rank, s.wins) for s in st] == [("1", 1, 3), ("2", 2, 2), ("3", 3, 1), ("4", 4, 0)]

    fa = p.fetch_free_agents(KEY, PUBLIC).value
    assert {f.player for f in fa} == {"espn:209", "espn:405", "espn:105"}, "waiver-status players are not free agents"
    assert {f.player: f.position for f in fa}["espn:405"] == "TE"

    tx = p.fetch_transactions(KEY, PUBLIC, week=1).value
    assert [(t.external_id, t.kind) for t in tx] == [("tx-1", "waiver"), ("tx-2", "free_agent")]
    assert tx[0].adds == {"espn:203": "1"} and tx[0].drops == {"espn:209": "1"} and tx[0].team_ids == ("1",)
    wk2 = p.fetch_transactions(KEY, PUBLIC, week=2).value
    assert [t.external_id for t in wk2] == ["tx-3"], "failed claims and lineup (ROSTER) moves are not transactions"
    assert p.fetch_transactions(KEY, PUBLIC, week=3).value == [], "a trade with no items says nothing"

    proj = p.fetch_projections(KEY, PUBLIC, week=3)
    assert proj.value["espn:101"] == 5.0 + 101 % 7 + 0.3 and "espn:-16012" in proj.value
    assert len(proj.value) == UNIVERSE_SIZE


def test_espn_trade_items_become_adds_and_drops_for_both_sides():
    payload = {"transactions": [{
        "id": "tr-1", "type": "TRADE_UPHOLD", "status": "EXECUTED", "teamId": 1, "scoringPeriodId": 4,
        "items": [{"type": "TRADE", "playerId": 301, "fromTeamId": 1, "toTeamId": 2},
                  {"type": "TRADE", "playerId": 302, "fromTeamId": 2, "toTeamId": 1}]}]}
    p, _ = make(transactions_override={4: payload})
    t = p.fetch_transactions(KEY, PUBLIC, week=4).value[0]
    assert t.kind == "trade" and t.adds == {"espn:301": "2", "espn:302": "1"}
    assert t.drops == {"espn:301": "1", "espn:302": "2"} and t.team_ids == ("1", "2")


def test_provenance_and_the_fixture_client_is_the_only_source():
    p, built = make()
    r = p.fetch_rosters(KEY, PUBLIC)
    assert r.provenance.source == "espn:mRoster" and r.provenance.fetched_at.tzinfo is None
    assert built and all(isinstance(c, FakeEspnClient) for _, _, c in built)


# ----------------------------------------------------------------------------- access paths --

def test_cookie_credential_reaches_the_factory_and_nowhere_else():
    p, built = make(public=False, owner=True)
    access = owner_access()
    results = [p.fetch_settings(KEY, access), p.fetch_teams(KEY, access), p.fetch_rosters(KEY, access),
               p.fetch_standings(KEY, access), p.fetch_scoring(KEY, access)]
    assert all(b[1] is access for b in built)
    everything = repr(results) + repr(p) + repr(vars(p)) + repr(p.capabilities())
    assert S2_SECRET not in everything and SWID_SECRET not in everything
    assert S2_SECRET not in repr(access) and SWID_SECRET not in repr(access)


def test_public_access_needs_the_flag_and_cookie_access_needs_a_credential():
    from app.leagues.providers.base import ProviderUnavailable

    p, built = make(public=False, owner=True)
    with pytest.raises(ProviderUnavailable, match="public-league"):
        p.fetch_settings(KEY, PUBLIC)
    with pytest.raises(ProviderUnavailable, match="No ESPN cookie credential was supplied"):
        p.fetch_settings(KEY, LeagueAccess(AccessMethod.COOKIES, object()))
    assert built == []


def test_espn_never_builds_a_client_without_cookies_for_owner_access():
    """The default factory passes cookies only for COOKIES; PUBLIC leagues are fetched cookie-less."""
    from app.leagues.providers import espn as mod

    seen = {}

    class Spy:
        def __init__(self, league_id, season, s2, swid):
            seen.update(league_id=league_id, season=season, s2=s2, swid=swid)

    import app.espn.client as client_mod
    orig = client_mod.EspnClient
    client_mod.EspnClient = Spy
    try:
        mod._default_client(KEY, PUBLIC)
        assert seen == {"league_id": LEAGUE_ID, "season": SEASON, "s2": None, "swid": None}
        mod._default_client(KEY, owner_access())
        assert seen["s2"] == S2_SECRET and seen["swid"] == SWID_SECRET
        mod._default_client(KEY, LeagueAccess(AccessMethod.OAUTH, owner_access().credential))
        assert seen["s2"] is None, "cookies only travel with COOKIES"
    finally:
        client_mod.EspnClient = orig


def test_unsupported_and_disabled_paths():
    p, _ = make()
    with pytest.raises(CapabilityUnavailable):
        p.discover_leagues(PUBLIC, user_handle="x", season=SEASON)
    off = EspnProvider(public_import_enabled=False, private_access_enabled=False)
    with pytest.raises(CapabilityUnavailable):
        off.fetch_free_agents(KEY, PUBLIC)
    assert off.capabilities().state(Capability.PRIVATE_LEAGUES).note.startswith("ESPN has no documented")


def test_a_payload_missing_expected_keys_is_a_sanitized_provider_error():
    from app.leagues.providers.base import ProviderUnavailable

    bad = league_payload()
    del bad["seasonId"]  # parse needs it; the payload is not what ESPN documents-by-habit
    p, _ = make(bad)
    with pytest.raises(ProviderUnavailable, match="unexpected response"):
        p.fetch_settings(KEY, PUBLIC)

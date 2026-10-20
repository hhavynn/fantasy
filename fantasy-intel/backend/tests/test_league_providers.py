"""Cross-provider contract: every provider declares every capability, disabled providers fail closed, the
registry follows the flags, errors are sanitized, and nothing here touches the network."""

import inspect

from types import SimpleNamespace

import pytest
import requests

from app.leagues.capabilities import Capability, CapabilityUnavailable, Support
from app.leagues.context import AccessMethod, LeagueKey, Provider
from app.leagues.providers import registry
from app.leagues.providers.base import (LeagueAccess, LeagueNotFound, LeagueProvider, ProviderUnavailable)
from app.leagues.providers.espn import EspnProvider
from app.leagues.providers.sleeper import SleeperProvider
from app.leagues.providers.yahoo import REASON_NO_APP, REASON_NOT_IMPLEMENTED, YahooProvider
from test_league_providers_support import (PUBLIC, S2_SECRET, SEASON, SLEEPER_L1, SWID_SECRET, clean_env, no_network,
                                           owner_access, skey)

pytestmark = pytest.mark.usefixtures("no_network", "clean_env")

KEY = LeagueKey(Provider.SLEEPER, SLEEPER_L1, SEASON)


def _calls(provider: LeagueProvider):
    """(capability, zero-arg callable) for every LeagueProvider method."""
    key = LeagueKey(provider.provider, "123", SEASON)
    return [
        (Capability.DISCOVER_LEAGUES, lambda: provider.discover_leagues(PUBLIC, user_handle="someone", season=SEASON)),
        (Capability.LEAGUE_SETTINGS, lambda: provider.fetch_settings(key, PUBLIC)),
        (Capability.TEAMS, lambda: provider.fetch_teams(key, PUBLIC)),
        (Capability.ROSTERS, lambda: provider.fetch_rosters(key, PUBLIC)),
        (Capability.SCORING_RULES, lambda: provider.fetch_scoring(key, PUBLIC)),
        (Capability.MATCHUPS, lambda: provider.fetch_matchups(key, PUBLIC, week=1)),
        (Capability.STANDINGS, lambda: provider.fetch_standings(key, PUBLIC)),
        (Capability.FREE_AGENTS, lambda: provider.fetch_free_agents(key, PUBLIC)),
        (Capability.TRANSACTIONS, lambda: provider.fetch_transactions(key, PUBLIC, week=1)),
        (Capability.PROJECTIONS, lambda: provider.fetch_projections(key, PUBLIC, week=1)),
    ]


def test_every_interface_method_is_covered_by_the_matrix_below():
    abstract = {n for n, m in inspect.getmembers(LeagueProvider) if getattr(m, "__isabstractmethod__", False)}
    abstract -= {"capabilities"}
    assert len(abstract) == len(_calls(SleeperProvider(source="disabled")))


# --------------------------------------------------------------------------- declarations --

@pytest.mark.parametrize("make", [
    lambda: SleeperProvider(source="disabled"), lambda: SleeperProvider(source="fixture"),
    lambda: SleeperProvider(source="live"),
    lambda: EspnProvider(public_import_enabled=False, private_access_enabled=False),
    lambda: EspnProvider(public_import_enabled=True, private_access_enabled=True),
    lambda: YahooProvider(client_id=None, _use_settings=False), lambda: YahooProvider(client_id="abc"),
])
def test_every_provider_declares_every_capability(make):
    caps = make().capabilities()
    assert set(caps.states) == set(Capability), "adapter must state support for every Capability explicitly"
    assert all(s.note for s in caps.states.values()), "every capability carries an honest note"
    d = caps.as_dict()
    assert set(d["capabilities"]) == {c.value for c in Capability}
    assert d["enabled"] is caps.enabled
    assert (d["reason"] is None) == caps.enabled, "a reason is given exactly when the adapter is disabled"


@pytest.mark.parametrize("make", [
    lambda: SleeperProvider(source="disabled"),
    lambda: EspnProvider(public_import_enabled=False, private_access_enabled=False),
    lambda: YahooProvider(client_id=None, _use_settings=False), lambda: YahooProvider(client_id="abc"),
])
def test_disabled_providers_raise_capability_unavailable_on_every_method(make):
    provider = make()
    caps = provider.capabilities()
    assert not caps.enabled and caps.reason
    for cap, call in _calls(provider):
        with pytest.raises(CapabilityUnavailable) as e:
            call()
        assert e.value.capability is cap
        assert e.value.state.support is Support.DISABLED
        assert e.value.provider is provider.provider
    assert caps.missing() == list(Capability)


def test_unsupported_capabilities_raise_even_when_the_adapter_is_enabled():
    sleeper = SleeperProvider(source="fixture")
    for cap, call in _calls(sleeper):
        if cap is Capability.PROJECTIONS:
            with pytest.raises(CapabilityUnavailable) as e:
                call()
            assert e.value.state.support is Support.UNSUPPORTED
    espn = EspnProvider(public_import_enabled=True, private_access_enabled=False)
    with pytest.raises(CapabilityUnavailable) as e:
        espn.discover_leagues(PUBLIC, user_handle="x", season=SEASON)
    assert e.value.state.support is Support.UNSUPPORTED


def test_sleeper_declares_derived_and_unsupported_honestly():
    st = SleeperProvider(source="fixture").capabilities().states
    assert st[Capability.FREE_AGENTS].support is Support.DERIVED and "no free-agent endpoint" in st[Capability.FREE_AGENTS].note
    assert st[Capability.STANDINGS].support is Support.DERIVED
    assert st[Capability.PROJECTIONS].support is Support.UNSUPPORTED
    assert st[Capability.PRIVATE_LEAGUES].support is Support.UNSUPPORTED
    assert "no per-user private access model" in st[Capability.PRIVATE_LEAGUES].note
    assert st[Capability.DISCOVER_LEAGUES].support is Support.SUPPORTED


def test_espn_private_leagues_require_member_cookies():
    st = EspnProvider(public_import_enabled=False, private_access_enabled=True).capabilities().states
    assert st[Capability.PRIVATE_LEAGUES].support is Support.REQUIRES_AUTH
    assert "sync jobs" in st[Capability.PRIVATE_LEAGUES].note and "encrypted" in st[Capability.PRIVATE_LEAGUES].note


def test_yahoo_reasons_differ_by_client_id():
    assert YahooProvider(client_id=None, _use_settings=False).capabilities().reason == REASON_NO_APP
    assert YahooProvider(client_id="  ", _use_settings=False).capabilities().reason == REASON_NO_APP
    assert YahooProvider(client_id="dj0yJmk9abc").capabilities().reason == REASON_NOT_IMPLEMENTED
    assert "approved Yahoo Developer app" in REASON_NO_APP and "not implemented yet" in REASON_NOT_IMPLEMENTED


def test_yahoo_never_keeps_or_shows_the_client_id():
    y = YahooProvider(client_id="super-secret-client-id")
    assert "super-secret-client-id" not in repr(vars(y)) + str(y.capabilities().as_dict())


# ------------------------------------------------------------------------------ registry --

def test_registry_defaults_are_all_disabled(clean_env):
    caps = {c.provider: c for c in registry.all_capabilities()}
    assert set(caps) == set(Provider)
    assert not any(c.enabled for c in caps.values())
    assert [c.provider for c in registry.all_capabilities()] == list(Provider)
    assert caps[Provider.SLEEPER].reason.startswith("Live Sleeper import is off")
    assert "terms" in caps[Provider.SLEEPER].reason
    assert caps[Provider.YAHOO].reason == REASON_NO_APP
    assert registry.data_origin(Provider.SLEEPER) is None


def test_registry_follows_environment_flags(clean_env):
    clean_env.setenv("SLEEPER_SOURCE", "fixture")
    assert registry.get_provider(Provider.SLEEPER).capabilities().enabled
    assert registry.data_origin(Provider.SLEEPER) == "fixture"
    clean_env.setenv("SLEEPER_SOURCE", "live")
    assert registry.data_origin(Provider.SLEEPER) == "live"
    clean_env.setenv("SLEEPER_SOURCE", "nonsense")
    assert not registry.get_provider(Provider.SLEEPER).capabilities().enabled, "unknown value fails closed"

    clean_env.setenv("ESPN_PUBLIC_IMPORT_ENABLED", "1")
    assert registry.get_provider(Provider.ESPN).capabilities().enabled
    clean_env.delenv("ESPN_PUBLIC_IMPORT_ENABLED")
    assert not registry.get_provider(Provider.ESPN).capabilities().enabled
    clean_env.setenv("ESPN_S2", "x" * 20)
    clean_env.setenv("SWID", "{abc}")
    assert not registry.get_provider(Provider.ESPN).capabilities().enabled, \
        "the legacy server-owned ESPN cookies do not enable the multi-league adapter"
    from app.control_plane.credential_crypto import generate_keypair
    clean_env.setenv("CREDENTIALS_PUBKEY_V1", generate_keypair()[1])
    assert registry.get_provider(Provider.ESPN).capabilities().enabled, "the encrypted-credential path enables private ESPN"

    clean_env.setenv("YAHOO_CLIENT_ID", "client")
    yahoo = registry.get_provider(Provider.YAHOO).capabilities()
    assert not yahoo.enabled and yahoo.reason == REASON_NOT_IMPLEMENTED


def test_registry_reuses_adapters_until_settings_change(clean_env):
    clean_env.setenv("SLEEPER_SOURCE", "fixture")
    a = registry.get_provider(Provider.SLEEPER)
    assert registry.get_provider(Provider.SLEEPER) is a
    clean_env.setenv("SLEEPER_SOURCE", "live")
    assert registry.get_provider(Provider.SLEEPER) is not a


def test_registry_accepts_provider_strings_and_rejects_unknown(clean_env):
    assert isinstance(registry.get_provider("yahoo"), YahooProvider)
    with pytest.raises(ValueError):
        registry.get_provider("fantrax")


def test_registry_capabilities_are_what_the_api_will_serialise(clean_env):
    for entry in (c.as_dict() for c in registry.all_capabilities()):
        assert set(entry) == {"provider", "enabled", "reason", "capabilities"}
        assert all(v["support"] == "disabled" for v in entry["capabilities"].values())


# -------------------------------------------------------------------------- sanitized errors --

class _Boom:
    """A Sleeper transport that fails with a message that must never reach callers."""

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, url, timeout):
        raise self.exc


def _sleeper_with(transport, tmp_path):
    from app.sleeper.client import SleeperClient
    return SleeperProvider(client=SleeperClient(cache_dir=tmp_path, transport=transport, sleep=lambda s: None,
                                               max_retries=1), source="live")


def test_sleeper_network_failure_is_sanitized(tmp_path):
    p = _sleeper_with(_Boom(OSError("connect to http://10.0.0.5:9000/token=SECRET-TOKEN failed")), tmp_path)
    with pytest.raises(ProviderUnavailable) as e:
        p.fetch_settings(skey(), PUBLIC)
    assert str(e.value) == "Sleeper could not be reached"
    assert "SECRET" not in repr(e.value) and e.value.__cause__ is None and e.value.__context__ is None


def test_sleeper_http_errors_are_translated(tmp_path):
    from app.sleeper.client import Response
    p = _sleeper_with(lambda url, t: Response(503, {}, b"<html>internal host db-7 stack trace</html>"), tmp_path)
    with pytest.raises(ProviderUnavailable, match="HTTP 503") as e:
        p.fetch_settings(skey(), PUBLIC)
    assert "db-7" not in str(e.value)
    p = _sleeper_with(lambda url, t: Response(429, {"Retry-After": "1"}, b""), tmp_path)
    with pytest.raises(ProviderUnavailable, match="rate limiting"):
        p.fetch_settings(skey(), PUBLIC)
    p = _sleeper_with(lambda url, t: Response(200, {}, b"{not json"), tmp_path)
    with pytest.raises(ProviderUnavailable, match="unexpected response"):
        p.fetch_settings(skey(), PUBLIC)


def test_sleeper_unknown_league_is_not_found(tmp_path):
    from app.sleeper.client import Response
    for resp in (Response(404, {}, b""), Response(200, {}, b"null")):
        p = _sleeper_with(lambda url, t, r=resp: r, tmp_path)
        with pytest.raises(LeagueNotFound, match="no NFL league"):
            p.fetch_teams(skey("999"), PUBLIC)


def test_sleeper_wrong_season_is_not_found():
    p = SleeperProvider(source="fixture")
    with pytest.raises(LeagueNotFound, match="season 2026, not 2025"):
        p.fetch_settings(skey(season=2025), PUBLIC)


def test_sleeper_fixture_id_not_in_fixture_set_is_not_found():
    with pytest.raises(LeagueNotFound):
        SleeperProvider(source="fixture").fetch_settings(skey("1180000000000000099"), PUBLIC)


def test_sleeper_id_map_failure_is_a_provider_error_not_a_crash(monkeypatch):
    import app.leagues.providers.sleeper as mod

    def explode():
        raise RuntimeError("polars exploded at /srv/secret/path")

    monkeypatch.setattr(mod, "load_fixture_id_map", explode)
    with pytest.raises(ProviderUnavailable, match="player id map is unavailable") as e:
        SleeperProvider(source="fixture").fetch_rosters(skey(), PUBLIC)
    assert "/srv/secret" not in str(e.value) and e.value.__context__ is None


def test_sleeper_discover_rejects_bad_handles_and_unknown_users():
    p = SleeperProvider(source="fixture")
    for bad in ("", "a b", "../league/1", "x" * 100, "name/leagues"):
        with pytest.raises(LeagueNotFound):
            p.discover_leagues(PUBLIC, user_handle=bad, season=SEASON)
    with pytest.raises(LeagueNotFound, match="user not found"):
        p.discover_leagues(PUBLIC, user_handle="nobody_here", season=SEASON)


def test_sleeper_week_is_validated():
    p = SleeperProvider(source="fixture")
    for bad in (0, 23, -1, True, "6"):
        with pytest.raises(ValueError):
            p.fetch_matchups(skey(), PUBLIC, week=bad)


class _FakeEspn:
    def __init__(self, exc):
        self.exc = exc

    def league(self):
        raise self.exc

    def transactions(self, week):
        raise self.exc


def _espn_with(exc):
    return EspnProvider(lambda key, access: _FakeEspn(exc), public_import_enabled=True,
                        private_access_enabled=True)


ESPN_KEY = LeagueKey(Provider.ESPN, "999", SEASON)


def test_espn_errors_are_sanitized_and_never_carry_cookies():
    from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

    leaky = f"cookies espn_s2={S2_SECRET}; SWID={SWID_SECRET}"
    cases = [
        (ESPNAccessDenied(leaky), LeagueNotFound), (ESPNInvalidLeague(leaky), LeagueNotFound),
        (ESPNUnknownError(leaky), ProviderUnavailable), (requests.ConnectionError(leaky), ProviderUnavailable),
        (OSError(leaky), ProviderUnavailable), (ValueError(leaky), ProviderUnavailable),
        (KeyError(leaky), ProviderUnavailable),
    ]
    for exc, expected in cases:
        p = _espn_with(exc)
        for call in (lambda: p.fetch_settings(ESPN_KEY, owner_access()),
                     lambda: p.fetch_transactions(ESPN_KEY, owner_access(), week=1)):
            with pytest.raises(expected) as e:
                call()
            text = str(e.value) + repr(e.value) + repr(e.value.__cause__) + repr(e.value.__context__)
            assert S2_SECRET not in text and SWID_SECRET not in text and "espn_s2" not in text
            assert e.value.__cause__ is None and e.value.__context__ is None


def test_espn_wrong_league_or_season_in_payload_is_not_found():
    from espn_fixture import league_payload

    class C:
        def __init__(self, **over):
            self.over = over

        def league(self):
            return {**league_payload(), **self.over}

    for over in ({"id": 1234}, {"seasonId": 2019}):
        p = EspnProvider(lambda k, a, o=over: C(**o), public_import_enabled=True, private_access_enabled=False)
        with pytest.raises(LeagueNotFound):
            p.fetch_settings(ESPN_KEY, PUBLIC)


def test_espn_access_methods_are_gated():
    called = []
    factory = lambda k, a: called.append(a) or _FakeEspn(RuntimeError())     # noqa: E731
    off = EspnProvider(factory, public_import_enabled=False, private_access_enabled=True)
    with pytest.raises(ProviderUnavailable, match="public-league import is not enabled"):
        off.fetch_settings(ESPN_KEY, PUBLIC)
    with pytest.raises(ProviderUnavailable, match="No ESPN cookie credential was supplied"):
        off.fetch_settings(ESPN_KEY, LeagueAccess(AccessMethod.COOKIES, None))
    private_off = EspnProvider(factory, public_import_enabled=True, private_access_enabled=False)
    with pytest.raises(ProviderUnavailable, match="Private ESPN leagues are not enabled"):
        private_off.fetch_settings(ESPN_KEY, LeagueAccess(AccessMethod.COOKIES, SimpleNamespace(espn_s2="s2", swid="{x}")))
    with pytest.raises(ProviderUnavailable, match="no OAuth"):
        off.fetch_settings(ESPN_KEY, LeagueAccess(AccessMethod.OAUTH, "token"))
    assert called == [], "the raw client is never built for a refused access method"


def test_access_repr_hides_the_credential_everywhere():
    a = owner_access()
    assert S2_SECRET not in repr(a) and SWID_SECRET not in repr(a)
    p = EspnProvider(lambda k, x: _FakeEspn(RuntimeError()), public_import_enabled=False,
                     private_access_enabled=True)
    assert S2_SECRET not in repr(vars(p)) + repr(p.capabilities())


def test_no_provider_module_imports_an_http_library_at_import_time_except_espn_client():
    """Importing the adapters must not open connections; Yahoo and the registry never import networking."""
    import app.leagues.providers.yahoo as y
    src = inspect.getsource(y)
    assert "requests" not in src and "httpx" not in src and "urllib" not in src


def test_capability_matrix_document_has_a_row_for_every_capability():
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "multileague" / "provider-capability-matrix.md"
    if not doc.is_file():
        pytest.skip("repository docs are not part of this checkout")
    text = doc.read_text()
    for cap in Capability:
        assert f"| `{cap.value}` |" in text, f"matrix row missing for {cap.value}"
    for needle in ("Unresolved before public launch", "ESPN", "Sleeper", "Yahoo", "2026-10-16", "unverified"):
        assert needle.lower() in text.lower()

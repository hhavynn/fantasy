"""Yahoo Fantasy Sports adapter: a declared, disabled placeholder in Phase 1.

Yahoo is the only one of the three platforms with a real third-party authorization model (OAuth 2.0
authorization code flow, Fantasy Sports read scope, short-lived access tokens plus refresh tokens), and it
gates access behind an approved developer application and an API agreement. None of that exists here yet:
no approved Yahoo Developer app, no encrypted token storage, no sign-in / refresh / revocation code. So the
adapter is always `enabled=False`, never touches the network, and every method raises
`CapabilityUnavailable`. It does not pretend to connect.

The per-capability notes record what Yahoo's API offers, so the capability matrix and the UI can say what
connecting Yahoo would later provide. They describe Yahoo, not this deployment; the effective state is
DISABLED (`ProviderCapabilities.state` reports it while `enabled` is False). Details marked "unverified"
in docs/multileague/provider-capability-matrix.md must be re-checked against Yahoo's documentation before
implementation.
"""

from __future__ import annotations

from ...config import get_settings
from ..capabilities import Capability, CapabilityState, ProviderCapabilities, Support
from ..context import LeagueKey, Provider
from ..models import LeagueSettings, Roster, ScoringRules, Sourced, Team
from .base import (FreeAgent, LeagueAccess, LeagueProvider, LeagueSummary, Matchup, Standing, Transaction)

REASON_NO_APP = ("Yahoo requires an approved Yahoo Developer app (OAuth 2.0) and encrypted token storage; "
                 "neither is configured.")
REASON_NOT_IMPLEMENTED = "Yahoo OAuth sign-in, token encryption, refresh and revocation are not implemented yet."


class YahooProvider(LeagueProvider):
    provider = Provider.YAHOO
    data_origin = None

    def __init__(self, client_id: str | None = None, *, _use_settings: bool = True):
        if client_id is None and _use_settings:
            client_id = getattr(get_settings(), "yahoo_client_id", None)
        self.has_client_id = bool(client_id and str(client_id).strip())     # presence only; the id is not kept

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=Provider.YAHOO, enabled=False,
            reason=REASON_NOT_IMPLEMENTED if self.has_client_id else REASON_NO_APP, states=_states())

    def discover_leagues(self, access: LeagueAccess, *, user_handle: str, season: int) -> Sourced[list[LeagueSummary]]:
        self.require(Capability.DISCOVER_LEAGUES)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_settings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[LeagueSettings]:
        self.require(Capability.LEAGUE_SETTINGS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_teams(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Team]]:
        self.require(Capability.TEAMS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_rosters(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Roster]]:
        self.require(Capability.ROSTERS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_scoring(self, key: LeagueKey, access: LeagueAccess) -> Sourced[ScoringRules]:
        self.require(Capability.SCORING_RULES)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_matchups(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Matchup]]:
        self.require(Capability.MATCHUPS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_standings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Standing]]:
        self.require(Capability.STANDINGS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_free_agents(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[FreeAgent]]:
        self.require(Capability.FREE_AGENTS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_transactions(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Transaction]]:
        self.require(Capability.TRANSACTIONS)
        raise AssertionError("unreachable")        # pragma: no cover

    def fetch_projections(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[dict[str, float]]:
        self.require(Capability.PROJECTIONS)
        raise AssertionError("unreachable")        # pragma: no cover


def _states() -> dict[Capability, CapabilityState]:
    D = Support.DISABLED
    offered = "Offered by Yahoo's Fantasy Sports API (OAuth 2.0 user token); not enabled here."
    return {
        Capability.DISCOVER_LEAGUES: CapabilityState(D, f"{offered} The signed-in user's leagues via the users/games/leagues collections."),
        Capability.LEAGUE_SETTINGS: CapabilityState(D, f"{offered} League settings resource."),
        Capability.TEAMS: CapabilityState(D, f"{offered} League teams resource."),
        Capability.ROSTERS: CapabilityState(D, f"{offered} Team roster resource; player ids are Yahoo player keys (yahoo:<key>)."),
        Capability.SCORING_RULES: CapabilityState(D, f"{offered} Part of league settings (stat modifiers)."),
        Capability.MATCHUPS: CapabilityState(D, f"{offered} League scoreboard / team matchups."),
        Capability.STANDINGS: CapabilityState(D, f"{offered} League standings resource."),
        Capability.FREE_AGENTS: CapabilityState(D, f"{offered} League players collection filtered to status=A (available)."),
        Capability.TRANSACTIONS: CapabilityState(D, f"{offered} League transactions resource."),
        Capability.PROJECTIONS: CapabilityState(Support.UNSUPPORTED, (
            "Not confirmed in Yahoo's documentation (unverified); treated as unsupported until verified.")),
        Capability.PRIVATE_LEAGUES: CapabilityState(Support.REQUIRES_AUTH, (
            "Yahoo leagues are private to their members; reading one needs that member's OAuth token.")),
    }

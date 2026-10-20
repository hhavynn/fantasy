"""The LeagueProvider interface: one adapter per fantasy platform.

Fantasy-league providers (ESPN, Sleeper, Yahoo) are a separate layer from the NFL statistics
provider (nflverse, app/nfl). Adapters here only describe leagues: settings, teams, rosters,
matchups, availability. They never compute projections or statistics of their own.

Contract every adapter honours:
  * `capabilities()` states support for every Capability. A method whose capability is not
    available raises CapabilityUnavailable; it never returns empty or invented data.
  * Every result is `Sourced`: it carries the provider, source and fetch time.
  * Network / provider failures raise ProviderError subclasses with sanitized messages (no
    cookies, tokens or raw response bodies), so one tenant's failure is reported for that league
    only and cannot leak another tenant's credentials into logs.
  * Adapters are stateless with respect to tenants: credentials arrive per call in `LeagueAccess`
    and are never cached on the adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..capabilities import Capability, CapabilityUnavailable, ProviderCapabilities
from ..context import AccessMethod, LeagueKey, Provider
from ..models import LeagueSettings, Roster, ScoringRules, Sourced, Team


class ProviderError(RuntimeError):
    """A provider call failed. `str(err)` is safe to show and to log."""


class ProviderUnavailable(ProviderError):
    """Provider down, rate limited, or disabled in this deployment."""


class LeagueNotFound(ProviderError):
    """The provider does not know this league, or will not show it with the given access."""


@dataclass(frozen=True)
class LeagueAccess:
    """How to reach one league for one call. Credentials are opaque handles, never logged."""
    method: AccessMethod
    credential: Any = field(default=None, repr=False)    # vault-resolved secret for COOKIES / OAUTH

    def __repr__(self) -> str:
        return f"LeagueAccess(method={self.method.value}, credential={'<set>' if self.credential else None})"


@dataclass(frozen=True)
class LeagueSummary:
    key: LeagueKey
    name: str | None
    team_count: int | None
    is_public: bool | None


@dataclass(frozen=True)
class Matchup:
    week: int
    external_matchup_id: str
    team_ids: tuple[str, ...]            # external team ids in this matchup
    points: tuple[float | None, ...]     # aligned with team_ids; None = not played / not reported


@dataclass(frozen=True)
class Standing:
    external_team_id: str
    rank: int | None
    wins: int | None
    losses: int | None
    ties: int | None
    points_for: float | None


@dataclass(frozen=True)
class Transaction:
    external_id: str
    kind: str                            # add | drop | trade | waiver | ...
    week: int | None
    team_ids: tuple[str, ...]
    adds: dict[str, str]                 # namespaced player id -> external team id
    drops: dict[str, str]


@dataclass(frozen=True)
class FreeAgent:
    player: str                          # namespaced player id
    name: str | None
    position: str | None


class LeagueProvider(ABC):
    provider: Provider

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    def require(self, cap: Capability) -> None:
        state = self.capabilities().state(cap)
        if not state.available:
            raise CapabilityUnavailable(self.provider, cap, state)

    # Every method below raises CapabilityUnavailable when its capability is not available.
    @abstractmethod
    def discover_leagues(self, access: LeagueAccess, *, user_handle: str, season: int) -> Sourced[list[LeagueSummary]]: ...

    @abstractmethod
    def fetch_settings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[LeagueSettings]: ...

    @abstractmethod
    def fetch_teams(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Team]]: ...

    @abstractmethod
    def fetch_rosters(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Roster]]: ...

    @abstractmethod
    def fetch_scoring(self, key: LeagueKey, access: LeagueAccess) -> Sourced[ScoringRules]: ...

    @abstractmethod
    def fetch_matchups(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Matchup]]: ...

    @abstractmethod
    def fetch_standings(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[Standing]]: ...

    @abstractmethod
    def fetch_free_agents(self, key: LeagueKey, access: LeagueAccess) -> Sourced[list[FreeAgent]]: ...

    @abstractmethod
    def fetch_transactions(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[list[Transaction]]: ...

    @abstractmethod
    def fetch_projections(self, key: LeagueKey, access: LeagueAccess, *, week: int) -> Sourced[dict[str, float]]: ...

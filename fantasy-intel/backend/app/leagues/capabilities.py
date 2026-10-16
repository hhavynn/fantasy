"""Explicit provider capabilities. A provider that cannot supply something says so here; it never
returns empty or ESPN-shaped placeholder data instead."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .context import Provider


class Capability(str, Enum):
    DISCOVER_LEAGUES = "discover_leagues"     # list the leagues a user can access
    LEAGUE_SETTINGS = "league_settings"
    TEAMS = "teams"
    ROSTERS = "rosters"
    SCORING_RULES = "scoring_rules"
    MATCHUPS = "matchups"
    STANDINGS = "standings"
    FREE_AGENTS = "free_agents"               # the provider names who is available (not inferred by us)
    TRANSACTIONS = "transactions"
    PROJECTIONS = "projections"               # fantasy projections published by the provider
    PRIVATE_LEAGUES = "private_leagues"       # can read leagues that are not public


class Support(str, Enum):
    SUPPORTED = "supported"
    DERIVED = "derived"                       # we compute it from other supported data (stated in `note`)
    UNSUPPORTED = "unsupported"               # the provider does not offer it
    REQUIRES_AUTH = "requires_auth"           # offered, but only with credentials we do not hold for this league
    DISABLED = "disabled"                     # offered, but this deployment has not enabled / been approved for it


@dataclass(frozen=True)
class CapabilityState:
    support: Support
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.support in (Support.SUPPORTED, Support.DERIVED)


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: Provider
    enabled: bool                             # the adapter may be used at all in this deployment
    reason: str | None                        # why not, when disabled (shown to users verbatim)
    states: dict[Capability, CapabilityState] = field(default_factory=dict)

    def state(self, cap: Capability) -> CapabilityState:
        if not self.enabled:
            return CapabilityState(Support.DISABLED, self.reason)
        return self.states.get(cap, CapabilityState(Support.UNSUPPORTED, "not declared by the adapter"))

    def missing(self) -> list[Capability]:
        return [c for c in Capability if not self.state(c).available]

    def as_dict(self) -> dict:
        return {
            "provider": self.provider.value,
            "enabled": self.enabled,
            "reason": self.reason,
            "capabilities": {c.value: {"support": self.state(c).support.value, "note": self.state(c).note}
                             for c in Capability},
        }


class CapabilityUnavailable(RuntimeError):
    """Raised by an adapter asked for something its capabilities do not list as available."""

    def __init__(self, provider: Provider, capability: Capability, state: CapabilityState):
        self.provider = provider
        self.capability = capability
        self.state = state
        super().__init__(f"{provider.value} cannot provide {capability.value}: "
                         f"{state.support.value}{' - ' + state.note if state.note else ''}")

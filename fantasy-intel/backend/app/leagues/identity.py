"""Globally safe player identity.

* Every provider player id is namespaced: `espn:4362628`, `sleeper:4046`, `yahoo:nfl.p.30977`.
  Raw provider ids are never compared across providers.
* The bridge to nflverse statistics is the GSIS id, and a link exists only when evidence supports
  it (a published ID map row, or the provider's own gsis field). Names are never evidence: two
  players called "Mike Williams" stay two unresolved identities until an id-based source links them.
* Unresolved and conflicting players are kept, with the reason, rather than dropped or guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .context import Provider

_RAW_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_GSIS = re.compile(r"00-\d{7}")


@dataclass(frozen=True)
class ProviderPlayerId:
    provider: Provider
    raw_id: str

    def __post_init__(self):
        if not isinstance(self.provider, Provider):
            raise TypeError("provider must be a Provider")
        if not isinstance(self.raw_id, str) or not _RAW_ID.fullmatch(self.raw_id):
            raise ValueError(f"malformed {self.provider.value} player id")

    @property
    def namespaced(self) -> str:
        return f"{self.provider.value}:{self.raw_id}"

    @classmethod
    def parse(cls, value: str) -> "ProviderPlayerId":
        provider, sep, raw = value.partition(":")
        if not sep:
            raise ValueError("player ids must be namespaced as provider:id")
        return cls(Provider.parse(provider), raw)

    def __str__(self) -> str:
        return self.namespaced


class IdentityMethod(str, Enum):
    ID_MAP = "id_map"                   # DynastyProcess / nflverse ff_playerids row (high confidence)
    PROVIDER_GSIS = "provider_gsis"     # the provider's own published gsis id (medium)
    ESPN_MAPPED = "espn_mapped"         # the existing ESPN player_id_map (its own match_confidence applies)
    TEAM_DEFENSE = "team_defense"       # a team defense: maps to an NFL team, not a player
    CONFLICT = "conflict"               # sources disagree; deliberately unresolved
    UNMATCHED = "unmatched"             # no id-based evidence


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    NONE = "none"


@dataclass(frozen=True)
class IdentityLink:
    player: ProviderPlayerId
    gsis_id: str | None
    nfl_team: str | None                # set for team defenses only
    method: IdentityMethod
    confidence: Confidence
    reason: str | None = None           # why unresolved / conflicting

    def __post_init__(self):
        if self.gsis_id is not None and not _GSIS.fullmatch(self.gsis_id):
            raise ValueError("gsis ids look like 00-0012345")
        resolved = self.gsis_id is not None or self.nfl_team is not None
        if self.method in (IdentityMethod.CONFLICT, IdentityMethod.UNMATCHED) and resolved:
            raise ValueError("an unresolved identity cannot carry a gsis id or team")
        if self.method not in (IdentityMethod.CONFLICT, IdentityMethod.UNMATCHED) and not resolved:
            raise ValueError(f"{self.method.value} links must name a gsis id or team")

    @property
    def resolved(self) -> bool:
        return self.method not in (IdentityMethod.CONFLICT, IdentityMethod.UNMATCHED)


def same_athlete(a: IdentityLink, b: IdentityLink) -> bool:
    """True only when both are resolved to the same GSIS id (or the same team defense).
    Identical names, positions and teams are not enough."""
    if not (a.resolved and b.resolved):
        return a.player == b.player
    if a.gsis_id and b.gsis_id:
        return a.gsis_id == b.gsis_id
    return a.nfl_team is not None and a.nfl_team == b.nfl_team and a.gsis_id is None and b.gsis_id is None


# player_id_map.match_method values that are id-based evidence. `name_position_team` (the legacy
# ESPN mapper's unique-name fallback) is deliberately absent: the single-league dashboard shows
# those rows as medium confidence, but cross-provider identity never rests on a name.
_ESPN_ID_METHODS = {"espn_id:nfl_players": Confidence.HIGH, "espn_id:ff_playerids": Confidence.HIGH,
                    "espn_id:rosters_weekly": Confidence.HIGH}


def link_from_espn_map_row(row: dict) -> IdentityLink:
    """An IdentityLink for one legacy `player_id_map` row, accepting id-based evidence only."""
    player = ProviderPlayerId(Provider.ESPN, str(row["espn_player_id"]))
    method = row.get("match_method")
    if method == "team_defense" and row.get("nfl_team"):
        return IdentityLink(player, None, row["nfl_team"], IdentityMethod.TEAM_DEFENSE, Confidence.HIGH)
    if method in _ESPN_ID_METHODS and row.get("gsis_id"):
        return IdentityLink(player, row["gsis_id"], None, IdentityMethod.ESPN_MAPPED, _ESPN_ID_METHODS[method])
    reason = ("name-only match in the legacy ESPN map; not accepted as identity evidence"
              if method == "name_position_team" else (row.get("notes") or "no id-based evidence"))
    return IdentityLink(player, None, None, IdentityMethod.UNMATCHED, Confidence.NONE, reason)

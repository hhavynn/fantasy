"""Provider-neutral league settings, lineup rules, teams and rosters.

Each adapter translates its provider's payload into these shapes. Values a provider does not
supply stay None; nothing defaults to ESPN's or to "standard PPR" behaviour.

Lineup slots carry their eligible positions explicitly, so FLEX (RB/WR/TE) and SUPERFLEX
(QB/RB/WR/TE) can never be confused by label: two slots are the same kind only when their
eligibility sets are equal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from .context import LeagueKey, Provider
from .identity import IdentityLink, ProviderPlayerId

T = TypeVar("T")


@dataclass(frozen=True)
class Provenance:
    provider: Provider
    source: str                          # "sleeper:/league/{id}", "espn:mSettings", "fixture:sleeper/league.json"
    fetched_at: datetime                 # when we fetched it (UTC, naive)
    source_updated_at: datetime | None = None   # when the provider says it changed, if it says


@dataclass(frozen=True)
class Sourced(Generic[T]):
    value: T
    provenance: Provenance


# ---- lineup -----------------------------------------------------------------------------------

class SlotKind(str, Enum):
    STARTER = "starter"
    BENCH = "bench"
    INJURED_RESERVE = "ir"
    TAXI = "taxi"                       # dynasty taxi squad: rostered, never starts


OFFENSE = frozenset({"QB", "RB", "WR", "TE"})
FLEX_POSITIONS = frozenset({"RB", "WR", "TE"})
SUPERFLEX_POSITIONS = OFFENSE


@dataclass(frozen=True)
class LineupSlot:
    code: str                           # canonical code: QB, RB, WR, TE, K, DST, FLEX, SUPERFLEX, WRRB_FLEX, ...
    count: int
    kind: SlotKind
    eligible: frozenset[str]            # canonical player positions; empty for bench/IR/taxi
    provider_code: str                  # the provider's own label, verbatim ("OP", "SUPER_FLEX", "RB/WR/TE", ...)

    def __post_init__(self):
        if self.count < 0:
            raise ValueError("slot count cannot be negative")
        if self.kind is SlotKind.STARTER and not self.eligible:
            raise ValueError(f"starter slot {self.code} needs eligible positions")


@dataclass(frozen=True)
class LineupRules:
    slots: tuple[LineupSlot, ...]

    def starters(self) -> list[LineupSlot]:
        return [s for s in self.slots if s.kind is SlotKind.STARTER and s.count]

    def count(self, code: str) -> int:
        return sum(s.count for s in self.slots if s.code == code)

    @property
    def superflex_slots(self) -> int:
        """Starting slots that accept a QB in addition to another position."""
        return sum(s.count for s in self.starters() if "QB" in s.eligible and len(s.eligible) > 1)

    @property
    def flex_slots(self) -> int:
        """Starting slots that accept several non-QB offensive positions (FLEX, WR/RB, WR/TE ...)."""
        return sum(s.count for s in self.starters()
                   if "QB" not in s.eligible and len(s.eligible & FLEX_POSITIONS) > 1)

    @property
    def is_superflex(self) -> bool:
        return self.superflex_slots > 0


# ---- scoring -----------------------------------------------------------------------------------

# Canonical stat keys a provider rule may map onto. Anything else stays in `unmapped`.
CANONICAL_STATS = frozenset({
    "pass_yd", "pass_td", "pass_int", "pass_2pt", "pass_sack",
    "rush_yd", "rush_td", "rush_2pt",
    "rec", "rec_yd", "rec_td", "rec_2pt", "rec_te_bonus",
    "fum_lost", "fum_rec_td",
    "fg_made", "fg_miss", "xp_made", "xp_miss",
})


@dataclass(frozen=True)
class ScoringRules:
    provider_rules: dict[str, float]    # the provider's rules, verbatim keys (stat ids / names)
    canonical: dict[str, float]         # rules we could map to CANONICAL_STATS, points per unit
    unmapped: tuple[str, ...]           # provider rule keys we could not map (reported, not dropped)

    def __post_init__(self):
        bad = set(self.canonical) - CANONICAL_STATS
        if bad:
            raise ValueError(f"not canonical stat keys: {sorted(bad)}")

    @property
    def points_per_reception(self) -> float | None:
        """None when the league's rules do not say (never assumed to be 0, 0.5 or 1)."""
        return self.canonical.get("rec")


# ---- other settings ----------------------------------------------------------------------------

@dataclass(frozen=True)
class WaiverSettings:
    kind: str | None                    # "faab" | "rolling" | "reverse_standings" | "none" | None (unknown)
    faab_budget: int | None = None
    process_days: tuple[str, ...] = ()
    clear_days: int | None = None


@dataclass(frozen=True)
class TradeSettings:
    deadline_week: int | None = None
    deadline_at: datetime | None = None
    review: str | None = None           # "commissioner" | "league_vote" | "none" | None (unknown)
    review_days: int | None = None


@dataclass(frozen=True)
class PlayoffSettings:
    teams: int | None = None
    start_week: int | None = None
    last_regular_season_week: int | None = None
    week_per_round: int | None = None


@dataclass(frozen=True)
class LeagueSettings:
    key: LeagueKey
    name: str | None
    team_count: int | None
    lineup: LineupRules
    scoring: ScoringRules
    waivers: WaiverSettings
    trades: TradeSettings
    playoffs: PlayoffSettings
    league_type: str | None = None      # "redraft" | "keeper" | "dynasty" | None
    is_public: bool | None = None       # provider-reported visibility, None when it does not say


# ---- teams and rosters -------------------------------------------------------------------------

@dataclass(frozen=True)
class Team:
    key: LeagueKey
    external_team_id: str               # provider team / roster id, as a string ("1", "3", "449.l.1.t.2")
    name: str | None
    manager_ids: tuple[str, ...]        # provider manager ids, namespaced ("sleeper:7311...", "espn:sha256:...")
    manager_display_names: tuple[str, ...] = ()
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    points_for: float | None = None

    @property
    def scoped_id(self) -> str:
        """Unique across leagues and providers: team "1" in two leagues never collides."""
        return f"{self.key.namespaced}:team:{self.external_team_id}"


@dataclass(frozen=True)
class RosterEntry:
    player: ProviderPlayerId
    slot_kind: SlotKind
    slot_code: str | None               # canonical slot for starters (None when the provider leaves it empty)
    name: str | None = None             # display only; never used to link identities
    position: str | None = None         # canonical position
    nfl_team: str | None = None         # nflverse abbreviation
    identity: IdentityLink | None = None


@dataclass(frozen=True)
class Roster:
    key: LeagueKey
    external_team_id: str
    entries: tuple[RosterEntry, ...] = field(default_factory=tuple)

    def player_ids(self) -> list[str]:
        return [e.player.namespaced for e in self.entries]

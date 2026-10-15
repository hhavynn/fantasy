"""Typed Sleeper objects parsed from raw API JSON. Pure functions, no network.

Ids stay strings: `sid()` only ever turns a value into a string (Sleeper sends a few ids, such
as bracket_id, as JSON numbers above 2^53). `roster_id` is the exception: it is a small int
slot number (1..total_rosters) used by matchups, not an entity id, and Sleeper sends it as an
int. Missing values stay None, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EMPTY_SLOT = "0"     # Sleeper's placeholder for an empty starting slot


def sid(value: Any) -> str | None:
    """A Sleeper identifier as a string; None for missing / empty / Sleeper's "0" placeholder."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    return None if s in ("", EMPTY_SLOT) else s


def _int(value: Any) -> int | None:
    """Counts and years (never ids)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _points(whole: Any, decimal: Any) -> float | None:
    """Sleeper splits points into an integer part and hundredths (fpts=725, fpts_decimal=64)."""
    if whole is None:
        return None
    return round(float(whole) + float(decimal or 0) / 100.0, 2)


def _ids(values: Any) -> tuple[str, ...]:
    return tuple(s for s in (sid(v) for v in (values or [])) if s is not None)


@dataclass(frozen=True)
class SleeperLeague:
    league_id: str
    name: str | None
    season: int | None
    status: str | None                     # pre_draft | drafting | in_season | complete
    sport: str | None
    season_type: str | None
    total_rosters: int | None
    roster_positions: tuple[str, ...]
    scoring_settings: dict
    settings: dict
    previous_league_id: str | None
    draft_id: str | None
    bracket_id: str | None


@dataclass(frozen=True)
class SleeperUser:
    user_id: str
    display_name: str | None
    team_name: str | None                  # metadata.team_name, if the manager set one
    is_owner: bool | None                  # league commissioner


@dataclass(frozen=True)
class SleeperRoster:
    roster_id: int
    owner_id: str | None                   # None = orphaned team
    co_owners: tuple[str, ...]
    players: tuple[str, ...]               # whole roster, including reserve and taxi
    starters: tuple[str | None, ...]       # aligned with the league's non-bench roster_positions; None = empty
    reserve: tuple[str, ...]               # IR
    taxi: tuple[str, ...]
    wins: int | None
    losses: int | None
    ties: int | None
    points_for: float | None
    points_against: float | None
    waiver_position: int | None
    waiver_budget_used: int | None
    total_moves: int | None
    division: int | None


@dataclass(frozen=True)
class SleeperPlayer:
    player_id: str
    full_name: str | None
    position: str | None                   # Sleeper label (DEF for team defenses)
    fantasy_positions: tuple[str, ...]
    team: str | None                       # Sleeper team code, None = free agent
    status: str | None
    injury_status: str | None
    active: bool | None
    gsis_id: str | None                    # Sleeper's own copy, when present (stripped)
    espn_id: str | None
    is_team_defense: bool


@dataclass(frozen=True)
class NflState:
    season: int | None
    season_type: str | None                # pre | regular | post | off
    week: int | None
    display_week: int | None
    leg: int | None


@dataclass
class SleeperLeagueData:
    """Everything fetched for one league, parsed."""
    league: SleeperLeague
    users: list[SleeperUser]
    rosters: list[SleeperRoster]
    state: NflState
    players: dict[str, SleeperPlayer] = field(default_factory=dict)   # rostered players only


def parse_league(raw: dict) -> SleeperLeague:
    league_id = sid(raw.get("league_id"))
    if league_id is None:
        raise ValueError("Sleeper league payload has no league_id")
    return SleeperLeague(
        league_id=league_id,
        name=raw.get("name"),
        season=_int(raw.get("season")),
        status=raw.get("status"),
        sport=raw.get("sport"),
        season_type=raw.get("season_type"),
        total_rosters=_int(raw.get("total_rosters")),
        roster_positions=tuple(raw.get("roster_positions") or ()),
        scoring_settings=dict(raw.get("scoring_settings") or {}),
        settings=dict(raw.get("settings") or {}),
        previous_league_id=sid(raw.get("previous_league_id")),
        draft_id=sid(raw.get("draft_id")),
        bracket_id=sid(raw.get("bracket_id")),
    )


def parse_users(raw: list[dict]) -> list[SleeperUser]:
    out = []
    for u in raw or []:
        user_id = sid(u.get("user_id"))
        if user_id is None:
            continue
        meta = u.get("metadata") or {}
        out.append(SleeperUser(user_id=user_id, display_name=u.get("display_name"),
                               team_name=(meta.get("team_name") or "").strip() or None,
                               is_owner=u.get("is_owner")))
    return out


def parse_rosters(raw: list[dict]) -> list[SleeperRoster]:
    out = []
    for r in raw or []:
        rid = r.get("roster_id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            raise ValueError(f"Sleeper roster has a non-integer roster_id: {rid!r}")
        s = r.get("settings") or {}
        out.append(SleeperRoster(
            roster_id=rid,
            owner_id=sid(r.get("owner_id")),
            co_owners=_ids(r.get("co_owners")),
            players=_ids(r.get("players")),
            starters=tuple(sid(p) for p in (r.get("starters") or [])),
            reserve=_ids(r.get("reserve")),
            taxi=_ids(r.get("taxi")),
            wins=_int(s.get("wins")),
            losses=_int(s.get("losses")),
            ties=_int(s.get("ties")),
            points_for=_points(s.get("fpts"), s.get("fpts_decimal")),
            points_against=_points(s.get("fpts_against"), s.get("fpts_against_decimal")),
            waiver_position=_int(s.get("waiver_position")),
            waiver_budget_used=_int(s.get("waiver_budget_used")),
            total_moves=_int(s.get("total_moves")),
            division=_int(s.get("division")),
        ))
    return sorted(out, key=lambda r: r.roster_id)


def parse_player(player_id: str, raw: dict) -> SleeperPlayer:
    pid = sid(raw.get("player_id")) or sid(player_id)
    if pid is None:
        raise ValueError("Sleeper player without an id")
    position = raw.get("position")
    is_def = position == "DEF" or "DEF" in (raw.get("fantasy_positions") or ()) or pid.isalpha()
    name = raw.get("full_name") or " ".join(x for x in (raw.get("first_name"), raw.get("last_name")) if x) or None
    return SleeperPlayer(
        player_id=pid,
        full_name=name,
        position=position,
        fantasy_positions=tuple(raw.get("fantasy_positions") or ()),
        team=sid(raw.get("team")) or (pid if is_def else None),
        status=raw.get("status"),
        injury_status=raw.get("injury_status"),
        active=raw.get("active"),
        gsis_id=sid(raw.get("gsis_id")),
        espn_id=sid(raw.get("espn_id")),
        is_team_defense=is_def,
    )


def parse_players(raw: dict[str, dict], only: set[str] | None = None) -> dict[str, SleeperPlayer]:
    """Parse the /players/nfl map, optionally restricted to the ids in `only`."""
    keys = raw.keys() if only is None else (k for k in only if k in raw)
    return {pid: parse_player(pid, raw[pid]) for pid in keys}


def parse_state(raw: dict) -> NflState:
    return NflState(season=_int(raw.get("season")), season_type=raw.get("season_type"),
                    week=_int(raw.get("week")), display_week=_int(raw.get("display_week")),
                    leg=_int(raw.get("leg")))

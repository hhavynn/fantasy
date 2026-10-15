"""Load one Sleeper league by explicit id. There is no "current" or "latest" league here."""

from __future__ import annotations

from .client import SleeperClient, check_league_id
from .models import SleeperLeagueData, SleeperRoster, parse_league, parse_players, parse_rosters, parse_state, parse_users


def fetch_league(client: SleeperClient, league_id: str) -> SleeperLeagueData:
    """League, users, rosters, NFL state and the rostered players' records for `league_id`.

    Raises TypeError for a non-string id and client.InvalidLeagueId for a malformed, unknown or
    non-NFL league (before any roster request is made)."""
    lid = check_league_id(league_id)
    league = parse_league(client.league(lid))
    rosters = parse_rosters(client.league_rosters(lid))
    users = parse_users(client.league_users(lid))
    state = parse_state(client.nfl_state())
    rostered = set(rostered_player_ids(rosters))
    players = parse_players(client.players(), only=rostered) if rostered else {}
    return SleeperLeagueData(league=league, users=users, rosters=rosters, state=state, players=players)


def rostered_player_ids(rosters: list[SleeperRoster]) -> list[str]:
    """Every rostered player id once, in roster order (starters, reserve and taxi included)."""
    seen: dict[str, None] = {}
    for r in rosters:
        for pid in (*r.players, *r.reserve, *r.taxi, *(s for s in r.starters if s)):
            seen.setdefault(pid)
    return list(seen)

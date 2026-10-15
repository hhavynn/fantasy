"""Sleeper models -> the internal row shapes the ESPN parser produces (`espn.parse.parse_league`
and `parse_rosters`): a `leagues` row, `league_members`, `fantasy_teams`, roster/lineup rows and
player rows, with the same column names wherever the meaning is the same.

Differences that are deliberate, not oversights:
  * `league_id`, `member_id`, `owner_ids` and player ids are strings (the DuckDB tables declare
    league_id BIGINT and espn_player_id BIGINT; storing Sleeper rows there is phase 2 work).
  * player rows use `player_id` (Sleeper id, str) instead of `espn_player_id`, and every row
    carries `provider = "sleeper"` so mixed data can never be joined by accident.
  * Sleeper user ids are public, so unlike ESPN SWIDs they are not hashed.
  * values Sleeper does not provide stay None (trade_deadline is a week number on Sleeper, kept
    in `trade_deadline_week`; ESPN's column is a timestamp).
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from .constants import BENCH, IR, POSITION_MAP, SLOT_MAP, TAXI, WAIVER_TYPE_FAAB, nflverse_team
from .models import NflState, SleeperLeague, SleeperLeagueData, SleeperPlayer, SleeperRoster, SleeperUser

PROVIDER = "sleeper"
LEAGUE_TYPES = {0: "redraft", 1: "keeper", 2: "dynasty"}


def internal_position(position: str | None) -> str | None:
    return POSITION_MAP.get(position, position) if position else None


def starting_slots(league: SleeperLeague) -> list[str]:
    """Internal labels of the starting slots, in Sleeper's order (aligned with roster.starters)."""
    return [SLOT_MAP.get(p, p) for p in league.roster_positions if p != "BN"]


def roster_slots(league: SleeperLeague) -> dict[str, int]:
    """Slot counts in the internal `leagues.roster_slots` shape: {"QB": 1, ..., "BE": 6, "IR": 2}."""
    counts = Counter(SLOT_MAP.get(p, p) for p in league.roster_positions)
    reserve = league.settings.get("reserve_slots")
    if reserve:
        counts[IR] = int(reserve)
    taxi = league.settings.get("taxi_slots")
    if taxi:
        counts[TAXI] = int(taxi)
    return dict(counts)


def schedule_weeks(league: SleeperLeague) -> dict:
    """Regular season / playoff weeks from league settings.

    Sleeper: playoffs start at `playoff_week_start` (0 = no playoffs); rounds = ceil(log2(teams));
    `playoff_round_type` 0 = one week per round, 1 = two-week championship, 2 = two weeks per round.
    Anything unknown leaves the derived values None rather than guessing."""
    s = league.settings
    start = int(s.get("start_week") or 1)
    pws = s.get("playoff_week_start")
    teams = s.get("playoff_teams")
    if not pws or not teams or int(teams) < 2:
        return {"start_week": start, "last_regular_season_week": None, "reg_season_matchups": None,
                "final_scoring_period": None}
    pws, teams = int(pws), int(teams)
    rounds = math.ceil(math.log2(teams))
    round_type = int(s.get("playoff_round_type") or 0)
    playoff_weeks = {0: rounds, 1: rounds + 1, 2: 2 * rounds}.get(round_type)
    return {
        "start_week": start,
        "last_regular_season_week": pws - 1,
        "reg_season_matchups": pws - start,
        "final_scoring_period": pws - 1 + playoff_weeks if playoff_weeks else None,
    }


def current_week(league: SleeperLeague, state: NflState, final_period: int | None) -> int | None:
    """NFL state week, only while that state describes this league's season."""
    if state.season != league.season or state.season_type not in ("regular", "post") or not state.week:
        return None
    return min(state.week, final_period) if final_period else state.week


def league_row(league: SleeperLeague, state: NflState, synced_at: datetime) -> dict:
    s, sched = league.settings, schedule_weeks(league)
    week = current_week(league, state, sched["final_scoring_period"])
    return {
        "provider": PROVIDER,
        "league_id": league.league_id,
        "season": league.season,
        "name": league.name,
        "size": league.total_rosters,
        "scoring_type": None,                      # Sleeper has no ESPN-style scoringType
        "league_type": LEAGUE_TYPES.get(s.get("type")),
        "status": league.status,
        "reception_points": league.scoring_settings.get("rec"),
        "is_faab": s.get("waiver_type") == WAIVER_TYPE_FAAB if "waiver_type" in s else None,
        "faab_budget": s.get("waiver_budget"),
        "current_week": week,
        "current_matchup_period": week,            # one matchup per week on Sleeper
        "final_scoring_period": sched["final_scoring_period"],
        "reg_season_matchups": sched["reg_season_matchups"],
        "last_regular_season_week": sched["last_regular_season_week"],
        "playoff_teams": s.get("playoff_teams"),
        "trade_deadline": None,
        "trade_deadline_week": s.get("trade_deadline"),
        "waiver_process_days": None,
        "roster_slots": roster_slots(league),
        "scoring_items": league.scoring_settings,
        "settings_raw": s,
        "previous_league_id": league.previous_league_id,
        "synced_at": synced_at,
    }


def member_rows(league: SleeperLeague, users: list[SleeperUser]) -> list[dict]:
    return [{"provider": PROVIDER, "league_id": league.league_id, "season": league.season,
             "member_id": u.user_id, "display_name": u.display_name, "first_name": None, "last_name": None}
            for u in users]


def team_rows(league: SleeperLeague, users: list[SleeperUser], rosters: list[SleeperRoster],
              synced_at: datetime, my_user_id: str | None = None) -> list[dict]:
    by_id = {u.user_id: u for u in users}
    out = []
    for r in rosters:
        owners = [o for o in (r.owner_id, *r.co_owners) if o]
        primary = by_id.get(r.owner_id) if r.owner_id else None
        name = (primary.team_name or primary.display_name) if primary else None
        out.append({
            "provider": PROVIDER,
            "league_id": league.league_id,
            "season": league.season,
            "team_id": r.roster_id,
            "abbrev": None,
            "name": name or f"Team {r.roster_id}",
            "owner_ids": owners,
            "owner_names": ", ".join(filter(None, (by_id[o].display_name for o in owners if o in by_id))) or None,
            "division_id": r.division,
            "wins": r.wins,
            "losses": r.losses,
            "ties": r.ties,
            "points_for": r.points_for,
            "points_against": r.points_against,
            "playoff_seed": None,
            "waiver_rank": r.waiver_position,
            "faab_spent": r.waiver_budget_used,
            "acquisitions": None,                  # Sleeper only reports total_moves
            "total_moves": r.total_moves,
            "is_mine": (my_user_id in owners) if my_user_id is not None else None,
            "synced_at": synced_at,
        })
    return out


def lineup_rows(league: SleeperLeague, rosters: list[SleeperRoster], players: dict[str, SleeperPlayer],
                scoring_period: int | None, synced_at: datetime) -> list[dict]:
    """One row per rostered player: starters in their slot, then IR, taxi and bench.
    Empty starting slots produce no row (there is no player)."""
    slots = starting_slots(league)
    out = []
    for r in rosters:
        def row(pid: str, slot: str, starter: bool) -> dict:
            p = players.get(pid)
            return {"provider": PROVIDER, "league_id": league.league_id, "season": league.season,
                    "scoring_period": scoring_period, "team_id": r.roster_id, "player_id": pid,
                    "lineup_slot": slot, "is_starter": starter,
                    "injury_status": p.injury_status if p else None, "synced_at": synced_at}

        placed: set[str] = set()
        for i, pid in enumerate(r.starters):
            if pid is None:
                continue
            out.append(row(pid, slots[i] if i < len(slots) else "UNKNOWN", True))
            placed.add(pid)
        for pid in r.reserve:
            if pid not in placed:
                out.append(row(pid, IR, False))
                placed.add(pid)
        for pid in r.taxi:
            if pid not in placed:
                out.append(row(pid, TAXI, False))
                placed.add(pid)
        for pid in r.players:
            if pid not in placed:
                out.append(row(pid, BENCH, False))
                placed.add(pid)
    return out


def player_row(p: SleeperPlayer, synced_at: datetime) -> dict:
    return {
        "provider": PROVIDER,
        "player_id": p.player_id,
        "full_name": p.full_name,
        "position": internal_position(p.position),
        "eligible_positions": [internal_position(x) for x in p.fantasy_positions],
        "pro_team": nflverse_team(p.team),
        "injury_status": p.injury_status,
        "status": p.status,
        "is_team_defense": p.is_team_defense,
        "synced_at": synced_at,
    }


def build_rows(data: SleeperLeagueData, synced_at: datetime, *, my_user_id: str | None = None) -> dict:
    """The full normalized snapshot: {"league", "members", "teams", "lineups", "players"}."""
    lg = league_row(data.league, data.state, synced_at)
    return {
        "league": lg,
        "members": member_rows(data.league, data.users),
        "teams": team_rows(data.league, data.users, data.rosters, synced_at, my_user_id),
        "lineups": lineup_rows(data.league, data.rosters, data.players, lg["current_week"], synced_at),
        "players": [player_row(p, synced_at) for p in data.players.values()],
    }

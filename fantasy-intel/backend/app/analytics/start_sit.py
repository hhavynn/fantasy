"""Weekly Start/Sit Command Center analytics engine.

Pure, deterministic optimization and decision analysis comparing actual ESPN starting
lineups with lock-constrained optimal lineups.

Constraints respected:
  * Game locks: Kickoffs at or before `now` (or with final scores) lock starters in their assigned
    slots and lock bench players to the bench.
  * Slot preservation: Duplicate slots (e.g. RB1, RB2) and FLEX instances are tracked as stable
    slot instances.
  * Fantasy IR: Players in fantasy IR slots cannot directly enter starting slots.
  * Verified NFL ineligibility: Projections for players in unavailable_weeks (reserve list rule)
    are ignored.
  * Projections: Missing ESPN projections (None) are distinct from measured zeros (0.0).
  * No double-counting: Multi-player transitions report headline gain as net lineup delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db.connection import fetch_dicts, table_exists
from .trade_data import kickoff_utc

# Lock status categories
LOCK_LOCKED = "LOCKED"
LOCK_UNLOCKED = "UNLOCKED"
LOCK_UNKNOWN = "UNKNOWN"
LOCK_BYE = "BYE"
LOCK_TEAMLESS = "TEAMLESS"

# Minimum regular season games required in nfl_schedules to confirm an NFL bye
MIN_SCHEDULED_GAMES_FOR_BYE = 12

TEAMLESS_ABBRS = frozenset({None, "", "FA"})


@dataclass(frozen=True)
class PlayerLock:
    team: str | None
    status: str
    kickoff_utc: datetime | None = None
    opponent: str | None = None
    is_home: bool | None = None
    has_final_score: bool = False
    home_score: int | None = None
    away_score: int | None = None
    reason: str = ""


def verify_schedule_coverage(con: Any, season: int, week: int) -> bool:
    """Verify if nfl_schedules has sufficient coverage of games for this week to confirm byes."""
    if not table_exists(con, "nfl_schedules"):
        return False
    rows = fetch_dicts(
        con,
        "SELECT count(*) AS cnt FROM nfl_schedules WHERE season = ? AND week = ? AND game_type = 'REG'",
        [season, week],
    )
    cnt = rows[0]["cnt"] if rows else 0
    return cnt >= MIN_SCHEDULED_GAMES_FOR_BYE


def detect_player_locks(
    con: Any,
    season: int,
    week: int,
    pro_teams: list[str | None],
    now: datetime,
) -> dict[str | None, PlayerLock]:
    """Determine kickoff and lock status for a list of NFL teams for a given season and week."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    # Normalize teams
    unique_teams = set(pro_teams)
    out: dict[str | None, PlayerLock] = {}

    has_sched_table = table_exists(con, "nfl_schedules")
    games = (
        fetch_dicts(
            con,
            """
            SELECT game_id, gameday, gametime, home_team, away_team, home_score, away_score
            FROM nfl_schedules
            WHERE season = ? AND week = ? AND game_type = 'REG'
            """,
            [season, week],
        )
        if has_sched_table
        else []
    )

    team_to_game: dict[str, dict] = {}
    for g in games:
        if g.get("home_team"):
            team_to_game[g["home_team"]] = g
        if g.get("away_team"):
            team_to_game[g["away_team"]] = g

    is_covered = len(games) >= MIN_SCHEDULED_GAMES_FOR_BYE

    for team in unique_teams:
        if team in TEAMLESS_ABBRS:
            out[team] = PlayerLock(
                team=team,
                status=LOCK_TEAMLESS,
                reason="Player has no current NFL team",
            )
            continue

        team_str = str(team)
        game = team_to_game.get(team_str)

        if game is not None:
            k_utc = kickoff_utc(game.get("gameday"), game.get("gametime"))
            h_score = game.get("home_score")
            a_score = game.get("away_score")
            has_final = (
                h_score is not None
                and a_score is not None
                and not (h_score == 0 and a_score == 0)
            )

            is_home = game.get("home_team") == team_str
            opponent = game.get("away_team") if is_home else game.get("home_team")

            if has_final:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_LOCKED,
                    kickoff_utc=k_utc,
                    opponent=opponent,
                    is_home=is_home,
                    has_final_score=True,
                    home_score=h_score,
                    away_score=a_score,
                    reason=f"Game completed ({opponent} vs {team_str}: {h_score}-{a_score})",
                )
            elif k_utc is not None and k_utc <= now:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_LOCKED,
                    kickoff_utc=k_utc,
                    opponent=opponent,
                    is_home=is_home,
                    has_final_score=False,
                    reason=f"Game kicked off at {k_utc.isoformat()}",
                )
            elif k_utc is not None and k_utc > now:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_UNLOCKED,
                    kickoff_utc=k_utc,
                    opponent=opponent,
                    is_home=is_home,
                    has_final_score=False,
                    reason=f"Scheduled kickoff at {k_utc.isoformat()}",
                )
            else:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_UNKNOWN,
                    opponent=opponent,
                    is_home=is_home,
                    has_final_score=False,
                    reason="Scheduled game exists but kickoff date/time is missing or invalid",
                )
        else:
            if is_covered:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_BYE,
                    reason=f"NFL bye week for {team_str} (confirmed by schedule coverage)",
                )
            else:
                out[team] = PlayerLock(
                    team=team,
                    status=LOCK_UNKNOWN,
                    reason=f"No game scheduled for {team_str}, but NFL schedule coverage for week {week} is incomplete ({len(games)} games stored)",
                )

    return out

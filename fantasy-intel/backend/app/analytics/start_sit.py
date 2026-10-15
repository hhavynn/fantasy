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
from ..espn.constants import BENCH_SLOT, IR_SLOT, NON_STARTER_SLOTS, SLOT_MAP
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, eligible
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


@dataclass(frozen=True)
class SlotInstance:
    slot_id: str          # e.g. "RB_0", "RB_1", "FLEX_0"
    slot_type: str        # e.g. "RB", "FLEX"
    index: int            # 0, 1, ...
    is_flex: bool


def build_slot_instances(roster_slots: dict[str, int]) -> list[SlotInstance]:
    """Expand league roster slot configuration into uniquely identified starting SlotInstance objects."""
    slots: list[SlotInstance] = []
    # Fixed slots in canonical order
    for s in FIXED_SLOTS:
        count = int((roster_slots or {}).get(s, 0))
        for i in range(count):
            slots.append(SlotInstance(slot_id=f"{s}_{i}", slot_type=s, index=i, is_flex=False))

    # Flex slots in canonical order (sorted by eligibility width)
    for s in sorted(FLEX_ELIGIBILITY, key=lambda k: len(FLEX_ELIGIBILITY[k])):
        count = int((roster_slots or {}).get(s, 0))
        for i in range(count):
            slots.append(SlotInstance(slot_id=f"{s}_{i}", slot_type=s, index=i, is_flex=True))

    return slots


def map_actual_starters_to_slots(
    actual_starters: list[dict],
    slots: list[SlotInstance],
) -> list[dict]:
    """Map actual starting players from lineup_snapshots to unique starting SlotInstance objects.

    Matches first on explicit slot label (e.g. FLEX -> FLEX_0, RB -> RB_0), then on positional eligibility.
    """
    assigned: list[dict] = []
    used_slots: set[str] = set()

    # Pass 1: Exact slot type matches
    unmatched_players: list[dict] = []
    for p in actual_starters:
        slot_label = p.get("lineup_slot")
        if not slot_label and p.get("lineup_slot_id") is not None:
            slot_label = SLOT_MAP.get(p["lineup_slot_id"])

        matched_slot = next(
            (s for s in slots if s.slot_id not in used_slots and s.slot_type == slot_label),
            None,
        )
        if matched_slot is not None:
            used_slots.add(matched_slot.slot_id)
            assigned.append({"slot": matched_slot, "player": p, "player_id": p.get("espn_player_id")})
        else:
            unmatched_players.append(p)

    # Pass 2: Positionally eligible matches for any remaining starters
    for p in unmatched_players:
        pos = p.get("position")
        matched_slot = next(
            (s for s in slots if s.slot_id not in used_slots and eligible(s.slot_type, pos)),
            None,
        )
        if matched_slot is not None:
            used_slots.add(matched_slot.slot_id)
            assigned.append({"slot": matched_slot, "player": p, "player_id": p.get("espn_player_id")})
        else:
            # Fallback placeholder if roster has extra players beyond slots
            assigned.append({"slot": None, "player": p, "player_id": p.get("espn_player_id")})

    return assigned


def preserve_locked_assignments(
    actual_assignments: list[dict],
    actual_bench: list[dict],
    locks: dict[str | None, PlayerLock],
    all_slots: list[SlotInstance],
) -> tuple[list[dict], list[SlotInstance], set[int], set[int]]:
    """Identify locked starters and locked bench players.

    Preserves locked starters in their exact assigned slot instances, removes those slots
    from remaining_slots, and identifies bench players who cannot enter the lineup.
    """
    locked_starters: list[dict] = []
    locked_starter_ids: set[int] = set()
    locked_slot_ids: set[str] = set()

    for item in actual_assignments:
        p = item.get("player") or {}
        pid = p.get("espn_player_id")
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)

        if lock.status == LOCK_LOCKED:
            locked_starters.append(item)
            if pid is not None:
                locked_starter_ids.add(pid)
            if item.get("slot") is not None:
                locked_slot_ids.add(item["slot"].slot_id)

    remaining_slots = [s for s in all_slots if s.slot_id not in locked_slot_ids]

    locked_bench_ids: set[int] = set()
    for p in actual_bench:
        pid = p.get("espn_player_id")
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)
        if lock.status == LOCK_LOCKED and pid is not None:
            locked_bench_ids.add(pid)

    return locked_starters, remaining_slots, locked_starter_ids, locked_bench_ids


def load_actual_lineup(
    con: Any,
    league_id: int,
    season: int,
    scoring_period: int,
    team_id: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Load actual lineup snapshots for a team in a specific scoring period.

    Returns:
      (starters, bench, ir)
    """
    rows = fetch_dicts(
        con,
        """
        SELECT l.team_id, l.espn_player_id, l.lineup_slot_id, l.lineup_slot, l.is_starter,
               l.points, l.projected_points AS snapshot_projected_points, l.synced_at AS snapshot_synced_at,
               fp.full_name, fp.position, fp.pro_team,
               coalesce(fp.injury_status, l.injury_status) AS injury_status
        FROM lineup_snapshots l
        JOIN fantasy_players fp USING (espn_player_id)
        WHERE l.league_id = ? AND l.season = ? AND l.scoring_period = ? AND l.team_id = ?
        ORDER BY l.is_starter DESC, l.lineup_slot_id, fp.full_name
        """,
        [league_id, season, scoring_period, team_id],
    )

    starters, bench, ir = [], [], []
    for r in rows:
        slot_id = r.get("lineup_slot_id")
        if slot_id == IR_SLOT:
            ir.append(r)
        elif r.get("is_starter") or (slot_id is not None and slot_id not in NON_STARTER_SLOTS):
            starters.append(r)
        else:
            bench.append(r)

    return starters, bench, ir


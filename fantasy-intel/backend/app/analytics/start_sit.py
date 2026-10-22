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

import json
from ..db.connection import fetch_dicts, table_exists
from ..espn.constants import BENCH_SLOT, IR_SLOT, NON_STARTER_SLOTS, SLOT_MAP
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, assign_lineup, eligible
from .player_availability import load_availability
from .reserve_slots import is_taxi
from .team_lineups import RosterPlayer
from .trade_data import kickoff_utc

# Lock status categories
LOCK_LOCKED = "LOCKED"
LOCK_UNLOCKED = "UNLOCKED"
LOCK_UNKNOWN = "UNKNOWN"
LOCK_BYE = "BYE"
LOCK_TEAMLESS = "TEAMLESS"

# Action types
ACTION_SWAP = "SWAP"
ACTION_COORDINATED_SWAP = "COORDINATED_SWAP"
ACTION_NO_CHANGE = "NO_CHANGE"

# Actionability categories
ACTIONABILITY_ACTIONABLE = "ACTIONABLE"
ACTIONABILITY_LOCKED = "LOCKED"
ACTIONABILITY_UNCERTAIN = "UNCERTAIN"
ACTIONABILITY_IR_BLOCKED = "IR_BLOCKED"
ACTIONABILITY_NO_CHANGE = "NO_CHANGE"

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


# The Start/Sit limitation line when the projection switch is on Ours.
OURS_NOTE = ("Projections are Fantasy Intel's own model (expected points, league scoring). Kickers and D/ST come from "
             "the kicker and D/ST models when their run exists; players the models can't project keep ESPN's number, "
             "marked.")

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


def _map_optimizer_output_to_slots(
    assignments: list[dict],
    slots: list[SlotInstance],
    player_by_id: dict[int, dict],
) -> list[dict]:
    """Map pure assign_lineup output onto exact SlotInstance objects."""
    out: list[dict] = []
    slots_by_type: dict[str, list[SlotInstance]] = {}
    for s in slots:
        slots_by_type.setdefault(s.slot_type, []).append(s)

    for row in assignments:
        stype = row["slot"]
        matching_slots = slots_by_type.get(stype, [])
        slot_inst = matching_slots.pop(0) if matching_slots else SlotInstance(
            slot_id=f"{stype}_anon", slot_type=stype, index=0, is_flex=row.get("is_flex", False)
        )
        pid = row.get("player_id")
        p = player_by_id.get(pid) if pid is not None else None
        out.append({
            "slot_id": slot_inst.slot_id,
            "slot": slot_inst.slot_type,
            "player_id": pid,
            "player": p,
            "position": row.get("position"),
            "projected_points": row.get("value") if pid is not None else None,
            "is_flex": slot_inst.is_flex,
            "is_locked": False,
        })
    return out


def optimize_lock_constrained_lineup(
    roster_players: list[dict],
    slot_instances: list[SlotInstance],
    locked_starter_assignments: list[dict],
    locked_bench_ids: set[int],
    unavailable_ids: set[int],
    ir_ids: set[int],
    projections: dict[int, float | None],
    actual_starters: list[dict],
) -> dict:
    """Optimize lineup preserving locked starter assignments and excluding locked bench players.

    Also calculates unconstrained optimum as a mathematical benchmark.
    """
    player_by_id: dict[int, dict] = {p["espn_player_id"]: p for p in roster_players if "espn_player_id" in p}
    for item in locked_starter_assignments:
        p = item.get("player")
        if p and "espn_player_id" in p:
            player_by_id[p["espn_player_id"]] = p

    # 1. Benchmark: Unconstrained optimal lineup
    unconstrained_candidates = [
        {"espn_player_id": p["espn_player_id"], "position": p.get("position"), "value": float(projections[p["espn_player_id"]])}
        for p in roster_players
        if p["espn_player_id"] not in ir_ids
        and p["espn_player_id"] not in unavailable_ids
        and projections.get(p["espn_player_id"]) is not None
    ]
    all_slot_types = [s.slot_type for s in slot_instances]
    unconstrained_raw = assign_lineup(unconstrained_candidates, all_slot_types)
    unconstrained_lineup = _map_optimizer_output_to_slots(unconstrained_raw, slot_instances, player_by_id)

    # 2. Lock-constrained recommended lineup
    locked_slot_ids = {a["slot"].slot_id for a in locked_starter_assignments if a.get("slot")}
    locked_starter_ids = {a["player_id"] for a in locked_starter_assignments if a.get("player_id") is not None}

    remaining_slot_instances = [s for s in slot_instances if s.slot_id not in locked_slot_ids]
    remaining_slot_types = [s.slot_type for s in remaining_slot_instances]

    candidate_pool = [
        {"espn_player_id": p["espn_player_id"], "position": p.get("position"), "value": float(projections[p["espn_player_id"]])}
        for p in roster_players
        if p["espn_player_id"] not in locked_starter_ids
        and p["espn_player_id"] not in locked_bench_ids
        and p["espn_player_id"] not in ir_ids
        and p["espn_player_id"] not in unavailable_ids
        and projections.get(p["espn_player_id"]) is not None
    ]

    new_assignments_raw = assign_lineup(candidate_pool, remaining_slot_types)
    new_assignments = _map_optimizer_output_to_slots(new_assignments_raw, remaining_slot_instances, player_by_id)

    # Format preserved locked starters
    preserved_locked: list[dict] = []
    for item in locked_starter_assignments:
        slot_inst = item.get("slot")
        p = item.get("player") or {}
        pid = item.get("player_id") or p.get("espn_player_id")
        proj_val = projections.get(pid) if pid is not None else None
        if pid in unavailable_ids:
            proj_val = 0.0          # verified NFL-ineligible: his projection is ignored
        preserved_locked.append({
            "slot_id": slot_inst.slot_id if slot_inst else "LOCKED",
            "slot": slot_inst.slot_type if slot_inst else p.get("position"),
            "player_id": pid,
            "player": p,
            "position": p.get("position"),
            "projected_points": proj_val,
            "is_flex": slot_inst.is_flex if slot_inst else False,
            "is_locked": True,
        })

    # Combine and sort into canonical slot_instances order
    slot_order = {s.slot_id: i for i, s in enumerate(slot_instances)}
    recommended_lineup = preserved_locked + new_assignments
    recommended_lineup.sort(key=lambda x: slot_order.get(x["slot_id"], 999))

    # Totals and completeness check
    missing_projections: list[int] = []
    actual_known_total = 0.0
    actual_complete = True
    for p in actual_starters:
        pid = p.get("espn_player_id")
        proj_val = 0.0 if pid in unavailable_ids else projections.get(pid)
        if proj_val is None:
            if p.get("pro_team") not in TEAMLESS_ABBRS:
                actual_complete = False
                if pid is not None:
                    missing_projections.append(pid)
        else:
            actual_known_total += proj_val

    rec_known_total = 0.0
    rec_complete = True
    for a in recommended_lineup:
        pts = a.get("projected_points")
        if pts is None:
            if a.get("player_id") is not None:
                rec_complete = False
        else:
            rec_known_total += pts

    unc_known_total = sum(a.get("projected_points") or 0.0 for a in unconstrained_lineup if a.get("projected_points") is not None)

    is_complete = actual_complete and rec_complete

    # No submitted lineup (a future-week preview or no snapshot) has no actual total: 0 would turn
    # the whole recommended lineup into a "gain".
    actual_total = round(actual_known_total, 2) if actual_complete and actual_starters else None
    recommended_total = round(rec_known_total, 2) if rec_complete else None
    unconstrained_total = round(unc_known_total, 2) if is_complete else None
    projected_gain = (
        round(recommended_total - actual_total, 2)
        if (actual_total is not None and recommended_total is not None)
        else None
    )

    return {
        "recommended_lineup": recommended_lineup,
        "unconstrained_lineup": unconstrained_lineup,
        "actual_total": actual_total,
        "recommended_total": recommended_total,
        "unconstrained_total": unconstrained_total,
        "known_actual_total": round(actual_known_total, 2),
        "known_recommended_total": round(rec_known_total, 2),
        "known_unconstrained_total": round(unc_known_total, 2),
        "projected_gain": projected_gain,
        "is_complete": is_complete,
        "missing_projection_ids": sorted(missing_projections),
    }


@dataclass(frozen=True)
class StartSitDecision:
    id: str
    action: str                       # SWAP, COORDINATED_SWAP, NO_CHANGE
    actionability: str                # ACTIONABLE, LOCKED, UNCERTAIN, IR_BLOCKED, NO_CHANGE
    start_players: list[dict]
    bench_players: list[dict]
    target_slot: str | None           # e.g. "WR" or "FLEX" or "Multiple"
    projected_net_gain: float | None
    explanation: str
    warnings: list[str]


def generate_start_sit_decisions(
    actual_starters: list[dict],
    recommended_lineup: list[dict],
    player_locks: dict[str | None, PlayerLock],
    projections: dict[int, float | None],
    actual_total: float | None,
    recommended_total: float | None,
    is_complete: bool,
    ir_ids: set[int] | None = None,
) -> list[StartSitDecision]:
    """Compare actual starting lineup against recommended lineup.

    Generates either a NO_CHANGE decision, a single 1-for-1 SWAP decision, or a
    COORDINATED_SWAP decision for multi-player cascades.
    """
    ir_ids = ir_ids or set()
    actual_pids = {p["espn_player_id"] for p in actual_starters if "espn_player_id" in p}
    rec_pids = {a["player_id"] for a in recommended_lineup if a.get("player_id") is not None}

    entering_ids = rec_pids - actual_pids
    displaced_ids = actual_pids - rec_pids

    net_gain = (
        round(recommended_total - actual_total, 2)
        if (recommended_total is not None and actual_total is not None)
        else None
    )

    # 1. No changes: Lineup is already optimal
    if not entering_ids and not displaced_ids:
        return [
            StartSitDecision(
                id="decision_optimal",
                action=ACTION_NO_CHANGE,
                actionability=ACTIONABILITY_NO_CHANGE,
                start_players=[],
                bench_players=[],
                target_slot=None,
                projected_net_gain=0.0,
                explanation="Your currently submitted lineup is already optimal under modeled constraints.",
                warnings=[],
            )
        ]

    # Helper to check actionability for a set of players
    def evaluate_actionability(players: list[dict]) -> tuple[str, list[str]]:
        warns: list[str] = []
        is_locked = False
        is_uncertain = False
        is_ir = False

        for p in players:
            pid = p.get("espn_player_id")
            if pid in ir_ids:
                is_ir = True
                warns.append(f"{p.get('full_name')} occupies a fantasy IR slot and cannot start without a roster move.")
            pro_team = p.get("pro_team")
            lock = player_locks.get(pro_team)
            if lock and lock.status == LOCK_LOCKED:
                is_locked = True
                warns.append(f"{p.get('full_name')} ({pro_team}) has already kicked off and cannot be moved.")
            elif not lock or lock.status == LOCK_UNKNOWN:
                is_uncertain = True
                warns.append(f"Kickoff status for {p.get('full_name')} ({pro_team}) could not be verified.")

        if not is_complete:
            is_uncertain = True
            warns.append("Comparison projection coverage is incomplete.")

        if is_ir:
            return ACTIONABILITY_IR_BLOCKED, warns
        if is_locked:
            return ACTIONABILITY_LOCKED, warns
        if is_uncertain:
            return ACTIONABILITY_UNCERTAIN, warns
        return ACTIONABILITY_ACTIONABLE, warns

    # 2. Single 1-for-1 swap
    if len(entering_ids) == 1 and len(displaced_ids) == 1:
        entering_id = list(entering_ids)[0]
        displaced_id = list(displaced_ids)[0]

        rec_entry = next(a for a in recommended_lineup if a.get("player_id") == entering_id)
        start_p = rec_entry.get("player") or {"espn_player_id": entering_id}
        bench_p = next((p for p in actual_starters if p.get("espn_player_id") == displaced_id), {"espn_player_id": displaced_id})

        target_slot = rec_entry.get("slot") or start_p.get("position")
        actionability, warnings = evaluate_actionability([start_p, bench_p])

        s_name = start_p.get("full_name") or f"Player {entering_id}"
        s_proj = projections.get(entering_id)
        s_proj_str = f" ({s_proj:.1f} pts)" if s_proj is not None else ""

        b_name = bench_p.get("full_name") or f"Player {displaced_id}"
        b_proj = projections.get(displaced_id)
        b_proj_str = f" ({b_proj:.1f} pts)" if b_proj is not None else ""

        gain_str = f"+{net_gain:.1f}" if net_gain is not None and net_gain > 0 else (f"{net_gain}" if net_gain is not None else "an unknown")
        explanation = f"Start {s_name}{s_proj_str} over {b_name}{b_proj_str} in {target_slot} for a {gain_str} pt projected improvement."

        return [
            StartSitDecision(
                id=f"swap_{entering_id}_{displaced_id}",
                action=ACTION_SWAP,
                actionability=actionability,
                start_players=[start_p],
                bench_players=[bench_p],
                target_slot=target_slot,
                projected_net_gain=net_gain,
                explanation=explanation,
                warnings=warnings,
            )
        ]

    # 3. Coordinated multi-player change (FLEX cascade)
    start_players = [a.get("player") or {"espn_player_id": a["player_id"]} for a in recommended_lineup if a.get("player_id") in entering_ids]
    bench_players = [p for p in actual_starters if p.get("espn_player_id") in displaced_ids]

    actionability, warnings = evaluate_actionability(start_players + bench_players)

    starts_desc = ", ".join(
        f"{p.get('full_name')} ({projections.get(p.get('espn_player_id'), 0):.1f} pts)"
        for p in start_players
    )
    benches_desc = ", ".join(
        f"{p.get('full_name')} ({projections.get(p.get('espn_player_id'), 0):.1f} pts)"
        for p in bench_players
    )
    gain_str = f"+{net_gain:.1f}" if net_gain is not None and net_gain > 0 else (f"{net_gain}" if net_gain is not None else "an unknown")

    explanation = (
        f"Coordinated multi-player change: Starting {starts_desc} displaces {benches_desc} "
        f"across starting slots for a {gain_str} pt net lineup gain."
    )

    return [
        StartSitDecision(
            id="coordinated_swap",
            action=ACTION_COORDINATED_SWAP,
            actionability=actionability,
            start_players=start_players,
            bench_players=bench_players,
            target_slot="Multiple slots",
            projected_net_gain=net_gain,
            explanation=explanation,
            warnings=warnings,
        )
    ]


def load_start_sit_evaluation(
    con: Any,
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    week: int | None = None,
    now: datetime | None = None,
    source: str | None = None,
) -> dict:
    """Full evaluation payload for Start/Sit Command Center. `source` is "espn" (default) or
    "independent" (Fantasy Intel's model, see analytics/projection_source.py)."""
    from .projection_source import describe, normalize, weekly

    source = normalize(source, con)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Fetch league
    lg_query = "SELECT * FROM leagues"
    params: list[Any] = []
    if season:
        lg_query += " WHERE season = ?"
        params.append(season)
    lg_query += " ORDER BY synced_at DESC LIMIT 1"
    lgs = fetch_dicts(con, lg_query, params)
    if not lgs:
        raise ValueError("ESPN league not synced yet.")
    lg = lgs[0]
    league_id = lg["league_id"]
    season = lg["season"]
    current_week = int(lg["current_week"])
    final_week = int(lg.get("final_scoring_period") or 18)

    # 2. Week validation & preview flag
    if week is None:
        target_week = current_week
    else:
        target_week = int(week)
        if target_week < 1 or target_week > final_week:
            raise ValueError(f"Week {target_week} is outside valid fantasy season (1-{final_week})")
    is_future_preview = target_week > current_week

    # 3. Team selection
    teams = fetch_dicts(
        con,
        "SELECT team_id, name, abbrev, is_mine FROM fantasy_teams WHERE league_id = ? AND season = ? ORDER BY team_id",
        [league_id, season],
    )
    if not teams:
        raise ValueError("No fantasy teams found for this league.")

    if team_id is not None:
        target_team = next((t for t in teams if t["team_id"] == team_id), None)
        if target_team is None:
            raise LookupError(f"Team {team_id} not found in this league.")
    else:
        target_team = next((t for t in teams if t.get("is_mine")), None)
        if target_team is None:
            target_team = teams[0]

    tid = target_team["team_id"]

    # 4. Load roster & lineup
    if not is_future_preview:
        starters, bench, ir = load_actual_lineup(con, league_id, season, target_week, tid)
    else:
        # Check if actual snapshot exists for future week; if not, use current week's snapshot
        future_starters, future_bench, future_ir = load_actual_lineup(con, league_id, season, target_week, tid)
        if future_starters or future_bench:
            starters, bench, ir = future_starters, future_bench, future_ir
        else:
            prev_s, prev_b, prev_ir = load_actual_lineup(con, league_id, season, current_week, tid)
            # In a future preview with no submitted lineup, treat all active players as unassigned candidates
            starters = []
            bench = prev_s + prev_b
            ir = prev_ir

    all_roster = starters + bench + ir
    # Sleeper taxi-squad players load as bench rows (slot label TAXI): rostered, but never a lineup candidate.
    taxi_ids = {p["espn_player_id"] for p in bench if "espn_player_id" in p and is_taxi(p)}

    # 5. Load projections for target_week
    proj_rows = fetch_dicts(
        con,
        """
        SELECT espn_player_id, projected_points, synced_at
        FROM espn_player_weekly_points
        WHERE season = ? AND scoring_period = ?
        """,
        [season, target_week],
    )
    projections: dict[int, float | None] = {}
    latest_proj_sync = None
    for r in proj_rows:
        pid = r["espn_player_id"]
        projections[pid] = float(r["projected_points"]) if r["projected_points"] is not None else None
        if r.get("synced_at") and (latest_proj_sync is None or r["synced_at"] > latest_proj_sync):
            latest_proj_sync = r["synced_at"]
    espn_projections = dict(projections)    # availability warnings quote ESPN's number by name
    src = weekly(con, season, target_week, source)
    origin: dict[int, str] = {}
    if source == "independent" and src["available"]:
        projections, origin = src["values"], src["origin"]
    else:
        origin = {pid: "espn" for pid, v in projections.items() if v is not None}

    # 6. Detect player locks & schedule coverage
    pro_teams = [p.get("pro_team") for p in all_roster]
    locks = detect_player_locks(con, season, target_week, pro_teams, now)

    # 7. Availability & injuries
    ir_ids = {p["espn_player_id"] for p in ir if "espn_player_id" in p}
    proj_by_week = {p["espn_player_id"]: {target_week: espn_projections.get(p["espn_player_id"])} for p in all_roster if "espn_player_id" in p}
    roster_players_for_avail = [
        RosterPlayer(
            espn_player_id=p["espn_player_id"],
            position=p.get("position"),
            pro_team=p.get("pro_team"),
            name=p.get("full_name"),
            on_ir=p["espn_player_id"] in ir_ids,
            injury_status=p.get("injury_status"),
            on_taxi=p["espn_player_id"] in taxi_ids,
        )
        for p in all_roster if "espn_player_id" in p
    ]
    avail_records = load_availability(con, season, roster_players_for_avail, (target_week,), proj_by_week)
    unavailable_ids = {
        pid for pid, rec in avail_records.items()
        if target_week in rec.get("unavailable_weeks", [])
    }

    # Collect injury warnings
    injury_warnings: list[dict] = []
    for p in all_roster:
        pid = p.get("espn_player_id")
        rec = avail_records.get(pid, {})
        inj_status = p.get("injury_status")
        if (inj_status and inj_status not in ("ACTIVE", "NORMAL")) or pid in ir_ids or pid in unavailable_ids or rec.get("warnings"):
            injury_warnings.append({
                "player_id": pid,
                "name": p.get("full_name"),
                "position": p.get("position"),
                "injury_status": inj_status,
                "on_ir": pid in ir_ids,
                "nfl_ineligible": pid in unavailable_ids,
                "warnings": rec.get("warnings", []),
            })

    # Schedule warnings
    schedule_warnings: list[str] = []
    if not verify_schedule_coverage(con, season, target_week):
        schedule_warnings.append(
            f"NFL schedule coverage for week {target_week} is incomplete. Some byes or kickoff times may be uncertain."
        )

    # 8. Slot instances & lock preservation
    roster_slots_raw = lg.get("roster_slots")
    if isinstance(roster_slots_raw, str):
        roster_slots_raw = json.loads(roster_slots_raw)
    slot_instances = build_slot_instances(roster_slots_raw or {})

    actual_assignments: list[dict] = []
    if not is_future_preview and starters:
        actual_assignments = map_actual_starters_to_slots(starters, slot_instances)
        locked_starters, remaining_slots, locked_starter_ids, locked_bench_ids = preserve_locked_assignments(
            actual_assignments, bench, locks, slot_instances
        )
    else:
        locked_starters = []
        remaining_slots = slot_instances
        locked_starter_ids = set()
        locked_bench_ids = set()

    # 9. Optimization
    opt = optimize_lock_constrained_lineup(
        roster_players=all_roster,
        slot_instances=slot_instances,
        locked_starter_assignments=locked_starters,
        locked_bench_ids=locked_bench_ids,
        unavailable_ids=unavailable_ids,
        ir_ids=ir_ids | taxi_ids,      # neither an IR nor a taxi player can start
        projections=projections,
        actual_starters=starters,
    )

    # Attach match/defense/opponent details to recommended_lineup
    actual_starter_pids = {p.get("espn_player_id") for p in starters}
    player_map = {p["espn_player_id"]: p for p in all_roster if "espn_player_id" in p}

    def enrich_lineup_row(row: dict) -> dict:
        pid = row.get("player_id")
        p = player_map.get(pid) or {}
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)
        rec = avail_records.get(pid, {})
        return {
            "slot_id": row["slot_id"],
            "slot": row["slot"],
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "pro_team": pro_team,
            "projected_points": row.get("projected_points"),
            "projection_origin": origin.get(pid) if pid is not None else None,
            "is_flex": row.get("is_flex", False),
            "is_currently_starting": pid in actual_starter_pids,
            "current_slot": p.get("lineup_slot") if pid in actual_starter_pids else ("BE" if p else None),
            "lock_status": lock.status,
            "kickoff_utc": lock.kickoff_utc.isoformat() if lock.kickoff_utc else None,
            "has_final_score": lock.has_final_score,
            "home_score": lock.home_score,
            "away_score": lock.away_score,
            "opponent": lock.opponent,
            "is_home": lock.is_home,
            "injury_status": p.get("injury_status"),
            "availability_status": rec.get("status", "available"),
        }

    enriched_recommended = [enrich_lineup_row(r) for r in opt["recommended_lineup"]]
    enriched_unconstrained = [enrich_lineup_row(r) for r in opt["unconstrained_lineup"]]

    enriched_actual = []
    for item in actual_assignments:
        slot_inst = item.get("slot")
        p = item.get("player") or {}
        pid = p.get("espn_player_id")
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)
        rec = avail_records.get(pid, {})
        enriched_actual.append({
            "slot_id": slot_inst.slot_id if slot_inst else "UNASSIGNED",
            "slot": slot_inst.slot_type if slot_inst else p.get("position"),
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "pro_team": pro_team,
            "projected_points": projections.get(pid),
            "projection_origin": origin.get(pid),
            "is_flex": slot_inst.is_flex if slot_inst else False,
            "is_currently_starting": True,
            "current_slot": p.get("lineup_slot"),
            "lock_status": lock.status,
            "kickoff_utc": lock.kickoff_utc.isoformat() if lock.kickoff_utc else None,
            "has_final_score": lock.has_final_score,
            "home_score": lock.home_score,
            "away_score": lock.away_score,
            "opponent": lock.opponent,
            "is_home": lock.is_home,
            "injury_status": p.get("injury_status"),
            "availability_status": rec.get("status", "available"),
        })

    # 10. Start/Sit decisions
    if not is_future_preview and starters:
        decisions_objs = generate_start_sit_decisions(
            actual_starters=starters,
            recommended_lineup=opt["recommended_lineup"],
            player_locks=locks,
            projections=projections,
            actual_total=opt["actual_total"],
            recommended_total=opt["recommended_total"],
            is_complete=opt["is_complete"],
            ir_ids=ir_ids,
        )
    else:
        decisions_objs = [
            StartSitDecision(
                id="future_preview_note",
                action=ACTION_NO_CHANGE,
                actionability=ACTIONABILITY_NO_CHANGE,
                start_players=[],
                bench_players=[],
                target_slot=None,
                projected_net_gain=None,
                explanation="Next-week informational preview based on current roster. Lineup decisions are not yet actionable.",
                warnings=[],
            )
        ]

    # Convert decisions to dicts
    decisions = [
        {
            "id": d.id,
            "action": d.action,
            "actionability": d.actionability,
            "start_players": [
                {
                    "id": p.get("espn_player_id"),
                    "name": p.get("full_name"),
                    "position": p.get("position"),
                    "pro_team": p.get("pro_team"),
                    "projection": projections.get(p.get("espn_player_id")),
                    "projection_origin": origin.get(p.get("espn_player_id")),
                    "lock_status": (locks.get(p.get("pro_team")) or PlayerLock(team=p.get("pro_team"), status=LOCK_UNKNOWN)).status,
                }
                for p in d.start_players
            ],
            "bench_players": [
                {
                    "id": p.get("espn_player_id"),
                    "name": p.get("full_name"),
                    "position": p.get("position"),
                    "pro_team": p.get("pro_team"),
                    "projection": projections.get(p.get("espn_player_id")),
                    "lock_status": (locks.get(p.get("pro_team")) or PlayerLock(team=p.get("pro_team"), status=LOCK_UNKNOWN)).status,
                }
                for p in d.bench_players
            ],
            "target_slot": d.target_slot,
            "projected_net_gain": d.projected_net_gain,
            "explanation": d.explanation,
            "warnings": d.warnings,
        }
        for d in decisions_objs
    ]

    # Format current bench
    bench_rows = []
    for p in bench:
        pid = p.get("espn_player_id")
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)
        bench_rows.append({
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "pro_team": pro_team,
            "projected_points": projections.get(pid),
            "projection_origin": origin.get(pid),
            "lock_status": lock.status,
            "kickoff_utc": lock.kickoff_utc.isoformat() if lock.kickoff_utc else None,
            "opponent": lock.opponent,
            "is_home": lock.is_home,
            "injury_status": p.get("injury_status"),
            "on_ir": False,
            "nfl_ineligible": pid in unavailable_ids,
            **({"on_taxi": True} if pid in taxi_ids else {}),
        })
    for p in ir:
        pid = p.get("espn_player_id")
        pro_team = p.get("pro_team")
        lock = locks.get(pro_team) or PlayerLock(team=pro_team, status=LOCK_UNKNOWN)
        bench_rows.append({
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "pro_team": pro_team,
            "projected_points": projections.get(pid),
            "projection_origin": origin.get(pid),
            "lock_status": lock.status,
            "kickoff_utc": lock.kickoff_utc.isoformat() if lock.kickoff_utc else None,
            "opponent": lock.opponent,
            "is_home": lock.is_home,
            "injury_status": p.get("injury_status"),
            "on_ir": True,
            "nfl_ineligible": pid in unavailable_ids,
        })

    # Summary status
    locked_count = sum(1 for p in all_roster if (locks.get(p.get("pro_team")) or PlayerLock(team=p.get("pro_team"), status=LOCK_UNKNOWN)).status == LOCK_LOCKED)
    all_locked = locked_count == len(all_roster) and len(all_roster) > 0

    if not opt["is_complete"]:
        summary_status = "incomplete_data"
    elif is_future_preview:
        summary_status = "future_preview"
    elif all_locked:
        summary_status = "all_locked"
    elif not decisions or decisions[0]["action"] == ACTION_NO_CHANGE or opt["projected_gain"] == 0.0:
        summary_status = "optimal"
    else:
        summary_status = "upgrade_available"

    # Freshness
    nfl_runs = fetch_dicts(con, "SELECT max(finished_at) as last_nfl FROM sync_runs WHERE source = 'nfl' AND status = 'ok'") if table_exists(con, "sync_runs") else []
    last_nfl = nfl_runs[0]["last_nfl"] if nfl_runs else None

    lineup_sync = starters[0].get("snapshot_synced_at") if starters else (bench[0].get("snapshot_synced_at") if bench else lg.get("synced_at"))

    return {
        "league": {
            "id": league_id,
            "name": lg.get("name"),
            "season": season,
            "current_week": current_week,
            "final_scoring_period": final_week,
        },
        "team": {
            "id": tid,
            "name": target_team.get("name"),
            "abbrev": target_team.get("abbrev"),
            "is_mine": bool(target_team.get("is_mine")),
        },
        "teams": [
            {
                "id": t["team_id"],
                "name": t.get("name"),
                "abbrev": t.get("abbrev"),
                "is_mine": bool(t.get("is_mine")),
            }
            for t in teams
        ],
        "scoring_period": target_week,
        "is_future_preview": is_future_preview,
        "projection_source": "Independent model" if source == "independent" and src["available"] else "ESPN",
        "projection_source_detail": describe(src),
        "summary": {
            "actual_projected_total": opt["actual_total"],
            "recommended_projected_total": opt["recommended_total"],
            "known_actual_total": opt["known_actual_total"],
            "known_recommended_total": opt["known_recommended_total"],
            "unconstrained_total": opt["unconstrained_total"],
            "known_unconstrained_total": opt["known_unconstrained_total"],
            "projected_gain": opt["projected_gain"],
            "projection_complete": opt["is_complete"],
            "missing_projections": [
                {"player_id": pid, "name": (player_map.get(pid) or {}).get("full_name"), "position": (player_map.get(pid) or {}).get("position")}
                for pid in opt["missing_projection_ids"]
            ],
            "starters_count": len(starters),
            "bench_count": len(bench),
            "ir_count": len(ir),
            "locked_players_count": locked_count,
            "status": summary_status,
        },
        "actual_lineup": enriched_actual,
        "recommended_lineup": enriched_recommended,
        "unconstrained_lineup": enriched_unconstrained,
        "decisions": decisions,
        "current_bench": bench_rows,
        "injury_warnings": injury_warnings,
        "schedule_warnings": schedule_warnings,
        "freshness": {
            "lineup_synced_at": str(lineup_sync) if lineup_sync else None,
            "projection_synced_at": str(latest_proj_sync) if latest_proj_sync else None,
            "nfl_schedule_synced_at": str(last_nfl) if last_nfl else None,
        },
        "limitations": [
            "Lock detection uses conservative NFL kickoff times and final scores from stored schedule data; exact ESPN transaction/lock acceptance cannot be guaranteed.",
            (OURS_NOTE if source == "independent" and src["available"] else
             "Projections are unmodified weekly ESPN projections (ESPN league scoring); live actual points are not blended into pregame projections."),
            "Future preview is an informational projection on current roster assets and does not represent a submitted ESPN lineup.",
        ],
    }





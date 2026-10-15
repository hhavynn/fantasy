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
from .lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, assign_lineup, eligible
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
        proj_val = projections.get(pid)
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

    actual_total = round(actual_known_total, 2) if actual_complete else None
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




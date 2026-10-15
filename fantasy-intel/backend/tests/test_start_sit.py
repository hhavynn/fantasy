"""Tests for Weekly Start/Sit Command Center analytics and API."""

from datetime import datetime, timezone
import pytest
import duckdb

from app.analytics.start_sit import (
    LOCK_LOCKED,
    LOCK_UNLOCKED,
    LOCK_UNKNOWN,
    LOCK_BYE,
    LOCK_TEAMLESS,
    PlayerLock,
    detect_player_locks,
    verify_schedule_coverage,
)

UTC = timezone.utc


@pytest.fixture
def mem_db():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE nfl_schedules (
            season INTEGER,
            week INTEGER,
            game_type VARCHAR,
            game_id VARCHAR,
            gameday VARCHAR,
            gametime VARCHAR,
            home_team VARCHAR,
            away_team VARCHAR,
            home_score INTEGER,
            away_score INTEGER
        )
    """)
    return con


def test_lock_status_past_kickoff(mem_db):
    now = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)
    # Buffalo at Kansas City, Thursday Night, kicked off at 17:00 UTC
    mem_db.execute("""
        INSERT INTO nfl_schedules VALUES
        (2026, 7, 'REG', '2026_07_BUF_KC', '2026-10-15', '13:00', 'KC', 'BUF', NULL, NULL)
    """)
    # week has 16 teams / full schedule
    for i in range(1, 16):
        mem_db.execute(f"INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', 'g{i}', '2026-10-18', '13:00', 'H{i}', 'A{i}', NULL, NULL)")

    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["KC", "BUF"], now=now)
    assert locks["KC"].status == LOCK_LOCKED
    assert locks["KC"].has_final_score is False
    assert locks["BUF"].status == LOCK_LOCKED


def test_lock_status_final_score_locks_game(mem_db):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    # Gametime is missing, but final score is present
    mem_db.execute("""
        INSERT INTO nfl_schedules VALUES
        (2026, 7, 'REG', '2026_07_MIN_DET', '2026-10-15', NULL, 'DET', 'MIN', 31, 28)
    """)
    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["DET"], now=now)
    assert locks["DET"].status == LOCK_LOCKED
    assert locks["DET"].has_final_score is True


def test_lock_status_future_kickoff_is_unlocked(mem_db):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    mem_db.execute("""
        INSERT INTO nfl_schedules VALUES
        (2026, 7, 'REG', '2026_07_GB_CHI', '2026-10-18', '13:00', 'CHI', 'GB', NULL, NULL)
    """)
    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["GB", "CHI"], now=now)
    assert locks["GB"].status == LOCK_UNLOCKED
    assert locks["GB"].kickoff_utc is not None
    assert locks["GB"].opponent == "CHI"
    assert locks["GB"].is_home is False
    assert locks["CHI"].status == LOCK_UNLOCKED
    assert locks["CHI"].opponent == "GB"
    assert locks["CHI"].is_home is True


def test_lock_status_missing_kickoff_is_unknown(mem_db):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    mem_db.execute("""
        INSERT INTO nfl_schedules VALUES
        (2026, 7, 'REG', '2026_07_DAL_PHI', NULL, NULL, 'PHI', 'DAL', NULL, NULL)
    """)
    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["DAL"], now=now)
    assert locks["DAL"].status == LOCK_UNKNOWN


def test_lock_status_confirmed_bye_vs_unconfirmed_missing_schedule(mem_db):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    # 1. When schedule table is completely empty for week 7, SF is UNKNOWN, NOT BYE
    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["SF"], now=now)
    assert locks["SF"].status == LOCK_UNKNOWN
    assert "schedule coverage" in locks["SF"].reason.lower()

    # 2. When schedule table has 14+ regular season games for week 7 (reliable coverage) and SF is not in it: SF is confirmed BYE
    for i in range(14):
        mem_db.execute(f"INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', 'g{i}', '2026-10-18', '13:00', 'T{i*2}', 'T{i*2+1}', NULL, NULL)")

    assert verify_schedule_coverage(mem_db, season=2026, week=7) is True
    locks_covered = detect_player_locks(mem_db, season=2026, week=7, pro_teams=["SF"], now=now)
    assert locks_covered["SF"].status == LOCK_BYE


def test_lock_status_teamless_player(mem_db):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    locks = detect_player_locks(mem_db, season=2026, week=7, pro_teams=[None, "FA", ""], now=now)
    assert locks[None].status == LOCK_TEAMLESS
    assert locks["FA"].status == LOCK_TEAMLESS
    assert locks[""].status == LOCK_TEAMLESS


def test_slot_preservation_and_locked_starters():
    from app.analytics.start_sit import (
        SlotInstance,
        build_slot_instances,
        map_actual_starters_to_slots,
        preserve_locked_assignments,
    )

    roster_slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1}
    slots = build_slot_instances(roster_slots)
    assert len(slots) == 9
    assert [s.slot_id for s in slots if s.slot_type == "RB"] == ["RB_0", "RB_1"]
    assert [s.slot_id for s in slots if s.slot_type == "FLEX"] == ["FLEX_0"]

    actual_starters = [
        {"espn_player_id": 1, "full_name": "Patrick Mahomes", "position": "QB", "lineup_slot": "QB", "lineup_slot_id": 0, "pro_team": "KC"},
        {"espn_player_id": 2, "full_name": "Christian McCaffrey", "position": "RB", "lineup_slot": "RB", "lineup_slot_id": 2, "pro_team": "SF"},
        {"espn_player_id": 3, "full_name": "Kyren Williams", "position": "RB", "lineup_slot": "RB", "lineup_slot_id": 2, "pro_team": "LAR"},
        {"espn_player_id": 4, "full_name": "Justin Jefferson", "position": "WR", "lineup_slot": "WR", "lineup_slot_id": 4, "pro_team": "MIN"},
        {"espn_player_id": 5, "full_name": "Amon-Ra St. Brown", "position": "WR", "lineup_slot": "WR", "lineup_slot_id": 4, "pro_team": "DET"},
        {"espn_player_id": 6, "full_name": "Travis Kelce", "position": "TE", "lineup_slot": "TE", "lineup_slot_id": 6, "pro_team": "KC"},
        {"espn_player_id": 7, "full_name": "Brian Thomas Jr.", "position": "WR", "lineup_slot": "FLEX", "lineup_slot_id": 23, "pro_team": "JAX"},
        {"espn_player_id": 8, "full_name": "Brandon Aubrey", "position": "K", "lineup_slot": "K", "lineup_slot_id": 17, "pro_team": "DAL"},
        {"espn_player_id": 9, "full_name": "San Francisco 49ers", "position": "D/ST", "lineup_slot": "D/ST", "lineup_slot_id": 16, "pro_team": "SF"},
    ]

    assignments = map_actual_starters_to_slots(actual_starters, slots)
    assert len(assignments) == 9
    # Brian Thomas Jr. is mapped to FLEX_0
    flex_starter = next(a for a in assignments if a["slot"].slot_id == "FLEX_0")
    assert flex_starter["player"]["espn_player_id"] == 7

    # Suppose Kyren Williams (LAR, RB) and Brian Thomas Jr. (JAX, FLEX) kicked off on Thursday and are LOCKED
    locks = {
        "LAR": PlayerLock(team="LAR", status=LOCK_LOCKED),
        "JAX": PlayerLock(team="JAX", status=LOCK_LOCKED),
        "KC": PlayerLock(team="KC", status=LOCK_UNLOCKED),
        "SF": PlayerLock(team="SF", status=LOCK_UNLOCKED),
        "MIN": PlayerLock(team="MIN", status=LOCK_UNLOCKED),
        "DET": PlayerLock(team="DET", status=LOCK_UNLOCKED),
        "DAL": PlayerLock(team="DAL", status=LOCK_UNLOCKED),
    }

    actual_bench = [
        {"espn_player_id": 10, "full_name": "Jameson Williams", "position": "WR", "pro_team": "DET", "lineup_slot_id": 20},
        {"espn_player_id": 11, "full_name": "Jordan Mason", "position": "RB", "pro_team": "LAR", "lineup_slot_id": 20},
    ]

    locked_starters, remaining_slots, locked_starter_ids, locked_bench_ids = preserve_locked_assignments(
        assignments, actual_bench, locks, slots
    )

    # 2 starters locked: Kyren Williams (RB_0 or RB_1) and Brian Thomas Jr. (FLEX_0)
    assert len(locked_starters) == 2
    assert locked_starter_ids == {3, 7}
    # Jordan Mason (LAR) was on the bench and LAR is locked -> locked on bench!
    assert locked_bench_ids == {11}

    # Remaining open slots should have exactly 1 RB, 2 WR, 1 QB, 1 TE, 1 K, 1 D/ST, and 0 FLEX!
    remaining_types = [s.slot_type for s in remaining_slots]
    assert remaining_types.count("FLEX") == 0
    assert remaining_types.count("RB") == 1
    assert remaining_types.count("WR") == 2
    assert len(remaining_slots) == 7


def test_constrained_optimization_invariants():
    from app.analytics.start_sit import (
        SlotInstance,
        build_slot_instances,
        optimize_lock_constrained_lineup,
    )

    roster_slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
    slots = build_slot_instances(roster_slots)

    roster = [
        {"espn_player_id": 1, "full_name": "Patrick Mahomes", "position": "QB", "pro_team": "KC"},
        {"espn_player_id": 2, "full_name": "Christian McCaffrey", "position": "RB", "pro_team": "SF"},
        {"espn_player_id": 3, "full_name": "Kyren Williams", "position": "RB", "pro_team": "LAR"},
        {"espn_player_id": 4, "full_name": "Justin Jefferson", "position": "WR", "pro_team": "MIN"},
        {"espn_player_id": 5, "full_name": "Amon-Ra St. Brown", "position": "WR", "pro_team": "DET"},
        {"espn_player_id": 6, "full_name": "Travis Kelce", "position": "TE", "pro_team": "KC"},
        {"espn_player_id": 7, "full_name": "Brian Thomas Jr.", "position": "WR", "pro_team": "JAX"},
        {"espn_player_id": 10, "full_name": "Bench High-Projecting WR", "position": "WR", "pro_team": "BUF"},
        {"espn_player_id": 11, "full_name": "Bench Locked RB", "position": "RB", "pro_team": "MIA"},
        {"espn_player_id": 12, "full_name": "IR Star RB", "position": "RB", "pro_team": "CLE"},
        {"espn_player_id": 13, "full_name": "Suspended / Ineligible WR", "position": "WR", "pro_team": "CIN"},
    ]

    projections = {
        1: 20.0,
        2: 18.0,
        3: 15.0,  # Locked starter
        4: 16.0,
        5: 14.0,  # Unlocked starter with 14.0
        6: 12.0,
        7: 11.0,  # Unlocked starter with 11.0
        10: 17.5, # Bench unlocked WR with 17.5 (should replace starter 7 in FLEX or WR)
        11: 25.0, # Bench LOCKED RB (cannot enter even though projects 25.0!)
        12: 30.0, # IR RB (cannot enter even though projects 30.0!)
        13: 22.0, # NFL-ineligible (cannot enter!)
    }

    # Locked assignment: player 3 (Kyren Williams) is locked in slot RB_1
    locked_starters = [
        {"slot": SlotInstance(slot_id="RB_1", slot_type="RB", index=1, is_flex=False),
         "player": roster[2], "player_id": 3}
    ]
    locked_bench_ids = {11}
    unavailable_ids = {13}
    ir_ids = {12}

    actual_starters = [roster[0], roster[1], roster[2], roster[3], roster[4], roster[5], roster[6]]

    res = optimize_lock_constrained_lineup(
        roster_players=roster,
        slot_instances=slots,
        locked_starter_assignments=locked_starters,
        locked_bench_ids=locked_bench_ids,
        unavailable_ids=unavailable_ids,
        ir_ids=ir_ids,
        projections=projections,
        actual_starters=actual_starters,
    )

    rec_lineup = res["recommended_lineup"]
    rec_pids = {a["player_id"] for a in rec_lineup if a["player_id"] is not None}

    # Invariants:
    # 1. Locked starter (pid 3) must be in recommended lineup in RB_1!
    rb1_assignment = next(a for a in rec_lineup if a["slot_id"] == "RB_1")
    assert rb1_assignment["player_id"] == 3

    # 2. Locked bench player (pid 11) must NOT be in recommended lineup
    assert 11 not in rec_pids

    # 3. IR player (pid 12) must NOT be in recommended lineup
    assert 12 not in rec_pids

    # 4. Ineligible player (pid 13) must NOT be in recommended lineup
    assert 13 not in rec_pids

    # 5. Unlocked high-projecting bench WR (pid 10) SHOULD be in recommended lineup
    assert 10 in rec_pids

    # 6. Actual starters total vs Recommended total:
    # Actual starters: 20 + 18 + 15 + 16 + 14 + 12 + 11 = 106.0
    # Recommended starters: 20 + 18 + 15 + 16 + 17.5 + 12 + 14 = 112.5 (10 replaces 7, net gain = 6.5)
    assert res["actual_total"] == 106.0
    assert res["recommended_total"] == 112.5
    assert res["projected_gain"] == 6.5
    assert res["is_complete"] is True


def test_decision_attribution_single_swap_and_already_optimal():
    from app.analytics.start_sit import (
        ACTION_NO_CHANGE,
        ACTION_SWAP,
        ACTIONABILITY_ACTIONABLE,
        ACTIONABILITY_LOCKED,
        ACTIONABILITY_NO_CHANGE,
        generate_start_sit_decisions,
    )

    actual_starters = [
        {"espn_player_id": 1, "full_name": "Patrick Mahomes", "position": "QB", "pro_team": "KC"},
        {"espn_player_id": 2, "full_name": "Tyler Lockett", "position": "WR", "pro_team": "SEA"},
    ]
    recommended_lineup = [
        {"slot_id": "QB_0", "slot": "QB", "player_id": 1, "player": actual_starters[0], "projected_points": 20.0},
        {"slot_id": "WR_0", "slot": "WR", "player_id": 3, "player": {"espn_player_id": 3, "full_name": "Brian Thomas Jr.", "position": "WR", "pro_team": "JAX"}, "projected_points": 14.5},
    ]

    locks = {
        "KC": PlayerLock(team="KC", status=LOCK_UNLOCKED),
        "SEA": PlayerLock(team="SEA", status=LOCK_UNLOCKED),
        "JAX": PlayerLock(team="JAX", status=LOCK_UNLOCKED),
    }
    projections = {1: 20.0, 2: 9.5, 3: 14.5}

    # 1. Single swap: 3 enters WR_0, 2 is displaced to bench
    decisions = generate_start_sit_decisions(
        actual_starters=actual_starters,
        recommended_lineup=recommended_lineup,
        player_locks=locks,
        projections=projections,
        actual_total=29.5,
        recommended_total=34.5,
        is_complete=True,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == ACTION_SWAP
    assert d.actionability == ACTIONABILITY_ACTIONABLE
    assert d.start_players[0]["espn_player_id"] == 3
    assert d.bench_players[0]["espn_player_id"] == 2
    assert d.target_slot == "WR"
    assert d.projected_net_gain == 5.0
    assert "Brian Thomas Jr." in d.explanation
    assert "Tyler Lockett" in d.explanation

    # 2. If Tyler Lockett (SEA) was already locked: actionability is LOCKED
    locks_locked = {**locks, "SEA": PlayerLock(team="SEA", status=LOCK_LOCKED)}
    decisions_locked = generate_start_sit_decisions(
        actual_starters=actual_starters,
        recommended_lineup=recommended_lineup,
        player_locks=locks_locked,
        projections=projections,
        actual_total=29.5,
        recommended_total=34.5,
        is_complete=True,
    )
    assert decisions_locked[0].actionability == ACTIONABILITY_LOCKED

    # 3. Already optimal
    optimal_rec = [
        {"slot_id": "QB_0", "slot": "QB", "player_id": 1, "player": actual_starters[0], "projected_points": 20.0},
        {"slot_id": "WR_0", "slot": "WR", "player_id": 2, "player": actual_starters[1], "projected_points": 9.5},
    ]
    decisions_optimal = generate_start_sit_decisions(
        actual_starters=actual_starters,
        recommended_lineup=optimal_rec,
        player_locks=locks,
        projections=projections,
        actual_total=29.5,
        recommended_total=29.5,
        is_complete=True,
    )
    assert len(decisions_optimal) == 1
    assert decisions_optimal[0].action == ACTION_NO_CHANGE
    assert decisions_optimal[0].actionability == ACTIONABILITY_NO_CHANGE
    assert decisions_optimal[0].projected_net_gain == 0.0


def test_decision_attribution_coordinated_cascade_no_double_counting():
    from app.analytics.start_sit import (
        ACTION_COORDINATED_SWAP,
        ACTIONABILITY_ACTIONABLE,
        generate_start_sit_decisions,
    )

    actual_starters = [
        {"espn_player_id": 1, "full_name": "RB1", "position": "RB", "pro_team": "KC"},
        {"espn_player_id": 2, "full_name": "RB2 Starter", "position": "RB", "pro_team": "SF"},
        {"espn_player_id": 3, "full_name": "WR1", "position": "WR", "pro_team": "MIN"},
        {"espn_player_id": 4, "full_name": "WR2 Starter", "position": "WR", "pro_team": "DET"},
        {"espn_player_id": 5, "full_name": "TE1 Starter (FLEX)", "position": "TE", "pro_team": "BUF"},
    ]
    # In recommended lineup:
    # RB2 and TE1 displaced; Bench RB3 and Bench WR3 enter
    recommended_lineup = [
        {"slot_id": "RB_0", "slot": "RB", "player_id": 1, "player": actual_starters[0], "projected_points": 18.0},
        {"slot_id": "RB_1", "slot": "RB", "player_id": 6, "player": {"espn_player_id": 6, "full_name": "Bench RB3", "position": "RB", "pro_team": "MIA"}, "projected_points": 16.0},
        {"slot_id": "WR_0", "slot": "WR", "player_id": 3, "player": actual_starters[2], "projected_points": 15.0},
        {"slot_id": "WR_1", "slot": "WR", "player_id": 7, "player": {"espn_player_id": 7, "full_name": "Bench WR3", "position": "WR", "pro_team": "JAX"}, "projected_points": 14.0},
        {"slot_id": "FLEX_0", "slot": "FLEX", "player_id": 4, "player": actual_starters[3], "projected_points": 12.0}, # WR2 moved to FLEX
    ]

    locks = {t: PlayerLock(team=t, status=LOCK_UNLOCKED) for t in ("KC", "SF", "MIN", "DET", "BUF", "MIA", "JAX")}
    projections = {1: 18.0, 2: 10.0, 3: 15.0, 4: 12.0, 5: 8.0, 6: 16.0, 7: 14.0}
    # Actual total = 18 + 10 + 15 + 12 + 8 = 63.0
    # Recommended total = 18 + 16 + 15 + 14 + 12 = 75.0
    # True net gain = 75 - 63 = 12.0 (NOT 6.0 + 6.0 or any arbitrary sum!)
    decisions = generate_start_sit_decisions(
        actual_starters=actual_starters,
        recommended_lineup=recommended_lineup,
        player_locks=locks,
        projections=projections,
        actual_total=63.0,
        recommended_total=75.0,
        is_complete=True,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == ACTION_COORDINATED_SWAP
    assert d.actionability == ACTIONABILITY_ACTIONABLE
    assert len(d.start_players) == 2
    assert len(d.bench_players) == 2
    assert d.projected_net_gain == 12.0
    assert "Coordinated multi-player change" in d.explanation




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


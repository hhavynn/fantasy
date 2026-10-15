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

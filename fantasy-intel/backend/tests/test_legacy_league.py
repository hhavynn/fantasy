"""analytics/legacy_league.latest_league: the one pinned single-league selector (tests/test_league_scope_audit.py)."""

import pytest

from app.analytics.legacy_league import latest_league
from app.db.connection import connect, init_db


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.duckdb")
    init_db(c)
    yield c
    c.close()


def add(con, league_id, season, synced, name="L", final=17):
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period, scoring_items,
                   settings_raw, synced_at) VALUES (?, ?, ?, 6, ?, '[]', '{}', ?)""",
                [league_id, season, name, final, synced])


def test_none_without_a_row_or_a_table(con, tmp_path):
    assert latest_league(con) is None
    bare = connect(tmp_path / "bare.duckdb")
    assert latest_league(bare) is None
    bare.close()


def test_the_most_recently_synced_row_wins(con):
    add(con, 1, 2026, "2026-10-01 00:00:00", "Old")
    add(con, 2, 2026, "2026-10-15 00:00:00", "New")
    assert latest_league(con, ["league_id", "name"]) == {"league_id": 2, "name": "New"}
    assert latest_league(con)["final_scoring_period"] == 17                     # default: every column


def test_season_filter(con):
    add(con, 1, 2025, "2026-10-20 00:00:00", final=16)
    add(con, 1, 2026, "2026-10-15 00:00:00", final=17)
    assert latest_league(con, ["season"])["season"] == 2025
    assert latest_league(con, ["final_scoring_period"], season=2026) == {"final_scoring_period": 17}
    assert latest_league(con, ["season"], season=2024) is None


@pytest.mark.parametrize("bad", [[], ["name; DROP TABLE leagues"], ["*"], ["Name"], ["league_id, season"]])
def test_columns_are_plain_identifiers(con, bad):
    with pytest.raises(ValueError):
        latest_league(con, bad)

"""The research tools as league-member pages (#289 research-tools rulings): a bounded opportunity list, no betting lines
in game splits until the owner decides, and unavailable states that speak to the reader, not to an operator. Access
(member 200, outsider 404, anonymous public 200, Projection Accuracy still owner-only): tests/test_league_scope.py,
tests/test_game_splits_scope.py and tests/test_pace_lab.py."""

import json

import duckdb
import pytest

import synthetic_splits as S
from app.analytics import research
from app.analytics.game_splits import build_game_splits, build_split_players, validate
from app.analytics.pace_lab import PaceQuery, build_pace_lab
from app.analytics.research import build_opportunity
from app.db.connection import init_db
from test_research import SEASON, seeded  # noqa: F401  (fixture)

OPERATOR = ("scripts/", "sync_nfl", "Run ")


def test_the_opportunity_list_is_capped_after_percentiles_and_the_cut_is_counted(seeded, monkeypatch):  # noqa: F811
    full = build_opportunity(seeded, SEASON, "WR", min_games=1, min_volume=0)
    assert len(full["players"]) == 3 and "limit" not in full["pool"]["excluded"]
    monkeypatch.setattr(research, "MAX_OPPORTUNITY_PLAYERS", 2)
    cut = build_opportunity(seeded, SEASON, "WR", min_games=1, min_volume=0)
    assert [p["gsis_id"] for p in cut["players"]] == [p["gsis_id"] for p in full["players"][:2]]   # highest xFP/G
    assert cut["pool"]["excluded"]["limit"] == 1 and cut["pool"]["shown"] == 2
    assert cut["pool"]["at_position"] == full["pool"]["at_position"]
    assert [p["percentiles"] for p in cut["players"]] == [p["percentiles"] for p in full["players"][:2]]


@pytest.fixture
def splits():
    c = duckdb.connect(":memory:")
    init_db(c)
    S.write(c)
    # a stored betting line for every team-game of the world: game splits must not read it
    c.execute("""INSERT INTO team_game_lines (season, week, game_id, team, opponent, is_home, spread_line, total_line,
                                              implied_total)
                 SELECT season, week, game_id, home_team, away_team, true, 3.0, 45.0, 24.0 FROM nfl_schedules""")
    yield c
    c.close()


def test_game_splits_never_return_betting_lines(splits):
    out = build_game_splits(splits, validate(S.SUBJECT, S.WR2, S.SEASON, None, None, None, None, S.SEASON))
    assert out["status"] in ("ok", "low_n")
    text = json.dumps(out)
    assert "implied_total" not in text
    for group in out["groups"].values():
        assert "implied_total_pg" not in group["environment"]
        assert "implied_total_pg" not in group["environment"]["coverage"]


def test_unavailable_states_speak_to_the_reader():
    con = duckdb.connect(":memory:")
    init_db(con)
    messages = [build_opportunity(con, None, "WR")["message"], build_split_players(con, None)["message"],
                build_pace_lab(con, PaceQuery())["message"]]
    con.close()
    for m in messages:
        assert m and not any(word in m for word in OPERATOR), m

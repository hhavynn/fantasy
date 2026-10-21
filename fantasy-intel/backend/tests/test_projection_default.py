"""The league's default projection source (multi-league Phase 4b): ESPN's for a league with provider projections,
Ours for one without (a Sleeper league), read by every engine through projection_source."""

import pytest

from app.analytics import projection_source as ps
from app.analytics.projections import build_ros_projections
from app.analytics.trade_data import load_league_state

from test_independent_projections import live  # noqa: F401  (fixture)
from test_projection_source import WEEKLY_SHA, _ifp2, _store


@pytest.fixture
def no_provider(live):  # noqa: F811
    """The synthetic league as a Sleeper league looks: marked as Sleeper, no provider projections, one stored Ours
    run for week 6."""
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    con.execute("UPDATE espn_player_weekly_points SET projected_points = NULL")
    con.execute("""UPDATE leagues SET settings_raw = '{"provider": "sleeper"}'""")
    return con


def test_an_espn_league_keeps_espn_and_a_sleeper_league_gets_ours(live, no_provider):  # noqa: F811
    con = no_provider
    assert ps.default_source(con) == "independent"
    assert ps.normalize(None, con) == "independent" and ps.normalize("espn", con) == "espn"
    con.execute("UPDATE leagues SET settings_raw = '{}'")
    assert ps.default_source(con) == "espn"        # an ESPN league, even before its projections sync
    assert ps.normalize(None) == "espn"            # no connection: the historical default


def test_league_projections_equal_what_the_weekly_switch_shows(no_provider):
    con = no_provider
    got = ps.league_projections(con, 2026, [6], "independent")
    want = ps.weekly(con, 2026, 6, "independent")
    assert {pid: w[6][0] for pid, w in got.items()} == {pid: v for pid, v in want["values"].items() if v is not None}
    assert all(w[6][1] == want["run"]["created_at"] for w in got.values())
    assert ps.league_projections(con, 2026, [6], "espn") == {}


def test_the_trade_state_carries_ours_and_says_so(no_provider):
    con = no_provider
    state = load_league_state(con)
    if state is None:
        pytest.skip("the synthetic league has no trade window")
    assert state.projection_source == "independent"
    week6 = {pid: w[6] for pid, w in state.projections.items() if 6 in w}
    assert week6 and week6 == {pid: v for pid, v in ps.weekly(con, 2026, 6, "independent")["values"].items()
                                if v is not None and pid in week6}


def test_ros_rows_come_from_ours_and_are_labeled(no_provider):
    con = no_provider
    out = build_ros_projections(con, 2026)
    if "skipped" in out:
        pytest.skip(out["skipped"])
    labels = {r[0] for r in con.execute("SELECT DISTINCT projection_source FROM player_ros_projections").fetchall()}
    assert labels <= {"Ours", None}


def test_a_defense_keeps_its_latest_projection_in_later_weeks_it_plays_and_none_on_a_bye(tmp_path):
    """The D/ST model projects the coming week only: later weeks carry it flat where the team plays."""
    import duckdb

    con = duckdb.connect(str(tmp_path / "d.duckdb"))
    con.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT, pro_team VARCHAR)")
    con.execute("INSERT INTO fantasy_players VALUES (-16012, 'KC'), (101, 'KC')")
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 3, 'REG', 'KC', 'BUF'), (2026, 5, 'REG', 'DEN', 'KC')")
    out = {-16012: {3: (7.1, "t")}, 101: {3: (10.0, "t")}}
    ps._carry_dst_forward(con, 2026, [3, 4, 5], out)
    assert out[-16012] == {3: (7.1, "t"), 5: (7.1, "t")}          # week 4 is KC's bye
    assert out[101] == {3: (10.0, "t")}                             # players are never carried

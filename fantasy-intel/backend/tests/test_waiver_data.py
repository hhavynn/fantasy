"""Stored-data adapter and end-to-end recommendation consistency."""
import json
from types import SimpleNamespace

from app.analytics.waiver_data import select_candidates, load_waiver_context, comparable_free_agent
from app.analytics.waivers import PlayerValue
from datetime import datetime, timedelta, timezone
from app.analytics.waivers import build_waivers
from app.db.connection import connect, fetch_dicts
from app.espn.sync import run_espn_sync
from app.db.connection import open_db
from espn_fixture import FakeEspnClient, MY_SWID, SEASON


def test_low_owned_breakout_has_a_projection_and_role_lane():
    pool = [dict(espn_player_id=i, position="RB", percent_owned=90, full_name=f"Veteran{i}") for i in range(1, 400)]
    pool.append(dict(espn_player_id=900, position="RB", percent_owned=0, gsis_id="rookie", full_name="Breakout"))
    selected, coverage = select_candidates(pool, {}, {900: {7: 18}}, {"rookie": {"rank": 1}}, [7])
    assert 900 in {p["espn_player_id"] for p in selected}
    assert len(selected) <= 60 and coverage["eligible"] == 400
    assert coverage["excluded_by_limit"] == 400 - len(selected)


def test_fixture_pipeline_has_explicit_move_and_cost_diagnostics(tmp_path):
    path = tmp_path / "waivers.duckdb"
    run_espn_sync(lambda: open_db(path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(path)
    before = fetch_dicts(con, "SELECT * FROM player_scores")
    stats = build_waivers(con)
    assert stats["coverage"]["evaluated"] == stats["candidates"] > 0
    rows = fetch_dicts(con, "SELECT * FROM waiver_recommendations")
    for row in rows:
        d = json.loads(row["components"])["decision"]
        assert d["version"] == 3 and d["roster_move"]["status"]
        if d["actionable"]:
            assert d["roster_move"]["status"] in ("ADD_WITHOUT_DROP", "ADD_WITH_DROP")
        if d["roster_move"]["status"] == "ADD_WITHOUT_DROP":
            assert row["suggested_drop_id"] is None
        if row["availability_status"] == "FREEAGENT":
            assert d["resources"]["kind"] == "FREE_AGENT"
    assert fetch_dicts(con, "SELECT * FROM player_scores") == before
    con.execute("UPDATE leagues SET current_week=18, final_scoring_period=17")
    ctx = load_waiver_context(con, fetch_dicts(con, "SELECT * FROM leagues")[0])
    assert ctx["actionable"] is False and ctx["weeks"] == []
    build_waivers(con)
    assert all(json.loads(r["components"])["decision"]["action"] == "SEASON_COMPLETE"
               for r in fetch_dicts(con, "SELECT components FROM waiver_recommendations"))
    con.close()


def test_priority_alternative_must_be_healthy_current_and_comparable():
    now = datetime.now(timezone.utc)
    candidate = dict(espn_player_id=99, position="RB")
    pool = [dict(espn_player_id=i, position="RB", full_name=f"FA{i}", availability_status="FREEAGENT",
                 injury_status=injury, detail_synced_at=stamp) for i, injury, stamp in
            [(1, "OUT", now), (2, "ACTIVE", now - timedelta(days=7)), (3, "ACTIVE", now)]]
    vals = {i: PlayerValue(i, "RB", 8, 8, 8, None, None, "xfp") for i in [1, 2, 3, 99]}
    ctx = {"now": now, "weeks": [], "projections": {}, "games": {}, "covered": set()}
    assert comparable_free_agent(candidate, vals, pool, ctx)["espn_player_id"] == 3


def test_stale_and_unknown_injury_do_not_create_claims(tmp_path):
    path = tmp_path / "stale.duckdb"
    run_espn_sync(lambda: open_db(path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(path)
    con.execute("UPDATE fantasy_players SET detail_synced_at = TIMESTAMP '2020-01-01', injury_status=NULL")
    build_waivers(con)
    for row in fetch_dicts(con, "SELECT components FROM waiver_recommendations"):
        d = json.loads(row["components"])["decision"]
        assert not d["actionable"] and any("unknown" in r.lower() for r in d["risks"])
    con.close()


def test_stale_roster_and_stale_projections_cannot_establish_a_move(tmp_path):
    path = tmp_path / "snapshots.duckdb"
    run_espn_sync(lambda: open_db(path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(path)
    con.execute("UPDATE lineup_snapshots SET synced_at=TIMESTAMP '2020-01-01'")
    con.execute("UPDATE espn_player_weekly_points SET synced_at=TIMESTAMP '2020-01-01'")
    ctx = load_waiver_context(con, fetch_dicts(con, "SELECT * FROM leagues")[0])
    assert not ctx["projections"] and ctx["stale_projection_count"] > 0
    build_waivers(con)
    for row in fetch_dicts(con, "SELECT components FROM waiver_recommendations"):
        d = json.loads(row["components"])["decision"]
        assert d["roster_move"]["status"] == "UNKNOWN" and not d["actionable"]
    con.close()


def test_malformed_kickoff_is_unknown_not_a_failed_build(tmp_path):
    path = tmp_path / "kickoff.duckdb"
    run_espn_sync(lambda: open_db(path), SimpleNamespace(swid=MY_SWID, my_team_id=None),
                  client=FakeEspnClient(), save_raw_payloads=False)
    con = connect(path)
    league = fetch_dicts(con, "SELECT * FROM leagues")[0]
    con.execute("CREATE TABLE nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, home_team VARCHAR, away_team VARCHAR, gameday VARCHAR, gametime VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', 'KC', 'NYJ', 'bad-date', 'bad-time')", [league["season"], league["current_week"]])
    ctx = load_waiver_context(con, league)
    assert ctx["actionable"] is None and any("kickoff" in r.lower() for r in ctx["warnings"])
    assert build_waivers(con)["candidates"] > 0
    con.close()

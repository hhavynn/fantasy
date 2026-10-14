"""Forward ESPN projections: fetch window, storage, stale-data semantics, coverage, byes,
ROS summary, projection history, score isolation and the v5 migration. Synthetic data only;
nothing here talks to ESPN."""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analytics.pipeline import run_analytics
from app.analytics.projections import (LeagueWindow, build_ros_projections, forward_weeks, last_regular_season_week,
                                       player_forward, summarize)
from app.db.connection import SCHEMA_VERSION, connect, fetch_dicts, init_db, open_db, table_exists, upsert_rows
from app.espn import parse
from app.espn import sync as sync_mod
from app.espn.sync import fetch_all, projection_weeks, run_espn_sync, write_all

from test_external_usage import SEASON as NFL_SEASON, _snapshot, nfl  # noqa: F401  (nfl is a fixture)
from espn_fixture import (CURRENT, MY_SWID, ROSTERS, SEASON, FakeEspnClient, league_payload, player_obj,
                          projection_payload, projection_value, roster_payload)

FINAL = 17
WEEKS = list(range(CURRENT, FINAL + 1))
ROSTERED = {pid for entries in ROSTERS.values() for pid, _ in entries}
FAS = {209, 309, 405, 105}


def sync(con, client=None):
    return write_all(con, fetch_all(client or FakeEspnClient()), swid=MY_SWID, my_team_id=None)


def proj(con, pid, week):
    row = con.execute("SELECT actual_points, projected_points FROM espn_player_weekly_points "
                      "WHERE season = ? AND espn_player_id = ? AND scoring_period = ?", [SEASON, pid, week]).fetchone()
    return row  # None = no row at all


def ros(con, pid):
    rows = fetch_dicts(con, "SELECT * FROM player_ros_projections WHERE season = ? AND espn_player_id = ?", [SEASON, pid])
    if not rows:
        return None
    return {k: json.loads(v) if k in ("bye_weeks", "missing_weeks") else v for k, v in rows[0].items()}


def add_schedule(con, byes: dict[str, int]):
    """Every fixture team plays every week 1..18 except its bye. KC=12, BUF=2, DEN=8, PHI=21, ..."""
    teams = ["KC", "BUF", "DEN", "PHI", "SF", "SEA", "TB", "WAS", "NYJ", "MIA"]
    con.execute("""CREATE TABLE IF NOT EXISTS nfl_schedules (season INT, week INT, game_type VARCHAR, game_id VARCHAR,
                   home_team VARCHAR, away_team VARCHAR, spread_line DOUBLE, total_line DOUBLE)""")
    for week in range(1, 19):
        playing = [t for t in teams if byes.get(t) != week]
        for i in range(0, len(playing) - 1, 2):
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, ?, ?, NULL, NULL)",
                        [SEASON, week, f"g{week}{i}", playing[i], playing[i + 1]])


# ------------------------------------------------------------------ fetch window --

def test_future_weeks_fetched_current_through_final_fantasy_period():
    client = FakeEspnClient()
    p = fetch_all(client)
    assert client.projection_requests == WEEKS
    assert p.projection_weeks == WEEKS and sorted(p.projections) == WEEKS


def test_league_final_period_respected_not_nfl_week_18():
    client = FakeEspnClient()
    lg = league_payload()
    lg["status"]["finalScoringPeriod"] = 16
    client.league = lambda: lg
    fetch_all(client)
    assert client.projection_requests == list(range(CURRENT, 17))
    assert 18 not in client.projection_requests and 17 not in client.projection_requests
    assert projection_weeks(6, 17) == list(range(6, 18))
    assert projection_weeks(17, 17) == [17]
    assert projection_weeks(6, None) == []


def test_unknown_final_period_fetches_nothing_and_warns():
    client = FakeEspnClient()
    lg = league_payload()
    del lg["status"]["finalScoringPeriod"]
    client.league = lambda: lg
    p = fetch_all(client)
    assert client.projection_requests == []
    assert any("final scoring period unknown" in w for w in p.warnings)


# ------------------------------------------------------------------------ storage --

def test_rostered_and_free_agent_projections_persisted_for_future_weeks(con):
    stats = sync(con)
    for pid in ROSTERED | FAS:
        assert proj(con, pid, 10) == (None, projection_value(pid, 10)), pid
    cov = {c["week"]: c for c in stats["projections"]["coverage"]}
    assert sorted(cov) == WEEKS
    n = len(ROSTERED) + len(FAS)
    assert (cov[10]["espn_total"], cov[10]["returned"], cov[10]["returned_with_projection"]) == (n, n, n)
    assert cov[10]["complete"] and cov[10]["reason"] is None
    assert cov[10]["rostered"] == {"returned": len(ROSTERED), "with_projection": len(ROSTERED), "without_projection": 0,
                                   "zero": 0, "rostered": len(ROSTERED), "expected": len(ROSTERED),
                                   "expected_returned": len(ROSTERED), "expected_with_projection": len(ROSTERED),
                                   "omitted": 0}
    assert cov[10]["available"] == {"returned": len(FAS), "with_projection": len(FAS), "without_projection": 0, "zero": 0}
    assert "expected" not in cov[10]["available"]          # no free-agent universe is manufactured
    assert stats["projections"]["weeks_complete"] == WEEKS and stats["projections"]["weeks_failed"] == []


def test_zero_projection_stays_zero_and_missing_stays_null(con):
    client = FakeEspnClient(projections={5: projection_payload(5, values={201: 0.0}, missing=(202,), exclude=(203,))})
    stats = sync(con, client)
    assert proj(con, 201, 5) == (None, 0.0)        # measured zero
    assert proj(con, 202, 5) is None               # returned without a projection: no row = unknown
    assert proj(con, 203, 5) is None               # not returned at all: unknown
    cov = next(c for c in stats["projections"]["coverage"] if c["week"] == 5)
    assert cov["zero"] == 1 and cov["returned_without_projection"] == 1
    assert cov["rostered"]["zero"] == 1 and cov["rostered"]["without_projection"] == 1
    assert cov["rostered"]["omitted"] == 1 and cov["rostered"]["expected_with_projection"] == len(ROSTERED) - 2
    assert not cov["complete"] and "expected rostered" in cov["reason"]   # 203 has an NFL team but was omitted


def test_other_periods_and_season_splits_in_a_week_payload_are_ignored():
    out = parse.parse_projection_week(projection_payload(7, extra_periods=True), season=SEASON, scoring_period=7,
                                      synced_at=datetime(2026, 1, 1), rostered_ids=ROSTERED,
                                      expected_rostered_ids=ROSTERED, limit=3000)
    assert {r["scoring_period"] for r in out["rows"]} == {7}
    assert all(r["projected_points"] != 99.0 and r["projected_points"] != 500.0 for r in out["rows"])


def test_future_projection_never_overwrites_actual_points(con):
    sync(con)
    con.execute("UPDATE espn_player_weekly_points SET actual_points = 12.0 WHERE espn_player_id = 201 "
                "AND scoring_period = 5")   # e.g. an actual that is already known
    client = FakeEspnClient(projections={5: projection_payload(5, values={201: 3.0})})
    sync(con, client)
    assert proj(con, 201, 5) == (12.0, 3.0)
    # past actuals (from player cards) are untouched by the forward fetch
    assert proj(con, 201, 1)[0] == 9.0


def test_current_week_roster_and_pool_merge_with_projection_query(con):
    current_roster = roster_payload(CURRENT)
    for t in current_roster["teams"]:
        for e in t["roster"]["entries"]:
            if e["playerId"] == 201:
                e["playerPoolEntry"]["player"] = player_obj(201, CURRENT, 7.0)   # actual 7.0, roster projection 8.0
    pool = {"players": [{"id": 209, "onTeamId": 0, "status": "FREEAGENT", "player": {
        **player_obj(209), "stats": [{"seasonId": SEASON, "scoringPeriodId": CURRENT, "statSourceId": 1,
                                      "statSplitTypeId": 1, "appliedTotal": 11.5}]}}]}
    client = FakeEspnClient(projections={CURRENT: projection_payload(CURRENT, values={201: 9.5}, exclude=(209,))})
    client.roster_for_period = lambda period: current_roster if period == CURRENT else roster_payload(period)
    client.player_pool = lambda period, limit=500: pool
    sync(con, client)
    assert proj(con, 201, CURRENT) == (7.0, 9.5)   # roster actual kept; projection-query value applied
    assert proj(con, 209, CURRENT) == (None, 11.5)  # pool projection kept although the week was replaced
    assert proj(con, 202, CURRENT) == (None, projection_value(202, CURRENT))


# ------------------------------------------------------------- stale-data semantics --

def test_stale_future_projection_cleared_after_successful_complete_week_refresh(con):
    sync(con)
    assert proj(con, 209, 10) is not None and proj(con, 201, 10)[1] is not None
    # ESPN's own player count says the universe is now one smaller, and the response matches it
    stats = sync(con, FakeEspnClient(projections={10: projection_payload(10, exclude=(209,), missing=(201,))}))
    assert 10 in stats["projections"]["weeks_complete"]
    assert proj(con, 209, 10) is None          # proven-complete universe no longer has him: no stale value
    assert proj(con, 201, 10) is None          # returned without a projection: unknown, not the old number
    assert proj(con, 209, 11) == (None, projection_value(209, 11))   # other weeks unaffected


def test_stale_projection_preserved_when_that_weeks_fetch_fails(con):
    sync(con)
    before = proj(con, 201, 10)
    stats = sync(con, FakeEspnClient(projections={10: ConnectionError("ESPN timeout")}))
    assert proj(con, 201, 10) == before and proj(con, 209, 10) is not None
    assert stats["projections"]["weeks_failed"] == [10]
    assert any("ESPN projections week 10 failed" in w for w in stats["warnings"])


def test_one_failed_week_does_not_destroy_other_weeks(con):
    sync(con)
    w10 = {pid: proj(con, pid, 10) for pid in ROSTERED | FAS}
    stats = sync(con, FakeEspnClient(projections={10: RuntimeError("500"),
                                                  11: projection_payload(11, values={201: 1.25})}))
    assert {pid: proj(con, pid, 10) for pid in ROSTERED | FAS} == w10
    assert proj(con, 201, 11) == (None, 1.25)
    assert 11 in stats["projections"]["weeks_complete"] and 10 not in stats["projections"]["weeks_complete"]


def history(con, pid, week):
    return con.execute("SELECT projected_points FROM espn_projection_history WHERE espn_player_id = ? "
                       "AND scoring_period = ? AND snapshot_date = ?", [pid, week, date.today()]).fetchall()


def test_few_free_agents_with_all_rostered_preserves_omitted_fas(con):
    """ESPN's count covers the whole universe but only the rostered players and one FA came back:
    the response is not proven complete, so omitted FAs keep their projection."""
    sync(con)
    short = projection_payload(10, exclude=(309, 405, 105), values={209: 2.0}, total="universe")
    stats = sync(con, FakeEspnClient(projections={10: short}))
    for pid in (309, 405, 105):
        assert proj(con, pid, 10) == (None, projection_value(pid, 10)), pid      # preserved, not erased
    assert proj(con, 209, 10) == (None, 2.0)                                     # returned value applied
    assert proj(con, 201, 10) == (None, projection_value(201, 10))
    assert stats["projections"]["weeks_unverified"] == [10]
    cov = next(c for c in stats["projections"]["coverage"] if c["week"] == 10)
    assert (cov["espn_total"], cov["returned"], cov["complete"]) == (len(ROSTERED) + len(FAS), len(ROSTERED) + 1, False)
    assert cov["reason"] == f"returned {len(ROSTERED) + 1} of ESPN's {len(ROSTERED) + len(FAS)} matching players"
    assert any("week 10: universe not verified" in w and "omitted players keep" in w for w in stats["warnings"])


def test_few_free_agents_does_not_delete_omitted_fa_history(con):
    sync(con)
    assert history(con, 309, 10) == [(projection_value(309, 10),)]
    sync(con, FakeEspnClient(projections={10: projection_payload(10, exclude=(309,), total="universe")}))
    assert history(con, 309, 10) == [(projection_value(309, 10),)]              # today's snapshot kept


def test_missing_count_header_never_clears_omitted_players(con):
    sync(con)
    stats = sync(con, FakeEspnClient(projections={10: projection_payload(10, exclude=(309, 202), total=None)}))
    assert proj(con, 309, 10) == (None, projection_value(309, 10))
    assert proj(con, 202, 10) == (None, projection_value(202, 10))
    cov = next(c for c in stats["projections"]["coverage"] if c["week"] == 10)
    assert cov["espn_total"] is None and "header missing" in cov["reason"]


def test_returned_player_without_projection_becomes_null_even_when_unverified(con):
    sync(con)
    short = projection_payload(10, exclude=(405,), missing=(201, 209), total="universe")
    stats = sync(con, FakeEspnClient(projections={10: short}))
    assert stats["projections"]["weeks_unverified"] == [10]
    assert proj(con, 201, 10) is None          # rostered, returned, projection removed -> unknown
    assert proj(con, 209, 10) is None          # FA explicitly returned without a projection -> unknown
    assert history(con, 209, 10) == []         # today's snapshot follows the returned state
    assert proj(con, 405, 10) == (None, projection_value(405, 10))               # omitted -> kept
    assert history(con, 405, 10) == [(projection_value(405, 10),)]


def test_zero_projection_kept_as_measured_zero_in_unverified_response(con):
    sync(con)
    sync(con, FakeEspnClient(projections={10: projection_payload(10, exclude=(405,), values={209: 0.0},
                                                                 total="universe")}))
    assert proj(con, 209, 10) == (None, 0.0) and history(con, 209, 10) == [(0.0,)]


def test_truncated_response_preserves_omitted_players(con, monkeypatch):
    sync(con)
    page = projection_payload(10, exclude=(105,), values={209: 3.0}, total="universe")
    monkeypatch.setattr(sync_mod, "PROJECTION_LIMIT", len(page["players"]))      # response filled the page
    stats = sync(con, FakeEspnClient(projections={10: page}))
    cov = next(c for c in stats["projections"]["coverage"] if c["week"] == 10)
    assert cov["truncated"] and not cov["complete"]
    assert proj(con, 105, 10) == (None, projection_value(105, 10))
    assert proj(con, 209, 10) == (None, 3.0)


def _teamless(client, pid):
    """Make a rostered player a released NFL free agent (proTeamId 0) in every ESPN payload."""
    def fix(payload):
        for t in payload.get("teams", []):
            for e in t["roster"]["entries"]:
                if e["playerId"] == pid:
                    e["playerPoolEntry"]["player"]["proTeamId"] = 0
        return payload
    base_roster, base_league, base_pro = client.roster_for_period, client.league, client.pro_players
    client.roster_for_period = lambda period: fix(base_roster(period))
    client.league = lambda: fix(base_league())
    client.pro_players = lambda: [{**p, "proTeamId": 0} if p["id"] == pid else p for p in base_pro()]
    return client


def test_teamless_rostered_player_is_not_expected_so_real_shaped_response_is_complete(con):
    # Los Subies shape: every rostered player returned, one rostered player has no NFL team and no
    # projection, ESPN's count matches the response -> complete.
    client = _teamless(FakeEspnClient(projections={w: projection_payload(w, missing=(207,)) for w in WEEKS}), 207)
    stats = sync(con, client)
    assert stats["projections"]["weeks_complete"] == WEEKS and stats["projections"]["weeks_unverified"] == []
    cov = next(c for c in stats["projections"]["coverage"] if c["week"] == 10)
    assert cov["rostered"]["expected"] == len(ROSTERED) - 1 and cov["rostered"]["rostered"] == len(ROSTERED)
    assert cov["rostered"]["expected_with_projection"] == len(ROSTERED) - 1
    # teamless player omitted entirely: still complete (absence is legitimate)
    client = _teamless(FakeEspnClient(projections={10: projection_payload(10, exclude=(207,))}), 207)
    assert 10 in sync(con, client)["projections"]["weeks_complete"]
    # a rostered player WITH an NFL team omitted: not proven complete
    stats = sync(con, FakeEspnClient(projections={10: projection_payload(10, exclude=(208,))}))
    assert 10 in stats["projections"]["weeks_unverified"]
    assert proj(con, 208, 10) == (None, projection_value(208, 10))


def test_projection_eligible_excludes_only_known_teamless_players():
    from app.espn.sync import projection_eligible
    players = [{"espn_player_id": 1, "pro_team_id": 12}, {"espn_player_id": 2, "pro_team_id": 0},
               {"espn_player_id": 3, "pro_team_id": None}]
    assert projection_eligible({1, 2, 3, 4}, players) == {1, 4}   # 4: unknown player stays expected


def test_truncated_response_is_not_complete():
    payload = projection_payload(10)
    n = len(payload["players"])
    out = parse.parse_projection_week(payload, season=SEASON, scoring_period=10, synced_at=datetime(2026, 1, 1),
                                      rostered_ids=ROSTERED, expected_rostered_ids=ROSTERED, limit=n)
    assert out["coverage"]["truncated"] and not out["complete"]


# ------------------------------------------------------------------- byes / ROS --

def test_bye_detected_from_schedule_and_non_bye_zero_stays_zero(con):
    add_schedule(con, {"KC": 10, "BUF": 7})
    client = FakeEspnClient(projections={10: projection_payload(10, values={201: 0.0}),
                                         11: projection_payload(11, values={201: 0.0})})
    sync(con, client)
    r = ros(con, 201)                       # Echo RB, KC
    assert r["bye_weeks_remaining"] == 1 and r["bye_weeks"] == [10]
    weeks = {w["week"]: w for w in player_forward(con, SEASON, 201, "KC")["weeks"]}
    assert weeks[10]["on_bye"] is True and weeks[10]["opponent"] is None and weeks[10]["espn_projected_points"] == 0.0
    assert weeks[11]["on_bye"] is False and weeks[11]["opponent"] is not None
    assert weeks[11]["espn_projected_points"] == 0.0 and weeks[11]["projection_available"]   # not rewritten
    assert r["ros_projected_points"] is not None                                             # zeros count as available
    assert ros(con, 202)["bye_weeks"] == [7]         # BUF bye comes from the schedule only


def test_bye_unknown_without_schedule():
    window = LeagueWindow(SEASON, 6, 8, 7)
    weeks = forward_weeks(window, "KC", {6: 1.0, 7: 2.0, 8: 3.0}, {}, set())
    assert [w["on_bye"] for w in weeks] == [None, None, None]
    assert summarize(window, weeks)["bye_weeks_remaining"] is None


def test_ros_total_with_full_coverage(con):
    sync(con)
    r = ros(con, 201)
    assert r["ros_projected_points"] == pytest.approx(sum(projection_value(201, w) for w in WEEKS))
    assert (r["ros_weeks_expected"], r["ros_weeks_available"], r["ros_coverage"]) == (len(WEEKS), len(WEEKS), 1.0)
    assert r["current_week_projection"] == projection_value(201, CURRENT)
    assert r["projection_source"] == "ESPN" and (r["as_of_week"], r["final_week"]) == (CURRENT, FINAL)


def test_incomplete_ros_coverage_is_surfaced_not_zero_filled(con):
    client = FakeEspnClient(projections={5: projection_payload(5, missing=(202,)),
                                         6: projection_payload(6, missing=(202,))})
    sync(con, client)
    r = ros(con, 202)
    assert (r["ros_weeks_expected"], r["ros_weeks_available"]) == (15, 13)
    assert r["ros_coverage"] == pytest.approx(13 / 15, abs=1e-4)
    assert r["ros_projected_points"] is None                      # not an artificially low total
    assert r["regular_season_projected_points"] is None           # weeks 5-6 are regular season
    assert r["playoff_projected_points"] == pytest.approx(sum(projection_value(202, w) for w in (15, 16, 17)))
    assert r["missing_weeks"] == [5, 6]


def test_regular_season_playoff_split_follows_league_settings(con):
    sync(con)   # fixture: 14 regular-season matchups, final period 17
    r = ros(con, 201)
    assert r["last_regular_season_week"] == 14
    assert (r["regular_season_weeks_expected"], r["playoff_weeks_expected"]) == (12, 3)
    assert r["regular_season_projected_points"] == pytest.approx(sum(projection_value(201, w) for w in range(3, 15)))
    assert r["playoff_projected_points"] == pytest.approx(sum(projection_value(201, w) for w in (15, 16, 17)))

    client = FakeEspnClient()
    lg = league_payload()
    lg["settings"]["scheduleSettings"]["matchupPeriodCount"] = 13
    client.league = lambda: lg
    sync(con, client)
    r = ros(con, 201)
    assert (r["last_regular_season_week"], r["regular_season_weeks_expected"], r["playoff_weeks_expected"]) == (13, 11, 4)


def test_last_regular_season_week_uses_matchup_period_map():
    two_week = {"matchupPeriodLength": 1, "matchupPeriods": {str(m): [m] for m in range(1, 13)} | {"13": [13, 14]}}
    assert last_regular_season_week(13, 17, two_week) == 14
    assert last_regular_season_week(14, 17, {"matchupPeriodLength": 1}) == 14
    assert last_regular_season_week(14, 17, {}) == 14
    assert last_regular_season_week(7, 17, {"matchupPeriodLength": 2}) is None   # can't map without the table
    assert last_regular_season_week(None, 17, {}) is None


def test_split_unknown_leaves_split_totals_null():
    window = LeagueWindow(SEASON, 6, 8, None)
    s = summarize(window, forward_weeks(window, None, {6: 1.0, 7: 2.0, 8: 3.0}, {}, set()))
    assert s["ros_projected_points"] == 6.0
    assert s["regular_season_projected_points"] is None and s["playoff_projected_points"] is None


def test_rostered_player_without_any_projection_still_gets_a_row(con):
    client = FakeEspnClient(projections={w: projection_payload(w, missing=(401,)) for w in WEEKS})
    stats = sync(con, client)
    r = ros(con, 401)
    assert r["ros_weeks_available"] == 0 and r["ros_coverage"] == 0 and r["ros_projected_points"] is None
    assert stats["ros_projections"]["rostered_incomplete"] >= 1


# -------------------------------------------------------------------- history --

def test_daily_history_snapshot_created_and_same_day_sync_updates(con):
    stats = sync(con, FakeEspnClient(projections={5: projection_payload(5, values={201: 0.0}, missing=(202,))}))
    n = con.execute("SELECT count(*) FROM espn_projection_history").fetchone()[0]
    assert n == stats["projections"]["projection_rows"] == stats["projections"]["history_rows"]
    today = date.today()
    h = dict(con.execute("SELECT espn_player_id, projected_points FROM espn_projection_history "
                         "WHERE snapshot_date = ? AND scoring_period = 5", [today]).fetchall())
    assert h[201] == 0.0 and 202 not in h        # 0 kept; missing not invented

    sync(con, FakeEspnClient(projections={5: projection_payload(5, values={201: 4.5}, exclude=(209,))}))
    assert con.execute("SELECT count(*) FROM espn_projection_history").fetchone()[0] == n   # 202 back, 209 gone
    h = dict(con.execute("SELECT espn_player_id, projected_points FROM espn_projection_history "
                         "WHERE snapshot_date = ? AND scoring_period = 5", [today]).fetchall())
    assert h[201] == 4.5 and 209 not in h and 202 in h


def test_history_keeps_one_snapshot_per_day(con, monkeypatch):
    sync(con)
    n = con.execute("SELECT count(*) FROM espn_projection_history").fetchone()[0]

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date(2099, 1, 2)
    monkeypatch.setattr(sync_mod, "date", Tomorrow)
    sync(con)
    assert con.execute("SELECT count(DISTINCT snapshot_date), count(*) FROM espn_projection_history").fetchone() == (2, 2 * n)


def test_failed_week_leaves_todays_history_for_that_week_alone(con):
    sync(con, FakeEspnClient(projections={10: projection_payload(10, values={201: 6.0})}))
    sync(con, FakeEspnClient(projections={10: RuntimeError("down")}))
    assert con.execute("SELECT projected_points FROM espn_projection_history WHERE espn_player_id = 201 "
                       "AND scoring_period = 10").fetchall() == [(6.0,)]


# ------------------------------------------------------------------ isolation --

ANALYTICS_TABLES = {
    "waiver_recommendations": "espn_player_id", "team_position_needs": "team_id, position",
    "manager_metrics": "team_id, metric", "manager_classifications": "team_id, dimension",
    "player_scores": "gsis_id", "player_week_usage": "gsis_id, week",
}


def _analytics_snapshot(db_path):
    con = connect(db_path)
    try:
        out = {}
        for table, order in ANALYTICS_TABLES.items():
            rows = fetch_dicts(con, f"SELECT * FROM {table} ORDER BY {order}")
            out[table] = [{k: v for k, v in r.items() if not k.endswith("_at") and k != "run_id"} for r in rows]
        return out
    finally:
        con.close()


def _sync_and_analyze(db_path, client):
    settings = SimpleNamespace(swid=MY_SWID, my_team_id=None)
    run_espn_sync(lambda: open_db(db_path), settings, client=client, save_raw_payloads=False)
    con = connect(db_path)
    try:
        run_analytics(con, SEASON)
    finally:
        con.close()


def test_waivers_needs_and_managers_identical_with_and_without_future_projections(tmp_path):
    no_proj = FakeEspnClient(projections={w: RuntimeError("off") for w in WEEKS})
    _sync_and_analyze(tmp_path / "a.duckdb", no_proj)
    _sync_and_analyze(tmp_path / "b.duckdb", FakeEspnClient())
    a, b = _analytics_snapshot(tmp_path / "a.duckdb"), _analytics_snapshot(tmp_path / "b.duckdb")
    assert a["waiver_recommendations"] and a["team_position_needs"] and a["manager_metrics"]
    assert a == b
    # ...and wildly different projections change nothing either
    zeros = FakeEspnClient(projections={w: projection_payload(w, values={pid: 0.0 for pid in ROSTERED | FAS})
                                        for w in WEEKS})
    _sync_and_analyze(tmp_path / "b.duckdb", zeros)
    assert _analytics_snapshot(tmp_path / "b.duckdb") == a
    con = connect(tmp_path / "b.duckdb")
    assert con.execute("SELECT count(*) FROM player_ros_projections").fetchone()[0] > 0   # but the baseline exists
    con.close()


def test_market_signals_scores_and_context_identical_before_and_after_projections(nfl):
    con = nfl
    # ESPN side: league + mapped players with known actual league points for weeks 1-3
    upsert_rows(con, "leagues", [{"league_id": 1, "season": NFL_SEASON, "current_week": 4, "final_scoring_period": 17,
                                  "reg_season_matchups": 14, "settings_raw": {}, "synced_at": datetime(2026, 1, 1)}],
                ["league_id", "season"])
    ids = {"G1": 301, "G2": 201, "G3": 401}
    for g, pid in ids.items():
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                    "VALUES (?, ?, 'espn_id:nfl_players', 'high')", [pid, g])
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, availability_status) "
                    "VALUES (?, ?, 'WR', 'KC', 'ONTEAM')", [pid, g])
        for w in (1, 2, 3):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, ?, NULL, NULL)",
                        [NFL_SEASON, pid, w, 10.0 + w])
    before = _snapshot(nfl)
    assert before[1] and any(s["market_signal"] for s in before[1].values())
    league_pts = fetch_dicts(nfl, "SELECT gsis_id, week, league_points FROM player_week_usage ORDER BY 1, 2")

    # ESPN forward projections arrive: past rows gain projections, future rows appear, ROS is built
    con.execute("UPDATE espn_player_weekly_points SET projected_points = 99.0")
    for pid in ids.values():
        for w in range(4, 18):
            con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, NULL)", [NFL_SEASON, pid, w, 0.0])
    assert build_ros_projections(con, NFL_SEASON)["players"] == 3
    after = _snapshot(nfl)
    assert after == before   # usage, opportunity/production/value scores, market signal, sustainability, context
    assert fetch_dicts(nfl, "SELECT gsis_id, week, league_points FROM player_week_usage ORDER BY 1, 2") == league_pts
    assert any(r["league_points"] is not None for r in league_pts)   # the actual-points join was exercised


def test_scoring_and_waiver_code_never_read_projections():
    from pathlib import Path

    import app.analytics.manager_metrics as mm
    import app.analytics.player_scores as ps
    import app.analytics.trends as tr
    import app.analytics.usage as us
    import app.analytics.waivers as wv
    for mod in (ps, wv, tr, mm, us):
        src = Path(mod.__file__).read_text()
        for name in ("projected_points", "player_ros_projections", "espn_projection_history", "ros_projected"):
            assert name not in src, (mod.__name__, name)


# ------------------------------------------------------------- migration + API --

def test_v4_database_migrates_create_only(tmp_path):
    con = connect(tmp_path / "v4.duckdb")
    init_db(con)
    con.execute("DROP TABLE espn_projection_history")
    con.execute("DROP TABLE player_ros_projections")
    con.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
    con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, 1, 1, 5.0, 6.0, NULL)")
    con.execute("INSERT INTO external_player_map (provider, source_player_key, gsis_id) VALUES ('p', 'k', 'G1')")
    con.execute("INSERT INTO external_player_week_usage_revisions (import_id) VALUES ('imp-1')")
    con.execute("INSERT INTO player_ownership_history VALUES ('2026-10-01', 1, 50, 10, 1)")
    con.execute("INSERT INTO player_week_usage (season, week, gsis_id) VALUES (2026, 1, 'G1')")
    init_db(con)
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION) == "5"
    assert table_exists(con, "espn_projection_history") and table_exists(con, "player_ros_projections")
    for t in ("espn_player_weekly_points", "external_player_map", "external_player_week_usage_revisions",
              "player_ownership_history", "player_week_usage"):
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 1, t
    con.close()


def test_api_forward_view_and_data_quality(tmp_path, monkeypatch):
    db_path = tmp_path / "api.duckdb"
    client = FakeEspnClient(projections={7: ConnectionError("timeout"),
                                         8: projection_payload(8, values={201: 0.0}, missing=(202,))})
    _sync_and_analyze(db_path, client)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)

    fwd = api.get("/api/players/202/forward").json()
    assert fwd["source"] == "ESPN" and "baseline" in fwd["note"]
    assert [w["week"] for w in fwd["weeks"]] == WEEKS
    w8 = next(w for w in fwd["weeks"] if w["week"] == 8)
    assert w8["projection_available"] is False and w8["espn_projected_points"] is None
    w7 = next(w for w in fwd["weeks"] if w["week"] == 7)
    assert w7["projection_available"] is False   # fetch failed on a fresh DB: unknown, not zero
    assert fwd["summary"]["ros_projected_points"] is None and fwd["summary"]["missing_weeks"] == [7, 8]
    assert set(fwd["weeks"][0]) >= {"week", "opponent", "on_bye", "espn_projected_points", "projection_available"}

    detail = api.get("/api/players/201").json()
    assert detail["forward_projection"]["summary"]["current_week_projection"] == projection_value(201, CURRENT)
    assert next(w for w in detail["forward_projection"]["weeks"] if w["week"] == 8)["espn_projected_points"] == 0.0

    dq = api.get("/api/data-quality").json()["espn_projections"]
    fetch = dq["latest_fetch"]["fetch"]
    assert fetch["weeks_requested"] == WEEKS and fetch["weeks_failed"] == [7]
    assert "ESPN projections week 7 failed" in fetch["failures"]["7"]
    stored = {s["week"]: s for s in dq["stored"]}
    assert stored[7]["projected"] == 0 and stored[8]["zero"] == 1
    assert stored[9]["rostered_projected"] == stored[9]["rostered"] == len(ROSTERED)
    assert dq["ros_summary"]["incomplete"] >= 1 and dq["history"]["rows"] > 0
    assert any(r["missing_weeks"] == [7, 8] for r in dq["ros_incomplete"])     # JSON decoded to a list
    assert api.get("/api/players/123456789/forward").status_code == 404


def test_unmigrated_v4_database_is_refused_by_api_and_skipped_by_ros_builder(tmp_path, monkeypatch):
    db_path = tmp_path / "v4.duckdb"
    _sync_and_analyze(db_path, FakeEspnClient())
    con = connect(db_path)
    con.execute("DROP TABLE espn_projection_history")
    con.execute("DROP TABLE player_ros_projections")
    con.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
    assert build_ros_projections(con, SEASON)["skipped"].startswith("schema older")
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    for path in ("/api/data-quality", "/api/players/201/forward"):   # the read-only API never migrates
        res = api.get(path)
        assert res.status_code == 503 and "older than v5" in res.json()["detail"]

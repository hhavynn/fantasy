"""Schedule intelligence: opponents, byes, phases, no-lookahead defensive samples, completion checks,
metric definitions and denominators, missing-vs-zero, coverage, identity notes, ranks, read-only API
and isolation from every existing decision system. Synthetic inputs only (tests/synthetic_nfl.py)."""

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.analytics.schedule_intelligence import (BAND, FAVORABLE_RANK, METRICS, MIN_DEF_GAMES, MIN_RANKED_DEFENSES,
                                                 NotFound, band, build_schedule_intelligence, defense_samples,
                                                 load_schedule, metric_value, rank_permissive)
from app.db.connection import connect, fetch_dicts

import synthetic_nfl as nfl
from espn_fixture import CURRENT, SEASON
from test_trade_data import _raw_snapshot, synced

WEEK = 8                                   # ESPN current week in these tests; weeks 1-7 complete
FINAL_WEEKS = set(range(1, WEEK))
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _bye_pair():
    """BUF and its scheduled week-10 opponent rest in week 10."""
    pairs = nfl.rounds(nfl.TEAMS, 10)[9]
    opp = next(a if h == "BUF" else h for h, a in pairs if "BUF" in (h, a))
    return {10: {"BUF", opp}}


def setup(tmp_path, partial=None, final_weeks=FINAL_WEEKS, byes=None, overrides=None, sched=True, defense=True,
          name="si.duckdb"):
    db_path = tmp_path / name
    con = synced(db_path, analytics=False)
    con.execute("UPDATE leagues SET current_week = ?", [WEEK])
    con.execute(f"""INSERT INTO lineup_snapshots SELECT * REPLACE ({WEEK} AS scoring_period)
                    FROM lineup_snapshots WHERE scoring_period = {CURRENT}""")
    if sched:
        nfl.write(con, SEASON, nfl.schedule(18, byes if byes is not None else _bye_pair()), final_weeks,
                  partial=partial if partial is not None else {WEEK: {h for h, _ in nfl.schedule(18)[WEEK][:8]}},
                  defense=defense, overrides=overrides)
    return db_path, con


def stable(obj):
    """Drop wall-clock fields (evaluation timestamp, search timings) and discovery cache diagnostics."""
    if isinstance(obj, dict):
        return {k: stable(v) for k, v in obj.items() if k not in ("evaluated_at", "seconds", "exact_evaluations_cached", "lineup_solves_stage_a")}
    if isinstance(obj, (list, tuple)):
        return [stable(v) for v in obj]
    return obj


def week(player: dict, w: int) -> dict:
    return next(x for x in player["weeks"] if x["week"] == w)


def player(res: dict, pid: int) -> dict:
    return next(p for p in res["players"] if p["espn_player_id"] == pid)


def metric(row: dict, name: str) -> dict:
    return next(m for m in row["evidence"]["metrics"] if m["metric"] == name)


# ----------------------------------------------------------------------- schedule --

def test_opponent_home_away_and_bye(tmp_path):
    _, con = setup(tmp_path)
    res = build_schedule_intelligence(con, now=NOW)
    qb = player(res, 102)                                    # Bravo QB, BUF
    sched = nfl.schedule(18, _bye_pair())
    for w in (9, 11, 15):
        h, a = next(p for p in sched[w] if "BUF" in p)
        row = week(qb, w)
        assert row["status"] == "scheduled" and row["opponent"] == (a if h == "BUF" else h)
        assert row["home"] is (h == "BUF")
    bye = week(qb, 10)
    assert bye["status"] == "bye" and bye["label"] == "bye" and "evidence" not in bye and bye["opponent"] is None
    assert qb["summary"]["all"]["counts"]["bye"] == 1
    con.close()


def test_regular_season_and_playoff_windows_from_league_settings(tmp_path):
    _, con = setup(tmp_path)
    res = build_schedule_intelligence(con, now=NOW)
    assert res["league"]["last_regular_season_week"] == 14 and res["window"]["playoff_weeks"] == [15, 16, 17]
    qb = player(res, 102)
    assert {w["week"]: w["phase"] for w in qb["weeks"]}[14] == "regular" and week(qb, 15)["phase"] == "playoffs"
    assert qb["summary"]["playoffs"]["weeks"] == [15, 16, 17] and qb["summary"]["regular"]["weeks"] == list(range(9, 15))
    po = build_schedule_intelligence(con, phase="playoffs", now=NOW)
    assert [w["week"] for w in player(po, 102)["weeks"]] == [15, 16, 17]
    # A different league configuration moves the boundary (nothing is hard-coded to 15-17).
    settings = json.loads(con.execute("SELECT settings_raw FROM leagues").fetchone()[0])
    settings["scheduleSettings"]["matchupPeriods"] = {**{str(i): [i] for i in range(1, 13)}, "13": [13, 14],
                                                      "14": [15, 16], "15": [17]}
    con.execute("UPDATE leagues SET settings_raw = ?, reg_season_matchups = 12", [json.dumps(settings)])
    res = build_schedule_intelligence(con, now=NOW)
    assert res["league"]["last_regular_season_week"] == 12 and res["window"]["playoff_weeks"] == [13, 14, 15, 16, 17]
    assert res["window"]["matchup_periods"]["14"] == 13 and res["window"]["matchup_periods"]["16"] == 14
    assert week(player(res, 102), 16)["matchup_period"] == 14              # two-week playoff matchup
    con.close()


def test_kickoff_aware_window_and_informational_current_week(tmp_path):
    _, con = setup(tmp_path)                                    # week 8: some games final, others in progress
    res = build_schedule_intelligence(con, now=NOW)
    assert res["window"]["first_actionable_week"] == WEEK + 1 and res["window"]["remaining_weeks"]
    qb = player(res, 102)
    cur = week(qb, WEEK)
    assert cur["actionable"] is False and "informational" in cur["window_note"]
    assert qb["summary"]["next"]["weeks"] == [WEEK + 1] and WEEK not in qb["summary"]["all"]["weeks"]
    con.close()


def test_no_remaining_weeks(tmp_path):
    _, con = setup(tmp_path, final_weeks=set(range(1, 19)), partial={})
    con.execute("UPDATE leagues SET current_week = 17")
    con.execute("""INSERT INTO lineup_snapshots SELECT * REPLACE (17 AS scoring_period)
                   FROM lineup_snapshots WHERE scoring_period = 8""")
    res = build_schedule_intelligence(con, now=NOW)
    assert res["window"]["remaining_weeks"] is False and res["window"]["first_actionable_week"] is None
    qb = player(res, 102)
    assert all(not w["actionable"] for w in qb["weeks"]) and qb["summary"]["all"]["weeks"] == []
    con.close()


def test_missing_schedule_week_and_missing_schedule_table(tmp_path):
    _, con = setup(tmp_path)
    con.execute("DELETE FROM nfl_schedules WHERE week = 16")
    qb = player(build_schedule_intelligence(con, now=NOW), 102)
    assert week(qb, 16)["status"] == "schedule_missing" and week(qb, 16)["label"] == "unavailable"
    assert week(qb, 15)["status"] == "scheduled"
    con.close()
    _, con = setup(tmp_path, sched=False, name="nosched.duckdb")
    res = build_schedule_intelligence(con, now=NOW)
    assert res["schedule"]["available"] is False
    assert all(w["status"] == "schedule_missing" for w in player(res, 102)["weeks"])
    con.close()


# ------------------------------------------------------------- defensive evidence --

def test_no_future_week_leakage_and_in_progress_games_excluded(tmp_path):
    # Week 8 is under way: half its games are final. The other half have PBP rows but no final score.
    final_homes = {h for h, _ in nfl.schedule(18)[WEEK][:8]}
    _, con = setup(tmp_path, overrides={(WEEK, d): {"pass_epa_sum": 999.0} for d in nfl.TEAMS})
    res = build_schedule_intelligence(con, now=NOW)
    for p in res["players"]:
        for w in p["weeks"]:
            if w["status"] != "scheduled":
                continue
            ev = w["evidence"]
            assert all(x < w["week"] for x in ev["weeks_sampled"])          # never week W or later
            assert all(x < w["week"] for x in ev["excluded_unverified_weeks"])
    qb = player(res, 102)
    cur = week(qb, WEEK)["evidence"]                                       # week-8 evidence: weeks 1-7 only
    assert cur["weeks_sampled"] == list(range(1, WEEK)) and metric(week(qb, WEEK), "pass_epa_per_play")["value"] < 1
    nxt = week(qb, WEEK + 1)
    opp = nxt["opponent"]
    opp_w8 = next(p for p in nfl.schedule(18)[WEEK] if opp in p)
    if opp_w8[0] in final_homes:                                           # completed week-8 game is used
        assert WEEK in nxt["evidence"]["weeks_sampled"]
    else:                                                                  # in-progress game is excluded
        assert nxt["evidence"]["excluded_unverified_weeks"] == [WEEK] and WEEK not in nxt["evidence"]["weeks_sampled"]
    # Across the league: exactly the final week-8 games count, the in-progress ones are listed as excluded.
    sched = load_schedule(con, SEASON)
    rows = fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ?", [SEASON])
    s = defense_samples(rows, sched["completed"], WEEK + 1)
    finals = {t for h, a in nfl.schedule(18)[WEEK] if h in final_homes for t in (h, a)}
    assert {d for d, x in s.items() if WEEK in x["weeks"]} == finals
    assert {d for d, x in s.items() if x["excluded_unverified"] == [WEEK]} == set(nfl.TEAMS) - finals
    con.close()


def test_pass_play_label_definitions_and_denominators(tmp_path):
    _, con = setup(tmp_path)
    res = build_schedule_intelligence(con, now=NOW)
    pass_def = res["metrics"]["pass_epa_per_play"]
    assert pass_def["label"] == "EPA allowed per eligible pass play" and "dropback" not in pass_def["label"].lower()
    assert "not verified identical to `qb_dropback`" in pass_def["definition"]
    assert (pass_def["numerator"], pass_def["denominator"]) == ("pass_epa_sum", "pass_plays")
    assert (res["metrics"]["rush_epa_per_play"]["denominator"], res["metrics"]["rush_success_rate"]["denominator"]) \
        == ("rush_plays", "rush_plays")
    for m in METRICS.values():
        assert m["definition"] and m["numerator"] and m["denominator"]
    rb = week(player(res, 204), WEEK + 1)                                  # Hotel RB, PHI
    opp, games = rb["opponent"], rb["evidence"]["games"]
    rush = metric(rb, "rush_epa_per_play")
    assert rush["denominator"] == 25 * games and rush["value"] == pytest.approx(nfl.rush_rate(opp))
    assert rush["numerator"] == pytest.approx(25 * games * nfl.rush_rate(opp))
    succ = metric(rb, "rush_success_rate")
    assert succ["denominator"] == 25 * games and succ["numerator"] == games * (5 + nfl.TEAMS.index(opp) % 10)
    qb = week(player(res, 102), WEEK + 1)
    pe = metric(qb, "pass_epa_per_play")
    assert pe["denominator"] == 40 * qb["evidence"]["games"] and pe["value"] == pytest.approx(nfl.pass_rate(qb["opponent"]))
    assert [m["metric"] for m in qb["evidence"]["metrics"]] == ["pass_epa_per_play"]          # QB: pass only
    assert [m["metric"] for m in rb["evidence"]["metrics"]] == ["rush_epa_per_play", "rush_success_rate", "rb_target_share"]
    assert "EPA per eligible pass play" in qb["evidence"]["sentence"] and f"{pe['denominator']} pass plays" in qb["evidence"]["sentence"]
    con.close()


def test_missing_epa_zero_attempts_and_zero_observed_values(tmp_path):
    zero_rush = {(w, "DET"): {"rush_plays": 0, "rush_epa_sum": 0.0, "rush_success": 0} for w in range(1, WEEK)}
    no_success = {(w, "SF"): {"rush_success": 0} for w in range(1, WEEK)}
    missing_pass = {(3, "KC"): {"pass_plays": None, "pass_epa_sum": None}}
    _, con = setup(tmp_path, overrides={**zero_rush, **no_success, **missing_pass})
    rows = fetch_dicts(con, "SELECT * FROM nfl_defense_week WHERE season = ?", [SEASON])
    s = defense_samples(rows, load_schedule(con, SEASON)["completed"], WEEK)
    v, why = metric_value(s["DET"], "rush_epa_per_play")
    assert v is None and "no eligible rush plays" in why                  # zero attempts: unknown, not 0
    v, why = metric_value(s["SF"], "rush_success_rate")
    assert v == 0.0 and why is None                                        # a real observed 0 stays 0
    v, why = metric_value(s["KC"], "pass_epa_per_play")
    assert v is None and "missing for 1 game" in why                       # a missing column is never 0
    assert metric_value(s["KC"], "rush_epa_per_play")[0] is not None       # other metrics unaffected
    con.close()


def test_receiver_position_coverage_is_reported(tmp_path):
    _, con = setup(tmp_path, overrides={(w, d): {"targets": 40, "rb_targets": 6, "wr_targets": 16, "te_targets": 6}
                                        for w in range(1, WEEK + 1) for d in nfl.TEAMS})
    res = build_schedule_intelligence(con, position="TE", now=NOW)
    te = week(res["players"][0], WEEK + 1)
    share = metric(te, "te_target_share")
    assert share["value"] == pytest.approx(6 / 40) and share["position_coverage"] == pytest.approx(28 / 40)
    assert any("unidentified positions" in n for n in res["notes"])
    con.close()


def test_rank_direction_bands_and_insufficient_samples():
    vals = {t: nfl.pass_rate(t) for t in nfl.TEAMS}
    ranks = rank_permissive(vals)
    assert ranks["KC"] == 1 and ranks[nfl.TEAMS[-1]] == 32                 # 1 = most permissive (highest allowed)
    assert band(1, 32) == "permissive" and band(FAVORABLE_RANK, 32) == "permissive"
    assert band(FAVORABLE_RANK + 1, 32) == "middle" and band(32 - BAND, 32) == "middle"
    assert band(32 - BAND + 1, 32) == "tougher" and band(32, 32) == "tougher"
    assert band(1, MIN_RANKED_DEFENSES - 1) == "insufficient" and band(None, 32) == "insufficient"
    assert rank_permissive({"A": 0.1, "B": 0.1, "C": 0.0}) == {"A": 1, "B": 1, "C": 3}


def test_early_season_sample_is_insufficient(tmp_path):
    _, con = setup(tmp_path, final_weeks={1, 2}, partial={})
    con.execute("UPDATE leagues SET current_week = 3")
    res = build_schedule_intelligence(con, now=NOW)
    row = week(player(res, 102), 3)
    assert row["evidence"]["games"] == 2 and row["label"] == "insufficient"
    pe = metric(row, "pass_epa_per_play")
    assert pe["value"] is not None and pe["rank"] is None and f"< {MIN_DEF_GAMES}" in pe["missing_reason"]
    con.close()


def test_season_boundary_excludes_other_seasons(tmp_path):
    _, con = setup(tmp_path)                          # synthetic_nfl writes extreme 2025 rows for every defense
    assert con.execute("SELECT count(*) FROM nfl_defense_week WHERE season = ?", [SEASON - 1]).fetchone()[0] == 32
    qb = week(player(build_schedule_intelligence(con, now=NOW), 102), WEEK + 1)
    assert abs(metric(qb, "pass_epa_per_play")["value"]) < 1
    con.close()


def test_identity_notes_team_change_uncertain_mapping_and_teamless(tmp_path):
    _, con = setup(tmp_path)
    con.execute("DELETE FROM player_id_map WHERE espn_player_id = 102")
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (102, 'G-102', 'medium')")
    con.execute("""CREATE TABLE nfl_rosters_weekly (season INT, week INT, gsis_id VARCHAR, team VARCHAR)""")
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, 6, 'G-102', 'BUF'), (?, 7, 'G-102', 'MIA')", [SEASON, SEASON])
    con.execute("UPDATE fantasy_players SET pro_team = NULL WHERE espn_player_id = 205")
    res = build_schedule_intelligence(con, now=NOW)
    qb = player(res, 102)
    assert qb["identity"]["match_confidence"] == "medium" and qb["identity"]["nflverse_latest_team"] == "MIA"
    assert any("ESPN lists BUF" in n and "MIA" in n for n in qb["notes"]) and week(qb, 9)["opponent"]   # ESPN team used
    rb = player(res, 205)
    assert all(w["status"] == "team_unknown" and w["label"] == "unavailable" for w in rb["weeks"])
    assert any("No current NFL team" in n for n in rb["notes"])
    con.close()


def test_repeated_requests_are_identical_and_fast(tmp_path):
    _, con = setup(tmp_path)
    t = time.perf_counter()
    a = build_schedule_intelligence(con, now=NOW)
    elapsed = time.perf_counter() - t
    b = build_schedule_intelligence(con, now=NOW)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert elapsed < 5
    con.close()


def test_filters_and_validation(tmp_path):
    _, con = setup(tmp_path)
    assert {p["position"] for p in build_schedule_intelligence(con, position="RB", now=NOW)["players"]} == {"RB"}
    every = build_schedule_intelligence(con, now=NOW)["players"]
    assert {p["position"] for p in every} <= {"QB", "RB", "WR", "TE"}     # K and D/ST excluded
    assert [p["position"] for p in every] == sorted((p["position"] for p in every), key=["QB", "RB", "WR", "TE"].index)
    assert build_schedule_intelligence(con, team_id=4, position="RB", now=NOW)["players"] == []      # empty state
    one = build_schedule_intelligence(con, player_id=309, now=NOW)       # any skill player, e.g. a free agent
    assert [p["espn_player_id"] for p in one["players"]] == [309]
    with pytest.raises(NotFound):
        build_schedule_intelligence(con, team_id=99, now=NOW)
    with pytest.raises(ValueError):
        build_schedule_intelligence(con, position="K", now=NOW)
    with pytest.raises(ValueError):
        build_schedule_intelligence(con, phase="postseason", now=NOW)
    con.close()


# ------------------------------------------------------------------------------ API --

def test_api_is_read_only_and_leaves_every_decision_system_unchanged(tmp_path, monkeypatch):
    db_path, con = setup(tmp_path)
    con.close()
    from app.analytics.pipeline import run_analytics
    con = connect(db_path)
    run_analytics(con, SEASON)
    raw = _raw_snapshot(con)
    tables = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in
              ("player_scores", "player_context", "defense_context", "waiver_recommendations",
               "manager_classifications", "team_ros_lineup_projections", "espn_player_weekly_points")}
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    trade = {"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304], "team_b_sends": [301]}
    before = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json())
    mtime = db_path.stat().st_mtime_ns

    res = api.get("/api/schedule-intelligence")
    assert res.status_code == 200
    body = res.json()
    assert body["team"]["is_mine"] is True and body["query"]["team_id"] == 2          # default: my team
    assert body["players"] and all(p["position"] in ("QB", "RB", "WR", "TE") for p in body["players"])
    assert api.get("/api/schedule-intelligence", params={"position": "wr"}).json()["query"]["position"] == "WR"
    po = api.get("/api/schedule-intelligence", params={"phase": "playoffs"}).json()
    assert {w["week"] for p in po["players"] for w in p["weeks"]} <= set(po["window"]["playoff_weeks"])
    one = api.get("/api/schedule-intelligence", params={"player_id": 204}).json()
    assert [p["espn_player_id"] for p in one["players"]] == [204] and one["players"][0]["weeks"]
    assert api.get("/api/schedule-intelligence", params={"team_id": 99}).status_code == 404
    assert api.get("/api/schedule-intelligence", params={"player_id": 123456}).status_code == 404
    assert api.get("/api/schedule-intelligence", params={"position": "K"}).status_code == 422
    assert api.get("/api/schedule-intelligence", params={"phase": "x"}).status_code == 422
    empty = api.get("/api/schedule-intelligence", params={"team_id": 4, "position": "RB"}).json()
    assert empty["players"] == [] and empty["team"]["team_id"] == 4

    after = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json())
    assert stable(before) == stable(after)
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == tables
    con.close()


def test_api_without_league_and_schedule_module_isolation(tmp_path, monkeypatch):
    from app.db.connection import init_db
    db_path = tmp_path / "empty.duckdb"
    con = connect(db_path)
    init_db(con)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    assert TestClient(app).get("/api/schedule-intelligence").status_code == 503
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "analytics"
    for f in ("player_scores.py", "waivers.py", "manager_metrics.py", "manager_scouting.py", "projections.py",
              "trades.py", "team_lineups.py", "trade_data.py", "trade_discovery.py", "pipeline.py", "context.py"):
        assert "schedule_intelligence" not in (root / f).read_text(), f

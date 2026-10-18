"""scripts/dst_shadow.py: the prospective shadow of the D/ST research model dfp-r1 against ESPN. Freeze rule,
write-once pregame ledger with the line stored at freeze time, point-in-time inputs, weekly scoring against ESPN's
pregame snapshots, the 8-week gate, failure isolation (here and in learning_cycle.py / refresh_all.py). A synthetic
8-team league in temporary DuckDB files and a synthetic ESPN cache; no test makes a network request."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import duckdb
import numpy as np
import pytest

from app.analytics import dst_research as dr
from test_dst_research import LEAGUE

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
TEAMS = {"ATL": 1, "BUF": 2, "CHI": 3, "CIN": 4, "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8}
SEASON, FINAL_THROUGH, WEEKS = 2026, 6, 10
TUE = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)              # week 6 final, week 7 ahead
THU_GAME = datetime(2026, 10, 23, 0, 15, tzinfo=UTC)         # week 7's Thursday game (20:15 ET)
SUN_EARLY = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)        # week 7's Sunday 13:00 ET games


def _load(name, file):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ds():
    return _load("dst_shadow", "dst_shadow.py")


# ----------------------------------------------------------------------------- synthetic world


def _gameday(season, week):
    """Thursday of the week (the 2026 calendar for 2026, any calendar earlier: kickoffs only matter in 2026)."""
    return date(season, 9, 10) + timedelta(days=7 * (week - 1))


def world(seed=3):
    """Schedules (2019-2026, 2026 final through FINAL_THROUGH, lines on every game), ESPN actual / projected D/ST
    statistics per season, ESPN league points per 2026 week. Points allowed follow the line."""
    rng = np.random.default_rng(seed)
    names = list(TEAMS)
    strength = {t: rng.normal(0, 4) for t in names}
    sched, espn = [], {}
    for s in range(2019, SEASON + 1):
        espn[s] = {"actual": {}, "espn": {}}
        for w in range(1, (WEEKS if s == SEASON else 8) + 1):
            order = names[:1] + names[1:][w % 7:] + names[1:][:w % 7]
            final = s < SEASON or w <= FINAL_THROUGH
            for i, (a, b) in enumerate(zip(order[:4], order[4:][::-1])):
                spread = round((strength[a] - strength[b]) * 2) / 2
                hs = max(0, int(rng.normal((44 + spread) / 2, 7)))
                as_ = max(0, int(rng.normal((44 - spread) / 2, 7)))
                day = _gameday(s, w) + timedelta(days=0 if i == 0 else 3)
                sched.append({"season": s, "week": w, "game_type": "REG", "gameday": day.isoformat(),
                              "gametime": "20:15" if i == 0 else "13:00", "home_team": a, "away_team": b,
                              "home_score": hs if final else None, "away_score": as_ if final else None,
                              "spread_line": spread, "total_line": 44.0, "location": "Home"})
                for team, pa in ((a, as_), (b, hs)):
                    sacks = float(rng.poisson(2.5))
                    ya = float(max(0, rng.normal(330, 60)))
                    stats = {99: sacks, 100: 2 * sacks, 95: float(rng.poisson(0.8)), 96: float(rng.poisson(0.6)),
                             120: float(pa), 127: ya, dr.tier_of(pa, dr.PA_TIERS): 1.0,
                             dr.tier_of(ya, dr.YA_TIERS): 1.0}
                    if rng.random() < 0.1:
                        stats[103] = 1.0
                    if final:
                        espn[s]["actual"][(team, w)] = stats
                    espn[s]["espn"][(team, w)] = {99: 2.4, 95: 0.8, 96: 0.6, 91: 0.2, 92: 0.2, 122: 0.3, 130: 0.3}
    return sched, espn


def payload(espn_season, season):
    """A kona_player_info payload (the format dst_research.py fetch caches)."""
    players = []
    for team, pid in TEAMS.items():
        stats = []
        for src, kind in ((0, "actual"), (1, "espn")):
            for (t, w), st in espn_season[kind].items():
                if t == team:
                    stats.append({"seasonId": season, "statSplitTypeId": 1, "scoringPeriodId": w,
                                  "statSourceId": src, "stats": {str(k): v for k, v in st.items()}})
        players.append({"player": {"defaultPositionId": 16, "proTeamId": pid, "stats": stats}})
    return {"players": players, "_fetched_at": "2026-10-20T14:00:00+00:00"}


def write_cache(cache: Path, espn, seasons=None):
    cache.mkdir(parents=True, exist_ok=True)
    for s in seasons or espn:
        (cache / f"espn_dst_{s}.json").write_text(json.dumps(payload(espn[s], s)))


def dst_id(team):
    return -16000 - TEAMS[team]


def build_live(path: Path, sched, espn, scoring):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE nfl_schedules (season INTEGER, week INTEGER, game_type VARCHAR, gameday VARCHAR,
                   gametime VARCHAR, home_team VARCHAR, away_team VARCHAR, home_score INTEGER, away_score INTEGER,
                   spread_line DOUBLE, total_line DOUBLE, location VARCHAR)""")
    con.executemany("INSERT INTO nfl_schedules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [[g[k] for k in ("season", "week", "game_type", "gameday", "gametime", "home_team", "away_team",
                                     "home_score", "away_score", "spread_line", "total_line", "location")]
                     for g in sched])
    con.execute("CREATE TABLE leagues (league_id BIGINT, season INTEGER, scoring_items JSON, synced_at TIMESTAMP)")
    con.execute("INSERT INTO leagues VALUES (1, ?, ?, '2026-09-01 00:00:00')", [SEASON, json.dumps(LEAGUE)])
    con.execute("CREATE TABLE fantasy_players (espn_player_id BIGINT, full_name VARCHAR, position VARCHAR, "
                "pro_team VARCHAR)")
    con.executemany("INSERT INTO fantasy_players VALUES (?,?,?,?)",
                    [[dst_id(t), f"{t} D/ST", "D/ST", t] for t in TEAMS])
    con.execute("CREATE TABLE espn_player_weekly_points (season INTEGER, espn_player_id BIGINT, scoring_period "
                "INTEGER, actual_points DOUBLE, projected_points DOUBLE, synced_at TIMESTAMP)")
    con.executemany("INSERT INTO espn_player_weekly_points VALUES (?,?,?,?,NULL,'2026-10-20 12:00:00')",
                    [[SEASON, dst_id(t), w, scoring.points(st)] for (t, w), st in espn[SEASON]["actual"].items()])
    con.execute("CREATE TABLE espn_projection_history (snapshot_date DATE, season INTEGER, espn_player_id BIGINT, "
                "scoring_period INTEGER, projected_points DOUBLE, synced_at TIMESTAMP)")
    # ESPN's league-scored projections for weeks 7-10, snapshotted Monday night (before the freeze)
    con.executemany("INSERT INTO espn_projection_history VALUES ('2026-10-19', ?, ?, ?, ?, '2026-10-20 04:00:00')",
                    [[SEASON, dst_id(t), w, 5.0 + 0.1 * TEAMS[t]] for t in TEAMS for w in range(7, WEEKS + 1)])
    con.execute("CREATE TABLE sync_runs (run_id VARCHAR, source VARCHAR, started_at TIMESTAMP, finished_at "
                "TIMESTAMP, status VARCHAR)")
    con.execute("INSERT INTO sync_runs VALUES ('n1', 'nfl', '2026-10-20 13:00:00', '2026-10-20 13:05:00', 'ok')")
    con.close()


@pytest.fixture(scope="module")
def synthetic():
    sched, espn = world()
    return sched, espn, dr.resolve_dst_scoring(LEAGUE)


@pytest.fixture
def env(ds, synthetic, tmp_path):
    sched, espn, scoring = synthetic
    build_live(tmp_path / "live.duckdb", sched, espn, scoring)
    write_cache(tmp_path / "cache", espn)
    args = ds.parse(["pregame", "--season", str(SEASON), "--live-db", str(tmp_path / "live.duckdb"),
                     "--history-db", str(tmp_path / "absent.duckdb"), "--cache", str(tmp_path / "cache"),
                     "--out", str(tmp_path / "dst")])
    return args


def sql(args, stmt, params=None):
    con = duckdb.connect(str(args.live_db))
    try:
        con.execute(stmt, params or [])
    finally:
        con.close()


def no_fetch(args):
    pytest.fail("the cache is complete: no fetch")


def _runs(args):
    d = args.out / "shadow" / "runs"
    return sorted(d.glob("*.json")) if d.exists() else []


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- freeze rule (pure)


def g(team, kickoff, spread=-3.0, total=44.0, home=True):
    return {"team": team, "opponent": "X", "kickoff": kickoff, "spread_line": spread, "total_line": total,
            "home": home, "final": False}


def test_complete_inputs_freeze_as_early_as_possible(ds):
    d = ds.freeze_decision([g("A", THU_GAME), g("B", SUN_EARLY)], TUE, [])
    assert d["action"] == "freeze" and not d["degraded"] and not d["warnings"] and d["open"] == ["A", "B"]


def test_a_missing_line_or_previous_week_statistic_defers_quietly_far_from_kickoff(ds):
    d = ds.freeze_decision([g("A", THU_GAME), g("B", SUN_EARLY, spread=None)], TUE, [])
    assert d["action"] == "defer" and d["no_line"] == ["B"] and not d["warnings"] and d["notes"]
    d = ds.freeze_decision([g("A", THU_GAME)], TUE, [("A", 6)])
    assert d["action"] == "defer" and d["missing_actuals"] == [["A", 6]] and not d["warnings"]
    d = ds.freeze_decision([g("A", THU_GAME)], TUE, [], ["B", "C"])          # Monday night's score not posted
    assert d["action"] == "defer" and d["previous_week_unfinished"] == ["B", "C"] and "not final" in d["notes"][0]


def test_incomplete_inputs_close_to_kickoff_freeze_degraded_with_warnings(ds):
    d = ds.freeze_decision([g("A", THU_GAME), g("B", SUN_EARLY, total=None)], THU_GAME - timedelta(hours=2), [])
    assert d["action"] == "freeze" and d["degraded"] and "B" in d["warnings"][0] and "ratings-only" in d["warnings"][0]


def test_started_and_unknown_kickoff_teams_are_excluded(ds):
    d = ds.freeze_decision([g("A", THU_GAME), g("B", SUN_EARLY), g("C", None)], THU_GAME + timedelta(hours=1), [])
    assert d["action"] == "freeze" and d["open"] == ["B"] and d["started"] == ["A"]
    assert d["unknown_kickoff"] == ["C"] and "unknown kickoff" in d["warnings"][0]
    assert ds.freeze_decision([g("A", THU_GAME)], SUN_EARLY, [])["action"] == "nothing_open"


# ----------------------------------------------------------------------------- the freeze (real forecast)


def test_freeze_writes_a_complete_ledger_with_the_line_stored_at_freeze_time(ds, env):
    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    assert rec["status"] == "frozen", rec
    led = json.loads(Path(rec["ledger"]).read_text())
    assert rec["ledger_sha256"] == _sha(Path(rec["ledger"]))
    assert led["candidate"] == "dfp-r1" and led["week"] == 7 and led["created_at"] == TUE.isoformat()
    assert len(led["rows"]) == 8 and {r["team"] for r in led["rows"]} == set(TEAMS)
    assert led["config"]["dfp-r1"]["half_life"] == 16.0 and led["config"]["dfp-r1"]["k"] == 3.0
    assert led["training"]["seasons"] == [2020, 2025] and led["training"]["information_through"]["week"] == 6
    # every variant is a finite projection; the line is oriented to each defense exactly as in the research table
    con = duckdb.connect(str(env.live_db), read_only=True)
    games = con.execute("SELECT home_team, away_team, spread_line, total_line FROM nfl_schedules "
                        "WHERE season = 2026 AND week = 7").fetchall()
    con.close()
    rows = {r["team"]: r for r in led["rows"]}
    for home, away, spread, total in games:
        assert rows[home]["margin"] == spread and rows[away]["margin"] == -spread
        assert rows[home]["implied_opp"] == pytest.approx((total - spread) / 2)
        assert rows[home]["spread_line"] == spread and rows[home]["total_line"] == total
    for r in led["rows"]:
        assert r["has_line"] and all(np.isfinite(r[k]) for k in ("pred", "pred_line_only", "pred_ratings_only"))
        assert r["espn_at_freeze"] == pytest.approx(5.0 + 0.1 * TEAMS[r["team"]])
        assert r["espn_at_freeze_synced_at"].startswith("2026-10-20T04:00")
    # the line-only baseline differs from the full model (team ratings matter on this league)
    assert any(abs(r["pred"] - r["pred_line_only"]) > 1e-6 for r in led["rows"])
    # provenance: the lines, the ESPN cache files, the code and the nflverse sync time
    assert led["inputs"]["lines"]["sha256"] == ds._sha_obj(led["inputs"]["lines"]["rows"])
    assert led["inputs"]["lines"]["nfl_synced_at"].startswith("2026-10-20 13:05")
    assert set(led["inputs"]["espn_dst_cache"]) == {str(s) for s in range(2019, 2027)}
    assert set(led["code_sha256"]) == {"backend/app/analytics/dst_research.py", "scripts/dst_research.py",
                                       "scripts/dst_shadow.py"}
    assert len(set(led["code_sha256"].values())) == 3
    assert led["rows_sha256"] == ds._sha_obj([led["rows"], led["multi_week"]])
    # multi-week: the ratings-only model at horizons 1-4 for weeks 7-10, with ESPN's snapshot at freeze time
    assert sorted({(m["horizon"], m["week"]) for m in led["multi_week"]}) == [(1, 7), (2, 8), (3, 9), (4, 10)]
    assert all(m["espn_at_freeze"] is not None for m in led["multi_week"])
    run = json.loads(_runs(env)[0].read_text())
    assert run["status"] == "frozen" and run["ledger_sha256"] == rec["ledger_sha256"]


def test_a_frozen_ledger_is_never_rewritten(ds, env):
    first = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    path = Path(first["ledger"])
    before, runs = path.read_bytes(), _runs(env)
    # the line moves and the ESPN statistics change after the freeze: nothing is recomputed
    sql(env, "UPDATE nfl_schedules SET spread_line = spread_line + 7 WHERE season = 2026 AND week = 7")
    again = ds.run_pregame(env, SEASON, 7, now=TUE + timedelta(hours=20),
                           ledger_fn=lambda *a: pytest.fail("a frozen week is never forecast again"))
    assert again["status"] == "already_frozen" and again["ledger_sha256"] == first["ledger_sha256"]
    assert path.read_bytes() == before and _runs(env) == runs           # idempotent: no new record either
    from app.analytics.weekly_learning import write_once
    assert write_once(path, "{}") is False and path.read_bytes() == before


def test_a_concurrent_freeze_keeps_the_first_ledger(ds, env):
    """Two attempts racing past the existence check: the second write is refused, the first ledger stays."""
    real = ds.build_ledger

    def racing(args, season, week, now, games, dec):
        led = real(args, season, week, now, games, dec)
        ds._write_once(ds.ledger_path(args, season, week), {**led, "rows": [], "note": "the other process"})
        return led

    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch, ledger_fn=racing)
    assert rec["status"] == "already_frozen"
    assert json.loads(Path(rec["ledger"]).read_text())["note"] == "the other process"


def test_the_forecast_is_point_in_time(ds, synthetic):
    """Week 7 and later results (here: the Thursday game already final, wild numbers) never move week 7's forecast."""
    sched, espn, scoring = synthetic
    rows = dr.build_games(sched, [], espn, scoring)
    base = ds.forecast(rows, scoring, SEASON, 7, set(TEAMS), last=WEEKS)
    tampered = [dict(r) for r in rows]
    for r in tampered:
        if r["season"] == SEASON and r["week"] >= 7:
            r.update(final=True, sacks=9.0, ints=4.0, fr=3.0, pa=0.0, ya=50.0, pts=40.0, points=40.0, hits=12.0,
                     blk=1.0, saf=1.0, ret_td=1.0, int_td=1.0, fr_td=0.0, blk_td=0.0, ret2=0.0, saf1=0.0, kr_td=1.0,
                     pr_td=0.0)
    after = ds.forecast(tampered, scoring, SEASON, 7, set(TEAMS), last=WEEKS)
    assert after["rows"] == base["rows"] and after["multi_week"] == base["multi_week"]


def test_weeks_before_the_preregistered_start_are_never_frozen(ds, env):
    rec = ds.run_pregame(env, SEASON, 6, now=TUE - timedelta(days=7), fetch=no_fetch)
    assert rec["status"] == "before_prospective_start" and not (env.out / "shadow").exists()


def test_a_dry_run_decides_without_forecasting_fetching_or_recording(ds, env):
    env.write = False
    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch,
                         ledger_fn=lambda *a: pytest.fail("a dry run never forecasts"))
    assert rec["status"] == "would_freeze" and not (env.out / "shadow").exists()


def test_a_stale_cache_is_fetched_once_then_frozen(ds, env, synthetic):
    _, espn, _ = synthetic
    stale = {SEASON: {"actual": {k: v for k, v in espn[SEASON]["actual"].items() if k[1] < 6}, "espn": {}}}
    (env.cache / f"espn_dst_{SEASON}.json").write_text(json.dumps(payload(stale[SEASON], SEASON)))
    calls = []

    def fetch(args):
        calls.append(args.season)
        write_cache(env.cache, espn, [SEASON])
        return {SEASON: "fetched"}

    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=fetch)
    assert calls == [SEASON] and rec["status"] == "frozen"


def test_an_unreachable_espn_defers_instead_of_failing(ds, env, synthetic):
    _, espn, _ = synthetic
    stale = {k: v for k, v in espn[SEASON]["actual"].items() if k[1] < 6}
    (env.cache / f"espn_dst_{SEASON}.json").write_text(json.dumps(payload({"actual": stale, "espn": {}}, SEASON)))

    def down(args):
        raise ConnectionError("lm-api-reads.fantasy.espn.com unreachable")

    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=down)
    assert rec["status"] == "deferred" and "unreachable" in rec["fetch"]["error"]
    assert any("fetch failed" in n for n in rec["notes"])


def test_missing_statistics_without_a_fetch_defer_and_warn_near_kickoff(ds, env, synthetic):
    _, espn, _ = synthetic
    stale = {k: v for k, v in espn[SEASON]["actual"].items() if k[1] < 6}
    (env.cache / f"espn_dst_{SEASON}.json").write_text(json.dumps(payload({"actual": stale, "espn": {}}, SEASON)))
    env.fetch = False
    far = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    assert far["status"] == "deferred" and not far["warnings"] and "lack ESPN D/ST statistics" in far["notes"][0]
    assert len(far["cache"]["missing_actuals"]) == 8
    near = ds.run_pregame(env, SEASON, 7, now=THU_GAME - timedelta(hours=1), fetch=no_fetch)
    assert near["status"] == "frozen" and near["warnings"]          # frozen on week-5 information, flagged


def test_an_unscored_previous_week_game_defers_the_freeze(ds, env):
    sql(env, "UPDATE nfl_schedules SET home_score = NULL WHERE season = 2026 AND week = 6 AND gametime = '20:15'")
    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    assert rec["status"] == "deferred" and len(rec["decision"]["previous_week_unfinished"]) == 2


def test_a_failed_forecast_is_recorded_not_raised(ds, env):
    def boom(*a):
        raise MemoryError("out of memory")

    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch, ledger_fn=boom)
    assert rec["status"] == "failed" and "MemoryError" in rec["error"] and rec["warnings"]
    assert not ds.ledger_path(env, SEASON, 7).exists()
    assert json.loads(_runs(env)[0].read_text())["status"] == "failed"


def test_unsupported_league_scoring_abstains(ds, env):
    sql(env, "UPDATE leagues SET scoring_items = ?", [json.dumps(LEAGUE + [{"statId": 107, "points": 0.1}])])
    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    assert rec["status"] == "failed" and "abstains" in rec["error"]


def test_a_started_week_without_a_ledger_is_recorded_missed_once(ds, env):
    rec = ds.check_missed(env, SEASON, 7, now=SUN_EARLY + timedelta(hours=1))
    assert rec and rec["week"] == 7
    assert (env.out / "shadow" / "missed" / "s2026w07.json").exists()
    assert ds.check_missed(env, SEASON, 7, now=TUE) is None                 # not started yet
    assert ds.check_missed(env, SEASON, 6, now=SUN_EARLY) is None           # before the prospective start


# ----------------------------------------------------------------------------- scoring


def finish_week_7(env, espn_actual_points: dict):
    sql(env, "UPDATE nfl_schedules SET home_score = 20, away_score = 17 WHERE season = 2026 AND week = 7")
    con = duckdb.connect(str(env.live_db))
    con.executemany("INSERT INTO espn_player_weekly_points VALUES (2026, ?, 7, ?, NULL, '2026-10-27 12:00:00')",
                    [[dst_id(t), p] for t, p in espn_actual_points.items()])
    # a fresher ESPN snapshot Saturday (pregame for every team except the Thursday game) and one after kickoff
    con.executemany("INSERT INTO espn_projection_history VALUES ('2026-10-24', 2026, ?, 7, ?, '2026-10-24 15:00:00')",
                    [[dst_id(t), 7.0] for t in TEAMS])
    con.executemany("INSERT INTO espn_projection_history VALUES ('2026-10-25', 2026, ?, 7, ?, '2026-10-25 23:00:00')",
                    [[dst_id(t), 99.0] for t in TEAMS])
    con.close()


def test_scoring_waits_for_a_fully_final_week_then_uses_espns_pregame_snapshot(ds, env):
    rec = ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    led = json.loads(Path(rec["ledger"]).read_text())
    s = ds.score(env)
    assert s["scored_weeks"] == 0 and s["gate"]["candidates"]["dfp-r1"]["status"] == "accumulating"
    pts = {t: float(TEAMS[t]) for t in TEAMS}
    finish_week_7(env, pts)
    s = ds.score(env)
    assert s["scored_weeks"] == 1 and s["weeks"][0]["final"]
    one, multi, _ = ds.scored_rows(env, [led])
    thursday = {r["team"] for r in led["rows"] if r["kickoff"] == THU_GAME.isoformat()}
    for r in one:
        # ESPN pregame = latest snapshot strictly before the team's own kickoff; the post-kickoff 99 never counts
        assert r["espn_pregame"] == (pytest.approx(5.0 + 0.1 * TEAMS[r["team"]]) if r["team"] in thursday else 7.0)
        assert r["points"] == pts[r["team"]]
    pooled = s["pooled_vs_espn_pregame"]["methods"]
    mae = np.mean([abs(pts[r["team"]] - r["pred"]) for r in led["rows"]])
    assert pooled["dfp-r1"]["mae"] == pytest.approx(mae, abs=1e-4)
    assert pooled["ESPN pregame"]["mae"] == pytest.approx(np.mean([abs(r["points"] - r["espn_pregame"]) for r in one]), abs=1e-4)
    assert s["gate"]["candidates"]["dfp-r1"] == {"status": "accumulating", "reason": "1 of 8 scored weeks"}
    # multi-week: only the horizon-1 week (7) is final so far
    assert set(s["multi_week"]["horizons"]) == {"1"} and {m["horizon"] for m in multi} == {1}


def test_a_partially_synced_week_is_not_scored(ds, env):
    ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    finish_week_7(env, {t: 6.0 for t in list(TEAMS)[:7]})          # one D/ST's ESPN points not synced yet
    assert ds.score(env)["scored_weeks"] == 0


def test_run_score_records_and_replaces_the_scores_file(ds, env):
    assert ds.run_score(env, now=TUE)["status"] == "no_ledgers" and _runs(env) == []
    ds.run_pregame(env, SEASON, 7, now=TUE, fetch=no_fetch)
    finish_week_7(env, {t: float(TEAMS[t]) for t in TEAMS})
    rec = ds.run_score(env, now=TUE + timedelta(days=8))
    assert rec["status"] == "scored"
    stored = json.loads((env.out / "shadow" / "scores.json").read_text())
    assert stored["scored_weeks"] == 1 and stored["gate"]["baseline"] == "ESPN pregame"
    sql(env, "UPDATE espn_player_weekly_points SET actual_points = actual_points + 9 WHERE scoring_period = 7")   # stat correction
    ds.run_score(env, now=TUE + timedelta(days=9))
    assert json.loads((env.out / "shadow" / "scores.json").read_text())["pooled_vs_espn_pregame"]["methods"][
        "dfp-r1"]["mae"] != stored["pooled_vs_espn_pregame"]["methods"]["dfp-r1"]["mae"]


def _ev(weeks, mae_ci, rho_ci, bias):
    m = {"bias_actual_minus_pred": bias}
    return {"n": weeks * 32, "weeks": weeks, "methods": {"dfp-r1": m, "ESPN pregame": m},
            "vs_ESPN pregame": {"dfp-r1": {"mae_diff_ci90": mae_ci, "spearman_diff_ci90": rho_ci}}}


def test_the_gate_needs_eight_weeks_and_every_preregistered_check(ds):
    gate = lambda ev: ds.gate(ev, ["dfp-r1"], "ESPN pregame")["candidates"]["dfp-r1"]["status"]  # noqa: E731
    assert gate(_ev(7, [-0.3, -0.1], [0.0, 0.1], 0.1)) == "accumulating"
    assert gate(_ev(8, [-0.3, -0.1], [0.0, 0.1], 0.1)) == "passes"
    assert gate(_ev(8, [-0.3, 0.05], [0.0, 0.1], 0.1)) == "fails"
    assert gate(_ev(8, [-0.3, -0.1], [-0.05, 0.1], 0.1)) == "fails"
    assert gate(_ev(8, [-0.3, -0.1], [0.0, 0.1], 0.6)) == "fails"


# ----------------------------------------------------------------------------- commands


def test_cycle_scores_records_a_missed_week_and_freezes_the_target(ds, env):
    out = ds.cmd_cycle(env, TUE)
    assert out["status"] == "ok" and out["pregame"]["status"] == "frozen" and out["missed"] is None
    assert out["score"]["status"] == "no_ledgers"
    # a cycle after week 8 kicked off with no week-8 ledger: missed and warned
    sql(env, "UPDATE nfl_schedules SET home_score = 1, away_score = 0 WHERE season = 2026 AND week IN (7, 8)")
    late = ds.cmd_cycle(env, datetime(2026, 11, 3, 15, 0, tzinfo=UTC))
    assert late["missed"]["week"] == 8 and any("week 8 started" in w for w in late["warnings"])


def test_cli_exit_codes_and_status(ds, env, monkeypatch, capsys):
    base = ["--season", "2026", "--live-db", str(env.live_db), "--history-db", str(env.history_db),
            "--cache", str(env.cache), "--out", str(env.out)]
    assert ds.main(["pregame", "--week", "7", "--now", TUE.isoformat(), *base]) == 0          # dry run, no warnings
    assert '"would_freeze"' in capsys.readouterr().out and not (env.out / "shadow").exists()
    monkeypatch.setattr(ds, "run_pregame", lambda *a, **k: {"status": "failed", "warnings": ["x"]})
    assert ds.main(["pregame", "--week", "7", *base]) == 1
    monkeypatch.setattr(ds, "run_pregame", lambda *a, **k: {"status": "frozen", "warnings": ["degraded"]})
    assert ds.main(["pregame", "--week", "7", *base]) == 3
    assert ds.main(["status", *base]) == 0


# ----------------------------------------------------------------------------- the callers


@pytest.fixture(scope="module")
def lc():
    return _load("learning_cycle_dst", "learning_cycle.py")


class _Proc:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_learning_cycle_stage_command_and_failure_handling(lc, tmp_path, monkeypatch):
    ns = lc.parse(["run", "--season", "2026", "--live-db", str(tmp_path / "l.duckdb"), "--out", str(tmp_path / "w"),
                   "--dst-dir", str(tmp_path / "dst")])
    ns.week = 7
    seen = []
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or _Proc(
        3, json.dumps({"status": "ok", "warnings": ["no line"]}), "WARNING: no line"))
    assert lc.stage_dst_shadow(ns, [], tmp_path / "log.txt") == {"status": "ok", "warnings": ["no line"]}
    cmd, kw = seen[0]
    assert Path(cmd[1]).name == "dst_shadow.py" and cmd[2:5] == ["cycle", "--season", "2026"]
    assert cmd[cmd.index("--week") + 1] == "7" and cmd[cmd.index("--out") + 1] == str(tmp_path / "dst")
    assert kw["timeout"] == lc.DST_SHADOW_TIMEOUT_S and "--now" not in cmd
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: _Proc(1, "", "Traceback ... MemoryError"))
    with pytest.raises(RuntimeError, match="dst_shadow.py exited 1"):
        lc.stage_dst_shadow(ns, [], tmp_path / "log.txt")


def test_learning_cycle_stage_forwards_the_simulated_clock(lc, tmp_path, monkeypatch):
    ns = lc.parse(["run", "--season", "2026", "--now", "2027-01-20T12:00:00+00:00", "--out", str(tmp_path / "w")])
    seen = []
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc(0, "{}"))
    lc.stage_dst_shadow(ns, [], tmp_path / "log.txt")
    assert seen[0][seen[0].index("--now") + 1] == "2027-01-20T12:00:00+00:00" and "--week" not in seen[0]
    assert "--out" not in seen[0]                       # dst_shadow.py's own default (DST_RESEARCH_DIR)

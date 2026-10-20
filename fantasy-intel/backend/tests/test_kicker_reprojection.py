"""Final-designation re-projection of the kicker week run (app/analytics/kicker_reprojection.py).

Synthetic world (tests/synthetic_kicker.py), week 4: AAA (Kicker A, 101) at home to BBB (Kicker B, 102) on Thursday
night, CCC (Kicker H, 108) at home to DDD (Kicker E, 105) on Sunday. The Wednesday kicker run is the base; then
the late-week news. No test makes a network request (both week-4 venues are roofed; weather is never fetched).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.analytics import kicker_live as kl
from app.analytics import kicker_reprojection as kr
from app.analytics import kicker_store as ks
from app.db.connection import fetch_dicts

import synthetic_kicker as sk
from test_kicker_projections import scoring, world  # noqa: F401  (module fixtures)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WEDNESDAY = datetime(2026, 10, 14, 15, 0)          # naive UTC, like the store's utcnow
THURSDAY_NOON = datetime(2026, 10, 15, 16, 0)      # before AAA-BBB's 20:15 ET kickoff
FRIDAY = datetime(2026, 10, 16, 18, 0)             # after AAA-BBB (00:15 UTC Friday), before CCC-DDD on Sunday
SATURDAY = datetime(2026, 10, 17, 16, 0)
TUESDAY = datetime(2026, 10, 20, 12, 0)            # every week-4 game has kicked off


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc)


def _at(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(kl, "utcnow", lambda: when)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    sys.path.insert(0, str(SCRIPTS))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPTS))
    return mod


def _calendar(frame, games: dict):
    import polars as pl

    for (week, home), (day, clock) in games.items():
        hit = (pl.col("season") == 2026) & (pl.col("week") == week)
        if home is not None:
            hit = hit & (pl.col("home_team") == home)
        frame = frame.with_columns(pl.when(hit).then(pl.lit(day)).otherwise(pl.col("gameday")).alias("gameday"),
                                   pl.when(hit).then(pl.lit(clock)).otherwise(pl.col("gametime")).alias("gametime"))
    return frame


CALENDAR = {(4, None): ("2026-10-18", "13:00"), (4, "AAA"): ("2026-10-15", "20:15"), (5, None): ("2026-10-25", "13:00")}


@pytest.fixture
def week(con, world, scoring, tmp_path, monkeypatch):  # noqa: F811
    """Week 4 on a real calendar, the season artifact saved as a file, and the Wednesday kicker run stored."""
    sk.database(con, world)
    for (wk, home), (day, clock) in CALENDAR.items():
        con.execute("UPDATE nfl_schedules SET gameday = ?, gametime = ? WHERE season = 2026 AND week = ?"
                    + (" AND home_team = ?" if home else ""), [day, clock, wk] + ([home] if home else []))
    frames = {**world, "schedule": _calendar(world["schedule"], CALENDAR)}
    art = sk.artifact(world, scoring)
    adir = tmp_path / "kicker"
    adir.mkdir()
    apath = adir / f"{ks.MODEL_VERSION}_2026.json"
    apath.write_text(json.dumps(art.to_dict(), sort_keys=True))
    sha = hashlib.sha256(apath.read_bytes()).hexdigest()
    art = kl.load_artifact(apath)
    _at(monkeypatch, WEDNESDAY)
    meta, rows = kl.build_run(con, frames, art, 2026, 4, artifact_sha256=sha, now=_aware(WEDNESDAY),
                              fetch_weather=False)
    base = ks.write_run(con, meta, rows)
    by = {r["espn_player_id"]: r for r in rows}
    assert by[101]["coverage_status"] == by[105]["coverage_status"] == "projected"
    return SimpleNamespace(con=con, frames=frames, art=art, sha=sha, adir=adir, base_id=base["run_id"], base_rows=by,
                           db=tmp_path / "test.duckdb", fp=meta["scoring_fingerprint"])


def _reproject(w, monkeypatch, when=FRIDAY):
    _at(monkeypatch, when)
    base = kr.base_run(w.con, 2026, 4, w.fp)
    built = kr.build(w.con, w.frames, w.art, 2026, 4, base=base, artifact_sha256=w.sha, now=_aware(when),
                     fetch_weather=False)
    return built, kr.store(w.con, built)


def _rows(con, run_id):
    return {r["espn_player_id"]: r for r in fetch_dicts(con, f"SELECT * FROM {ks.ROWS} WHERE run_id = ?", [run_id])}


def _snapshot(con):
    return (con.execute(f"SELECT * FROM {ks.RUNS} ORDER BY run_id").fetchall(),
            con.execute(f"SELECT * FROM {ks.ROWS} ORDER BY run_id, espn_player_id").fetchall())


def _news(con, pids, status="OUT"):
    con.execute(f"UPDATE fantasy_players SET injury_status = ? WHERE espn_player_id IN ({', '.join(map(str, pids))})",
                [status])


# ----------------------------------------------------------------------------- the new run


def test_late_news_is_a_new_run_and_kicked_off_teams_are_carried(week, monkeypatch):
    before = _snapshot(week.con)
    _news(week.con, [101, 105])                    # Kicker A (Thursday game) and Kicker E (Sunday) ruled OUT
    built, res = _reproject(week, monkeypatch)
    assert res["status"] == kr.WRITTEN and res["created"] and res["run_id"] != week.base_id
    runs, rows = _snapshot(week.con)
    assert set(before[0]) < set(runs) and set(before[1]) < set(rows)                   # the base run is untouched
    assert set(built["kicked_off_teams"]) == {"AAA", "BBB"} and built["base_run_id"] == week.base_id
    new = _rows(week.con, res["run_id"])
    assert new[105]["coverage_status"] == "withheld_unavailable" and new[105]["pregame"]
    a = new[101]                                   # kicked off Thursday: the Wednesday row, flagged, not a forecast
    assert a["coverage_status"] == "projected" and a["expected_points"] == week.base_rows[101]["expected_points"]
    assert any(f.startswith(kr.CARRIED_PREFIX) and week.base_id in f for f in json.loads(a["flags"]))
    assert not a["pregame"]
    thursday = {pid for pid, r in week.base_rows.items() if r["nfl_team"] in ("AAA", "BBB")}
    assert {pid for pid, r in new.items() if any(f.startswith(kr.CARRIED_PREFIX) for f in json.loads(r["flags"]))} \
        == thursday and built["carried"] == len(thursday)
    assert built["changes"]["changed_by_team"] == {"DDD": 1}
    assert ks.select_run(week.con, 2026, 4)["run"]["run_id"] == res["run_id"]          # consumers see the new run
    o = ks.overlay(week.con, 2026, 4)
    assert o["origin"][105] == "ours_zero" and o["values"][101] == week.base_rows[101]["expected_points"]


def test_no_news_writes_nothing_and_a_retry_is_a_no_op(week, monkeypatch):
    before = _snapshot(week.con)
    built, res = _reproject(week, monkeypatch)
    assert res == {"status": kr.UNCHANGED, "run_id": week.base_id, "created": False} and _snapshot(week.con) == before
    _news(week.con, [105])
    _, first = _reproject(week, monkeypatch)
    assert first["status"] == kr.WRITTEN
    after = _snapshot(week.con)
    _, again = _reproject(week, monkeypatch, SATURDAY)
    assert again == {"status": kr.UNCHANGED, "run_id": first["run_id"], "created": False} and _snapshot(week.con) == after


def test_a_carried_row_keeps_the_original_runs_flag_across_reprojections(week, monkeypatch):
    _news(week.con, [105])
    _, first = _reproject(week, monkeypatch)
    _news(week.con, [105], "ACTIVE")               # cleared on Saturday
    built, second = _reproject(week, monkeypatch, SATURDAY)
    assert second["status"] == kr.WRITTEN and built["base_run_id"] == first["run_id"]
    flags = json.loads(_rows(week.con, second["run_id"])[101]["flags"])
    assert [f for f in flags if f.startswith(kr.CARRIED_PREFIX)] == [kr.CARRIED.format(run_id=week.base_id)]
    assert _rows(week.con, second["run_id"])[105]["coverage_status"] == "projected"


def test_unknown_or_missing_kickoffs_are_never_reprojected(week, monkeypatch):
    _news(week.con, [105])
    week.con.execute("UPDATE nfl_schedules SET gametime = NULL WHERE season = 2026 AND week = 4 AND home_team = 'CCC'")
    built, res = _reproject(week, monkeypatch, THURSDAY_NOON)
    assert {"CCC", "DDD"} <= set(built["unknown_kickoff_teams"]) and res["status"] == kr.UNCHANGED
    week.con.execute("DELETE FROM nfl_schedules WHERE season = 2026 AND week = 4 AND home_team = 'CCC'")
    built, res = _reproject(week, monkeypatch, THURSDAY_NOON)
    assert set(built["missing_from_schedule_teams"]) == {"CCC", "DDD"} and res["status"] == kr.UNCHANGED


def test_refusals(week, monkeypatch):
    base = kr.base_run(week.con, 2026, 4, week.fp)
    with pytest.raises(kr.ReprojectRefused, match="not the base run's"):
        kr.build(week.con, week.frames, week.art, 2026, 4, base=base, artifact_sha256="f" * 64, fetch_weather=False)
    with pytest.raises(kr.ReprojectRefused, match="no verified"):
        kr.check_base(kr.base_run(week.con, 2026, 5, week.fp), 2026, 5)
    with pytest.raises(kr.ReprojectRefused, match="every week-4 game has kicked off"):
        _reproject(week, monkeypatch, TUESDAY)
    with pytest.raises(kr.ReprojectRefused, match="no kfp artifact file"):
        kr.artifact_file(week.adir, "e" * 64)
    (week.adir / f"{ks.MODEL_VERSION}_2027.json").write_text("{}")                 # another file is never picked
    assert kr.artifact_file(week.adir, week.sha).name == f"{ks.MODEL_VERSION}_2026.json"


def test_a_betting_line_posted_after_the_base_run_is_a_change(week, monkeypatch):
    """kfp-1.1.0 takes the team implied total: Wednesday's run had no line (league-average fill, flagged); a line
    posted for the Sunday game since is a changed input and is written. (The synthetic history has no lines, so its
    o_implied coefficient is ~0 and only the row's line context and flag move here; in production the points move.)"""
    import polars as pl

    sun = (pl.col("season") == 2026) & (pl.col("week") == 4) & (pl.col("home_team") == "CCC")
    sched = week.frames["schedule"].with_columns(pl.when(sun).then(pl.lit(-7.0)).otherwise(None).alias("spread_line"),
                                                 pl.when(sun).then(pl.lit(51.5)).otherwise(None).alias("total_line"))
    w = SimpleNamespace(**{**vars(week), "frames": {**week.frames, "schedule": sched}})
    built, res = _reproject(w, monkeypatch)
    assert res["status"] == kr.WRITTEN
    new = _rows(week.con, res["run_id"])
    for pid in (105, 108):                                  # DDD at CCC: both kickers get the posted line
        assert week.base_rows[pid]["context"]["team"]["o_implied"] is None
        assert any("no betting line" in m for m in week.base_rows[pid]["missing_inputs"])
        assert json.loads(new[pid]["context"])["team"]["o_implied"] is not None
        assert not any("no betting line" in m for m in json.loads(new[pid]["missing_inputs"]))


def test_the_new_run_keeps_the_base_runs_model_version(week, monkeypatch):
    """A week whose consumer run is a retired version (kfp-1.0.0, weeks projected before kfp-1.1.0) is re-projected
    under that version with that version's artifact, never under the current MODEL_VERSION."""
    from dataclasses import replace

    old = replace(week.art, version=ks.RETIRED_VERSIONS[0])
    with pytest.raises(kr.ReprojectRefused, match="not the base run's model"):
        kr.build(week.con, week.frames, old, 2026, 4, base=kr.base_run(week.con, 2026, 4, week.fp),
                 artifact_sha256=week.sha, fetch_weather=False)
    week.con.execute(f"DELETE FROM {ks.ROWS}")
    week.con.execute(f"DELETE FROM {ks.RUNS}")
    _at(monkeypatch, WEDNESDAY)
    ks.write_run(week.con, *kl.build_run(week.con, week.frames, old, 2026, 4, artifact_sha256=week.sha,
                                         now=_aware(WEDNESDAY), fetch_weather=False))
    _news(week.con, [105])
    _, res = _reproject(SimpleNamespace(**{**vars(week), "art": old}), monkeypatch)
    assert res["status"] == kr.WRITTEN
    assert week.con.execute(f"SELECT model_version FROM {ks.RUNS} WHERE run_id = ?", [res["run_id"]]).fetchone()[0] \
        == ks.RETIRED_VERSIONS[0]


def test_in_progress_week_is_the_week_of_the_next_kickoff(week):
    assert kr.in_progress_week(week.con, 2026, _aware(FRIDAY)) == 4
    assert kr.in_progress_week(week.con, 2026, _aware(TUESDAY)) == 5


def test_a_new_forecast_is_a_change_on_purpose_but_a_re_read_is_not():
    """kfp-1.x ignores weather, but the row shows the forecast: a fresher forecast (new values) writes a run by
    design; the same forecast retrieved again (new times only) does not."""
    row = {c: None for c in ks.ROW_COLUMNS}
    wx = {"status": "ok", "values": {"wind_mph_mean": 12}, "retrieved_at": "2026-10-16T12:00:00", "age_hours": 1.0,
          "stale": False, "reason": None, "provider": "nws"}
    a = {**row, "espn_player_id": 1, "context": {"weather": wx},
         "explanation": [{"feature": "headline", "text": "x"}, {"feature": "weather_forecast", "text": "retrieved 12:00"}]}
    b = {**a, "context": {"weather": {**wx, "retrieved_at": "2026-10-18T09:00:00", "age_hours": 0.2}},
         "explanation": [{"feature": "headline", "text": "x"}, {"feature": "weather_forecast", "text": "retrieved 09:00"}]}
    assert kr.comparable(a) == kr.comparable(b)
    c = {**b, "context": {"weather": {**wx, "values": {"wind_mph_mean": 25}}}}
    assert kr.comparable(a) != kr.comparable(c)                          # a new forecast is a change


def test_the_app_never_imports_the_kicker_reprojection_writer():
    app = Path(kr.__file__).resolve().parents[1]
    for f in app.rglob("*.py"):
        if f.name != "kicker_reprojection.py":
            assert "kicker_reprojection" not in f.read_text(), f"{f.relative_to(app)} imports kicker_reprojection"


# ----------------------------------------------------------------------------- the CLI and the scheduled step


def test_cli_reproject_reuses_the_base_runs_artifact_file(week, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    cli = _load("kicker_projections")
    _news(week.con, [105])
    week.con.close()
    _at(monkeypatch, FRIDAY)
    args = argparse.Namespace(season=2026, live_db=week.db, history_db=week.db.parent / "none.duckdb", out=week.adir,
                              artifact=None, week=4, no_weather=True)
    # the CLI loads its frames from the database: the stored calendar is the one in force
    out = cli.cmd_reproject(args)
    assert out["status"] == kr.WRITTEN and out["artifact_id"] == week.sha and out["base_run_id"] == week.base_id
    assert out["kicked_off_teams"] == ["AAA", "BBB"] and out["changes"]["changed_by_team"] == {"DDD": 1}
    assert cli.cmd_reproject(args)["status"] == kr.UNCHANGED
    args.out = week.db.parent / "empty"
    args.out.mkdir()
    assert cli.main(["reproject", "--season", "2026", "--week", "4", "--live-db", str(week.db), "--history-db",
                     str(args.history_db), "--out", str(args.out), "--no-weather"]) == 2      # no artifact file: refused


def test_learning_cycle_runs_the_kicker_step_after_the_skill_position_step(week, monkeypatch, tmp_path):
    lc = _load("learning_cycle")
    args = lc.parse(["reproject", "--skip-sync", "--out", str(tmp_path / "weekly"), "--live-db", str(week.db),
                     "--week", "4"])
    monkeypatch.setattr(lc, "preflight", lambda a: None)
    calls = []
    monkeypatch.setattr(lc, "stage_kicker_reproject", lambda a, p: calls.append(a.week) or {"status": "written"})
    monkeypatch.setattr(lc, "stage_reproject", lambda a, p: {"refused": "no verified run"})
    code, report = lc.run_reproject(args, now=_aware(FRIDAY))
    assert code == lc.EXIT_NOT_READY and report["status"] == "refused" and calls == [4]     # ifp refusal: kicker still runs
    assert report["kicker_reprojection"] == {"status": "written"}
    assert [s["name"] for s in report["stages"]][-1] == "kicker_reproject"

    def boom(a, p):
        raise RuntimeError("kicker broke")
    monkeypatch.setattr(lc, "stage_kicker_reproject", boom)
    monkeypatch.setattr(lc, "stage_reproject", lambda a, p: {"status": "written", "week": 4, "run_id": "r"})
    code, report = lc.run_reproject(args, now=_aware(SATURDAY))
    assert code == lc.EXIT_OK and report["status"] == "written"                            # a kicker failure is a warning
    assert report["stages"][-1]["status"] == "failed_nonfatal" and "kicker broke" in report["warnings"][0]
    monkeypatch.setattr(lc, "stage_kicker_reproject", lambda a, p: calls.append("again"))
    args.no_kicker = True
    lc.run_reproject(args, now=_aware(SATURDAY))
    args.no_kicker, args.skip_sync = False, False
    monkeypatch.setattr(lc, "stage_sync_nfl", lambda a, p: (_ for _ in ()).throw(RuntimeError("feed down")))
    assert lc.run_reproject(args, now=_aware(SATURDAY))[0] == lc.EXIT_FAILED
    assert calls == [4]                                    # never with --no-kicker, never after a failed sync


def test_stage_kicker_reproject_returns_a_refusal(week, tmp_path):
    lc = _load("learning_cycle")
    week.con.close()
    args = lc.parse(["reproject", "--out", str(tmp_path / "weekly"), "--live-db", str(week.db), "--week", "4",
                     "--kicker-dir", str(tmp_path / "no-artifacts"), "--history-db", str(tmp_path / "none.duckdb")])
    (tmp_path / "no-artifacts").mkdir()
    out = lc.stage_kicker_reproject(args, tmp_path / "k.log")
    assert "no kfp artifact file" in out["refused"]

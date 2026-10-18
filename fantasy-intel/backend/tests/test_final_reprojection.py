"""Final-designation re-projection (app/analytics/final_reprojection.py, backtest_independent_projections.py
reproject, learning_cycle.py reproject): a later run of the week in progress with the base run's approved artifact.
Temp DuckDB files and synthetic artifacts only; the clock is fixed by patching the store's utcnow."""

from datetime import datetime, timezone
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analytics import final_reprojection as fr
from app.analytics import independent_projection_store as store
from app.analytics import injury_freshness as fresh
from app.analytics import production_forecasts as pf
from app.analytics import projection_source as ps
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import load_sources
from app.db.connection import fetch_dicts

from test_approved_artifacts import PROD, sha, weekly_artifact
from test_independent_projections import SCORING, live  # noqa: F401  (live is a fixture)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WEDNESDAY = datetime(2026, 10, 14, 15, 0)         # naive UTC, like the store's utcnow
FRIDAY = datetime(2026, 10, 16, 18, 0)            # after AAA's Thursday 20:15 ET (00:15 UTC Friday) kickoff
SATURDAY = datetime(2026, 10, 17, 16, 0)
TUESDAY = datetime(2026, 10, 20, 12, 0)            # every week-6 game has kicked off
THURSDAY_GAME = ("2026-10-15", "20:15")


def _at(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(store, "utcnow", lambda: when)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    sys.path.insert(0, str(SCRIPTS))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPTS))
    return mod


@pytest.fixture
def week(live, tmp_path, monkeypatch):  # noqa: F811
    """Week 6 with AAA (and its opponent) playing Thursday night, a registered store artifact, and the Wednesday
    production run stored. Then ESPN marks players 1 (AAA) and 6 (BBB) OUT: the late-week news."""
    con, _, path = live
    # a real calendar for weeks 6-7 (the synthetic schedule puts one week per day): Sunday 13:00 ET, AAA on Thursday
    con.execute("UPDATE nfl_schedules SET gameday = '2026-10-18', gametime = '13:00' WHERE season = 2026 AND week = 6")
    con.execute("UPDATE nfl_schedules SET gameday = '2026-10-25', gametime = '13:00' WHERE season = 2026 AND week = 7")
    con.execute("UPDATE nfl_schedules SET gameday = ?, gametime = ? WHERE season = 2026 AND week = 6 "
                "AND (home_team = 'AAA' OR away_team = 'AAA')", list(THURSDAY_GAME))
    src = load_sources(con, SCORING)
    art = weekly_artifact(src)
    _, apath, _ = wl.save_artifact(art, tmp_path)
    digest, _ = wl.register_saved_artifact(con, art, apath, tmp_path)
    _at(monkeypatch, WEDNESDAY)
    meta, rows = store.build_live(con, src, art, 2026, 6, artifact_sha256=digest)
    base = store.write_run(con, meta, rows)
    thursday = {t for t, ko in fresh.scheduled_kickoffs(con, 2026, 6).items()
                if ko is not None and fresh.naive_utc(ko) <= FRIDAY}
    assert "AAA" in thursday and "BBB" not in thursday
    assert rows_by(rows)[1]["coverage_status"] == rows_by(rows)[6]["coverage_status"] == "projected"
    con.execute("UPDATE fantasy_players SET injury_status = 'OUT' WHERE espn_player_id IN (1, 6)")
    return SimpleNamespace(con=con, src=src, art=art, sha=digest, path=path, base_id=base["run_id"],
                           base_rows={r["espn_player_id"]: r for r in rows}, thursday=thursday, root=tmp_path)


def rows_by(rows):
    return {r["espn_player_id"]: r for r in rows}


def _reproject(w, monkeypatch, when=FRIDAY):
    _at(monkeypatch, when)
    base = fr.base_run(w.con, 2026, 6, PROD, w.art.scoring_fingerprint)
    built = fr.build(w.con, w.src, w.art, 2026, 6, base=base, artifact_sha256=w.sha)
    return built, fr.store(w.con, built)


def _rows(con, run_id):
    return {r["espn_player_id"]: r for r in fetch_dicts(con, f"SELECT * FROM {pf.ROWS} WHERE run_id = ?", [run_id])}


def _snapshot(con):
    return (con.execute(f"SELECT * FROM {pf.RUNS} ORDER BY run_id").fetchall(),
            con.execute(f"SELECT * FROM {pf.ROWS} ORDER BY run_id, espn_player_id").fetchall())


# ----------------------------------------------------------------------------- the new run


def test_reprojection_is_a_new_run_and_the_base_run_is_untouched(week, monkeypatch):
    before = _snapshot(week.con)
    built, res = _reproject(week, monkeypatch)
    assert res["status"] == fr.WRITTEN and res["created"] and res["run_id"] != week.base_id
    runs, rows = _snapshot(week.con)
    assert [r for r in runs if r[0] != res["run_id"]] == before[0]           # every earlier run byte-identical
    assert [r for r in rows if r[0] != res["run_id"]] == before[1]
    for run_id in (week.base_id, res["run_id"]):                            # both verify against their own hash
        [run] = fetch_dicts(week.con, f"SELECT * FROM {pf.RUNS} WHERE run_id = ?", [run_id])
        assert pf.integrity_problem(run, list(_rows(week.con, run_id).values())) is None
    new = week.con.execute(f"SELECT artifact_id, forecast_horizon FROM {pf.RUNS} WHERE run_id = ?",
                           [res["run_id"]]).fetchone()
    assert new == (week.sha, 0)                                              # the base run's artifact, horizon 0
    assert built["base_run_id"] == week.base_id and built["kicked_off_teams"] == sorted(week.thursday)


def test_late_news_reaches_pregame_teams_and_kicked_off_teams_are_carried(week, monkeypatch):
    built, res = _reproject(week, monkeypatch)
    rows = _rows(week.con, res["run_id"])
    assert rows[6]["coverage_status"] == "withheld_unavailable" and rows[6]["pregame"]   # BBB: Sunday, re-projected
    carried = rows[1]                                                         # AAA: played Thursday night
    base = _rows(week.con, week.base_id)[1]
    for col in pf.ROW_COLUMNS:
        if col != "flags":
            assert carried[col] == base[col], col                             # the pregame number, unchanged
    assert json.loads(carried["flags"]) == json.loads(base["flags"]) + [fr.CARRIED.format(run_id=week.base_id)]
    assert carried["pregame"] is False or carried["pregame"] == 0             # never a post-kickoff "forecast"
    assert built["carried"] == sum(1 for r in rows.values() if r["nfl_team"] in week.thursday) > 0
    assert built["changes"]["coverage_status_changed"] == 1 and built["changes"]["changed_by_team"] == {"BBB": 1}


def test_consumers_see_the_new_run_and_evidence_keeps_each_players_pregame_row(week, monkeypatch):
    _, res = _reproject(week, monkeypatch)
    sel = ps.select_run(week.con, 2026, 6)
    assert sel["run"]["run_id"] == res["run_id"] and sel["run"]["artifact_check"] == "verified"
    view = ps.weekly(week.con, 2026, 6, "independent")
    assert view["values"][6] == 0.0 and view["origin"][6] == ps.OURS_ZERO        # Out now shows as Out
    late = [r for r in sel["rows"] if not r["pregame"]]
    assert view["timing"]["rows_not_pregame"] == len(late)
    assert {r["nfl_team"] for r in late} == week.thursday | {"HHH"}          # carried rows (+ the bye: no kickoff)
    ev, _ = pf.pregame_forecasts(week.con, 2026, [6], model_version=PROD, scoring_fingerprint=week.art.scoring_fingerprint)
    by = {r["espn_player_id"]: r["run_id"] for r in ev}
    assert by[1] == week.base_id and by[6] == res["run_id"]                  # evidence readers are unchanged


def test_an_unchanged_reprojection_writes_nothing_and_a_retry_is_a_no_op(week, monkeypatch):
    week.con.execute("UPDATE fantasy_players SET injury_status = 'ACTIVE' WHERE espn_player_id = 6")
    before = _snapshot(week.con)
    built, res = _reproject(week, monkeypatch)                # only the kicked-off AAA player changed: nothing pregame did
    assert res == {"status": fr.UNCHANGED, "run_id": week.base_id, "created": False}
    assert _snapshot(week.con) == before
    week.con.execute("UPDATE fantasy_players SET injury_status = 'OUT' WHERE espn_player_id = 6")
    _, first = _reproject(week, monkeypatch)
    _, again = _reproject(week, monkeypatch, SATURDAY)   # Saturday: the base is now the re-projection
    assert first["created"] and again["status"] == fr.UNCHANGED and again["run_id"] == first["run_id"]
    rows = _rows(week.con, first["run_id"])
    assert sum(fr.CARRIED_PREFIX in f for f in json.loads(rows[1]["flags"])) == 1


def test_a_carried_row_keeps_the_original_runs_flag_across_reprojections(week, monkeypatch):
    _, first = _reproject(week, monkeypatch)
    week.con.execute("UPDATE fantasy_players SET injury_status = 'QUESTIONABLE' WHERE espn_player_id = 6")
    _, second = _reproject(week, monkeypatch, SATURDAY)
    assert second["created"] and second["run_id"] != first["run_id"]
    flags = json.loads(_rows(week.con, second["run_id"])[1]["flags"])
    assert [f for f in flags if f.startswith(fr.CARRIED_PREFIX)] == [fr.CARRIED.format(run_id=week.base_id)]


# ----------------------------------------------------------------------------- refusals


def test_refuses_without_a_base_run_or_with_another_artifact(week, monkeypatch):
    with pytest.raises(fr.ReprojectRefused, match="no verified"):
        fr.check_base(fr.base_run(week.con, 2026, 7, PROD, week.art.scoring_fingerprint), 2026, 7, PROD)
    base = fr.base_run(week.con, 2026, 6, PROD, week.art.scoring_fingerprint)
    with pytest.raises(fr.ReprojectRefused, match="never a newer model"):
        fr.build(week.con, week.src, week.art, 2026, 6, base=base, artifact_sha256="0" * 64)
    with pytest.raises(fr.ReprojectRefused, match="artifact file sha256"):
        fr.check_base({"run": {**base["run"], "artifact_id": "dict:" + "a" * 64}, "rows": []}, 2026, 6, PROD)


def test_refuses_once_every_game_has_kicked_off(week, monkeypatch):
    before = _snapshot(week.con)
    with pytest.raises(fr.ReprojectRefused, match="nothing is still"):
        _reproject(week, monkeypatch, TUESDAY)
    assert _snapshot(week.con) == before


def test_unknown_kickoff_is_never_reprojected(week, monkeypatch):
    week.con.execute("UPDATE nfl_schedules SET gametime = NULL WHERE season = 2026 AND week = 6 "
                     "AND (home_team = 'BBB' OR away_team = 'BBB')")
    built, res = _reproject(week, monkeypatch)
    assert "BBB" in built["unknown_kickoff_teams"]
    assert res["status"] == fr.UNCHANGED                       # player 6's news is not applied: kickoff unprovable


def test_in_progress_week_is_the_week_of_the_next_kickoff(week):
    assert fr.in_progress_week(week.con, 2026, WEDNESDAY) == 6
    assert fr.in_progress_week(week.con, 2026, FRIDAY.replace(tzinfo=timezone.utc)) == 6   # after Thursday night
    assert fr.in_progress_week(week.con, 2026, TUESDAY) == 7


def test_injury_retrieval_times_are_reported_never_a_gate(week, monkeypatch):
    fresh.record_observations(week.con, fresh.espn_status_observations(
        [{"espn_player_id": 6, "pro_team": "BBB", "injury_status": "OUT"}], 2026,
        retrieved_at=datetime(2026, 10, 16, 16, 0)))
    built, res = _reproject(week, monkeypatch)
    f = built["injury_freshness"]
    assert f["available"] and f["teams_with_evidence_after_base_run"] == ["BBB"]
    team = next(t for t in f["teams"] if t["team"] == "BBB")
    assert team["source_used"] == fresh.SOURCE_ESPN and team["evidence_retrieved_at"].startswith("2026-10-16T16:00")
    assert res["status"] == fr.WRITTEN                          # stale/missing teams elsewhere change nothing


def test_the_app_never_imports_the_reprojection_writer():
    """Local-only (polars, the model packages, a write path): the hosted API and consumer views never import it."""
    app = Path(fr.__file__).resolve().parents[1]
    for f in app.rglob("*.py"):
        if f.name != "final_reprojection.py":
            assert "final_reprojection" not in f.read_text(), f"{f.relative_to(app)} imports final_reprojection"


# ----------------------------------------------------------------------------- the CLI and the scheduled step


def test_cli_reproject_loads_the_base_runs_artifact_file_by_sha(week, monkeypatch):
    mod = _load("backtest_independent_projections")
    newer = weekly_artifact(week.src)                              # a newer artifact for the week: never used
    newer = type(newer)(**{**newer.__dict__, "created_at": "2026-10-13T00:00:00+00:00"})
    wl.save_artifact(newer, week.root)
    week.con.close()
    _at(monkeypatch, FRIDAY)
    args = SimpleNamespace(model=PROD, weekly_dir=week.root, season=2026, week=None, out=week.root, live_db=week.path)
    out = mod.cmd_reproject(args, None, SCORING, now=FRIDAY.replace(tzinfo=timezone.utc))
    assert out["week"] == 6 and out["status"] == fr.WRITTEN and out["artifact_id"] == week.sha
    assert out["base_run_id"] == week.base_id and out["carried"] > 0 and out["kicked_off_teams"] == sorted(week.thursday)
    assert "AAA" not in out["changes"]["changed_by_team"]


def test_learning_cycle_reproject_reports_and_never_runs_the_cycle_stages(week, monkeypatch, tmp_path):
    lc = _load("learning_cycle")
    args = lc.parse(["reproject", "--skip-sync", "--out", str(tmp_path / "weekly"), "--live-db", str(week.path),
                     "--week", "6"])
    assert args.week == 6
    monkeypatch.setattr(lc, "preflight", lambda a: None)
    calls = []
    for name in ("stage_train", "stage_project", "stage_kicker", "stage_ctx_shadow"):
        monkeypatch.setattr(lc, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(lc, "stage_reproject", lambda a, p: {"refused": "no verified run"})
    code, report = lc.run_reproject(args, now=FRIDAY.replace(tzinfo=timezone.utc))
    assert code == lc.EXIT_NOT_READY and report["status"] == "refused" and calls == []
    monkeypatch.setattr(lc, "stage_reproject", lambda a, p: {"status": "written", "week": 6, "run_id": "r"})
    code, report = lc.run_reproject(args, now=SATURDAY.replace(tzinfo=timezone.utc))
    assert code == lc.EXIT_OK and report["status"] == "written" and calls == []
    latest = json.loads((tmp_path / "weekly" / "status" / "latest-reproject.json").read_text())
    assert latest["reprojection"]["run_id"] == "r"
    assert len(list((tmp_path / "weekly" / "reports").glob("reproject-*.json"))) == 2
    args.now = "2026-10-16T18:00:00+00:00"
    assert lc.run_reproject(args)[0] == lc.EXIT_CONFIG                     # no simulated clock for a writing step
    with pytest.raises(SystemExit):
        lc.parse(["run", "--week", "6"])


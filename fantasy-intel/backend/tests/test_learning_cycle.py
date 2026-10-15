"""scripts/learning_cycle.py on synthetic nflverse-shaped data: stage order, readiness gates,
kickoff safety, idempotence, locking and restartability. No network: the nflverse sync stages are
skipped (--skip-sync) or replaced; nothing is published and no production file is written."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

from app.analytics import weekly_learning as wl
from app.db.connection import connect, init_db
from synthetic_forecast import add_league, add_participation, generate, write

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
# synthetic week W kicks off 2025-09-W 13:00 US Eastern (17:00 UTC); weeks >= 10 have no results yet.
# The live DB records an nflverse sync at 2025-09-10 03:00 UTC, 10 h after week 9's kickoff, so week 9
# is presumed final (weekly_learning.game_presumed_final); cycles run after it and before week 10.
NFL_SYNC = "2025-09-10 03:00:00"
PRE_W10 = "2025-09-10T08:00:00+00:00"          # week 9 final, week 10 not started (a "Tuesday")
DURING_W10 = "2025-09-10T18:00:00+00:00"       # week 10 kicked off, not final


@pytest.fixture(scope="module")
def lc():
    import sys
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("learning_cycle", SCRIPTS / "learning_cycle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: Path, seasons, unfinished=None, league=True):
    con = connect(path)
    init_db(con)
    write(con, add_participation(generate(seasons, unfinished=unfinished)))
    if league:
        add_league(con, 2025, 10)
        con.execute("INSERT INTO sync_runs (run_id, source, started_at, finished_at, status, stats) VALUES "
                    "('synthetic-nfl', 'nfl', ?, ?, 'ok', ?)",
                    [NFL_SYNC, NFL_SYNC, json.dumps({"season": 2025, "nfl_schedules": 1})])
    con.close()


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    d = tmp_path_factory.mktemp("cycle_base")
    _make_db(d / "live.duckdb", [2024, 2025], {2025: 10})
    _make_db(d / "history.duckdb", [2024], league=False)
    return d


@pytest.fixture
def env(base, tmp_path, lc):
    """Fresh copies of the databases, a production ifp-1.0.0 directory and an output directory."""
    for name in ("live.duckdb", "history.duckdb"):
        shutil.copy(base / name, tmp_path / name)
    from app.analytics.independent_projection_store import league_scoring
    con = connect(tmp_path / "live.duckdb")
    _, scoring = league_scoring(con, 2025)
    con.close()
    ifp = tmp_path / "models"
    ifp.mkdir()
    sel = {"positions": {p: {"primary": "C", "lambda": {"B": 0.01, "C": 0.01}, "intervals": {}}
                         for p in ("QB", "RB", "WR", "TE")},
           "protocol": {"train_start_season": 2020}, "scoring_fingerprint": scoring.fingerprint}
    (ifp / "selection.json").write_text(json.dumps(sel))
    sha = hashlib.sha256((ifp / "selection.json").read_bytes()).hexdigest()[:16]
    (ifp / "heldout_report.json").write_text(json.dumps({"status": {}, "positions": {}, "selection_sha": sha}))
    return tmp_path


def _args(lc, env, now=PRE_W10, *extra):
    return lc.parse(["run", "--skip-sync", "--season", "2025", "--now", now, "--live-db", str(env / "live.duckdb"),
                     "--history-db", str(env / "history.duckdb"), "--ifp-dir", str(env / "models"),
                     "--out", str(env / "out"), *extra])


def _run(lc, env, now=PRE_W10, *extra):
    return lc.run_cycle(_args(lc, env, now, *extra))


def _sql(env, *stmts):
    con = connect(env / "live.duckdb")
    for s in stmts:
        con.execute(s)
    con.close()


def _files(env, sub):
    return sorted((env / "out" / sub).rglob("*.json")) if (env / "out" / sub).exists() else []


def _digest(paths):
    return {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}


def _stages(report):
    return [(s["name"], s["status"]) for s in report["stages"]]


# ----------------------------------------------------------------------------- happy path


def test_normal_pre_thursday_run_with_all_prior_games_final(lc, env):
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen", rep["errors"]
    assert rep["target_week"] == 10 and rep["target_first_kickoff"] == "2025-09-10T17:00:00+00:00"
    assert rep["target_first_kickoff_eastern"].startswith("2025-09-10T13:00:00-04:00")
    assert _stages(rep) == [("sync_nfl", "skipped"), ("sync_exits", "skipped"), ("load_sources", "ok"),
                            ("readiness", "ok"), ("evaluate", "ok"), ("train", "ok"), ("predict", "ok")]
    assert rep["data"]["readiness_ok"] and rep["data"]["previous_week_finality"]["fully_final"]
    assert rep["training"]["latest_included_week"] == [2025, 9] and not rep["training"]["reused"]
    assert all(a["created"] for a in rep["training"]["artifacts"].values())
    assert rep["forecast"]["new"] and rep["forecast"]["pregame_rows"] == rep["forecast"]["rows"] > 0
    assert rep["espn_pregame_benchmark"]["available"] and not rep["espn_pregame_benchmark"]["captured"]
    assert rep["missed_window"]["week"] == 9                 # the pipeline did not exist before week 9 kicked off
    assert not (env / "out" / "locks" / "cycle.lock").exists()
    latest = json.loads((env / "out" / "status" / "latest-cycle.json").read_text())
    assert latest["status"] == "forecast_frozen" and len(_files(env, "reports")) >= 2   # cycle + evaluate reports
    assert rep["prediction_models_changed"] is False


def test_repeated_identical_execution_is_a_no_op(lc, env):
    code1, rep1 = _run(lc, env)
    arts, ledger = _digest(_files(env, "artifacts")), _digest(_files(env, "ledger"))
    code2, rep2 = _run(lc, env, "2025-09-10T09:00:00+00:00")
    assert (code1, code2) == (lc.EXIT_OK, lc.EXIT_OK)
    assert rep2["status"] == "up_to_date" and rep2["training"]["reused"]
    assert not any(a["created"] for a in rep2["training"]["artifacts"].values())
    assert rep2["forecast"]["run_id"] == rep1["forecast"]["run_id"] and not rep2["forecast"]["new"]
    assert _digest(_files(env, "artifacts")) == arts and _digest(_files(env, "ledger")) == ledger
    assert rep2["training"]["artifacts"] == {v: {**a, "created": False} for v, a in rep1["training"]["artifacts"].items()}


def test_changed_source_data_before_cutoff_retrains_and_keeps_originals(lc, env):
    _, rep1 = _run(lc, env)
    arts, ledger = _digest(_files(env, "artifacts")), _digest(_files(env, "ledger"))
    _sql(env, "UPDATE nfl_weekly_stats SET receiving_yards = receiving_yards + 60 "
              "WHERE season = 2025 AND week = 8 AND player_id = '00-AAA-WR1'")   # a stat correction
    code, rep2 = _run(lc, env, "2025-09-10T11:00:00+00:00")
    assert code == lc.EXIT_OK and rep2["status"] == "forecast_frozen" and not rep2["training"]["reused"]
    assert rep2["training"]["fingerprint"] != rep1["training"]["fingerprint"]
    assert rep2["forecast"]["run_id"] != rep1["forecast"]["run_id"]
    # every original artifact and ledger run is still there, byte-identical
    assert all(_digest([p])[p] == h for p, h in {**arts, **ledger}.items())
    assert len(_files(env, "ledger")) == len(ledger) + 1
    assert len(_files(env, "artifacts")) > len(arts)
    # --force-retrain refits on identical inputs: content addressing reproduces the same ids
    code, rep3 = _run(lc, env, "2025-09-10T13:00:00+00:00", "--force-retrain")
    assert code == lc.EXIT_OK and not rep3["training"]["reused"]
    assert {v: a["id"] for v, a in rep3["training"]["artifacts"].items()} == \
        {v: a["id"] for v, a in rep2["training"]["artifacts"].items()}
    assert rep3["status"] == "up_to_date"


# ----------------------------------------------------------------------------- readiness gates


@pytest.mark.parametrize("stmt,needle", [
    ("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL WHERE season = 2025 AND week = 9 AND game_id = "
     "(SELECT max(game_id) FROM nfl_schedules WHERE season = 2025 AND week = 9)", "not final"),       # Monday game
    ("DELETE FROM nfl_snap_counts WHERE season = 2025 AND week = 9 AND team = 'AAA'", "snap counts"),
    ("DELETE FROM nfl_ff_opportunity WHERE season = 2025 AND week = 9", "expected points"),
    # the only nflverse sync ran 2 h after week 9's kickoff: its scores may be live
    ("UPDATE sync_runs SET started_at = TIMESTAMP '2025-09-09 19:00:00'", "cannot be presumed final"),
])
def test_incomplete_previous_week_blocks_training_and_prediction(lc, env, stmt, needle):
    _sql(env, stmt)
    code, rep = _run(lc, env)
    assert code == lc.EXIT_NOT_READY and rep["status"] == "waiting_for_data"
    assert any(needle in e for e in rep["errors"]), rep["errors"]
    assert rep["required_actions"] and "before Wed 2025-09-10 13:00" in rep["required_actions"][0]
    names = [s["name"] for s in rep["stages"]]
    assert "train" not in names and "predict" not in names and "evaluate" not in names
    assert _files(env, "artifacts") == [] and _files(env, "ledger") == []
    assert not (env / "out" / "locks" / "cycle.lock").exists()


def test_run_after_the_target_week_started_records_a_missed_window(lc, env):
    code, rep = _run(lc, env, DURING_W10)
    # week 10 has kicked off: the next target is week 11, which waits for week 10 to be final
    assert code == lc.EXIT_NOT_READY and rep["target_week"] == 11 and rep["status"] == "waiting_for_data"
    assert rep["missed_window"]["week"] == 10
    assert (env / "out" / "missed" / "s2025w10.json").exists()
    assert _files(env, "ledger") == [] and _files(env, "artifacts") == []    # nothing fabricated or backdated


def test_a_frozen_pregame_week_is_not_missed(lc, env):
    _run(lc, env)
    code, rep = _run(lc, env, DURING_W10)
    assert code == lc.EXIT_NOT_READY and rep["missed_window"] is None
    assert not (env / "out" / "missed" / "s2025w10.json").exists()


def test_historical_retrospective_replay_is_never_pregame_evidence(lc, env):
    args = ["--season", "2025", "--now", PRE_W10, "--live-db", str(env / "live.duckdb"),
            "--history-db", str(env / "history.duckdb"), "--ifp-dir", str(env / "models"), "--out", str(env / "out")]
    assert lc.cli.main(["train", "--week", "9", "--retrospective", *args]) == 0
    assert lc.cli.main(["predict", "--week", "9", "--retrospective", *args]) == 0
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK
    assert rep["missed_window"]["week"] == 9                  # a replay does not fill the pregame window
    assert rep["evaluation"]["prospective_weeks"] == 0
    runs = wl.load_ledger(env / "out")
    assert {r["kind"] for r in runs if r["week"] == 9} == {"retrospective"}


# ----------------------------------------------------------------------------- failures and restarts


def test_interrupted_training_leaves_nothing_and_retry_succeeds(lc, env, monkeypatch):
    def killed(*a, **k):
        raise RuntimeError("killed mid-training")
    monkeypatch.setattr(lc.cli, "cmd_train", killed)
    code, rep = _run(lc, env)
    assert code == lc.EXIT_FAILED and rep["status"] == "failed"
    assert ("train", "failed") in _stages(rep) and "predict" not in [s["name"] for s in rep["stages"]]
    assert not (env / "out" / "state").exists() and _files(env, "ledger") == []
    assert not (env / "out" / "locks" / "cycle.lock").exists()
    monkeypatch.undo()
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen"


def test_interrupted_write_never_leaves_a_partial_file(env, monkeypatch):
    target = env / "x" / "artifact.json"

    def crash(src, dst):
        raise OSError("power loss")
    monkeypatch.setattr(os, "link", crash)
    with pytest.raises(OSError):
        wl.write_once(target, "{}")
    assert not target.exists() and list(target.parent.iterdir()) == []
    monkeypatch.undo()
    assert wl.write_once(target, "{}") and not wl.write_once(target, '{"other": 1}')
    assert target.read_text() == "{}"
    # a truncated file left by an older version is reported, never overwritten or silently used
    bad = env / "x" / "bad.json"
    bad.write_text('{"trunc')
    with pytest.raises(wl.NotReady, match="interrupted write"):
        wl.load_artifact(bad)


def test_failure_in_one_stage_stops_every_later_stage(lc, env, monkeypatch):
    monkeypatch.setattr(lc.cli, "cmd_evaluate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad ledger")))
    code, rep = _run(lc, env)
    assert code == lc.EXIT_FAILED and ("evaluate", "failed") in _stages(rep)
    assert [s["name"] for s in rep["stages"]][-1] == "evaluate"
    assert _files(env, "artifacts") == [] and _files(env, "ledger") == []


def test_sync_failure_stops_the_cycle(lc, env, monkeypatch):
    monkeypatch.setattr(lc, "stage_sync_nfl", lambda args, log: (_ for _ in ()).throw(RuntimeError("lock held")))
    args = _args(lc, env)
    args.skip_sync = False
    code, rep = lc.run_cycle(args)
    assert code == lc.EXIT_FAILED and _stages(rep) == [("sync_nfl", "failed")]


def test_overlapping_attempts_are_refused_and_stale_locks_taken_over(lc, env):
    lock = env / "out" / "locks" / "cycle.lock"
    lock.parent.mkdir(parents=True)
    import socket
    held = {"pid": os.getpid(), "host": socket.gethostname(), "started_at": PRE_W10}
    lock.write_text(json.dumps(held))
    code, rep = _run(lc, env)
    assert code == lc.EXIT_LOCKED and rep["status"] == "locked" and rep["stages"] == []
    assert json.loads(lock.read_text()) == held                       # the other cycle's lock is untouched
    assert not (env / "out" / "status" / "latest-cycle.json").exists()
    lock.write_text(json.dumps({**held, "pid": 2 ** 22 + 4321}))      # that process is gone
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK and not lock.exists()
    lock.write_text(json.dumps({**held, "host": "another-mac", "started_at": "2025-09-09T00:00:00+00:00"}))
    code, rep = _run(lc, env, "2025-09-10T09:00:00+00:00")            # > 6 h old on another host: stale
    assert code == lc.EXIT_OK and rep["status"] == "up_to_date"


@pytest.mark.parametrize("setup,needle,action", [
    (lambda env, mp: mp.setenv("MOTHERDUCK_DATABASE", "fantasy_intel"), "MOTHERDUCK_DATABASE", "writable local"),
    (lambda env, mp: (env / "history.duckdb").unlink(), "history database", "sync_nfl_history.py"),
    (lambda env, mp: (env / "live.duckdb").unlink(), "live database", "sync_nfl.py"),
    (lambda env, mp: (env / "models" / "selection.json").unlink(), "selection.json", "INDEPENDENT_MODEL_DIR"),
])
def test_missing_dependency_is_an_actionable_error(lc, env, monkeypatch, setup, needle, action):
    setup(env, monkeypatch)
    code, rep = _run(lc, env)
    assert code == lc.EXIT_CONFIG and rep["status"] == "config_error"
    assert needle in rep["errors"][0] and any(action in a for a in rep["required_actions"])
    assert not (env / "out" / "locks" / "cycle.lock").exists() and _files(env, "artifacts") == []


def test_production_projections_and_models_are_unchanged(lc, env):
    prod = _digest([env / "models" / "selection.json", env / "models" / "heldout_report.json",
                    env / "live.duckdb", env / "history.duckdb"])
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK
    assert _digest(prod) == prod                                      # read-only on every production input
    con = connect(env / "live.duckdb")
    for table in ("independent_player_projections", "independent_projection_runs"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    con.close()
    assert all(str(env / "out") in a["path"] for a in rep["training"]["artifacts"].values())


def test_status_command_and_cli_entry(lc, env, capsys):
    assert lc.main(["run", "--skip-sync", "--season", "2025", "--now", PRE_W10,
                    "--live-db", str(env / "live.duckdb"), "--history-db", str(env / "history.duckdb"),
                    "--ifp-dir", str(env / "models"), "--out", str(env / "out")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "forecast_frozen" and out["target_week"] == 10
    assert lc.main(["status", "--out", str(env / "out")]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["latest_cycle"]["status"] == "forecast_frozen" and st["lock"] is None
    assert st["artifacts"] == {"ifp-1.0.0": 1, "ifp-2.0.0": 1} and st["ledger_runs"] == 1
    assert st["missed_windows"] == ["s2025w09.json"]

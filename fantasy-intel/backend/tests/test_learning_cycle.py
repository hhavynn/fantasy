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


def _args(lc, env, now=PRE_W10, *extra, project=False, kicker=False, ctx_shadow=False, signal_shadows=False):
    """Cycle arguments; the `project` stage (a live-database write) only when a test asks for it, and the kicker stage
    (after project) and the ifp-ctx-p1 / signal research shadow stages only when a test asks for them too."""
    return lc.parse(["run", "--skip-sync", "--season", "2025", "--now", now, "--live-db", str(env / "live.duckdb"),
                     "--history-db", str(env / "history.duckdb"), "--ifp-dir", str(env / "models"),
                     "--out", str(env / "out"), "--kicker-dir", str(env / "kicker"), "--ctx-dir", str(env / "ctx"),
                     *([] if project else ["--no-project"]), *([] if kicker else ["--no-kicker"]),
                     *([] if ctx_shadow else ["--no-ctx-shadow"]),
                     *([] if signal_shadows else ["--no-signal-shadows"]), *extra])


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
                            ("readiness", "ok"), ("evaluate", "ok"), ("train", "ok"), ("predict", "ok"),
                            ("project", "skipped")]
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
    assert lc.main(["run", "--skip-sync", "--no-project", "--season", "2025", "--now", PRE_W10,
                    "--live-db", str(env / "live.duckdb"), "--history-db", str(env / "history.duckdb"),
                    "--ifp-dir", str(env / "models"), "--out", str(env / "out")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "forecast_frozen" and out["target_week"] == 10
    assert lc.main(["status", "--out", str(env / "out")]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["latest_cycle"]["status"] == "forecast_frozen" and st["lock"] is None
    assert st["artifacts"] == {"ifp-1.0.0": 1, "ifp-2.0.0": 1} and st["ledger_runs"] == 1
    assert st["missed_windows"] == ["s2025w09.json"]


def test_empty_forecast_is_not_ready_not_success(lc, env):
    """No current-season roster rows: nothing can be forecast, so the cycle must keep retrying."""
    _sql(env, "DELETE FROM nfl_rosters_weekly WHERE season = 2025")
    code, rep = _run(lc, env)
    assert code == lc.EXIT_NOT_READY and rep["status"] == "waiting_for_data"
    assert ("predict", "not_ready") in _stages(rep) and any("no pregame forecast row" in e for e in rep["errors"])
    assert _files(env, "ledger") == []


def test_unreadable_ledger_file_still_writes_a_report(lc, env):
    bad = env / "out" / "ledger" / "s2025w09" / "pregame-truncated.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"trunc')                                       # left by an older, non-atomic writer
    code, rep = _run(lc, env)
    assert code == lc.EXIT_FAILED and rep["status"] == "failed"
    assert any("interrupted write" in e for e in rep["errors"]) and rep["required_actions"]
    assert json.loads((env / "out" / "status" / "latest-cycle.json").read_text())["status"] == "failed"
    assert not (env / "out" / "locks" / "cycle.lock").exists() and bad.read_text() == '{"trunc'


def test_sync_subprocess_uses_the_cycles_database_and_season(lc, env, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return type("Done", (), {"returncode": 0})()
    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    monkeypatch.setattr(lc, "stage_sync_exits", lambda args: {"seasons": []})
    args = _args(lc, env)
    args.skip_sync = False
    code, rep = lc.run_cycle(args)
    assert code == lc.EXIT_OK and seen["cmd"][-1].endswith("sync_nfl.py")
    assert seen["env"]["FANTASY_DB_PATH"] == str((env / "live.duckdb").resolve())
    assert seen["env"]["NFL_SEASON"] == "2025"


# ----------------------------------------------------------------------------- injury retrieval warnings


def test_cycle_report_carries_injury_warnings_but_status_and_exit_are_unchanged(lc, env):
    """The synthetic database has an empty retrieval log: every week-10 team is `missing`. That is a
    warning in the report and nothing else: same stages, same exit code, same frozen forecast."""
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen", rep["errors"]
    assert _stages(rep) == [("sync_nfl", "skipped"), ("sync_exits", "skipped"), ("load_sources", "ok"),
                            ("readiness", "ok"), ("evaluate", "ok"), ("train", "ok"), ("predict", "ok"),
                            ("project", "skipped")]
    assert any("no injury evidence retrieved" in w for w in rep["warnings"]), rep["warnings"]
    assert rep["warnings"] == rep["data"]["warnings"] and rep["errors"] == []
    info = rep["injury_freshness"]
    assert info["log_available"] and len(info["upcoming_issues"]) == 8


def test_cycle_handles_an_absent_injury_retrieval_log(lc, env):
    _sql(env, "DROP TABLE injury_retrievals")
    code, rep = _run(lc, env)
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen", rep["errors"]
    assert any(w.startswith("injury retrieval log unavailable") for w in rep["warnings"])
    assert rep["injury_freshness"]["log_available"] is False
    assert not any("stale" in w or "no injury evidence" in w for w in rep["warnings"])


def test_injury_warnings_do_not_turn_a_not_ready_cycle_into_anything_else(lc, env):
    _sql(env, "DELETE FROM nfl_snap_counts WHERE season = 2025 AND week = 9 AND team = 'AAA'")
    code, rep = _run(lc, env)
    assert code == lc.EXIT_NOT_READY and rep["status"] == "waiting_for_data"
    assert rep["warnings"] and any("no injury evidence retrieved" in w for w in rep["warnings"])
    assert all("injury" not in e for e in rep["errors"])


def _espn_players(env):
    """ESPN side of the synthetic league: every ACT skill player of the last roster week, with a confident id
    mapping (the cycle's synthetic database has no ESPN players otherwise, so `project` would store nothing)."""
    con = connect(env / "live.duckdb")
    players = con.execute("SELECT DISTINCT gsis_id, position, team FROM nfl_rosters_weekly WHERE season = 2025 "
                          "AND week = 9 AND status = 'ACT' AND position IN ('QB', 'RB', 'WR', 'TE') ORDER BY 1").fetchall()
    for pid, (g, pos, team) in enumerate(players, start=1):
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, on_team_id) "
                    "VALUES (?, ?, ?, ?, 1)", [pid, g, pos, team])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                    "VALUES (?, ?, 'espn_id:nfl_players', 'high')", [pid, g])
    con.close()


def test_kicker_stage_failure_never_fails_the_cycle_or_the_ifp_run(lc, env):
    """The kicker stage runs after `project`; here it fails (the synthetic databases hold no kicker data), and the
    cycle still exits OK with the ifp-2.0.0 run stored, the failure reported as a warning."""
    _espn_players(env)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True, kicker=True))
    assert code == lc.EXIT_OK, rep["errors"]
    assert _stages(rep)[-3:] == [("predict", "ok"), ("project", "ok"), ("kicker", "failed_nonfatal")]
    assert any("kicker projection failed" in w for w in rep["warnings"])
    con = connect(env / "live.duckdb")
    assert con.execute("SELECT count(*) FROM independent_projection_runs WHERE model_version = 'ifp-2.0.0'"
                       ).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM kicker_projection_runs").fetchone()[0] == 0
    con.close()


def test_cycle_stores_the_production_ifp2_run_the_switch_reads(lc, env):
    """The `project` stage runs backtest_independent_projections.py project on the artifact the cycle just
    trained: an ifp-2.0.0 run in the live DB, identified by that artifact file's sha256; a rerun is a no-op.
    (The same stage also stores the rest-of-season runs: test_cycle_projects_every_remaining_week...)"""
    from app.analytics import projection_source as ps

    _espn_players(env)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True))
    assert code == lc.EXIT_OK, rep["errors"]
    assert _stages(rep)[-2:] == [("predict", "ok"), ("project", "ok")]
    art = rep["training"]["artifacts"]["ifp-2.0.0"]["path"]
    assert rep["projection"]["model_version"] == ps.MODEL_VERSION == "ifp-2.0.0" and rep["projection"]["created"]
    assert rep["projection"]["artifact_id"] == hashlib.sha256(Path(art).read_bytes()).hexdigest()
    con = connect(env / "live.duckdb")
    runs = con.execute("SELECT run_id, model_version, row_count FROM independent_projection_runs").fetchall()
    assert runs == [(rep["projection"]["run_id"], "ifp-2.0.0", rep["projection"]["rows"])] and runs[0][2] > 0
    models = {r[0] for r in con.execute("SELECT DISTINCT model FROM independent_projection_rows "
                                        "WHERE coverage_status = 'projected'").fetchall()}
    assert models == {"ifp2_ridge"}
    assert ps.weekly(con, 2025, 10, "independent")["run"]["run_id"] == runs[0][0]
    con.close()
    code, again = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True))
    assert code == lc.EXIT_OK and again["status"] == "up_to_date"
    assert again["projection"]["run_id"] == runs[0][0] and again["projection"]["created"] is False


LAST_WEEK = 17      # the synthetic season's last regular-season week


def _runs(env):
    con = connect(env / "live.duckdb")
    try:
        return con.execute("SELECT week, forecast_horizon, projected_from_week, artifact_id, run_id, row_count "
                           "FROM independent_projection_runs ORDER BY week").fetchall()
    finally:
        con.close()


def test_cycle_projects_every_remaining_week_from_the_target_weeks_artifact(lc, env):
    """Target week W = 10: runs for weeks 10..17, every one by W's artifact (one sha), horizons 0..7, projected
    from week 10; a week-12 bye is a bye row; an unchanged cycle writes nothing; the approved-artifact gate accepts
    W's artifact for the later weeks; evidence readers see only the horizon-0 run."""
    from app.analytics import production_forecasts as pf
    from app.analytics import projection_source as ps
    from app.analytics.independent_projection_store import league_scoring

    _espn_players(env)
    _sql(env, "DELETE FROM nfl_schedules WHERE season = 2025 AND week = 12 AND (home_team = 'AAA' OR away_team = 'AAA')")
    code, rep = lc.run_cycle(_args(lc, env, project=True))
    assert code == lc.EXIT_OK, rep["errors"]
    assert rep["last_regular_season_week"] == LAST_WEEK
    art_sha = hashlib.sha256(Path(rep["training"]["artifacts"]["ifp-2.0.0"]["path"]).read_bytes()).hexdigest()
    runs = _runs(env)
    assert [r[0] for r in runs] == list(range(10, LAST_WEEK + 1))
    assert [r[1] for r in runs] == list(range(0, LAST_WEEK - 10 + 1))
    assert {r[2] for r in runs} == {10} and {r[3] for r in runs} == {art_sha} and all(r[5] > 0 for r in runs)
    ros = rep["projection"]["rest_of_season"]
    assert [r["week"] for r in ros] == list(range(11, LAST_WEEK + 1)) and all(r["created"] for r in ros)
    assert rep["projection"]["rest_of_season_summary"]["failed"] == []
    assert not any("rest-of-season" in w for w in rep["warnings"])
    assert rep["status"] == "forecast_frozen"

    con = connect(env / "live.duckdb")
    bye = con.execute("SELECT count(*) FROM independent_projection_rows r JOIN independent_projection_runs u USING (run_id) "
                      "WHERE u.week = 12 AND r.nfl_team = 'AAA' AND r.coverage_status = 'bye'").fetchone()[0]
    assert bye > 0
    flags = con.execute("SELECT flags FROM independent_projection_rows r JOIN independent_projection_runs u USING (run_id) "
                        "WHERE u.week = 13 AND r.coverage_status = 'projected' LIMIT 1").fetchone()[0]
    assert "future week: no injury report yet" in flags and any("horizon 3" in f for f in json.loads(flags))
    # the gate: W's artifact is registered once (target week 10, cutoff before week 10) and covers every later week
    fp = league_scoring(con, 2025)[1].fingerprint
    for week in (10, 13, LAST_WEEK):
        run = pf.latest_run(con, 2025, week, model_version="ifp-2.0.0", scoring_fingerprint=fp)
        assert run is not None and run["artifact_check"] == "verified" and run["horizon"] == week - 10
    assert con.execute("SELECT count(*) FROM approved_artifacts WHERE model_version = 'ifp-2.0.0'").fetchone()[0] == 1
    assert ps.weekly(con, 2025, 14, "independent")["run"]["run_id"] == next(r[4] for r in runs if r[0] == 14)
    # evidence: horizon 0 only, the rest excluded with the reason
    forecasts, excluded = pf.pregame_forecasts(con, 2025, None, model_version="ifp-2.0.0", scoring_fingerprint=fp)
    assert {f["week"] for f in forecasts} <= {10}
    assert {e["week"] for e in excluded if e["reason"].startswith("rest-of-season forecast")} == \
        set(range(11, LAST_WEEK + 1))
    con.close()

    # an unchanged cycle writes nothing: same runs, same ids
    code, again = lc.run_cycle(_args(lc, env, project=True))
    assert code == lc.EXIT_OK and again["status"] == "up_to_date"
    assert _runs(env) == runs
    assert not any(r["created"] for r in again["projection"]["rest_of_season"]) and not again["projection"]["created"]


def _cli():
    spec = importlib.util.spec_from_file_location("backtest_cli3", SCRIPTS / "backtest_independent_projections.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_a_failed_future_week_is_reported_and_the_other_weeks_are_still_projected(lc, env, monkeypatch):
    import argparse

    from app.analytics import independent_projection_store as store

    _espn_players(env)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True))   # trains + registers W=10
    assert code == lc.EXIT_OK, rep["errors"]
    cli = _cli()
    real = store.build_live

    def flaky(con, src, art, season, week, **kw):
        if week == 13:
            raise RuntimeError("boom")
        return real(con, src, art, season, week, **kw)

    monkeypatch.setattr(store, "build_live", flaky)
    args = argparse.Namespace(live_db=env / "live.duckdb", history_db=env / "history.duckdb", out=env / "models",
                              weekly_dir=env / "out", season=2025, week=10, through_week=15, artifact_week=None,
                              model="ifp-2.0.0", register_artifact="learning_cycle")
    hist = cli._ro(args.history_db)
    live = cli._ro(args.live_db)
    scoring = cli._scoring(live, 2025)
    live.close()
    out = cli.cmd_project(args, hist, scoring)
    hist.close()
    assert out["week"] == 10 and out["forecast_horizon"] == 0
    assert out["rest_of_season_failed"] == [13]
    by_week = {r["week"]: r for r in out["rest_of_season"]}
    assert by_week[13]["error"] == "RuntimeError: boom"
    assert {w for w, r in by_week.items() if "error" not in r} == {11, 12, 14, 15}
    assert [r[0] for r in _runs(env)] == [10, 11, 12, 14, 15]


def test_the_cycle_report_lists_a_failed_future_week_without_failing_the_cycle(lc, env, monkeypatch):
    _espn_players(env)

    def fake_project(args, conns, log_path):
        for con in conns:
            con.close()
        conns.clear()
        return {"run_id": "r10", "created": True, "rows": 5, "model_version": "ifp-2.0.0", "artifact_id": "a" * 64,
                "rest_of_season": [{"week": 11, "forecast_horizon": 1, "run_id": "r11", "created": True, "rows": 5},
                                   {"week": 12, "forecast_horizon": 2, "error": "RuntimeError: boom"}]}

    monkeypatch.setattr(lc, "stage_project", fake_project)
    code, rep = lc.run_cycle(_args(lc, env, project=True))
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen"
    assert rep["projection"]["rest_of_season_summary"] == {
        "weeks": [11], "created": 1, "unchanged": 0, "failed": [{"week": 12, "error": "RuntimeError: boom"}]}
    assert any("week 12 failed: RuntimeError: boom" in w for w in rep["warnings"])


def test_project_cli_with_artifact_week_projects_a_later_week_from_that_artifact(lc, env):
    _espn_players(env)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True))
    assert code == lc.EXIT_OK, rep["errors"]
    spec = importlib.util.spec_from_file_location("backtest_cli2", SCRIPTS / "backtest_independent_projections.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    base = ["project", "--season", "2025", "--live-db", str(env / "live.duckdb"), "--history-db",
            str(env / "history.duckdb"), "--out", str(env / "models"), "--weekly-dir", str(env / "out")]
    assert cli.main([*base, "--week", "13", "--artifact-week", "10", "--register-artifact", "learning_cycle"]) == 0
    runs = _runs(env)
    assert [(r[0], r[1], r[2]) for r in runs] == [(10, 0, 10), (13, 3, 10)] and len({r[3] for r in runs}) == 1
    assert cli.main([*base, "--week", "13", "--artifact-week", "10"]) == 0       # identical retry: no new run
    assert len(_runs(env)) == 2
    assert cli.main([*base, "--week", "9", "--artifact-week", "10"]) == 2        # an artifact cannot project the past
    assert cli.main([*base, "--week", "13", "--artifact-week", "12"]) == 2       # no week-12 artifact was trained


def test_project_cli_refuses_a_week_without_an_ifp2_artifact(lc, env):
    _run(lc, env)
    spec = importlib.util.spec_from_file_location("backtest_cli", SCRIPTS / "backtest_independent_projections.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["project", "--season", "2025", "--week", "11", "--live-db", str(env / "live.duckdb"),
                     "--history-db", str(env / "history.duckdb"), "--out", str(env / "models"),
                     "--weekly-dir", str(env / "out")]) == 2


# ----------------------------------------------------------------------------- ifp-ctx-p1 research shadow stage


def test_ctx_shadow_stage_runs_last_and_is_a_no_op_before_the_prospective_window(lc, env):
    """The real scripts/ctx_shadow.py subprocess on the synthetic 2025 world: before the pre-registered 2026 week-7
    start nothing is forecast, and there are no ledgers to score, so it changes nothing and reports no warning."""
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, ctx_shadow=True))
    assert code == lc.EXIT_OK, rep["errors"]
    assert _stages(rep)[-3:] == [("predict", "ok"), ("project", "skipped"), ("ctx_shadow", "ok")]
    cs = rep["ctx_shadow"]
    assert (cs["status"], cs["pregame"]["status"], cs["score"]["status"]) == (
        "ok", "before_prospective_start", "no_ledgers")
    assert not [w for w in rep["warnings"] if "ifp-ctx-p1" in w]
    assert not (env / "ctx").exists()                       # nothing written anywhere


def test_ctx_shadow_failure_never_fails_the_cycle_or_the_production_run(lc, env, monkeypatch):
    _espn_players(env)

    def boom(args, conns, log_path):
        raise RuntimeError("ctx_shadow.py exited 1: MemoryError")

    monkeypatch.setattr(lc, "stage_ctx_shadow", boom)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, "--no-rest-of-season", project=True, ctx_shadow=True))
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen", rep["errors"]
    assert _stages(rep)[-3:] == [("predict", "ok"), ("project", "ok"), ("ctx_shadow", "failed_nonfatal")]
    assert any("ifp-ctx-p1 shadow stage failed" in w for w in rep["warnings"])
    con = connect(env / "live.duckdb")
    assert con.execute("SELECT count(*) FROM independent_projection_runs WHERE model_version = 'ifp-2.0.0'"
                       ).fetchone()[0] == 1
    con.close()


def test_ctx_shadow_warnings_reach_the_cycle_report(lc, env, monkeypatch):
    monkeypatch.setattr(lc, "stage_ctx_shadow", lambda args, conns, log_path: {
        "status": "ok", "warnings": ["the week's nflverse injury report is not ingested yet"]})
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, ctx_shadow=True))
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen"
    assert "ifp-ctx-p1 shadow: the week's nflverse injury report is not ingested yet" in rep["warnings"]
    stored = json.loads((env / "out" / "status" / "latest-cycle.json").read_text())
    assert stored["ctx_shadow"]["warnings"] and stored["stages"][-1]["name"] == "ctx_shadow"


def test_ctx_shadow_still_scores_when_no_week_is_left(lc, env, monkeypatch):
    """After the season's last kickoff the cycle stops with no_upcoming_week (exit 0), but the shadow stage still runs
    first, so the last frozen week is scored once it is final."""
    calls = []
    monkeypatch.setattr(lc, "stage_ctx_shadow", lambda args, conns, log_path: calls.append(args.week) or {
        "status": "ok", "warnings": []})
    code, rep = lc.run_cycle(_args(lc, env, "2026-06-01T00:00:00+00:00", ctx_shadow=True))
    assert code == lc.EXIT_OK and rep["status"] == "no_upcoming_week", rep["errors"]
    assert calls == [None] and ("ctx_shadow", "ok") in _stages(rep)


# ----------------------------------------------------------------------------- signal research shadows stage


def test_signal_shadows_stage_runs_after_ctx_shadow_and_is_a_no_op_before_the_prospective_window(lc, env):
    """The real scripts/signal_shadows.py subprocess on the synthetic 2025 world: before 2026 week 7 nothing is
    fetched or forecast and there is no ledger to score, so it writes nothing and reports no warning."""
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, ctx_shadow=True, signal_shadows=True))
    assert code == lc.EXIT_OK, rep["errors"]
    assert _stages(rep)[-2:] == [("ctx_shadow", "ok"), ("signal_shadows", "ok")]
    ss = rep["signal_shadows"]
    assert (ss["status"], ss["pregame"]["status"], ss["score"]["status"], ss["prepare"]) == (
        "ok", "before_prospective_start", "no_ledgers", None)
    assert not [w for w in rep["warnings"] if "signal shadows" in w]


def test_signal_shadows_failure_never_fails_the_cycle(lc, env, monkeypatch):
    def boom(args, conns, log_path):
        raise RuntimeError("signal_shadows.py exited 1: nflverse unreachable")

    monkeypatch.setattr(lc, "stage_signal_shadows", boom)
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, signal_shadows=True))
    assert code == lc.EXIT_OK and rep["status"] == "forecast_frozen", rep["errors"]
    assert _stages(rep)[-1] == ("signal_shadows", "failed_nonfatal")
    assert any("signal shadows stage failed (production runs unaffected)" in w for w in rep["warnings"])


def test_signal_shadows_warnings_reach_the_report_and_it_runs_when_no_week_is_left(lc, env, monkeypatch):
    calls = []
    monkeypatch.setattr(lc, "stage_signal_shadows", lambda args, conns, log_path: calls.append(args.week) or {
        "status": "ok", "warnings": ["depth_2026 fetch failed; using the stored snapshot"]})
    code, rep = lc.run_cycle(_args(lc, env, PRE_W10, signal_shadows=True))
    assert code == lc.EXIT_OK and "signal shadows: depth_2026 fetch failed; using the stored snapshot" in rep["warnings"]
    code, rep = lc.run_cycle(_args(lc, env, "2026-06-01T00:00:00+00:00", signal_shadows=True))
    assert rep["status"] == "no_upcoming_week" and calls[-1] is None and ("signal_shadows", "ok") in _stages(rep)

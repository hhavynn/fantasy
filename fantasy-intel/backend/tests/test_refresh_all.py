"""scripts/refresh_all.py: step order, MotherDuck gating, stop-on-failure and the pregame warning."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def refresh(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("refresh_all", SCRIPTS / "refresh_all.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_publish_only_with_a_motherduck_token(refresh, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    assert refresh.plan(publish=True) == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py"]
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "  ")
    assert "publish_motherduck.py" not in refresh.plan(publish=True)
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    assert refresh.plan(publish=True)[-1] == "publish_motherduck.py"
    assert "publish_motherduck.py" not in refresh.plan(publish=False)


def test_a_failed_sync_skips_the_steps_that_need_it(refresh, monkeypatch, tmp_path):
    for name, body in (("sync_espn.py", "print('espn ok')"), ("sync_nfl.py", "raise SystemExit(3)"),
                       ("compute_analytics.py", "open(__file__ + '.ran', 'w').close()")):
        (tmp_path / name).write_text(body)
    shadows = []
    monkeypatch.setattr(refresh, "SCRIPTS", tmp_path)
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "check_pregame", lambda log: pytest.fail("check must not run after a failure"))
    monkeypatch.setattr(refresh, "run_evidence", lambda *a, **k: pytest.fail("no full evidence after a failed sync"))
    monkeypatch.setattr(refresh, "run_shadows", lambda log: shadows.append(log))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish"])
    assert refresh.main() == 1
    assert not (tmp_path / "compute_analytics.py.ran").exists()
    log = next((tmp_path / "logs").glob("refresh-*.log")).read_text()
    assert "espn ok" in log and "<== exit 3" in log and "FAILED: sync_nfl.py exited 3" in log
    assert "SKIPPED: compute_analytics.py (needs sync_nfl.py, which failed)" in log
    assert len(shadows) == 1                       # the research shadows' freezes still ran


def _snap(**kw):
    return {"week": 6, "first_kickoff": "2026-10-16T00:15:00+00:00", "last_kickoff": "2026-10-20T00:15:00+00:00",
            "latest_pregame_snapshot_at": None, "latest_snapshot_at": None, "captured": False, "players": 0, **kw}


def test_pregame_warning(refresh):
    warn = refresh.pregame_warning
    before = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)
    assert warn(_snap(captured=True, latest_pregame_snapshot_at="2026-10-15T22:00:00+00:00"), after) is None
    assert "must land before the first kickoff" in warn(_snap(), before)
    late = warn(_snap(latest_snapshot_at="2026-10-16T01:00:00+00:00"), after)
    assert late.startswith("missed the week 6 pregame ESPN snapshot") and "2026-10-16T01:00" in late
    assert "can't be checked" in warn(_snap(first_kickoff=None), after)


# ----------------------------------------------------------------------------- evidence stage

UTC = timezone.utc
# Synthetic schedule (UTC). Week 6: TNF Thu 2026-10-15 20:15 ET, Sunday 13:00 ET, SNF, MNF Mon 20:15 ET.
# Week 7 starts the next Thursday.
KICKOFFS = {
    6: [datetime(2026, 10, 16, 0, 15, tzinfo=UTC), datetime(2026, 10, 18, 17, 0, tzinfo=UTC),
        datetime(2026, 10, 19, 0, 20, tzinfo=UTC), datetime(2026, 10, 20, 0, 15, tzinfo=UTC)],
    7: [datetime(2026, 10, 23, 0, 15, tzinfo=UTC), datetime(2026, 10, 25, 17, 0, tzinfo=UTC)],
}
PT = timezone(timedelta(hours=-7))      # Pacific daylight time, the launchd schedule's zone


@pytest.mark.parametrize("when, upcoming, done", [
    (datetime(2026, 10, 15, 15, 0, tzinfo=PT), 6, []),           # Thu 15:00 PT, before TNF
    (datetime(2026, 10, 18, 8, 0, tzinfo=PT), 6, []),            # Sun 08:00 PT: TNF kicked off, week 6 still current
    (datetime(2026, 10, 19, 15, 0, tzinfo=PT), 6, []),           # Mon 15:00 PT: before MNF (20:15 ET)
    (datetime(2026, 10, 20, 8, 0, tzinfo=PT), 7, [6]),           # Tue 08:00 PT: week 6 over, next week upcoming
    (datetime(2026, 10, 22, 15, 0, tzinfo=PT), 7, [6]),          # Thu 15:00 PT before week 7's TNF
    (datetime(2026, 10, 26, 8, 0, tzinfo=PT), None, [7, 6]),     # past the synthetic schedule
])
def test_week_selection_around_kickoffs(refresh, when, upcoming, done):
    assert refresh.pick_evidence_weeks(KICKOFFS, when.astimezone(UTC)) == (upcoming, done)


def test_week_selection_ignores_weeks_without_a_known_kickoff(refresh):
    assert refresh.pick_evidence_weeks({6: [], 7: KICKOFFS[7]}, datetime(2026, 10, 1, tzinfo=UTC)) == (7, [])


def _schedule_db(path):
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, "
                "gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.execute("CREATE TABLE leagues (league_id VARCHAR, season INT, current_week INT, synced_at TIMESTAMP)")
    con.execute("INSERT INTO leagues VALUES ('L', 2026, 6, '2026-10-14 12:00:00')")
    games = [(6, "2026-10-15", "20:15", "AAA", "BBB"), (6, "2026-10-18", "13:00", "CCC", "DDD"),
             (6, "2026-10-19", "20:15", "EEE", "FFF"), (7, "2026-10-22", "20:15", "BBB", "CCC"),
             (7, "2026-10-25", "13:00", "AAA", "DDD")]
    con.executemany("INSERT INTO nfl_schedules VALUES (2026, ?, 'REG', ?, ?, ?, ?)", games)
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 18, 'POST', '2027-01-20', '20:00', 'AAA', 'BBB')")
    con.close()


def test_selection_reads_the_schedule_and_benchmarks_only_a_fully_final_week(refresh, tmp_path):
    import duckdb

    db = tmp_path / "live.duckdb"
    _schedule_db(db)
    con = duckdb.connect(str(db), read_only=True)
    try:
        tue = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)         # all of week 6 has kicked off
        asked = []

        def not_final(c, season, week):
            asked.append(week)
            return False, "2/6 teams final"

        sel = refresh.select_evidence(con, 2026, tue, final=not_final)
        assert (sel["week"], sel["final_week"], asked) == (7, None, [6])
        assert "week 6 is not fully final (2/6 teams final)" in sel["notes"][0]
        assert [label for label, _ in refresh.evidence_steps(sel)] == [
            "injury_freshness week 7", "start_sit_evaluation week 7", "ctx_shadow pregame week 7",
            "qb_scenario_shadow pregame week 7", "signal_shadows pregame week 7",
            "injury_return_shadow pregame week 7", "dst_shadow pregame week 7",
            "distribution_ledger pregame week 7", "ipm_cal_shadow cycle"]   # no benchmark

        sel = refresh.select_evidence(con, 2026, tue, final=lambda c, s, w: (True, ""))
        assert (sel["week"], sel["final_week"]) == (7, 6)
        steps = dict(refresh.evidence_steps(sel))
        assert list(steps) == ["injury_freshness week 7", "start_sit_evaluation week 7", "ctx_shadow pregame week 7",
                               "qb_scenario_shadow pregame week 7", "signal_shadows pregame week 7",
                               "injury_return_shadow pregame week 7", "dst_shadow pregame week 7", "distribution_ledger pregame week 7", "ipm_cal_shadow cycle",
                               "espn_benchmark week 6"]
        assert steps["signal_shadows pregame week 7"][2:] == ["pregame", "--season", "2026", "--week", "7"]
        assert steps["injury_return_shadow pregame week 7"][2:] == ["pregame", "--season", "2026", "--week", "7"]
        assert steps["distribution_ledger pregame week 7"][2:] == ["freeze", "--season", "2026", "--week", "7"]
        assert steps["ctx_shadow pregame week 7"][2:] == ["pregame", "--season", "2026", "--week", "7"]
        assert steps["qb_scenario_shadow pregame week 7"][2:] == ["pregame", "--season", "2026", "--week", "7"]
        assert steps["dst_shadow pregame week 7"][2:] == ["pregame", "--season", "2026", "--week", "7"]
        assert steps["ipm_cal_shadow cycle"][2:] == ["cycle", "--season", "2026"]
        assert steps["espn_benchmark week 6"][2:] == ["record", "--season", "2026", "--week", "6"]
        assert steps["injury_freshness week 7"][2] == "report"

        thu = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)          # week 6 not started: nothing can be final
        sel = refresh.select_evidence(con, 2026, thu, final=lambda *a: pytest.fail("no finished week to check"))
        assert (sel["week"], sel["final_week"]) == (6, None)

        def broken(c, season, week):
            raise ValueError("unsupported scoring")

        sel = refresh.select_evidence(con, 2026, tue, final=broken)    # a failing check means "not final"
        assert sel["final_week"] is None and "finality check failed" in sel["notes"][0]
    finally:
        con.close()


def test_a_newer_week_that_is_not_final_does_not_hide_the_latest_final_one(refresh, monkeypatch):
    monkeypatch.setattr(refresh, "week_kickoffs", lambda con, season: KICKOFFS)
    sel = refresh.select_evidence(None, 2026, datetime(2026, 10, 26, tzinfo=UTC),
                                  final=lambda c, s, w: (w == 6, "pending"))
    assert (sel["week"], sel["final_week"]) == (None, 6)
    assert [n for n in sel["notes"] if "week 7 is not fully final" in n]
    assert [label for label, _ in refresh.evidence_steps(sel)] == ["ipm_cal_shadow cycle",
                                                                  "espn_benchmark week 6"]   # no upcoming week


class _Settings:
    def __init__(self, db):
        self.db_path, self.nfl_season = db, 2026


@pytest.fixture
def evidence_env(refresh, monkeypatch, tmp_path):
    db = tmp_path / "live.duckdb"
    _schedule_db(db)
    monkeypatch.setattr(refresh, "get_settings", lambda: _Settings(db))
    return refresh.Log(tmp_path / "logs" / "x.log")


TUE = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def test_run_evidence_runs_the_three_read_only_scripts(refresh, evidence_env, monkeypatch):
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd) or 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    assert [(Path(c[1]).name, c[2], c[-1]) for c in ran] == [
        ("injury_freshness.py", "report", "7"), ("start_sit_evaluation.py", "record", "7"),
        ("ctx_shadow.py", "pregame", "7"), ("qb_scenario_shadow.py", "pregame", "7"),
        ("signal_shadows.py", "pregame", "7"), ("injury_return_shadow.py", "pregame", "7"),
        ("dst_shadow.py", "pregame", "7"),
        ("distribution_ledger.py", "freeze", "7"), ("participation_cal_shadow.py", "cycle", "2026"),
        ("espn_benchmark.py", "record", "6")]
    text = evidence_env.path.read_text()
    assert "Evidence: season 2026, upcoming week 7, latest fully final week 6" in text and "all steps ok" in text


def test_every_evidence_step_failure_is_reported_and_none_raises(refresh, evidence_env, monkeypatch):
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    calls = []

    def fake(cmd, log, timeout=None):
        calls.append(Path(cmd[1]).name)
        if cmd[1].endswith("injury_freshness.py"):
            raise OSError("boom")
        return 3 if cmd[1].endswith("espn_benchmark.py") else 1

    monkeypatch.setattr(refresh, "run_step", fake)
    problems = refresh.run_evidence(evidence_env, now=TUE)
    assert calls == ["injury_freshness.py", "start_sit_evaluation.py", "ctx_shadow.py", "qb_scenario_shadow.py",
                     "signal_shadows.py", "injury_return_shadow.py", "dst_shadow.py", "distribution_ledger.py",
                     "participation_cal_shadow.py", "espn_benchmark.py"]      # a failure does not stop the rest
    assert len(problems) == 10
    text = evidence_env.path.read_text()
    assert "could not run: OSError('boom')" in text and "start_sit_evaluation week 7 exited 1" in text
    assert "espn_benchmark week 6 exited 3" in text


def test_start_sit_with_nothing_eligible_is_info_not_a_warning(refresh, evidence_env, monkeypatch):
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 2 if "start_sit" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    text = evidence_env.path.read_text()
    assert "INFO: Evidence: start_sit_evaluation week 7 recorded nothing" in text and "WARNING" not in text


def test_ctx_shadow_injury_warnings_are_info_and_its_failure_a_warning(refresh, evidence_env, monkeypatch):
    """The research shadow's own injury-evidence warnings (exit 3) never count as an evidence problem; a failed
    shadow run (exit 1) is reported like any other step, and neither stops the later steps."""
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 3 if "ctx_shadow" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    assert "INFO: Evidence: ctx_shadow pregame week 7 reported injury-evidence warnings" in evidence_env.path.read_text()
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd[1]) or (
        1 if "ctx_shadow" in cmd[1] else 0))
    problems = refresh.run_evidence(evidence_env, now=TUE)
    assert problems == ["ctx_shadow pregame week 7 exited 1 (output above; the refresh exit code is unchanged)"]
    assert Path(ran[-1]).name == "espn_benchmark.py"


def test_distribution_ledger_warnings_are_info_and_its_failure_a_warning(refresh, evidence_env, monkeypatch):
    """The research distribution ledger's own warnings (exit 3: no artifact yet, a missed week) never count as an
    evidence problem; a failed run (exit 1) is reported like any other step and does not stop the later steps."""
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 3 if "distribution_ledger" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    assert ("INFO: Evidence: distribution_ledger pregame week 7 reported warnings for its research ledger"
            in evidence_env.path.read_text())
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd[1]) or (
        1 if "distribution_ledger" in cmd[1] else 0))
    problems = refresh.run_evidence(evidence_env, now=TUE)
    assert problems == ["distribution_ledger pregame week 7 exited 1 (output above; the refresh exit code is unchanged)"]
    assert Path(ran[-1]).name == "espn_benchmark.py"


def test_the_other_research_shadows_warnings_are_info_and_their_failures_warnings(refresh, evidence_env, monkeypatch):
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    shadow = ("qb_scenario_shadow", "participation_cal_shadow")
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 3 if any(s in cmd[1] for s in shadow) else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    text = evidence_env.path.read_text()
    assert "INFO: Evidence: qb_scenario_shadow pregame week 7 reported injury-evidence warnings" in text
    assert "INFO: Evidence: ipm_cal_shadow cycle reported warnings for the research shadow" in text
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 1 if any(s in cmd[1] for s in shadow) else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == [
        "qb_scenario_shadow pregame week 7 exited 1 (output above; the refresh exit code is unchanged)",
        "ipm_cal_shadow cycle exited 1 (output above; the refresh exit code is unchanged)"]


def test_dst_shadow_input_warnings_are_info_and_its_failure_a_warning(refresh, evidence_env, monkeypatch):
    """The D/ST research shadow's own missing-input warnings (exit 3) are not an evidence problem; a failed run is."""
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 3 if "dst_shadow" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    assert "INFO: Evidence: dst_shadow pregame week 7 reported missing-input warnings" in evidence_env.path.read_text()
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 1 if "dst_shadow" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == [
        "dst_shadow pregame week 7 exited 1 (output above; the refresh exit code is unchanged)"]

def test_signal_shadows_warnings_are_info_and_its_failure_a_warning(refresh, evidence_env, monkeypatch):
    """scripts/signal_shadows.py, like ctx_shadow.py: its own warnings (exit 3) are not an evidence problem, a failed
    run (exit 1) is, and neither stops the later steps."""
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: 3 if "signal_shadows" in cmd[1] else 0)
    assert refresh.run_evidence(evidence_env, now=TUE) == []
    assert "INFO: Evidence: signal_shadows pregame week 7 reported" in evidence_env.path.read_text()
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd[1]) or (
        1 if "signal_shadows" in cmd[1] else 0))
    problems = refresh.run_evidence(evidence_env, now=TUE)
    assert problems == ["signal_shadows pregame week 7 exited 1 (output above; the refresh exit code is unchanged)"]
    assert Path(ran[-1]).name == "espn_benchmark.py"


SLEEPER = [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"]


def test_a_hung_evidence_step_is_killed_and_the_refresh_still_exits_0(refresh, evidence_env, monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    monkeypatch.setattr(refresh, "evidence_steps", lambda sel: [("start_sit_evaluation week 7", SLEEPER)])
    monkeypatch.setenv("REFRESH_EVIDENCE_TIMEOUT_S", "1")           # the configurable per-step deadline
    started = time.monotonic()
    problems = refresh.run_evidence(evidence_env, now=TUE)
    assert time.monotonic() - started < 10
    assert len(problems) == 1 and "start_sit_evaluation week 7 timed out after 1s" in problems[0]
    text = evidence_env.path.read_text()
    assert text.count("timed out after") == 1 and "<== killed: no exit after 1s" in text
    pid = int(next(line for line in text.splitlines() if line.strip().isdigit()))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                             # the child is gone, not orphaned

    # through main(): sync steps are stubbed (no timeout), the evidence step is the real, hung process
    real = refresh.run_step
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "check_pregame", lambda log: None)
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: real(cmd, log, timeout) if timeout else 0)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish"])
    started = time.monotonic()
    assert refresh.main() == 0
    assert time.monotonic() - started < 10
    assert "WARNING: Evidence: start_sit_evaluation week 7 timed out" in next(
        (tmp_path / "logs").glob("refresh-*.log")).read_text()


def test_sync_steps_get_no_deadline(refresh, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: seen.append(timeout) or 0)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: None)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish", "--skip-evidence"])
    # the three syncs run to completion; only the report and replay steps (not syncs) have the evidence deadline
    assert refresh.main() == 0 and seen == [None, None, None, refresh.evidence_timeout(), refresh.evidence_timeout(),
                                            None]   # report, replay, ranges load


def test_the_real_finality_check_on_a_synthetic_sources_world(refresh, tmp_path):
    """No stubs: weekly_learning.week_finality over real tables. Week 2 has a team without synced box
    scores (final but missing ingestion), week 7 has no scores yet (pending), the rest are complete."""
    import duckdb

    import test_espn_benchmark as world

    world.make_world(tmp_path / "w.duckdb", skip_stats_teams=[(2, "CCC")]).close()
    con = duckdb.connect(str(tmp_path / "w.duckdb"), read_only=True)
    try:
        ok1, _ = refresh.week_is_final(con, 2026, 1)
        ok2, why2 = refresh.week_is_final(con, 2026, 2)
        ok7, why7 = refresh.week_is_final(con, 2026, 7)
        assert ok1 is True
        assert ok2 is False and "1 without synced box scores or snaps" in why2
        assert ok7 is False and "0/8 teams final" in why7

        import app.analytics.forecast_features as ff

        loads = []
        real_load = ff.load_sources
        ff.load_sources = lambda *a, **k: loads.append(1) or real_load(*a, **k)
        try:
            sel = refresh.select_evidence(con, 2026, datetime(2027, 6, 1, tzinfo=UTC))   # walks 7 -> 6, both checked
        finally:
            ff.load_sources = real_load
        assert (sel["week"], sel["final_week"]) == (None, 6)
        assert [n for n in sel["notes"] if n.startswith("week 7 is not fully final")]
        assert len(loads) == 1                                   # sources loaded once for the whole walk
    finally:
        con.close()


def test_run_evidence_reports_a_database_that_cannot_be_read(refresh, evidence_env, monkeypatch, tmp_path):
    monkeypatch.setattr(refresh, "get_settings", lambda: _Settings(tmp_path / "missing.duckdb"))
    monkeypatch.setattr(refresh, "run_step", lambda *a: pytest.fail("no step may run without a selection"))
    problems = refresh.run_evidence(evidence_env)
    assert len(problems) == 1 and "could not choose the season and week" in problems[0]


def _main(refresh, monkeypatch, tmp_path, *argv, step_code=0):
    """Run main() with stub steps; returns (exit code, ordered events)."""
    events = []

    def fake_step(cmd, log):
        events.append(Path(cmd[1]).name)
        return step_code if Path(cmd[1]).name == "sync_nfl.py" else 0

    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "run_step", fake_step)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: events.append("pregame_check"))
    monkeypatch.setattr(refresh, "run_evidence", lambda log: events.append("evidence") or [])
    monkeypatch.setattr(refresh, "run_shadows", lambda log: events.append("shadows"))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish", *argv])
    return refresh.main(), events


def test_evidence_runs_after_the_syncs_and_analytics(refresh, monkeypatch, tmp_path):
    code, events = _main(refresh, monkeypatch, tmp_path)
    assert code == 0
    assert events == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py", "projection_ranges.py", "pregame_check",
                      "evidence"]


def test_after_a_failed_step_only_the_research_shadows_run_and_flags_skip_them(refresh, monkeypatch, tmp_path):
    code, events = _main(refresh, monkeypatch, tmp_path / "a", step_code=3)
    assert code == 1 and events == ["sync_espn.py", "sync_nfl.py", "shadows"]
    code, events = _main(refresh, monkeypatch, tmp_path / "d", "--skip-evidence", step_code=3)
    assert code == 1 and events == ["sync_espn.py", "sync_nfl.py"]
    for name, flag in (("b", "--skip-evidence"), ("c", "--check-only")):
        code, events = _main(refresh, monkeypatch, tmp_path / name, flag)
        assert code == 0 and "evidence" not in events and "shadows" not in events


def test_an_evidence_exception_never_changes_the_exit_status(refresh, monkeypatch, tmp_path):
    def explode(log):
        raise RuntimeError("evidence blew up")

    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log: 0)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: None)
    monkeypatch.setattr(refresh, "run_evidence", explode)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish"])
    assert refresh.main() == 0
    log = next((tmp_path / "logs").glob("refresh-*.log")).read_text()
    assert "WARNING: Evidence: stage failed: RuntimeError('evidence blew up')" in log
    assert "Refresh complete." in log


def test_a_failed_publish_still_runs_evidence_and_keeps_its_exit_code(refresh, monkeypatch, tmp_path):
    events = []

    def fake_step(cmd, log, timeout=None):
        events.append(Path(cmd[1]).name)
        return 7 if Path(cmd[1]).name == "publish_motherduck.py" else 0

    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "run_step", fake_step)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: events.append("pregame_check"))
    monkeypatch.setattr(refresh, "run_evidence", lambda log: events.append("evidence") or [])
    monkeypatch.setattr(sys, "argv", ["refresh_all.py"])
    assert refresh.main() == 1                                   # the publish failure still fails the refresh
    assert events == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py", "projection_performance.py", "model_replay.py",
                      "projection_ranges.py", "publish_motherduck.py", "evidence"]
    assert "FAILED: publish_motherduck.py exited 7" in next((tmp_path / "logs").glob("refresh-*.log")).read_text()

    events.clear()                                               # --skip-evidence is still honoured
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs2")
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--skip-evidence"])
    assert refresh.main() == 1 and "evidence" not in events


def test_a_failed_local_step_still_skips_evidence_with_publish_enabled(refresh, monkeypatch, tmp_path):
    events = []
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: events.append(Path(cmd[1]).name) or (
        3 if Path(cmd[1]).name == "compute_analytics.py" else 0))
    monkeypatch.setattr(refresh, "run_evidence", lambda log: events.append("evidence") or [])
    monkeypatch.setattr(refresh, "run_shadows", lambda log: events.append("shadows"))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py"])
    assert refresh.main() == 1
    assert events == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py", "shadows"]   # no report, no publish


# ----------------------------------------------------------------------------- DuckDB lock contention (2026-10-18)

LOCK_LINE = ('app.db.connection.DatabaseBusy: IO Error: Could not set lock on file "fantasy.duckdb": Conflicting lock '
             'is held in /Users/x/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/bin/python3.13 (PID 54372)')


def _lock_steps(refresh, monkeypatch, outcomes):
    """run_step stub: per script name, a list of exit codes to return in order; a non-zero code from a script whose
    outcome is marked 'lock' logs the DatabaseBusy traceback line first."""
    calls = []

    def fake(cmd, log, timeout=None):
        name = Path(cmd[1]).name
        calls.append(name)
        seq = outcomes.get(name, [0])
        code = seq.pop(0) if len(seq) > 1 else seq[0]
        if code == "lock":
            log(LOCK_LINE)
            return 1
        return code

    monkeypatch.setattr(refresh, "run_step", fake)
    monkeypatch.setattr(refresh, "LOCK_RETRY_DELAYS_S", (0, 0))
    return calls


def test_a_step_that_failed_on_the_lock_is_retried_and_recovers(refresh, monkeypatch, tmp_path):
    calls = _lock_steps(refresh, monkeypatch, {"sync_espn.py": ["lock", 0]})
    log = refresh.Log(tmp_path / "x.log")
    assert refresh.run_db_step([sys.executable, "sync_espn.py"], log) == 0
    assert calls == ["sync_espn.py", "sync_espn.py"]
    assert "sync_espn.py could not take the DuckDB lock (another process holds it); retry 1/2" in log.path.read_text()


def test_a_failure_that_is_not_the_lock_is_not_retried(refresh, monkeypatch, tmp_path):
    calls = _lock_steps(refresh, monkeypatch, {"sync_espn.py": [1, 0]})
    assert refresh.run_db_step([sys.executable, "sync_espn.py"], refresh.Log(tmp_path / "x.log")) == 1
    assert calls == ["sync_espn.py"]


def test_a_lock_that_is_never_released_gives_up_after_the_retries(refresh, monkeypatch, tmp_path):
    calls = _lock_steps(refresh, monkeypatch, {"sync_espn.py": ["lock"]})
    log = refresh.Log(tmp_path / "x.log")
    assert refresh.run_db_step([sys.executable, "sync_espn.py"], log) == 1
    assert calls == ["sync_espn.py"] * 3
    assert "still could not take the DuckDB lock after 3 attempts" in log.path.read_text()


def test_a_held_lock_on_the_espn_sync_still_runs_nflverse_and_the_shadow_freezes(refresh, monkeypatch, tmp_path):
    """The 2026-10-18 08:00 refresh: sync_espn.py lost the DuckDB lock and the whole chain, including the ifp-ctx-p1
    freeze, was skipped. Now the nflverse sync (the injury reports the freeze gate reads) still runs, the steps that
    need ESPN data are skipped, and the research shadows still freeze on the stored data."""
    calls = _lock_steps(refresh, monkeypatch, {"sync_espn.py": ["lock"]})
    events = []
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "check_pregame", lambda log: pytest.fail("needs the ESPN sync"))
    monkeypatch.setattr(refresh, "run_report", lambda log: pytest.fail("needs the analytics"))
    monkeypatch.setattr(refresh, "run_evidence", lambda log, **kw: events.append(kw) or [])
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish"])
    assert refresh.main() == 1
    assert calls == ["sync_espn.py"] * 3 + ["sync_nfl.py"]
    assert events == [{"shadows_only": True}]
    log = next((tmp_path / "logs").glob("refresh-*.log")).read_text()
    assert "FAILED: sync_espn.py exited 1" in log and "SKIPPED: compute_analytics.py" in log


def test_shadows_only_runs_just_the_research_shadow_freezes(refresh, evidence_env, monkeypatch):
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd) or 3)
    assert refresh.run_evidence(evidence_env, now=TUE, shadows_only=True) == []        # exit 3: the shadow's warnings
    assert [(Path(c[1]).name, c[2]) for c in ran] == [          # every research shadow, nothing else
        ("ctx_shadow.py", "pregame"), ("qb_scenario_shadow.py", "pregame"), ("signal_shadows.py", "pregame"),
        ("injury_return_shadow.py", "pregame"), ("dst_shadow.py", "pregame"), ("distribution_ledger.py", "freeze"), ("participation_cal_shadow.py", "cycle")]
    text = evidence_env.path.read_text()
    assert "research shadows only" in text and "INFO: Evidence: ctx_shadow pregame week 7 reported" in text


def test_any_pregame_week_step_counts_as_a_research_shadow(refresh, evidence_env, monkeypatch):
    """A shadow added later (labelled "<name> pregame week W") runs after a failed sync with no other wiring."""
    real = refresh.evidence_steps
    monkeypatch.setattr(refresh, "evidence_steps", lambda sel: [*real(sel), ("dst_shadow pregame week 7", ["x", "dst"])])
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    ran = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: ran.append(cmd[1]) or 0)
    refresh.run_evidence(evidence_env, now=TUE, shadows_only=True)
    assert [Path(c).name for c in ran][0] == "ctx_shadow.py" and [Path(c).name for c in ran][-1] == "dst"
    assert not any(Path(c).name in ("injury_freshness.py", "start_sit_evaluation.py", "espn_benchmark.py") for c in ran)
    assert refresh.is_shadow("dst_shadow pregame week 7") and not refresh.is_shadow("start_sit_evaluation week 7")
    assert refresh.is_shadow("ipm_cal_shadow cycle") and refresh.is_shadow("qb_scenario_shadow pregame week 7")
    assert refresh.is_shadow("signal_shadows pregame week 7")
    assert refresh.is_shadow("injury_return_shadow pregame week 7")
    assert not refresh.is_shadow("injury_freshness week 7") and not refresh.is_shadow("espn_benchmark week 6")


def test_every_step_process_gets_the_lock_wait(refresh, monkeypatch, tmp_path):
    from app.db.connection import LOCK_WAIT_ENV

    monkeypatch.delenv(LOCK_WAIT_ENV, raising=False)
    log = refresh.Log(tmp_path / "x.log")
    cmd = [sys.executable, "-c", f"import os; print('wait=' + os.environ['{LOCK_WAIT_ENV}'])"]
    assert refresh.run_step(cmd, log) == 0
    assert f"wait={refresh.LOCK_WAIT_S}" in log.path.read_text()
    monkeypatch.setenv(LOCK_WAIT_ENV, "7")                      # an explicit setting wins
    assert refresh.run_step(cmd, log, timeout=30) == 0
    assert "wait=7" in log.path.read_text()


def test_log_since_returns_only_the_new_lines(refresh, tmp_path):
    log = refresh.Log(tmp_path / "x.log")
    log("a")
    mark = log.lines
    assert log.since(mark) == []
    log("b")
    log("c")
    assert log.since(mark) == ["b", "c"] and log.since(0) == ["a", "b", "c"]


def test_r2_snapshot_publish_replaces_motherduck_when_configured(refresh, monkeypatch):
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    for key in ("SNAPSHOT_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID"):
        monkeypatch.setenv(key, "x")
    assert refresh.plan(publish=True)[-1] == "publish_motherduck.py"    # incomplete R2 settings: unchanged
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "y")
    steps = refresh.plan(publish=True)
    assert steps[-1] == "publish_snapshot.py" and "publish_motherduck.py" not in steps
    assert refresh.plan(publish=False) == list(refresh.STEPS)

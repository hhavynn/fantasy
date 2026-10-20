"""Evidence health (scripts/evidence_audit.py, scripts/evidence_health.py): refresh-log contract, launchd
checks and idempotent install, calendar and schedule gaps, the per-week evidence audit on a synthetic world,
the pregame guard and alert de-duplication. Synthetic data only; nothing touches the real Mac's launchd."""

import argparse
import importlib.util
import json
import os
import plistlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.analytics import projection_source as ps

import pytest

from app.analytics import production_pin
from app.analytics import start_sit_evaluation as sse
from app.analytics.independent_projection_store import league_scoring, write_run
from app.db.connection import connect, init_db
from synthetic_forecast import add_league

UTC = timezone.utc
PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import evidence_audit as eh  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def refresh(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return _load("refresh_all")


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return _load("evidence_health")


# ----------------------------------------------------------------------------- refresh log contract


def _real_refresh_log(refresh, monkeypatch, tmp_path, *argv, codes=None):
    """Run refresh_all.main() for real (its own Log, run_step and run_evidence) with tiny stand-in scripts, and
    return the log text. `codes` maps a script name to its exit code."""
    codes = {"sync_espn.py": 0, "sync_nfl.py": 0, "compute_analytics.py": 0, "injury_freshness.py": 0,
             "start_sit_evaluation.py": 0, "ctx_shadow.py": 0, "qb_scenario_shadow.py": 0, "signal_shadows.py": 0,
             "injury_return_shadow.py": 0, "dst_shadow.py": 0,
             "distribution_ledger.py": 0, "participation_cal_shadow.py": 0, "espn_benchmark.py": 0,
             "projection_performance.py": 0, "model_replay.py": 0, "projection_ranges.py": 0,
             **(codes or {})}
    for name, code in codes.items():
        # child output that imitates refresh_all's own markers must never be taken for them
        (tmp_path / name).write_text("print('WARNING: Evidence: fake child line')\nprint('Refresh complete.')\n"
                                     f"print('FAILED: fake')\nraise SystemExit({code})\n")
    db = tmp_path / "live.duckdb"
    import duckdb
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, gametime VARCHAR, "
                "home_team VARCHAR, away_team VARCHAR)")
    con.executemany("INSERT INTO nfl_schedules VALUES (2026, ?, 'REG', ?, ?, ?, ?)",
                    [(6, "2026-10-15", "20:15", "AAA", "BBB"), (7, "2026-10-22", "20:15", "BBB", "CCC")])
    con.close()

    class S:
        db_path, nfl_season = db, 2026

    monkeypatch.setattr(refresh, "SCRIPTS", tmp_path)
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(refresh, "get_settings", lambda: S)
    monkeypatch.setattr(refresh, "check_pregame", lambda log: log("Pregame check: week 7 ESPN snapshot captured at "
                                                                  "2026-10-20T15:00:00+00:00 (600 players), first kickoff x."))
    monkeypatch.setattr(refresh, "week_is_final", lambda con, season, week, cache=None: (True, ""))
    real_evidence = refresh.run_evidence
    monkeypatch.setattr(refresh, "run_evidence", lambda log: real_evidence(log, now=datetime(2026, 10, 20, 16, tzinfo=UTC)))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--no-publish", *argv])
    code = refresh.main()
    log = next((tmp_path / "logs").glob("refresh-*.log"))
    return code, log.read_text()


def test_a_real_refresh_log_parses_step_by_step(refresh, monkeypatch, tmp_path):
    code, text = _real_refresh_log(refresh, monkeypatch, tmp_path)
    r = eh.parse_refresh_log(text)
    assert code == 0 and r["status"] == "complete" and r["failed"] is None
    assert [s["name"] for s in r["steps"]] == ["sync_espn.py", "sync_nfl.py", "compute_analytics.py",
                                               "projection_performance.py",   # the report step (never fatal)
                                               "model_replay.py",             # the weekly replay (never fatal)
                                               "projection_ranges.py"]        # the ranges load (never fatal)
    assert all(s["exit"] == 0 for s in r["steps"])
    ev = r["evidence"]
    assert ev["ran"] and ev["status"] == "ok" and ev["problems"] == []          # the child's fake WARNING is ignored
    assert [(s["name"], s["result"]) for s in ev["steps"]] == [
        ("injury_freshness.py", "ok"), ("start_sit_evaluation.py", "ok"), ("ctx_shadow.py", "ok"),
        ("qb_scenario_shadow.py", "ok"), ("signal_shadows.py", "ok"), ("injury_return_shadow.py", "ok"),
        ("dst_shadow.py", "ok"),
        ("distribution_ledger.py", "ok"),
        ("participation_cal_shadow.py", "ok"), ("espn_benchmark.py", "ok")]
    assert r["pregame"]["week"] == 7 and r["pregame"]["captured_at"] == "2026-10-20T15:00:00+00:00"
    assert eh.pregame_capture_ok(r)


def test_research_shadow_injury_warnings_never_make_the_capture_unhealthy(refresh, monkeypatch, tmp_path):
    """ctx_shadow.py exit 3 (its own injury-evidence WARNING lines) is not an evidence problem."""
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path, codes={"ctx_shadow.py": 3})
    r = eh.parse_refresh_log(text)
    assert r["evidence"]["status"] == "ok" and eh.pregame_capture_ok(r)


def test_dst_shadow_input_warnings_never_make_the_capture_unhealthy(refresh, monkeypatch, tmp_path):
    """dst_shadow.py exit 3 (its own missing-input WARNING lines) is not an evidence problem either."""
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path, codes={"dst_shadow.py": 3})
    r = eh.parse_refresh_log(text)
    assert r["evidence"]["status"] == "ok" and eh.pregame_capture_ok(r)


def test_signal_shadow_warnings_never_make_the_capture_unhealthy(refresh, monkeypatch, tmp_path):
    """signal_shadows.py exit 3 (its own WARNING lines) is not an evidence problem either."""
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path, codes={"signal_shadows.py": 3})
    r = eh.parse_refresh_log(text)
    assert r["evidence"]["status"] == "ok" and eh.pregame_capture_ok(r)


def test_a_failed_signal_shadow_is_a_problem_but_not_a_lost_capture(refresh, monkeypatch, tmp_path):
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path, codes={"signal_shadows.py": 1})
    r = eh.parse_refresh_log(text)
    assert r["evidence"]["status"] == "problems" and eh.pregame_capture_ok(r)


def test_a_failed_research_shadow_is_a_problem_but_not_a_lost_capture(refresh, monkeypatch, tmp_path):
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path, codes={"ctx_shadow.py": 1})
    r = eh.parse_refresh_log(text)
    assert r["evidence"]["status"] == "problems" and eh.pregame_capture_ok(r)


def test_exit_zero_with_failed_evidence_is_not_a_clean_capture(refresh, monkeypatch, tmp_path):
    code, text = _real_refresh_log(refresh, monkeypatch, tmp_path,
                                   codes={"start_sit_evaluation.py": 1, "espn_benchmark.py": 3})
    r = eh.parse_refresh_log(text)
    assert code == 0 and r["status"] == "complete"                  # refresh_all's semantics are unchanged ...
    assert r["evidence"]["status"] == "problems" and len(r["evidence"]["problems"]) == 2   # ... but health sees it
    assert not eh.pregame_capture_ok(r)


def test_nothing_eligible_and_benchmark_failure_still_count_as_a_pregame_capture(refresh, monkeypatch, tmp_path):
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path,
                                codes={"start_sit_evaluation.py": 2, "espn_benchmark.py": 3})
    r = eh.parse_refresh_log(text)
    assert dict((s["name"], s["result"]) for s in r["evidence"]["steps"])["start_sit_evaluation.py"] == "nothing_eligible"
    assert r["evidence"]["status"] == "problems" and eh.pregame_capture_ok(r)


def test_skip_evidence_and_failed_sync_logs(refresh, monkeypatch, tmp_path):
    (tmp_path / "a").mkdir()
    _, text = _real_refresh_log(refresh, monkeypatch, tmp_path / "a", "--skip-evidence")
    r = eh.parse_refresh_log(text)
    assert r["status"] == "complete" and not r["evidence"]["ran"] and not eh.pregame_capture_ok(r)
    (tmp_path / "b").mkdir()
    code, text = _real_refresh_log(refresh, monkeypatch, tmp_path / "b", codes={"sync_nfl.py": 3})
    r = eh.parse_refresh_log(text)
    assert code == 1 and r["status"] == "failed" and r["failed"].startswith("sync_nfl.py exited 3")


def test_unfinished_logs_are_running_or_killed(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "refresh-20261018T150000Z.log"
    f.write_text("==> sync_espn.py  (2026-10-18 08:00 PDT)\nsome output\n")
    now = datetime(2026, 10, 18, 15, 5, tzinfo=UTC)
    os.utime(f, (now.timestamp(), now.timestamp()))
    assert eh.refresh_runs(logs, now)[0]["status"] == "running"
    assert eh.refresh_runs(logs, now + timedelta(hours=3))[0]["status"] == "unfinished"


# ----------------------------------------------------------------------------- launchd


@pytest.mark.parametrize("job", eh.JOBS)
def test_templates_are_strict_plists_that_render(job, tmp_path):
    paths = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=tmp_path)
    plist, text = eh.expected_plist(paths, job, "/opt/homebrew/bin/uv")     # plistlib is a strict XML parser
    assert "__" not in text and plist["Label"] == eh.label(job)
    assert plist["ProgramArguments"][0] == "/opt/homebrew/bin/uv"
    assert plist["WorkingDirectory"] == f"{ROOT}/backend" and plist["StandardErrorPath"].startswith(f"{ROOT}/data/logs/")
    assert plist.get("StartCalendarInterval") or plist.get("StartInterval")
    assert plist["RunAtLoad"] is (job == "pregame-guard")     # only the cheap guard check runs at login


def test_guard_template_runs_the_guard_every_half_hour(tmp_path):
    paths = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=tmp_path)
    plist, _ = eh.expected_plist(paths, "pregame-guard", "uv")
    assert plist["StartInterval"] == 1800
    assert plist["ProgramArguments"][-4:] == ["guard", "--execute", "--catch-up", "--notify"]


def test_drift_ignores_formatting_but_reports_schedule_changes(tmp_path):
    paths = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=tmp_path)
    exp, _ = eh.expected_plist(paths, "refresh", "uv")
    reformatted = plistlib.loads(plistlib.dumps(exp, sort_keys=True))     # what `plutil -convert` produces
    assert eh.plist_drift(exp, reformatted) == []
    extra = {**exp, "StartCalendarInterval": [*exp["StartCalendarInterval"], {"Weekday": 3, "Hour": 8, "Minute": 0}]}
    assert eh.plist_drift(exp, extra) == ["extra schedule entries not in the template: Wed 08:00"]
    gone = {**exp, "StartCalendarInterval": exp["StartCalendarInterval"][1:]}
    assert eh.plist_drift(exp, gone) == ["template schedule entries missing from the installed job: Thu 15:00"]
    assert "ProgramArguments differs from the template" in eh.plist_drift(exp, {**exp, "ProgramArguments": ["x"]})


LOADED = """gui/501/com.fantasy-intel.refresh = {
\tactive count = 0
\tstate = not running
\truns = 4
\tlast exit code = 0
\tevent triggers = {
\t\tstate = active
"""


def fake_launchctl(loaded: set, calls: list | None = None):
    def runner(args):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ["launchctl", "print"]:
            return (0, LOADED) if args[2].split("/")[-1] in loaded else (113, "Could not find service")
        if args[:2] == ["launchctl", "bootstrap"]:
            loaded.add(Path(args[3]).stem)
        if args[:2] == ["launchctl", "bootout"]:
            loaded.discard(args[2].split("/")[-1])
        return 0, ""
    return runner


def test_launchctl_status_parsing():
    st = eh.launchctl_status("com.fantasy-intel.refresh", fake_launchctl({"com.fantasy-intel.refresh"}), 501)
    assert st == {"loaded": True, "state": "not running", "runs": 4, "last_exit_code": 0, "last_exit_text": "0"}
    assert eh.launchctl_status("com.fantasy-intel.refresh", fake_launchctl(set()), 501)["loaded"] is False
    never = LOADED.replace("last exit code = 0", "last exit code = (never exited)")
    st = eh.launchctl_status("x", lambda a: (0, never), 501)
    assert st["last_exit_code"] is None and st["last_exit_text"] == "(never exited)"


def test_install_plan_is_idempotent_and_never_duplicates(cli, tmp_path):
    paths = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=tmp_path / "my home")   # a space in the path
    loaded: set = set()
    calls: list = []
    runner = fake_launchctl(loaded, calls)
    plan = cli.launchd_plan(paths, ["refresh", "weekly-learning"], 501, "uv", runner)
    assert [p["state"] for p in plan] == ["not_installed", "not_installed"]
    assert any(a.startswith("launchctl bootstrap gui/501 ") for a in plan[0]["actions"])
    results = cli.apply_plan(plan, lambda a: (0, "") if a[0] in ("mkdir", "plutil") else runner(a))
    assert all(isinstance(c, list) for c in calls)
    assert all(r["exit"] == 0 for r in results)
    assert (paths.launch_agents / "com.fantasy-intel.refresh.plist").exists()
    again = cli.launchd_plan(paths, ["refresh", "weekly-learning"], 501, "uv", runner)
    assert [(p["state"], p["actions"]) for p in again] == [("up_to_date", []), ("up_to_date", [])]
    boots = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert len(boots) == 2                                                    # one per job, never twice
    assert boots[0][3] == str(paths.launch_agents / "com.fantasy-intel.refresh.plist")      # quoted, not split
    # a hand-edited job: backed up, booted out, replaced, booted in
    f = paths.launch_agents / "com.fantasy-intel.refresh.plist"
    p = plistlib.loads(f.read_bytes())
    p["StartCalendarInterval"].append({"Weekday": 3, "Hour": 8, "Minute": 0})
    f.write_bytes(plistlib.dumps(p))
    drift = cli.launchd_plan(paths, ["refresh"], 501, "uv", runner, now=datetime(2026, 10, 16, tzinfo=UTC))[0]
    assert drift["state"] == "drifted" and "Wed 08:00" in drift["drift"][0]
    assert drift["actions"][0].startswith(f"cp '{f}' '{f}.bak-20261016T000000Z")
    assert drift["actions"][1] == "launchctl bootout gui/501/com.fantasy-intel.refresh"


def test_install_never_repoints_a_job_of_another_checkout(cli, tmp_path):
    home = tmp_path / "home"
    real = eh.default_paths(tmp_path / "main" / "fantasy-intel", tmp_path / "x.duckdb", env={}, home=home)
    real.templates.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(ROOT / "scripts" / "launchd", real.templates)
    real.launch_agents.mkdir(parents=True)
    _, text = eh.expected_plist(real, "refresh", "uv")
    (real.launch_agents / "com.fantasy-intel.refresh.plist").write_text(text)
    worktree = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=home)       # this checkout
    plan = cli.launchd_plan(worktree, ["refresh"], 501, "uv", fake_launchctl({"com.fantasy-intel.refresh"}))
    assert plan[0]["state"] == "other_checkout" and plan[0]["actions"] == []
    plan = cli.launchd_plan(worktree, ["refresh"], 501, "uv", fake_launchctl({"com.fantasy-intel.refresh"}), repoint=True)
    assert plan[0]["state"] == "drifted" and "WorkingDirectory differs from the template" in plan[0]["drift"]
    # a job not installed yet joins the checkout the others run from, never a second one
    plan = cli.launchd_plan(worktree, ["pregame-guard"], 501, "uv", fake_launchctl(set()))
    assert plan[0]["state"] == "other_checkout" and plan[0]["actions"] == []
    plan = cli.launchd_plan(real, ["pregame-guard"], 501, "uv", fake_launchctl(set()))
    assert plan[0]["state"] == "not_installed" and plan[0]["actions"]


def test_job_status_reports_missing_and_unloaded_jobs(tmp_path):
    paths = eh.default_paths(ROOT, tmp_path / "x.duckdb", env={}, home=tmp_path)
    st = eh.job_status(paths, "refresh", fake_launchctl(set()), "uv", 501)
    assert not st["installed"] and st["calendar"][0] == "Sun 08:00"
    paths.launch_agents.mkdir(parents=True)
    _, text = eh.expected_plist(paths, "refresh", "uv")
    (paths.launch_agents / "com.fantasy-intel.refresh.plist").write_text(text)
    st = eh.job_status(paths, "refresh", fake_launchctl(set()), "uv", 501)
    assert st["installed"] and st["matches_template"] and not st["launchctl"]["loaded"]
    (paths.launch_agents / "com.fantasy-intel.refresh.plist").write_text("<plist>broken")
    assert eh.job_status(paths, "refresh", fake_launchctl(set()), "uv", 501)["valid"] is False


# ----------------------------------------------------------------------------- calendar and gaps

REFRESH_CAL = [(4, 15, 0), (0, 8, 0), (1, 15, 0), (2, 8, 0)]          # the refresh template, local Pacific


def test_calendar_follows_local_time_across_dst():
    start, end = datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
    sundays = [t for t in eh.occurrences(REFRESH_CAL, start, end, PT) if t.astimezone(PT).weekday() == 6]
    assert sundays == [datetime(2026, 10, 25, 15, 0, tzinfo=UTC),         # 08:00 PDT
                       datetime(2026, 11, 1, 16, 0, tzinfo=UTC)]          # 08:00 PST after the change


def test_scheduled_runs_ran_missed_pending():
    thu = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)                        # Thu 15:00 PDT
    sun = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)                        # Sun 08:00 PDT
    runs = eh.scheduled_runs(REFRESH_CAL, [thu + timedelta(minutes=1)], thu - timedelta(hours=1),
                             sun + timedelta(hours=1), 6.0, PT)
    assert [(r["scheduled_for"], r["status"]) for r in runs] == [(thu.isoformat(), "ran"), (sun.isoformat(), "pending")]
    later = eh.scheduled_runs(REFRESH_CAL, [thu], thu - timedelta(hours=1), sun + timedelta(hours=7), 6.0, PT)
    assert later[-1]["status"] == "missed"


def _slots(*games):
    """games: (week, ET date, ET time, away, home) -> kickoff_slots-like dict."""
    from app.analytics.projection_backtest import _kickoff

    out: dict = {}
    for w, d, t, a, h in games:
        k = _kickoff({"gameday": d, "gametime": t})
        out.setdefault(w, []).append({"kickoff": k, "teams": [a, h], "games": [f"{a}@{h}"]})
    return out


def test_fixed_schedule_gaps_london_and_thanksgiving():
    slots = _slots((6, "2026-10-18", "09:30", "HOU", "JAX"), (6, "2026-10-18", "13:00", "CHI", "ATL"),
                   (12, "2026-11-26", "12:30", "CHI", "DET"), (12, "2026-11-26", "16:30", "KC", "DAL"),
                   (12, "2026-11-26", "20:20", "CIN", "BAL"))
    gaps = eh.schedule_gaps(slots, REFRESH_CAL, datetime(2026, 10, 16, tzinfo=UTC), 12.0, PT, horizon_days=60)
    assert [g["games"] for g in gaps] == [["HOU@JAX"], ["CHI@DET"], ["KC@DAL"]]
    assert gaps[0]["last_scheduled_refresh"].startswith("Thu 10/15 15:00") and gaps[0]["age_h"] == 63.5
    # Thanksgiving's night game follows the Thursday 15:00 run by 2.3 h: covered


# ----------------------------------------------------------------------------- synthetic world


TNF = datetime(2026, 10, 16, 0, 15, tzinfo=UTC)          # Thu 10/15 20:15 ET
LONDON = datetime(2026, 10, 18, 13, 30, tzinfo=UTC)      # Sun 09:30 ET
SUNDAY = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
MNF = datetime(2026, 10, 20, 0, 15, tzinfo=UTC)
GAMES = [(5, "2026-10-12", "20:15", "AAA", "HHH"),
         (6, "2026-10-15", "20:15", "AAA", "BBB"), (6, "2026-10-18", "09:30", "CCC", "DDD"),
         (6, "2026-10-18", "13:00", "EEE", "FFF"), (6, "2026-10-19", "20:15", "GGG", "HHH"),
         (7, "2026-10-22", "20:15", "BBB", "CCC"), (7, "2026-10-25", "13:00", "AAA", "DDD")]
PLAYERS = {1: ("QB", "AAA"), 2: ("WR", "BBB"), 3: ("RB", "CCC"), 4: ("TE", "EEE"), 5: ("WR", "GGG")}
ROSTERED = (1, 2, 3, 4)
THU_PT_1500 = datetime(2026, 10, 15, 22, 0, 4, tzinfo=UTC)


def _ts(dt):
    return dt.astimezone(UTC).replace(tzinfo=None)


# The real database shape of 2026-10-16, one model generation on: production and its labeled fallback.
PRODUCTION, FALLBACK = ps.MODEL_VERSION, ps.FALLBACK_VERSION


def _run(con, version, week, created, fit="2026-10-15T09:00:00+00:00"):
    _, scoring = league_scoring(con, 2026)
    rows = [{"season": 2026, "week": week, "espn_player_id": pid, "gsis_id": f"00-{pid}", "position": pos, "nfl_team": team,
             "opponent": None, "is_home": None, "projected_points": 10.0 + pid, "interval_low": None, "interval_high": None,
             "model": "m", "coverage_status": "projected", "games_used": 5, "latest_usage_season": 2026,
             "latest_usage_week": 5, "missing_inputs": [], "flags": [], "explanation": {}, "p_play": 0.9,
             "expected_points": 9.0 + pid, "participation": None} for pid, (pos, team) in PLAYERS.items()]
    meta = {"run_id": f"{version}-w{week}-{created:%H%M}", "season": 2026, "week": week, "model_version": version,
            "scoring_fingerprint": scoring.fingerprint, "artifact_id": production_pin.APPROVED_ARTIFACT_SHA256 if version == production_pin.PINNED_MODEL_VERSION else f"art-{version}", "artifact_created_at": fit,
            "data_through_season": 2026, "data_through_week": 5, "model_status": {"QB": {"status": "shadow"}},
            "created_at": _ts(created)}
    return write_run(con, meta, rows)


@pytest.fixture
def world(tmp_path):
    """A Mac at Saturday 2026-10-17: TNF of week 6 played; the 15:00 Thursday refresh ran WITHOUT an evidence stage;
    ifp-2 was stored only after TNF kicked off; week 7 only has an ifp-1 run; one ESPN stamp has no sync run."""
    root = tmp_path / "fantasy-intel"
    db = root / "data" / "fantasy.duckdb"
    con = connect(db)
    init_db(con)
    add_league(con, 2026, 6)
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, gametime VARCHAR, "
                "home_team VARCHAR, away_team VARCHAR)")
    con.executemany("INSERT INTO nfl_schedules VALUES (2026, ?, 'REG', ?, ?, ?, ?)", [(w, d, t, h, a) for w, d, t, a, h in GAMES])
    for pid, (pos, team) in PLAYERS.items():
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES (?, ?, ?, ?)",
                    [pid, f"P{pid}", pos, team])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES "
                    "(?, ?, 'espn_id:nfl_players', 'high')", [pid, f"00-{pid}"])
        for stamp, week in ((THU_PT_1500, 6), (THU_PT_1500, 7), (datetime(2026, 10, 14, 12, tzinfo=UTC), 8)):
            con.execute("INSERT INTO espn_projection_history VALUES (?, 2026, ?, ?, 12.5, ?)",
                        [stamp.date(), pid, week, _ts(stamp)])
    for pid in ROSTERED:
        con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, lineup_slot, "
                    "is_starter, synced_at) VALUES (99, 2026, 6, 1, ?, 'FLEX', TRUE, ?)", [pid, _ts(THU_PT_1500)])
    con.execute("INSERT INTO sync_runs VALUES ('e1', 'espn', ?, ?, 'ok', NULL, NULL)",
                [_ts(THU_PT_1500 - timedelta(seconds=4)), _ts(THU_PT_1500 + timedelta(seconds=30))])
    con.execute("INSERT INTO sync_runs VALUES ('n1', 'nfl', ?, ?, 'ok', NULL, NULL)",
                [_ts(THU_PT_1500 + timedelta(minutes=1)), _ts(THU_PT_1500 + timedelta(minutes=2))])
    _run(con, FALLBACK, 6, datetime(2026, 10, 15, 15, 12, tzinfo=UTC))      # pregame fallback
    _run(con, PRODUCTION, 6, datetime(2026, 10, 16, 4, 46, tzinfo=UTC))     # after TNF kickoff
    _run(con, FALLBACK, 7, datetime(2026, 10, 16, 4, 19, tzinfo=UTC))       # week 7: fallback only
    con.close()
    logs = root / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "refresh-20261015T220004Z.log").write_text(
        "==> sync_espn.py  (2026-10-15 15:00 PDT)\n<== exit 0 after 30s\n"
        "==> sync_nfl.py  (2026-10-15 15:00 PDT)\n<== exit 0 after 20s\n"
        "==> compute_analytics.py  (2026-10-15 15:01 PDT)\n<== exit 0 after 9s\n"
        "Pregame check: week 6 ESPN snapshot captured at 2026-10-15T23:30:00+00:00 (5 players), first kickoff x.\n"
        "Refresh complete. Log: x\n")
    weekly = root / "data" / "models" / "weekly"
    (weekly / "ledger" / "s2026w06").mkdir(parents=True)
    rows = [{"gsis_id": f"00-{pid}", "position": pos, "team": team, "pregame": True,
             "kickoff": {"AAA": TNF, "BBB": TNF, "CCC": LONDON, "EEE": SUNDAY, "GGG": MNF}[team].isoformat()}
            for pid, (pos, team) in PLAYERS.items()]
    (weekly / "ledger" / "s2026w06" / "pregame-s2026w06-x.json").write_text(json.dumps(
        {"run_id": "r6", "kind": "pregame", "created_at": "2026-10-15T10:23:49+00:00", "rows": rows}))
    (weekly / "status").mkdir()
    (weekly / "status" / "latest-cycle.json").write_text(json.dumps(
        {"status": "waiting_for_data", "exit_code": 3, "target_week": 7, "errors": ["13 games not final"],
         "required_actions": ["rerun after nflverse publishes the missing data"]}))
    home = tmp_path / "home"
    paths = eh.default_paths(root, db, env={}, home=home)
    paths.launch_agents.mkdir(parents=True)
    tpl = ROOT / "scripts" / "launchd"
    (root / "scripts").mkdir()
    import shutil
    shutil.copytree(tpl, paths.templates)
    for job in eh.REQUIRED_JOBS:
        _, text = eh.expected_plist(paths, job, "uv")
        (paths.launch_agents / f"{eh.label(job)}.plist").write_text(text)
        os.utime(paths.launch_agents / f"{eh.label(job)}.plist",
                 (datetime(2026, 10, 15, 10, tzinfo=UTC).timestamp(),) * 2)
    return paths


def _ctx(paths, now, **kw):
    loaded = {eh.label(j) for j in eh.REQUIRED_JOBS}
    return eh.Context(now=now, season=2026, paths=paths, tz=PT, uv="uv", uid=501, runner=fake_launchctl(loaded), **kw)


def _assess(paths, now, **kw):
    con = connect(paths.db, read_only=True)
    try:
        return eh.assess(con, _ctx(paths, now, **kw))
    finally:
        con.close()


SAT = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_week_audit_names_every_missing_capture_and_its_cause(world):
    rep = _assess(world, SAT)
    w6 = next(w for w in rep["weeks"] if w["week"] == 6)
    assert w6["state"] == "in_progress" and [s["kicked_off"] for s in w6["slots"]] == [True, False, False, False]
    # capture windows: before the job's first trace nothing is "missed"; Thursday 15:00 ran, Sunday is ahead
    assert [(c["local"][:9], c["status"]) for c in w6["scheduled_captures"]] == [
        ("Tue 10/13", "before_install"), ("Thu 10/15", "ran"), ("Sun 10/18", "pending"), ("Mon 10/19", "pending")]
    tnf = w6["slots"][0]
    assert tnf["espn_pregame_snapshot"] == THU_PT_1500.isoformat() and tnf["research_frozen"]
    assert tnf["production_pregame_projected"] == 0 and tnf["fallback_pregame_projected"] == 2
    assert tnf["start_sit_needed"] and tnf["start_sit_recorded_at"] is None
    missing = {m["evidence"]: m["cause"] for m in w6["missing"]}
    assert set(missing) == {"production_forecast", "start_sit_decision_set"}
    assert "after this kickoff" in missing["production_forecast"] and "fallback has 2" in missing["production_forecast"]
    assert "evidence stage did not run" in missing["start_sit_decision_set"]
    # a pregame snapshot the refresh reported (23:30Z) is no longer stored: replaced after kickoff
    assert w6["lost_espn"][0]["reported"] == "2026-10-15T23:30:00+00:00"
    # week 6 is served by the production model, week 7 falls back to the previous one
    w7 = next(w for w in rep["weeks"] if w["week"] == 7)
    assert w6["served"]["model_version"] == PRODUCTION and not w6["served"]["fallback"]
    assert w7["served"]["model_version"] == FALLBACK and w7["served"]["fallback"]
    assert w6["production"]["usable_by_position"] == {"QB": 0, "RB": 1, "WR": 1, "TE": 1}   # TNF players excluded
    codes = {i["code"]: i for i in rep["issues"]}
    assert codes["evidence_stage_not_run"]["severity"] == eh.FAIL
    assert codes["ifp_fallback_served"]["subject"] == "w7"
    assert codes["start_sit_decision_set_missed"]["severity"] == eh.WARN
    assert codes["espn_pregame_snapshot_lost"]["severity"] == eh.WARN
    assert codes["snapshot_provenance_unverified"]["subject"] == "2026-10-14T12:00:00+00:00"
    assert codes["learning_cycle_waiting"]["severity"] == eh.INFO
    assert rep["status"] == eh.FAIL


def _write(paths, *sql_params):
    con = connect(paths.db)
    try:
        for sql, params in sql_params:
            con.execute(sql, params)
    finally:
        con.close()


def test_an_unverified_espn_stamp_is_never_a_capture(world, cli, capsys):
    """Review P1: a synced_at no successful ESPN sync run covers is reported as unverified, never as present."""
    orphan = datetime(2026, 10, 15, 23, 50, tzinfo=UTC)                 # after the verified 22:00Z, before TNF
    london_orphan = LONDON - timedelta(hours=1)                         # the only "capture" near the London kickoff
    # hand-written rows (their own snapshot_date): no ESPN sync run covers either stamp
    _write(world, *[("INSERT INTO espn_projection_history VALUES (?, 2026, ?, 6, 13.0, ?)", [day, pid, _ts(d)])
                    for d, day in ((orphan, datetime(2026, 10, 16).date()), (london_orphan, datetime(2026, 10, 18).date()))
                    for pid in PLAYERS])
    rep = _assess(world, SAT)
    w6 = next(w for w in rep["weeks"] if w["week"] == 6)
    tnf, london = w6["slots"][0], w6["slots"][1]
    assert tnf["espn_pregame_snapshot"] == THU_PT_1500.isoformat()    # the verified one still counts
    assert tnf["espn_unverified_snapshot"] == orphan.isoformat()
    assert {s["synced_at"]: s["verified"] for s in w6["espn_snapshots"]}[orphan.isoformat()] is False
    assert london["espn_pregame_snapshot"] == THU_PT_1500.isoformat() and london["espn_unverified_snapshot"]
    assert any("fresh ESPN snapshot" in m for m in rep["next_kickoff"]["missing"])          # not satisfied by the orphan
    assert any("unverified provenance" in m for m in rep["next_kickoff"]["missing"])
    assert orphan.isoformat() in rep["provenance"]["unverified_espn_snapshots"]
    # the guard sees no fresh verified capture for London, so it still refreshes
    con = connect(world.db, read_only=True)
    try:
        kicks, espn = eh.espn_latest_by_slot(con, 2026, LONDON - timedelta(hours=3))
    finally:
        con.close()
    assert espn[LONDON.isoformat()] == THU_PT_1500
    fresh = [_log(LONDON - timedelta(hours=2))]
    assert eh.guard_decision(LONDON - timedelta(hours=1, minutes=30), kicks, fresh, espn, {})["action"] == "run"
    # a played slot whose only snapshot is unverified is a missing capture, with that reason
    _write(world, ("DELETE FROM sync_runs WHERE source = 'espn'", []))
    rep = _assess(world, SAT)
    miss = next(m for w in rep["weeks"] if w["week"] == 6 for m in w["missing"] if m["evidence"] == "espn_snapshot")
    assert "only an UNVERIFIED snapshot" in miss["cause"]


def test_weeks_without_any_evidence_are_still_audited(world, monkeypatch):
    """Review P2: with no future kickoff and no stored evidence, every played week is still audited."""
    import shutil
    _write(world, ("DELETE FROM independent_projection_rows", []), ("DELETE FROM independent_projection_runs", []))
    shutil.rmtree(world.weekly / "ledger")
    monkeypatch.setattr(eh, "week_finality", lambda con, season, weeks: {w: {"fully_final": True, "detail": "fully final"}
                                                                         for w in weeks})
    rep = _assess(world, datetime(2026, 11, 2, tzinfo=UTC))          # after the last scheduled kickoff
    assert rep["next_kickoff"] is None
    assert [(w["week"], w["state"]) for w in rep["weeks"]] == [(6, "played"), (7, "played")]
    kinds = {m["evidence"] for w in rep["weeks"] for m in w["missing"]}
    assert {"production_forecast", "research_forecast"} <= kinds


def test_next_kickoff_and_london_gap(world):
    rep = _assess(world, SAT)
    nk = rep["next_kickoff"]
    assert nk["kickoff"] == LONDON.isoformat() and nk["games"] == ["CCC@DDD"]
    assert nk["next_scheduled_refresh"] is None              # Sun 08:00 PDT comes after the 06:30 PDT kickoff
    assert any("fresh ESPN snapshot" in m and "no scheduled refresh lands" in m for m in nk["missing"])
    assert any("Start/Sit" in m for m in nk["missing"])
    assert rep["schedule_gaps"][0]["games"] == ["CCC@DDD"]
    issue = next(i for i in rep["issues"] if i["code"] == "next_kickoff_unready")
    assert issue["severity"] == eh.WARN and "refresh_all.py" in issue["fix"]
    assert "before_next_kickoff" in rep["answers"] and "FALLBACK" in rep["answers"]["ifp2_served"]
    text = eh.render(rep)
    assert "Week 6 [in_progress]" in text and "MISSING" in text


def test_a_recorded_decision_set_counts_and_a_tampered_one_is_corrupt(world):
    cands = [{"league_id": 99, "fantasy_team_id": 1, "season": 2026, "week": 6, "espn_player_id": pid, "gsis_id": f"00-{pid}",
              "position": PLAYERS[pid][0], "team": PLAYERS[pid][1],
              "kickoff": {"AAA": TNF, "BBB": TNF, "CCC": LONDON, "EEE": SUNDAY}[PLAYERS[pid][1]], "slot": "FLEX",
              "is_starter": True, "roster_synced_at": THU_PT_1500, "preds": {}} for pid in ROSTERED]
    ds = sse.decision_set(2026, 6, cands, [], {99: {"QB": 1}}, THU_PT_1500 + timedelta(minutes=5), models=["espn"])
    path, created = sse.write_decision_set(world.start_sit, ds)
    rep = _assess(world, SAT)
    w6 = next(w for w in rep["weeks"] if w["week"] == 6)
    assert w6["slots"][0]["start_sit_recorded_at"] == (THU_PT_1500 + timedelta(minutes=5)).isoformat()
    assert "start_sit_decision_set" not in {m["evidence"] for m in w6["missing"]}
    assert w6["start_sit"]["effective"]["candidates"] == 4 and rep["answers"]["start_sit_growth"].startswith("1 decision set")
    bad = json.loads(path.read_text())
    bad["recorded_at"] = "2026-10-14T00:00:00+00:00"                     # backdating breaks the hash
    path.write_text(json.dumps(bad))
    rep = _assess(world, SAT)
    corrupt = [i for i in rep["issues"] if i["code"] == "evidence_file_corrupt"]
    assert corrupt and corrupt[0]["severity"] == eh.FAIL and "quarantine" in corrupt[0]["fix"]


def test_played_week_finality_and_benchmark(world, monkeypatch):
    tue = datetime(2026, 10, 20, 15, tzinfo=UTC)
    monkeypatch.setattr(eh, "week_finality", lambda con, season, weeks: {w: {"fully_final": True, "detail": "fully final"}
                                                                         for w in weeks})
    rep = _assess(world, tue)
    w6 = next(w for w in rep["weeks"] if w["week"] == 6)
    assert w6["state"] == "played"
    assert {m["evidence"] for m in w6["missing"]} >= {"start_sit_decision_set", "production_forecast"}
    assert any(i["code"] == "benchmark_not_recorded" and i["week"] == 6 for i in rep["issues"])
    monkeypatch.setattr(eh, "week_finality", lambda con, season, weeks: {w: {"fully_final": False, "detail": "6/8 teams final"}
                                                                         for w in weeks})
    rep = _assess(world, tue)
    assert any(i["code"] == "week_pending_finality" and "6/8" in i["message"] for i in rep["issues"])


def test_scheduler_problems_are_issues(world):
    (world.launch_agents / "com.fantasy-intel.refresh.plist").unlink()
    rep = _assess(world, SAT)
    assert any(i["code"] == "scheduler_not_installed" and i["subject"] == "refresh" for i in rep["issues"])
    con = connect(world.db, read_only=True)
    try:
        ctx = _ctx(world, SAT)
        ctx.runner = fake_launchctl(set())                                  # installed but nothing loaded
        rep = eh.assess(con, ctx)
    finally:
        con.close()
    assert any(i["code"] == "scheduler_not_loaded" and i["subject"] == "weekly-learning" for i in rep["issues"])


def test_storage_and_hosted_checks(world):
    import duckdb

    rep = _assess(world, SAT, thresholds=eh.Thresholds(disk_warn_gb=1e9, disk_fail_gb=1e8))
    assert next(i for i in rep["issues"] if i["code"] == "disk_low")["severity"] == eh.FAIL
    md = duckdb.connect()
    md.execute("CREATE TABLE schema_meta (key VARCHAR, value VARCHAR)")
    md.execute("INSERT INTO schema_meta VALUES ('schema_version', '7'), ('published_at', '2026-10-15T10:00:00')")

    class NoClose:
        def __init__(self, c):
            self.c = c

        def execute(self, *a):
            return self.c.execute(*a)

        def close(self):
            pass

    hosted = eh.hosted_status("fantasy_intel", lambda name: NoClose(md))
    assert hosted == {"checked": True, "schema_version": 7, "published_at": "2026-10-15T10:00:00+00:00"}
    rep = _assess(world, SAT, hosted=hosted)
    codes = {i["code"] for i in rep["issues"]}
    assert {"hosted_schema_mismatch", "hosted_outdated"} <= codes
    assert eh.hosted_status("x", lambda name: (_ for _ in ()).throw(RuntimeError("no token")))["error"].startswith("RuntimeError")


def test_filesystem_write_failure_is_reported(world):
    world.logs.chmod(0o500)
    try:
        if os.access(world.logs, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        assert eh.storage(world, eh.Thresholds())["write_ok"] is False
    finally:
        world.logs.chmod(0o700)


def test_cli_status_json_and_exit_code(cli, world, capsys, monkeypatch):
    monkeypatch.setattr(eh, "run_command", fake_launchctl({eh.label(j) for j in eh.REQUIRED_JOBS}))  # never the real launchd
    code = cli.main(["status", "--json", "--root", str(world.root), "--db", str(world.db), "--now", SAT.isoformat()])
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == eh.SCHEMA and out["status"] in (eh.FAIL, eh.WARN)
    assert code == cli.EXIT[out["status"]]
    assert cli.main(["--root", str(world.root), "--db", str(world.db), "--now", SAT.isoformat()]) == code   # status is the default
    assert "Evidence health 2026" in capsys.readouterr().out


# ----------------------------------------------------------------------------- guard


def _log(started: datetime, *, evidence=True, start_sit=0, status="complete"):
    return {"log": f"refresh-{started:%Y%m%dT%H%M%SZ}.log", "started_at": started.isoformat(), "status": status,
            "evidence": {"ran": evidence, "status": "ok",
                         "steps": [{"name": "start_sit_evaluation.py", "result": "ok" if start_sit == 0 else "failed"}]
                         if evidence else []}}


def test_guard_decisions():
    cfg = eh.GuardConfig()
    k = LONDON
    slot = k.isoformat()
    far = k - timedelta(hours=10)
    assert eh.guard_decision(far, [k], [], {}, {}, cfg)["action"] == "idle"
    now = k - timedelta(hours=4)
    assert eh.guard_decision(now, [k], [], {slot: None}, {}, cfg)["action"] == "run"
    fresh = [_log(k - timedelta(hours=5))]
    assert eh.guard_decision(now, [k], fresh, {slot: k - timedelta(hours=5)}, {}, cfg)["action"] == "captured"
    # an old Thursday capture (63.5 h) is not fresh; a refresh whose Start/Sit step failed is not a capture
    old = [_log(k - timedelta(hours=63.5))]
    assert eh.guard_decision(now, [k], old, {slot: k - timedelta(hours=63.5)}, {}, cfg)["action"] == "run"
    bad = [_log(k - timedelta(hours=5), start_sit=1)]
    assert eh.guard_decision(now, [k], bad, {slot: k - timedelta(hours=5)}, {}, cfg)["action"] == "run"
    assert eh.guard_decision(now, [k], [_log(now, status="running")], {}, {}, cfg)["action"] == "wait"
    recent = {slot: [{"at": (now - timedelta(minutes=5)).isoformat(), "exit": 1}]}
    assert eh.guard_decision(now, [k], [], {}, recent, cfg)["action"] == "wait"
    spent = {slot: [{"at": (now - timedelta(hours=h)).isoformat(), "exit": 1} for h in (3, 2, 1)]}
    assert eh.guard_decision(now, [k], [], {}, spent, cfg)["action"] == "gave_up"
    # inside the margin the kickoff is never chased: the guard moves on to the next kickoff
    d = eh.guard_decision(k - timedelta(minutes=5), [k, SUNDAY], [], {}, {}, cfg)
    assert d["slot"] == SUNDAY.isoformat() and d["action"] == "run"
    assert eh.guard_decision(k + timedelta(minutes=1), [k], [], {}, {}, cfg)["action"] == "idle"


def test_guard_defers_to_a_scheduled_refresh_that_covers_the_kickoff():
    cfg = eh.GuardConfig()
    thu_1500 = TNF - timedelta(hours=2, minutes=15)          # the refresh job's Thursday 15:00 PDT firing
    early = TNF - timedelta(hours=5)
    assert eh.guard_decision(early, [TNF], [], {}, {}, cfg, [thu_1500])["action"] == "wait"
    # 45 min after the firing with nothing captured: the scheduled run did not happen or failed -> fallback
    late = thu_1500 + timedelta(minutes=50)
    assert eh.guard_decision(late, [TNF], [], {}, {}, cfg, [thu_1500])["action"] == "run"
    # a firing after kickoff (Sunday 08:00 PDT vs a 06:30 PDT London game) covers nothing
    sun_0800 = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
    assert eh.guard_decision(LONDON - timedelta(hours=3), [LONDON], [], {}, {}, cfg, [sun_0800])["action"] == "run"


TUE_0800 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)       # the refresh job's Tuesday 08:00 PDT firing


def test_catchup_decisions():
    cfg = eh.CatchupConfig()
    fired = [datetime(2026, 10, 19, 22, 0, tzinfo=UTC), TUE_0800]    # Mon 15:00 and Tue 08:00 PDT
    later = TUE_0800 + timedelta(hours=3)
    # the Mac was off through Tuesday 08:00: the next check runs it, once for the latest firing only
    d = eh.catchup_decision(later, fired, [], {}, cfg)
    assert d["action"] == "run" and d["slot"] == TUE_0800.isoformat() and d["attempt"] == 1
    # a completed refresh after the firing (scheduled or manual) covers it; one before it does not
    assert eh.catchup_decision(later, fired, [_log(TUE_0800 + timedelta(seconds=4))], {}, cfg)["action"] == "covered"
    assert eh.catchup_decision(later, fired, [_log(TUE_0800 - timedelta(minutes=5))], {}, cfg)["action"] == "run"
    # a failed or killed run is retried; a running one is waited for
    failed = [_log(TUE_0800 + timedelta(seconds=4), status="failed")]
    d = eh.catchup_decision(later, fired, failed, {}, cfg)
    assert d["action"] == "run" and "failed" in d["reason"]
    assert eh.catchup_decision(later, fired, [_log(later, status="running")], {}, cfg)["action"] == "wait"
    # launchd already started the refresh it replays on wake, before that refresh wrote its log
    assert eh.catchup_decision(later, fired, [], {}, cfg, job_running=True)["action"] == "wait"
    # right after the firing the scheduled run may still be starting
    assert eh.catchup_decision(TUE_0800 + timedelta(minutes=5), fired, [], {}, cfg)["slot"] == fired[0].isoformat()
    # bounded retries, spaced
    slot = TUE_0800.isoformat()
    recent = {slot: [{"at": (later - timedelta(minutes=5)).isoformat(), "exit": 1}]}
    assert eh.catchup_decision(later, fired, [], recent, cfg)["action"] == "wait"
    spent = {slot: [{"at": (later - timedelta(hours=h)).isoformat(), "exit": 1} for h in (2.5, 2, 1)]}
    assert eh.catchup_decision(later, fired, [], spent, cfg)["action"] == "gave_up"
    # nothing in the lookback window: nothing to catch up
    assert eh.catchup_decision(later + timedelta(days=8), fired, [], {}, cfg)["action"] == "idle"
    assert eh.catchup_decision(later, [], [], {}, cfg)["action"] == "idle"


def _guard_args(world, now, **kw):
    base = dict(root=str(world.root), db=str(world.db), now=now.isoformat(), season=None, from_week=None, hosted=False,
                database="fantasy_intel", notify=False, execute=True, lead_hours=6.0, max_age_hours=12.0,
                max_attempts=3, retry_minutes=25.0, catch_up_settle_seconds=0)
    return argparse.Namespace(**{**base, **kw})


LAUNCHD = fake_launchctl({eh.label(j) for j in eh.REQUIRED_JOBS})


def test_guard_runs_bounded_retries_and_records_them(cli, world, capsys):
    ran = []
    t0 = LONDON - timedelta(hours=5)
    for i in range(5):                                       # every 30 min, the refresh keeps failing
        cli.cmd_guard(_guard_args(world, t0 + timedelta(minutes=30 * i)), refresh=lambda: ran.append(1) or 1,
                      runner=LAUNCHD)
    assert len(ran) == 3                                     # max_attempts, then it gives up
    state = json.loads((world.state / "guard-state.json").read_text())
    assert [a["exit"] for a in state["attempts"][LONDON.isoformat()]] == [1, 1, 1]
    assert state["last"]["action"] == "gave_up"
    capsys.readouterr()
    ran.clear()
    cli.cmd_guard(_guard_args(world, t0, execute=False), refresh=lambda: ran.append(1) or 0, runner=LAUNCHD)
    assert ran == [] and not (world.state / "guard.lock").exists()


@pytest.fixture
def pacific_mac():
    """launchd's calendar is the Mac's local clock: pin it to Pacific (CI runs in UTC)."""
    import time
    original = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_guard_catches_up_a_missed_scheduled_refresh(cli, world, capsys, pacific_mac, monkeypatch):
    monkeypatch.setenv("HOME", str(world.launch_agents.parent.parent))     # the world's LaunchAgents, not this Mac's
    ran = []
    sat_noon = SAT                                           # last firing: Thu 15:00 PDT, whose refresh completed
    cli.cmd_guard(_guard_args(world, sat_noon, catch_up=True), refresh=lambda: ran.append(1) or 0, runner=LAUNCHD)
    d = json.loads(capsys.readouterr().out)
    assert d["catchup"]["action"] == "covered" and ran == []
    # Monday 20:00 PDT, the Mac was off from Saturday: Sunday 08:00 and Monday 15:00 were missed -> one refresh
    mon = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    cli.cmd_guard(_guard_args(world, mon, catch_up=True), refresh=lambda: ran.append(1) or 1, runner=LAUNCHD)
    d = json.loads(capsys.readouterr().out)
    assert d["catchup"]["action"] == "run" and d["executed"] is True and ran == [1]
    assert d["catchup"]["slot"] == datetime(2026, 10, 19, 22, 0, tzinfo=UTC).isoformat()
    state = json.loads((world.state / "guard-state.json").read_text())
    assert [a["exit"] for a in state["catchup_attempts"][d["catchup"]["slot"]]] == [1]
    # on wake launchd starts the missed firing itself: after the settle pause the guard sees it running and waits
    calls = []

    def waking(args):
        if args[:2] == ["launchctl", "print"] and calls:
            return 0, LOADED.replace("state = not running", "state = running")
        return LAUNCHD(args)
    tue = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
    cli.cmd_guard(_guard_args(world, tue, catch_up=True, catch_up_settle_seconds=90),
                  refresh=lambda: ran.append(1) or 0, runner=waking, sleep=calls.append)
    d = json.loads(capsys.readouterr().out)
    assert calls == [90] and d["catchup"]["action"] == "wait" and ran == [1]
    # without the flag the guard behaves as before
    cli.cmd_guard(_guard_args(world, mon + timedelta(hours=1)), refresh=lambda: ran.append(1) or 0, runner=LAUNCHD)
    assert "catchup" not in json.loads(capsys.readouterr().out) and ran == [1]


def test_guard_does_nothing_far_from_kickoff_or_without_execute(cli, world, capsys):
    ran = []
    assert cli.cmd_guard(_guard_args(world, SAT - timedelta(days=1)), refresh=lambda: ran.append(1) or 0,
                         runner=LAUNCHD) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "idle"
    cli.cmd_guard(_guard_args(world, LONDON - timedelta(hours=2), execute=False), refresh=lambda: ran.append(1) or 0,
                  runner=LAUNCHD)
    d = json.loads(capsys.readouterr().out)
    assert d["action"] == "run" and d["executed"] is False and "refresh_all.py" in d["would_run"] and ran == []


# ----------------------------------------------------------------------------- alerts and publish records


def test_alerts_are_sent_once_per_open_issue():
    now = SAT
    a = {"fingerprint": "x:1", "severity": eh.FAIL, "message": "m"}
    b = {"fingerprint": "y:1", "severity": eh.INFO, "message": "context only"}
    fresh, state = eh.new_alerts([a, b], {}, now)
    assert fresh == [a] and list(state) == ["x:1"]
    fresh, state = eh.new_alerts([a], state, now + timedelta(hours=1))
    assert fresh == [] and state["x:1"] == now.isoformat()                # still open: not repeated
    fresh, state = eh.new_alerts([], state, now)
    assert state == {}                                                     # resolved: forgotten
    fresh, _ = eh.new_alerts([a], state, now)
    assert fresh == [a]                                                    # it came back: notify again


def test_a_failed_notification_is_retried_not_suppressed(cli, world):
    """Review P2: a fingerprint is stored only after the notification was delivered."""
    report = {"issues": [{"fingerprint": "x:1", "severity": eh.FAIL, "message": "m"}]}
    assert cli.notify(report, world, SAT, runner=lambda a: (1, "osascript failed")) == []
    assert eh.read_json(world.state / "alerts.json", None) == {}
    calls = []
    sent = cli.notify(report, world, SAT, runner=lambda a: calls.append(a) or (0, ""))
    assert [i["fingerprint"] for i in sent] == ["x:1"] and len(calls) == 1
    assert list(eh.read_json(world.state / "alerts.json", None)) == ["x:1"]
    assert cli.notify(report, world, SAT, runner=lambda a: calls.append(a) or (0, "")) == [] and len(calls) == 1


def test_notify_uses_osascript_once(cli, world):
    calls = []
    report = {"issues": [{"fingerprint": "x:1", "severity": eh.WARN, "message": 'say "hi"'}]}
    cli.notify(report, world, SAT, runner=lambda a: calls.append(a) or (0, ""))
    cli.notify(report, world, SAT, runner=lambda a: calls.append(a) or (0, ""))
    assert len(calls) == 1 and calls[0][0] == "osascript" and '\\"hi\\"' in calls[0][2]


def test_publish_records_from_logs_and_history(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    pub = _load("publish_motherduck")
    db = tmp_path / "f.duckdb"
    con = connect(db)
    init_db(con)
    con.close()
    hist = tmp_path / "logs" / "publish-history.jsonl"
    pub.record_publish("2026-10-16T04:46:22", "fantasy_intel", db, history=hist)
    rec = json.loads(hist.read_text())
    assert rec["published_at"] == "2026-10-16T04:46:22" and rec["local_schema_version"] == 8
    runs = [{"log": "refresh-a.log", "publish": {"published_at": "2026-10-16T04:02:49"}}]
    got = eh.publish_records(tmp_path / "logs", runs)
    assert [g["published_at"] for g in got] == ["2026-10-16T04:02:49", "2026-10-16T04:46:22"]


def test_an_r2_snapshot_publish_is_recognized_like_a_motherduck_one():
    """Review P2 (#144): refresh_all may run publish_snapshot.py instead of publish_motherduck.py."""
    text = "\n".join([
        "==> publish_snapshot.py  (started 2026-10-20T06:00:00Z)",
        "Published data/fantasy.duckdb -> r2:fantasy-intel/snapshots/20261020T060000.duckdb (95.4 MB) at 2026-10-20T06:00:01",
        "<== exit 0 after 3.1s",
        "Refresh complete.",
    ])
    r = eh.parse_refresh_log(text)
    assert r["publish"]["ran"] and r["publish"]["step"] == "publish_snapshot.py"
    assert r["publish"]["exit"] == 0 and r["publish"]["published_at"] == "2026-10-20T06:00:01"

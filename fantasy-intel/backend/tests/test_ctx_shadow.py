"""scripts/ctx_shadow.py: the prospective shadow automation of the frozen ifp-ctx-p1 research candidate.
Freeze rule, write-once / idempotent pregame freeze, missing-injury warnings, failure isolation (here and in
learning_cycle.py / refresh_all.py) and scoring alongside the frozen production ifp-2. context_research.py
itself is replaced by fakes (it needs the real feature store); nothing writes a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from app.analytics import weekly_learning as wl

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
SUN = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)          # 1h before a 10:00 ET (14:00 UTC) week-7 slate


@pytest.fixture(scope="module")
def cs():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("ctx_shadow", SCRIPTS / "ctx_shadow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args(cs, tmp_path):
    return cs.parse(["pregame", "--season", "2026", "--ifp-dir", str(tmp_path / "models"),
                     "--live-db", str(tmp_path / "live.duckdb"), "--history-db", str(tmp_path / "hist.duckdb"),
                     "--weekly-dir", str(tmp_path / "weekly"), "--exits-dir", str(tmp_path / "weekly")])


def team(name, hours, *, rows=6, designations=2, status="fresh", source="nflverse", started=False):
    """An injury_freshness.team_freshness row (only the keys the freeze rule reads)."""
    return {"team": name, "kickoff": None if hours is None else (SUN + timedelta(hours=hours)).isoformat(),
            "hours_to_kickoff": hours, "game_started": started, "status": status, "reason": "within_threshold",
            "source_used": source if designations else ("espn_status" if source == "espn_status" else "none"),
            "nflverse_designation_synced": designations > 0,
            "nflverse_report": {"rows": rows, "designations": designations, "post_kickoff_designations": 0,
                                "practice_only": rows - designations}}


def slate(n_ready, n_practice, hours=48.0, extra=()):
    return [*(team(f"R{i:02d}", hours) for i in range(n_ready)),
            *(team(f"P{i:02d}", hours, designations=0) for i in range(n_practice)), *extra]


def snapshot_of(teams):
    return lambda args, season, week, now: {"teams": teams, "summary": {"teams": len(teams)}, "log_present": True,
                                            "nflverse_writer": {"verified": True}, "upcoming_issues": []}


def fake_predict(calls, content="v1"):
    """Writes the ledger exactly as context_research.py cmd_predict does (write_once), and reports it."""
    def predict(args, season, week):
        calls.append((season, week))
        path = args.ifp_dir / "research" / "context" / "ledger" / f"s{season}w{week:02d}-pregame.json"
        written = wl.write_once(path, json.dumps({"season": season, "week": week, "created_at": SUN.isoformat(),
                                                  "rows": [], "content": content}))
        return {"ledger": str(path), "written": written, "rows": 0, "created_at": SUN.isoformat()}
    return predict


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _runs(args):
    d = args.ifp_dir / "research" / "context" / "shadow" / "runs"
    return sorted(d.glob("*.json")) if d.exists() else []


# ----------------------------------------------------------------------------- freeze rule


def test_final_reports_for_most_open_teams_freeze(cs):
    d = cs.freeze_decision(slate(26, 2))
    assert (d["action"], d["degraded"], d["open_teams"], d["ready_teams"]) == ("freeze", True, 28, 26)
    assert len(d["warnings"]) == 1 and "2 of 28 open teams" in d["warnings"][0]
    assert "P00 (practice report" in d["warnings"][0]
    assert cs.freeze_decision(slate(28, 0))["warnings"] == []


def test_a_mid_week_practice_report_defers_quietly(cs):
    """Thursday: the TNF teams have their final report, the Sunday teams only a practice report."""
    d = cs.freeze_decision(slate(2, 26, hours=60, extra=()))
    assert d["action"] == "defer" and d["warnings"] == [] and "deferred" in d["notes"][0]
    tnf = [team("T1", 2.0), team("T2", 2.0), *(team(f"S{i}", 70.0, designations=0) for i in range(26))]
    assert cs.freeze_decision(tnf)["action"] == "defer"          # 2 of 28 imminent is not the main slate


def test_an_imminent_main_slate_freezes_with_the_degraded_teams_listed(cs):
    teams = [*(team(f"R{i}", 1.0) for i in range(14)), *(team(f"S{i}", 1.0, status="stale") for i in range(14))]
    d = cs.freeze_decision(teams)
    assert (d["action"], d["degraded"]) == ("freeze", True)
    assert "degraded injury evidence for 14 of 28" in d["warnings"][0] and "S0 (stale" in d["warnings"][0]


def test_a_missing_injury_report_never_freezes_and_warns_when_the_slate_is_close(cs):
    far = cs.freeze_decision([team("A", 70, rows=0, designations=0), team("B", 70, rows=0, designations=0)])
    assert far["action"] == "defer" and far["warnings"] == [] and "not ingested" in far["notes"][0]
    near = cs.freeze_decision([team("A", 2, rows=0, designations=0), team("B", 2, rows=0, designations=0)])
    assert near["action"] == "defer" and "will be missed" in near["warnings"][0]


def test_started_unknown_and_espn_only_teams(cs):
    d = cs.freeze_decision([team("A", -3, started=True), team("B", None), team("C", 1, designations=0,
                                                                                source="espn_status")])
    assert d["open_teams"] == 1 and d["started_teams"] == 1 and d["unknown_kickoff"] == ["B"]
    assert "unknown kickoff" in d["warnings"][0]
    assert "reads only the nflverse report" in d["not_ready"]["C"]
    assert cs.freeze_decision([team("A", -3, started=True)])["action"] == "nothing_open"


# ----------------------------------------------------------------------------- pregame freeze


def test_freezes_once_and_reruns_are_idempotent(cs, args):
    calls = []
    rec = cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(slate(28, 0)), predict=fake_predict(calls))
    assert rec["status"] == "frozen" and calls == [(2026, 7)]
    ledger = cs.ledger_path(args, 2026, 7)
    freeze = args.ifp_dir / "research" / "context" / "shadow" / "freeze" / "s2026w07.json"
    snap = json.loads(freeze.read_text())
    assert snap["ledger_sha256"] == _sha(ledger) == rec["ledger_sha256"] and snap["decision"]["action"] == "freeze"
    assert len(_runs(args)) == 1
    before = {p: _sha(p) for p in (ledger, freeze)}
    for _ in range(2):                                          # rerun: nothing called, written or recorded
        again = cs.run_pregame(args, 2026, 7, now=SUN + timedelta(minutes=30), snapshot=snapshot_of(slate(28, 0)),
                               predict=lambda *a: pytest.fail("predict must not run once the week is frozen"))
        assert again["status"] == "already_frozen" and again["ledger_sha256"] == before[ledger]
    assert {p: _sha(p) for p in before} == before and len(_runs(args)) == 1


def test_a_frozen_ledger_is_never_overwritten(cs, args):
    """A second writer (another process racing the first, or a later run with newer data) cannot replace it."""
    calls = []
    cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(slate(28, 0)), predict=fake_predict(calls, "first"))
    ledger = cs.ledger_path(args, 2026, 7)
    first = ledger.read_text()
    res = fake_predict(calls, "second")(args, 2026, 7)          # the write primitive context_research uses
    assert res["written"] is False and ledger.read_text() == first
    ledger.unlink()                                             # simulate a race: the ledger appears mid-run
    racer = fake_predict(calls, "racer")

    def late(args_, season, week):
        racer(args_, season, week)
        return fake_predict(calls, "loser")(args_, season, week)

    rec = cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(slate(28, 0)), predict=late)
    assert rec["status"] == "already_frozen" and json.loads(ledger.read_text())["content"] == "racer"


def test_missing_injury_report_is_recorded_and_never_forecast(cs, args):
    teams = [team(f"T{i}", 2.0, rows=0, designations=0) for i in range(28)]
    rec = cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(teams),
                         predict=lambda *a: pytest.fail("no forecast without the injury report"))
    assert rec["status"] == "deferred" and "will be missed" in rec["warnings"][0]
    assert not cs.ledger_path(args, 2026, 7).exists()
    stored = json.loads(_runs(args)[0].read_text())
    assert stored["warnings"] == rec["warnings"] and stored["decision"]["report_rows"] == 0


def test_missing_report_through_the_real_injury_diagnostics(cs, args, tmp_path):
    """The default snapshot path: injury_freshness.diagnostics on a read-only database with the week's
    schedule but no injury rows and no retrieval log."""
    import duckdb

    con = duckdb.connect(str(args.live_db))
    con.execute("CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR, "
                "gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)")
    con.execute("INSERT INTO nfl_schedules VALUES (2026, 7, 'REG', '2026-10-25', '10:00', 'AAA', 'BBB'), "
                "(2026, 7, 'REG', '2026-10-25', '13:00', 'CCC', 'DDD')")
    con.execute("CREATE TABLE nfl_injuries (season INT, week INT, game_type VARCHAR, team VARCHAR, gsis_id VARCHAR, "
                "report_status VARCHAR, practice_status VARCHAR)")
    con.close()
    rec = cs.run_pregame(args, 2026, 7, now=SUN, predict=lambda *a: pytest.fail("no forecast without the report"))
    assert rec["status"] == "deferred" and rec["decision"]["report_rows"] == 0
    assert any("not ingested" in w for w in rec["warnings"])                 # 2 of 2 open teams within 6h
    assert any("no injury_retrievals log" in w for w in rec["warnings"])


def test_a_failed_forecast_is_recorded_not_raised(cs, args):
    def boom(*a):
        raise RuntimeError("context_research.py predict exited 1: every week-7 game has kicked off")

    rec = cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(slate(28, 0)), predict=boom)
    assert rec["status"] == "failed" and "kicked off" in rec["error"] and rec["warnings"]
    assert json.loads(_runs(args)[0].read_text())["status"] == "failed"
    assert not (args.ifp_dir / "research" / "context" / "shadow" / "freeze").exists()

    def broken_snapshot(*a):
        raise OSError("database is locked")

    assert cs.run_pregame(args, 2026, 7, now=SUN, snapshot=broken_snapshot, predict=boom)["status"] == "failed"


def test_cli_exit_codes(cs, args, monkeypatch, capsys):
    base = ["pregame", "--season", "2026", "--week", "7", "--ifp-dir", str(args.ifp_dir)]
    monkeypatch.setattr(cs, "injury_snapshot", snapshot_of([team("A", 2, rows=0, designations=0)]))
    assert cs.main(base) == cs.EXIT_WARNINGS                    # missing report, slate close
    assert "WARNING: ifp-ctx-p1 shadow" in capsys.readouterr().err
    monkeypatch.setattr(cs, "injury_snapshot", snapshot_of(slate(28, 0)))
    monkeypatch.setattr(cs, "default_predict", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cs.main(base) == cs.EXIT_FAILED
    monkeypatch.setattr(cs, "default_predict", fake_predict([]))
    assert cs.main(base) == cs.EXIT_OK
    assert cs.main([*base, "--now", "2026-10-25T13:00Z"]) == cs.EXIT_OK      # dry run of a frozen week


def test_dry_run_decides_without_forecasting_or_recording(cs, args):
    args.write = False
    rec = cs.run_pregame(args, 2026, 7, now=SUN, snapshot=snapshot_of(slate(28, 0)),
                         predict=lambda *a: pytest.fail("a dry run never forecasts"))
    assert rec["status"] == "would_freeze" and _runs(args) == []


def test_weeks_before_the_preregistered_start_are_never_frozen(cs, args):
    rec = cs.run_pregame(args, 2026, 6, now=SUN, snapshot=lambda *a: pytest.fail("not even checked"),
                         predict=lambda *a: pytest.fail("never"))
    assert rec["status"] == "before_prospective_start" and _runs(args) == []


def test_a_started_week_without_a_ledger_is_recorded_missed_once(cs, args):
    ko = {"AAA": SUN - timedelta(days=3), "BBB": SUN - timedelta(days=3)}
    rec = cs.check_missed(args, 2026, 7, now=SUN, kickoffs=ko)
    path = args.ifp_dir / "research" / "context" / "shadow" / "missed" / "s2026w07.json"
    assert rec["week"] == 7 and json.loads(path.read_text())["reason"].startswith("no ifp-ctx-p1")
    first = _sha(path)
    cs.check_missed(args, 2026, 7, now=SUN + timedelta(days=1), kickoffs=ko)
    assert _sha(path) == first
    assert cs.check_missed(args, 2026, 7, now=SUN - timedelta(days=4), kickoffs=ko) is None   # not started yet
    assert cs.check_missed(args, 2026, 6, now=SUN, kickoffs=ko) is None                        # before week 7
    fake_predict([])(args, 2026, 8)
    assert cs.check_missed(args, 2026, 8, now=SUN, kickoffs=ko) is None                        # frozen in time


# ----------------------------------------------------------------------------- scoring


def _scoring_world(args):
    import polars as pl

    ctx = args.ifp_dir / "research" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"key": [202607] * 3 + [202608], "gsis_id": ["a", "b", "c", "a"],
                  "position": ["QB", "WR", "WR", "QB"], "label": [20.0, 10.0, 4.0, 15.0]}).write_parquet(
        ctx / "store.parquet")
    rows = [{"gsis_id": "a", "ctx_points": 18.0, "ifp2_points": 16.0}, {"gsis_id": "b", "ctx_points": 11.0,
            "ifp2_points": 12.0}, {"gsis_id": "c", "ctx_points": None, "ifp2_points": 5.0},
            {"gsis_id": "z", "ctx_points": 3.0, "ifp2_points": 3.0}]           # z: did not appear
    wl.write_once(ctx / "ledger" / "s2026w07-pregame.json", json.dumps({"season": 2026, "week": 7, "rows": rows}))
    wl.write_once(ctx / "ledger" / "s2026w09-pregame.json",                     # week 9 not final yet
                  json.dumps({"season": 2026, "week": 9, "rows": rows}))
    prod = {"kind": "pregame", "created_at": "2026-10-20T10:00:00+00:00", "rows": [
        {"gsis_id": "a", "pregame": True, "challenger": {"points": 17.0}},
        {"gsis_id": "b", "pregame": True, "challenger": {"points": 12.5}}]}
    later = {**prod, "created_at": "2026-10-21T10:00:00+00:00",
             "rows": [{"gsis_id": "a", "pregame": True, "challenger": {"points": 99.0}}]}
    retro = {**prod, "kind": "retrospective", "created_at": "2026-10-19T10:00:00+00:00"}
    for name, run in (("p1", prod), ("p2", later), ("r", retro)):
        wl.write_once(args.weekly_dir / "ledger" / "s2026w07" / f"{name}.json", json.dumps(run))


def test_scores_identical_rows_against_the_frozen_production_ifp2(cs, args):
    _scoring_world(args)
    out = cs.score_alongside(args)
    assert [w["week"] for w in out["weeks"]] == [7]
    p = out["pooled"]                       # rows a and b: ctx 18/11, same-run ifp2 16/12, production 17/12.5
    assert p["n"] == 2
    assert p["ctx"] == {"mae": 1.5, "bias": -0.5}
    assert p["ifp2_same_run"] == {"mae": 3.0, "bias": -1.0}
    assert p["ifp2_production"] == {"mae": 2.75, "bias": -0.25}           # the earliest pregame run, not 99.0
    assert p["ctx_minus_ifp2_production_mae"] == -1.25
    assert set(out["by_position"]) == {"QB", "WR"}


def test_run_score_records_the_gate_and_scores(cs, args):
    assert cs.run_score(args, now=SUN, prospective=lambda a: pytest.fail("nothing to score"))["status"] == "no_ledgers"
    _scoring_world(args)
    rec = cs.run_score(args, now=SUN, prospective=lambda a: {"scored_weeks": 1, "gate": {"WR": {"status":
                                                                                              "insufficient_data"}}})
    assert rec["status"] == "scored"
    scores = json.loads((args.ifp_dir / "research" / "context" / "shadow" / "scores.json").read_text())
    assert scores["alongside_production"]["pooled"]["n"] == 2 and scores["prospective"]["scored_weeks"] == 1

    def boom(a):
        raise RuntimeError("prospective exited 1")

    assert cs.run_score(args, now=SUN, prospective=boom)["status"] == "failed"


def test_cycle_command_combines_score_missed_and_pregame(cs, args, monkeypatch):
    monkeypatch.setattr(cs, "injury_snapshot", snapshot_of([team("A", 50, rows=0, designations=0)]))
    monkeypatch.setattr(cs, "check_missed", lambda a, s, w, now=None: {"week": w})
    args.week = 7
    out = cs.cmd_cycle(args, SUN)
    assert out["status"] == "ok" and out["pregame"]["status"] == "deferred" and out["score"]["status"] == "no_ledgers"
    assert out["warnings"] == ["week 6 started without a frozen ifp-ctx-p1 ledger (recorded as missed)"]


# ----------------------------------------------------------------------------- isolation in the schedulers


@pytest.fixture(scope="module")
def lc():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("learning_cycle_ctx", SCRIPTS / "learning_cycle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Proc:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_cycle_stage_command_and_failure_handling(lc, tmp_path, monkeypatch):
    ns = lc.parse(["run", "--season", "2026", "--live-db", str(tmp_path / "l.duckdb"), "--out", str(tmp_path / "w"),
                   "--ctx-dir", str(tmp_path / "m")])
    ns.week = 7
    seen = []
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or _Proc(
        3, json.dumps({"status": "ok", "warnings": ["stale"]}), "WARNING: stale"))
    conns = []
    assert lc.stage_ctx_shadow(ns, conns, tmp_path / "log.txt") == {"status": "ok", "warnings": ["stale"]}
    cmd, kw = seen[0]
    assert Path(cmd[1]).name == "ctx_shadow.py" and cmd[2:5] == ["cycle", "--season", "2026"]
    assert cmd[cmd.index("--week") + 1] == "7"
    assert cmd[cmd.index("--ifp-dir") + 1] == str(tmp_path / "m") and kw["timeout"] == lc.CTX_SHADOW_TIMEOUT_S
    assert "WARNING: stale" in (tmp_path / "log.txt").read_text()
    assert "--now" not in cmd
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: _Proc(1, "", "Traceback ... MemoryError"))
    with pytest.raises(RuntimeError, match="ctx_shadow.py exited 1"):
        lc.stage_ctx_shadow(ns, [], tmp_path / "log.txt")


def test_cycle_stage_forwards_the_simulated_clock_and_omits_a_missing_week(lc, tmp_path, monkeypatch):
    """A `--now` audit cycle must not let the shadow act on wall-clock time: ctx_shadow.py gets the same clock,
    which makes it a dry run. After the last week there is no target week, so only scoring runs."""
    ns = lc.parse(["run", "--season", "2026", "--now", "2027-01-20T12:00:00+00:00", "--out", str(tmp_path / "w")])
    seen = []
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc(0, "{}"))
    lc.stage_ctx_shadow(ns, [], tmp_path / "log.txt")
    assert seen[0][seen[0].index("--now") + 1] == "2027-01-20T12:00:00+00:00" and "--week" not in seen[0]


def test_a_dry_run_never_runs_the_prospective_scorer(cs, args):
    _scoring_world(args)
    args.write = False
    rec = cs.run_score(args, now=SUN, prospective=lambda a: pytest.fail("prospective writes prospective.json"))
    assert rec["status"] == "would_score" and _runs(args) == []
    assert not (args.ifp_dir / "research" / "context" / "shadow" / "scores.json").exists()


def test_after_the_last_week_the_cycle_scores_and_checks_that_week(cs, args, monkeypatch):
    monkeypatch.setattr(cs, "_target_week", lambda a, now: None)
    monkeypatch.setattr(cs, "_last_week", lambda a: 18)
    checked = []
    monkeypatch.setattr(cs, "check_missed", lambda a, s, w, now=None: checked.append(w))
    _scoring_world(args)
    monkeypatch.setattr(cs, "default_prospective", lambda a: {"scored_weeks": 1, "gate": {}})
    out = cs.cmd_cycle(args, SUN + timedelta(days=90))
    assert out["score"]["status"] == "scored" and out["pregame"]["status"] == "no_upcoming_week"
    assert checked == [18] and out["status"] == "ok"

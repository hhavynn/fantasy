"""scripts/qb_scenario_shadow.py: the prospective shadow of the pre-registered QB-only scenario candidate
ifp-ctx-p2q. The frozen spec (write-once, bound to the ifp-2 config, scoring, roster-context version and the p2
model code), the write-once / idempotent pregame ledger, which ledgers count as evidence, p1's gate and the
prospective start-probability calibration. The model itself (qb_scenarios.py) is tested in test_qb_scenarios.py;
here the forecast is a fake (it needs the real feature store) and nothing writes a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import weekly_learning as wl

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
SUN = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def qss():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("qb_scenario_shadow", SCRIPTS / "qb_scenario_shadow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args(qss, tmp_path):
    return qss.parse(["pregame", "--season", "2026", "--ifp-dir", str(tmp_path / "models"),
                      "--live-db", str(tmp_path / "live.duckdb"), "--history-db", str(tmp_path / "hist.duckdb"),
                      "--weekly-dir", str(tmp_path / "weekly"), "--exits-dir", str(tmp_path / "weekly")])


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ready_snapshot(args, season, week, now):
    teams = [{"team": f"T{i:02d}", "kickoff": (SUN + timedelta(hours=2)).isoformat(), "hours_to_kickoff": 2.0,
              "game_started": False, "status": "fresh", "reason": "within_threshold", "source_used": "nflverse",
              "nflverse_designation_synced": True,
              "nflverse_report": {"rows": 4, "designations": 2, "post_kickoff_designations": 0, "practice_only": 2}}
             for i in range(10)]
    return {"teams": teams, "summary": {"teams": 10}, "log_present": True, "nflverse_writer": {"verified": True},
            "upcoming_issues": []}


def fake_predict(qss, calls, content="v1"):
    def predict(args, season, week):
        calls.append((season, week))
        path = qss.ledger_path(args, season, week)
        written = wl.write_once(path, json.dumps({"season": season, "week": week, "created_at": SUN.isoformat(),
                                                  "rows": [], "content": content}))
        return {"ledger": str(path), "written": written, "created_at": SUN.isoformat()}
    return predict


# ----------------------------------------------------------------------------- the frozen spec


def test_spec_is_qb_only_s2_under_p1s_gate(qss):
    spec = qss.spec_now("cfg", "scoring")
    assert spec["per_position"] == {"QB": "S2", "RB": "ifp2", "WR": "ifp2", "TE": "ifp2"}
    assert spec["gated_positions"] == ["QB"] and spec["first_prospective"] == [2026, 7]
    g = spec["gate"]
    assert (g["min_prospective_weeks"], g["min_matched_appearances_per_position"], g["bias_tolerance_points"],
            g["mae_ci95_upper_below"]) == (8, 400, 0.15, 0.0)
    assert g["min_prospective_weeks"] == ch.SHADOW_GATE["min_prospective_weeks"]          # p1's gate, not a copy
    h = hashlib.sha256()
    for name in ("qb_scenarios.py", "qb_scenario_research.py", "qb_scenario_forecast.py"):
        h.update(name.encode() + b"\0" + (SCRIPTS / name).read_bytes())
    assert spec["forecast_code_hash"] == h.hexdigest()           # the model, the study's build and this forecast
    assert qss.spec_now("cfg", "scoring")["config_hash"] == spec["config_hash"]           # deterministic


def test_spec_freezes_once_and_a_changed_binding_is_refused(qss, args, monkeypatch):
    first = qss.load_spec(args, "cfg-a", "scoring-a", now=SUN)
    path = qss.spec_path(args)
    sha = _sha(path)
    again = qss.load_spec(args, "cfg-a", "scoring-a", now=SUN + timedelta(days=7))
    assert again["frozen_at"] == first["frozen_at"] == SUN.isoformat() and _sha(path) == sha
    for kw, key in (({"cfg_hash": "cfg-b", "scoring_fp": "scoring-a"}, "ifp2_config_hash"),
                    ({"cfg_hash": "cfg-a", "scoring_fp": "scoring-b"}, "scoring_fingerprint")):
        with pytest.raises(RuntimeError, match=key):
            qss.load_spec(args, kw["cfg_hash"], kw["scoring_fp"])
    monkeypatch.setattr(qss, "forecast_code_hash", lambda: "edited-forecast")      # the forecast code changed
    with pytest.raises(RuntimeError, match="forecast_code_hash"):
        qss.load_spec(args, "cfg-a", "scoring-a")
    monkeypatch.undo()
    monkeypatch.setitem(qss.RULES, "gate", "a looser gate")                           # a rule edited after the freeze
    with pytest.raises(RuntimeError, match="rules"):
        qss.load_spec(args, "cfg-a", "scoring-a")
    assert _sha(path) == sha                                                         # never rewritten


def test_spec_records_the_p2_study_selection_without_binding_to_it(qss, args):
    sel = Path(args.ifp_dir) / "research" / "qb_scenarios" / "frozen_selection_ifp-ctx-p2.json"
    sel.parent.mkdir(parents=True)
    sel.write_text(json.dumps({"config_hash": "abc", "per_position": {"QB": "ifp2"}, "frozen_at": "x"}))
    spec = qss.load_spec(args, "cfg", "scoring", now=SUN)
    assert spec["provenance"]["p2_frozen_selection"]["config_hash"] == "abc"
    sel.unlink()                                                     # the study's directory may go away later
    assert qss.load_spec(args, "cfg", "scoring")["config_hash"] == spec["config_hash"]


# ----------------------------------------------------------------------------- write-once pregame ledger


def test_freezes_once_and_reruns_are_idempotent(qss, args):
    calls = []
    rec = qss.run_pregame(args, 2026, 7, now=SUN, snapshot=ready_snapshot, predict=fake_predict(qss, calls))
    assert rec["status"] == "frozen" and calls == [(2026, 7)]
    lpath = qss.ledger_path(args, 2026, 7)
    sha = _sha(lpath)
    assert rec["ledger_sha256"] == sha
    assert (qss.shadow_dir(args) / "freeze" / "s2026w07.json").exists()
    rec2 = qss.run_pregame(args, 2026, 7, now=SUN + timedelta(hours=1), snapshot=ready_snapshot,
                           predict=fake_predict(qss, calls, content="v2"))
    assert rec2["status"] == "already_frozen" and calls == [(2026, 7)] and _sha(lpath) == sha
    assert len(list((qss.shadow_dir(args) / "runs").glob("*.json"))) == 1     # the idempotent re-run records nothing
    # the ledger lives in the shadow's own directory, never p1's
    assert not (Path(args.ifp_dir) / "research" / "context").exists()


def test_a_frozen_ledger_is_never_overwritten_even_by_a_racing_writer(qss, args):
    calls = []
    lpath = qss.ledger_path(args, 2026, 7)
    wl.write_once(lpath, json.dumps({"season": 2026, "week": 7, "created_at": SUN.isoformat(), "rows": []}))
    sha = _sha(lpath)

    def racer(a, s, w):           # another process froze it between the check and the write
        return fake_predict(qss, calls, content="late")(a, s, w)

    rec = qss.run_pregame(args, 2026, 7, now=SUN, snapshot=ready_snapshot, predict=racer)
    assert rec["status"] == "already_frozen" and calls == [] and _sha(lpath) == sha


def test_weeks_before_the_preregistered_start_are_never_frozen(qss, args):
    rec = qss.run_pregame(args, 2026, 6, now=SUN, snapshot=lambda *a: pytest.fail("no snapshot"),
                          predict=lambda *a: pytest.fail("no forecast"))
    assert rec["status"] == "before_prospective_start"
    assert not qss.ledger_path(args, 2026, 6).exists()


def test_a_failed_or_refused_forecast_is_recorded_not_raised(qss, args):
    def refuse(a, s, w):
        raise SystemExit("every week-7 game has kicked off")

    rec = qss.run_pregame(args, 2026, 7, now=SUN, snapshot=ready_snapshot, predict=refuse)
    assert rec["status"] == "failed" and "kicked off" in rec["error"] and rec["warnings"]
    assert Path(rec["record"]).exists() and not qss.ledger_path(args, 2026, 7).exists()


def test_dry_run_decides_without_forecasting_or_recording(qss, args):
    args.write = False
    rec = qss.run_pregame(args, 2026, 7, now=SUN, snapshot=ready_snapshot, predict=lambda *a: pytest.fail("dry"))
    assert rec["status"] == "would_freeze" and not (qss.shadow_dir(args) / "runs").exists()


def test_a_started_week_without_a_ledger_is_recorded_missed_once(qss, args):
    kick = {"AAA": SUN - timedelta(days=3), "BBB": SUN - timedelta(hours=5)}
    rec = qss.check_missed(args, 2026, 7, now=SUN, kickoffs=kick)
    assert rec and rec["candidate"] == "ifp-ctx-p2q"
    path = qss.shadow_dir(args) / "missed" / "s2026w07.json"
    sha = _sha(path)
    qss.check_missed(args, 2026, 7, now=SUN + timedelta(days=1), kickoffs=kick)
    assert _sha(path) == sha
    assert qss.check_missed(args, 2026, 6, now=SUN, kickoffs=kick) is None        # before the prospective start


def test_predict_refuses_weeks_before_the_start_without_building_anything(qss, args):
    args.week = 6
    with pytest.raises(RuntimeError, match="prospective evidence starts"):
        qss.cmd_predict(args)
    assert not qss.shadow_dir(args).exists()


# ----------------------------------------------------------------------------- evidence and gate


def test_only_prospective_ledgers_under_the_frozen_spec_count(qss):
    spec = {"first_prospective": [2026, 7], "config_hash": "h", "frozen_at": SUN.isoformat(), "forecast_code_hash": "c"}
    ok = {"season": 2026, "week": 7, "config_hash": "h", "forecast_code_hash": "c",
          "created_at": (SUN + timedelta(hours=1)).isoformat()}
    assert qss.counted(ok, spec)
    assert not qss.counted({**ok, "forecast_code_hash": "edited"}, spec)              # made by other forecast code
    assert not qss.counted({k: v for k, v in ok.items() if k != "forecast_code_hash"}, spec)
    assert not qss.counted({**ok, "week": 6}, spec)                                   # before the start
    assert not qss.counted({**ok, "config_hash": "other"}, spec)                      # another spec
    assert not qss.counted({**ok, "created_at": (SUN - timedelta(hours=1)).isoformat()}, spec)   # before the freeze


def _report(n, weeks_ci, bias_ctx, bias_ifp2, sp_ctx, sp_ifp2):
    return {"QB": {"n": n, "minus_ifp2": {"ctx": {"diff": -0.3, "ci95": weeks_ci}},
                   "metrics": {"ctx": {"bias": bias_ctx}, "ifp2": {"bias": bias_ifp2}},
                   "cohorts": {"starters_pool": {"n": 100, "ctx": {"mae": sp_ctx}, "ifp2": {"mae": sp_ifp2}}}}}


def test_gate_is_p1s(qss):
    spec = qss.spec_now("cfg", "scoring")
    good = _report(450, [-0.4, -0.1], -0.2, -0.1, 6.0, 6.1)
    assert qss.gate(good, {"QB": 8}, spec)["QB"]["status"] == "meets_gate"
    assert qss.gate(good, {"QB": 7}, spec)["QB"]["status"] == "insufficient_data"
    assert qss.gate(_report(399, [-0.4, -0.1], 0, 0, 6, 6.1), {"QB": 9}, spec)["QB"]["status"] == "insufficient_data"
    worse_bias = _report(450, [-0.4, -0.1], -0.26, -0.1, 6.0, 6.1)                  # |bias| worse by > 0.15
    assert qss.gate(worse_bias, {"QB": 8}, spec)["QB"]["checks"]["bias"] is False
    assert qss.gate(worse_bias, {"QB": 8}, spec)["QB"]["status"] == "fails_gate"
    assert qss.gate(_report(450, [-0.4, 0.01], 0, 0, 6, 6.1), {"QB": 8}, spec)["QB"]["status"] == "fails_gate"
    assert qss.gate(_report(450, [-0.4, -0.1], 0, 0, 6.2, 6.1), {"QB": 8}, spec)["QB"]["checks"]["starters_pool"] is False
    assert qss.gate({}, {}, spec)["QB"]["status"] == "insufficient_data"


def test_only_weeks_with_matched_qb_appearances_count_toward_the_week_requirement(qss):
    """Seven weeks of matched QB rows plus one fully final ledger whose rows match nothing (no label, or a missing
    forecast) is seven weeks, not eight."""
    rows = [{"key": 202607 + w, "position": "QB", "label": 10.0, "pred_ctx": 9.0, "pred_ifp2": 9.5} for w in range(7)]
    rows += [{"key": 202614, "position": "QB", "label": None, "pred_ctx": 9.0, "pred_ifp2": 9.5},
             {"key": 202614, "position": "QB", "label": 10.0, "pred_ctx": None, "pred_ifp2": 9.5},
             {"key": 202615, "position": "RB", "label": 10.0, "pred_ctx": 9.0, "pred_ifp2": 9.5}]
    mw = qss.matched_weeks(pl.DataFrame(rows), ["QB"])
    assert mw == {"QB": 7}
    spec = qss.spec_now("cfg", "scoring")
    assert qss.gate(_report(450, [-0.4, -0.1], 0, 0, 6.0, 6.1), mw, spec)["QB"]["status"] == "insufficient_data"


def test_start_calibration_against_the_realized_starter(qss):
    led = {"season": 2026, "week": 7, "candidates": [
        {"team": "CHI", "gsis_id": "C1", "p_start": 0.9, "pi_start": 0.97, "p1_expected": 1.0, "first_game": False},
        {"team": "CHI", "gsis_id": "C2", "p_start": 0.1, "pi_start": 0.3, "p1_expected": 0.0, "first_game": False},
        {"team": "GB", "gsis_id": "G1", "p_start": 0.6, "pi_start": 0.8, "p1_expected": 1.0, "first_game": False},
        {"team": "GB", "gsis_id": "G2", "p_start": 0.4, "pi_start": 0.7, "p1_expected": 0.0, "first_game": False},
        {"team": "DET", "gsis_id": "D1", "p_start": 1.0, "pi_start": 1.0, "p1_expected": 1.0, "first_game": False}]}
    starters = pl.DataFrame({"team": ["CHI", "GB"], "qb_id": ["C1", "G2"]})          # DET's starter unknown
    panel = pl.DataFrame({"gsis_id": ["C1", "G1", "G2"], "team": ["CHI", "GB", "GB"]})
    rows = qss.realized(led, starters, panel)
    by = {r["gsis_id"]: r for r in rows}
    assert by["C1"]["started"] == 1.0 and by["G2"]["started"] == 1.0 and by["G1"]["started"] == 0.0
    assert by["D1"]["started"] is None and by["C2"]["appeared"] == 0.0 and by["G1"]["appeared"] == 1.0
    cal = qss.start_calibration(pl.DataFrame(rows))
    assert cal["p2"]["team_weeks"] == 2 and cal["p2"]["top1_accuracy"] == 0.5
    # team-week Brier: CHI (0.1^2 + 0.1^2) = 0.02, GB (0.6^2 + 0.6^2) = 0.72 -> 0.37
    assert cal["p2"]["brier"] == pytest.approx(0.37)
    assert cal["p1_hard"]["brier"] == pytest.approx(1.0)                              # (0 + 0) and (1 + 1)
    assert cal["pi_given_appearance"]["n"] == 3                                       # QBs who appeared, starter known


def test_score_needs_a_ledger_and_a_dry_run_never_scores(qss, args):
    assert qss.run_score(args, now=SUN, prospective=lambda a: pytest.fail("nothing to score"))["status"] == "no_ledgers"
    wl.write_once(qss.ledger_path(args, 2026, 7), json.dumps({"season": 2026, "week": 7, "rows": []}))
    args.write = False
    assert qss.run_score(args, now=SUN, prospective=lambda a: pytest.fail("dry"))["status"] == "would_score"
    args.write = True
    rec = qss.run_score(args, now=SUN, prospective=lambda a: (_ for _ in ()).throw(ValueError("store")))
    assert rec["status"] == "failed" and Path(rec["record"]).exists()


def test_cycle_combines_score_missed_and_pregame(qss, args, monkeypatch):
    monkeypatch.setattr(qss, "run_score", lambda a, now=None: {"kind": "score", "status": "no_ledgers",
                                                               "warnings": [], "notes": []})
    monkeypatch.setattr(qss, "check_missed", lambda a, s, w, now=None: {"week": w})
    monkeypatch.setattr(qss, "run_pregame", lambda a, s, w, now=None: {"kind": "pregame", "status": "deferred",
                                                                      "warnings": ["stale"], "notes": []})
    args.week = 8
    out = qss.cmd_cycle(args, SUN)
    assert out["status"] == "ok" and out["missed"] == {"week": 7}
    assert out["warnings"] == ["stale", "week 7 started without a frozen ifp-ctx-p2q ledger (recorded as missed)"]


def test_production_never_imports_the_shadows(qss):
    """Production modules never import either shadow."""
    app = Path(__file__).resolve().parents[1] / "app"
    for f in app.rglob("*.py"):
        src = f.read_text()
        assert "qb_scenario_shadow" not in src, f
        if f.name == "context_model.py":     # ifp-2.2.0 reads the ipm-cal shadow's frozen map FILE, never imports it
            assert "import participation_cal_shadow" not in src, f
            continue
        assert "participation_cal_shadow" not in src, f

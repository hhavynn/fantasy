"""scripts/participation_cal_shadow.py: the prospective re-test of the QB isotonic participation map ipm-cal-0.1.0.
The write-once frozen copy and its binding (sha256, base version, QB knots), which production pregame rows count,
the calibrated probability as a fixed map of the frozen p_play, the pre-registered gate, and write-once week records.
Hand-built artifacts and ledgers; nothing reads or writes a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import polars as pl
import pytest

from app.analytics import participation_calibration as pc
from app.analytics import weekly_learning as wl

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
UTC = timezone.utc
FROZE = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)               # before week 7's Thursday game


@pytest.fixture(scope="module")
def pcs():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("participation_cal_shadow", SCRIPTS / "participation_cal_shadow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def args(pcs, tmp_path):
    return pcs.parse(["cycle", "--season", "2026", "--models", str(tmp_path / "models"),
                      "--live-db", str(tmp_path / "live.duckdb"), "--history-db", str(tmp_path / "hist.duckdb"),
                      "--weekly-dir", str(tmp_path / "weekly"), "--exits-dir", str(tmp_path / "weekly")])


def artifact(qb="p_isotonic_expanding", rate=0.2, cutoff_key=202605) -> dict:
    """A shadow artifact whose QB isotonic map sends the base 0.5 to `rate` (one block of 400 rows)."""
    n = 400
    oos = pl.DataFrame({"key": [cutoff_key - (i % 20) for i in range(n)], "position": ["QB"] * n,
                        "p_model": [0.5] * n, "played": [1 if i < rate * n else 0 for i in range(n)]})
    art = pc.fit_artifact(oos, {"QB": {"selected": qb}}, created_at="2026-10-14T00:00:00+00:00", base_model={},
                          fingerprints={"scoring": "fp", "data_hash": "dh", "participation_selection_sha": "ps"},
                          validation={})
    return art.to_dict()


def write_source(pcs, args, art) -> Path:
    p = pcs.source_path(args)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art))
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- freeze and binding


def test_freezes_a_write_once_copy_bound_to_the_source_and_the_qb_knots(pcs, args):
    src = write_source(pcs, args, artifact())
    out = pcs.run_freeze(args, now=FROZE, build=lambda a: pytest.fail("the artifact exists"))
    assert out["status"] == "frozen"
    b = out["binding"]
    assert (b["participation_version"], b["position"], b["method"], b["window"], b["training_cutoff_key"]) == (
        "ipm-1.0.0", "QB", "isotonic", "expanding", 202605)
    assert b["source_sha256"] == _sha(src)
    fz = pcs.frozen_path(args)
    sha = _sha(fz)
    # the study is re-run later (a new fit overwrites the source): the frozen copy is kept and the change noted
    src.write_text(json.dumps(artifact(rate=0.3)))
    again = pcs.run_freeze(args, now=FROZE + timedelta(days=1))
    assert again["status"] == "already_frozen" and _sha(fz) == sha and "never the new one" in again["notes"][0]
    assert pcs.load_frozen(fz)["artifact"]["calibrators"]["QB"]["params"]["y"] == [pytest.approx(0.2)]


def test_a_changed_frozen_copy_is_refused(pcs, args):
    write_source(pcs, args, artifact())
    pcs.run_freeze(args, now=FROZE)
    fz = pcs.frozen_path(args)
    rec = json.loads(fz.read_text())
    rec["artifact"]["calibrators"]["QB"]["params"]["y"] = [0.9]               # someone edits the map
    fz.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="config_hash"):
        pcs.load_frozen(fz)
    # a recomputed hash does not help: the knots are bound separately
    body = {k: v for k, v in rec.items() if k not in ("config_hash", "frozen_at")}
    rec["config_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    fz.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="binding"):
        pcs.load_frozen(fz)
    assert pcs.run_score(args, now=FROZE)["status"] == "failed"               # never scored, recorded instead


def test_only_the_preregistered_fitted_qb_isotonic_map_can_be_frozen(pcs, args):
    write_source(pcs, args, artifact(qb="p_none"))
    with pytest.raises(ValueError, match="not the pre-registered fitted isotonic map"):
        pcs.run_freeze(args, now=FROZE)
    write_source(pcs, args, {**artifact(), "status": "production"})
    with pytest.raises(ValueError, match="research/shadow only"):
        pcs.run_freeze(args, now=FROZE)
    assert not pcs.frozen_path(args).exists()


def test_a_missing_artifact_is_built_by_the_study_cli_and_a_dry_run_builds_nothing(pcs, args):
    built = []
    args.write = False
    assert pcs.run_freeze(args, now=FROZE, build=lambda a: pytest.fail("dry"))["status"] == "would_build"
    args.write = True
    out = pcs.run_freeze(args, now=FROZE, build=lambda a: built.append(1) or write_source(pcs, a, artifact()))
    assert built == [1] and out["status"] == "frozen" and "participation_calibration.py run" in out["notes"][0]


def test_the_build_runs_participation_calibration_run_on_the_same_inputs(pcs, args, monkeypatch):
    seen = []

    class P:
        returncode, stdout, stderr = 0, "{}", ""

    monkeypatch.setattr(pcs.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or P())
    pcs.build_artifact(args)
    cmd, kw = seen[0]
    assert Path(cmd[1]).name == "participation_calibration.py" and cmd[2] == "run"
    assert cmd[cmd.index("--models") + 1] == str(args.models) and kw["timeout"] == pcs.STEP_TIMEOUT_S


# ----------------------------------------------------------------------------- evidence


def run(season, week, created, rows, participation="ipm-1.0.0"):
    return {"kind": "pregame", "season": season, "week": week, "run_id": f"r{season}{week}{created:%H}",
            "created_at": created.isoformat(), "participation": participation, "rows": rows}


def row(gid, kickoff, p, pos="QB", pts=20.0):
    blk = {"points": pts, "low": None, "high": None, "expected_points": None}
    return {"gsis_id": gid, "position": pos, "team": "CHI", "kickoff": kickoff.isoformat(), "pregame": True,
            "champion": blk, "challenger": {**blk, "points": pts + 1}, "pred_A1": 1.0, "pred_A2": 1.0, "p_play": p,
            "workload": {}}


def frozen_rec(pcs, args):
    write_source(pcs, args, artifact())
    pcs.run_freeze(args, now=FROZE)
    return pcs.load_frozen(pcs.frozen_path(args))


def test_only_prospective_rows_of_the_bound_base_version_count(pcs, args):
    fr = frozen_rec(pcs, args)
    thu = datetime(2026, 10, 22, 20, 15, tzinfo=UTC)
    runs = [run(2026, 6, datetime(2026, 10, 15, 10, tzinfo=UTC), [row("A", datetime(2026, 10, 18, 17, tzinfo=UTC), 0.5)]),
            run(2026, 7, datetime(2026, 10, 20, 10, tzinfo=UTC), [row("A", thu + timedelta(days=3), 0.5),
                                                                   row("B", thu, 0.5), row("R", thu, 0.5, pos="RB")]),
            run(2026, 7, datetime(2026, 10, 18, 10, tzinfo=UTC), [row("C", FROZE - timedelta(hours=1), 0.5)]),
            run(2026, 8, datetime(2026, 10, 27, 10, tzinfo=UTC), [row("A", thu + timedelta(days=10), 0.5)],
                participation="ipm-2.0.0")]
    rows, excluded = pcs.evidence_rows(runs, fr)
    assert sorted(rows["gsis_id"].to_list()) == ["A", "B"]                     # QB only; C kicked off before the freeze
    assert excluded == {"before_prospective_start": 1, "participation_ipm-2.0.0": 1,
                        "rows_not_kicking_off_after_the_freeze": 1}
    assert np.allclose(rows["p_cal"].to_numpy(), 0.2)                          # the frozen map of the frozen p_play
    assert rows["p_play"].to_list() == [0.5, 0.5]


def test_weeks_inside_the_maps_training_window_never_count(pcs, args):
    write_source(pcs, args, artifact(cutoff_key=202607))
    pcs.run_freeze(args, now=FROZE)
    fr = pcs.load_frozen(pcs.frozen_path(args))
    r7 = run(2026, 7, FROZE + timedelta(hours=1), [row("A", FROZE + timedelta(days=3), 0.5)])
    rows, excluded = pcs.evidence_rows([r7], fr)
    assert rows.height == 0 and excluded == {"before_prospective_start": 1}


def labelled(n_weeks, per_week, played_rate=0.2, cal=0.2, base=0.5):
    recs = []
    for w in range(n_weeks):
        for i in range(per_week):
            played = i < played_rate * per_week
            recs.append({"season": 2026, "week": 7 + w, "key": 202607 + w, "gsis_id": f"Q{i}", "p_play": base,
                         "p_cal": cal, "pred_champion": 20.0, "pred_challenger": 21.0,
                         "outcome_status": wl.APPEARED if played else wl.CONFIRMED_ABSENT,
                         "label": 20.0 if played else 0.0})
    return pl.DataFrame(recs)


def test_retest_gate(pcs):
    rep = pcs.retest(labelled(8, 50))
    assert rep["weeks"] == 8 and rep["n"] == 400
    assert rep["calibrated"]["probability"]["log_loss"] < rep["base"]["probability"]["log_loss"]
    assert rep["gate"]["status"] == "meets_gate", rep["gate"]
    assert pcs.retest(labelled(7, 60))["gate"]["status"] == "insufficient_data"
    assert pcs.retest(labelled(8, 40))["gate"]["status"] == "insufficient_data"            # 320 rows
    assert pcs.retest(labelled(8, 50, cal=0.8))["gate"]["status"] == "fails_gate"          # a worse map
    lab = labelled(8, 50).with_columns(pl.when(pl.col("gsis_id") == "Q0").then(pl.lit(wl.PENDING_GAME))
                                       .otherwise(pl.col("outcome_status")).alias("outcome_status"))
    assert pcs.retest(lab)["n"] == 392                                       # unresolved rows never become absences


def test_score_records_weeks_write_once_and_only_fully_final_weeks(pcs, args, monkeypatch):
    frozen_rec(pcs, args)
    ko = datetime(2026, 10, 25, 17, tzinfo=UTC)
    runs = [run(2026, 7, datetime(2026, 10, 24, 10, tzinfo=UTC), [row(f"Q{i}", ko, 0.5) for i in range(5)]),
            run(2026, 8, datetime(2026, 10, 31, 10, tzinfo=UTC), [row("Q9", ko + timedelta(days=7), 0.5)])]
    monkeypatch.setattr(wl, "week_finality", lambda src, s, w, f=None: {"fully_final": w == 7})

    def label(rows, src, fetched=None):
        return rows.with_columns(pl.lit(wl.APPEARED).alias("outcome_status"), pl.lit(10.0).alias("label"))

    monkeypatch.setattr(wl, "label_outcomes", label)
    rec = pcs.run_score(args, now=ko + timedelta(days=2), runs=runs, context=lambda a: (None, {}))
    assert rec["status"] == "scored" and rec["pending_weeks"] == [(2026, 8)] and rec["retest"]["n"] == 5
    week = pcs.shadow_dir(args) / "weeks" / "s2026w07.json"
    sha = _sha(week)
    assert json.loads(week.read_text())["rows"][0]["p_cal"] == pytest.approx(0.2)
    assert not (pcs.shadow_dir(args) / "weeks" / "s2026w08.json").exists()
    assert json.loads((pcs.shadow_dir(args) / "scores.json").read_text())["n"] == 5
    pcs.run_score(args, now=ko + timedelta(days=9), runs=runs, context=lambda a: (None, {}))
    assert _sha(week) == sha                                                  # first scored copy is kept
    assert len(list((pcs.shadow_dir(args) / "runs").glob("*.json"))) == 2


def test_cycle_never_raises_and_reports_failures(pcs, args, monkeypatch):
    monkeypatch.setattr(pcs, "build_artifact", lambda a: (_ for _ in ()).throw(RuntimeError("no selection.json")))
    out = pcs.cmd_cycle(args, FROZE)
    assert out["status"] == "failed" and "no selection.json" in out["warnings"][0]
    assert out["score"]["status"] == "not_frozen"

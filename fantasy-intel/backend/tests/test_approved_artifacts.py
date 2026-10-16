"""Approved artifacts (approved_artifacts, production_pin, production_forecasts.artifact_gate): ifp-2.0.0 is
retrained weekly, so each of its artifacts is approved by a write-once row in the database; ifp-1.0.0 keeps one
sha pin. Temp DuckDB files and synthetic artifacts only; no real model files."""

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analytics import production_pin
from app.analytics import projection_source as ps
from app.analytics import weekly_learning as wl
from app.analytics.independent_projection_store import APPROVED_SOURCES, register_approved_artifact
from app.analytics.production_forecasts import (NOT_REGISTERED, artifact_gate, eligible_run_ids, gate_active,
                                                latest_run, pregame_forecasts)
from app.db.connection import SCHEMA_VERSION, _REBUILD, schema_version, table_exists

from test_independent_projections import SCORING, _artifact, live  # noqa: F401  (live is a fixture)
from test_production_forecasts import BEFORE_ALL, FP, store

PROD = ps.MODEL_VERSION
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def weekly_artifact(src, week=6):
    """A synthetic ifp-2.0.0 artifact as weekly_learning would save it (target week and data cutoff recorded)."""
    art = _artifact(src, created_at="2026-09-01T00:00:00+00:00")
    return replace(art, version=PROD, config={**art.config, "provenance": {
        "target": [2026, week], "data_cutoff_key": 2026 * 100 + week - 1, "retrospective": False}})


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows_in(con):
    return con.execute("SELECT model_version, artifact_sha256, target_season, target_week, source, registered_at "
                       "FROM approved_artifacts ORDER BY artifact_sha256").fetchall()


# ----------------------------------------------------------------------------- registration (the learning cycle)


def test_a_learning_cycle_style_save_registers_the_file_sha(live, tmp_path):  # noqa: F811
    con, src, _ = live
    art = weekly_artifact(src)
    aid, path, created = wl.save_artifact(art, tmp_path)               # the write-once, content-addressed store
    assert created and path.name == f"s2026w06-{aid}.json"
    registered, new = wl.register_saved_artifact(con, art, path, tmp_path)
    assert new and registered == sha(path)                              # the run's artifact_id: sha256 of the file bytes
    assert registered != aid                                           # (the file name carries a different digest)
    [row] = rows_in(con)
    assert row[:5] == (PROD, registered, 2026, 6, "learning_cycle")
    assert wl.register_saved_artifact(con, art, path, tmp_path) == (registered, False)       # idempotent
    assert len(rows_in(con)) == 1


def test_only_an_artifact_saved_through_the_store_is_registered(live, tmp_path):  # noqa: F811
    con, src, _ = live
    art = weekly_artifact(src)
    _, path, _ = wl.save_artifact(art, tmp_path)
    copy = tmp_path / "hand-written.json"
    copy.write_bytes(path.read_bytes())
    with pytest.raises(wl.NotReady, match="write-once store"):
        wl.register_saved_artifact(con, art, copy, tmp_path)
    other_root = tmp_path / "elsewhere"
    with pytest.raises(wl.NotReady):
        wl.register_saved_artifact(con, art, path, other_root)
    assert rows_in(con) == []


def test_project_records_the_registered_sha_as_the_runs_artifact_id(live, tmp_path):  # noqa: F811
    """`project` loads the newest store artifact for the week and stamps sha256(file bytes) on the run: the
    value register_saved_artifact registers."""
    con, src, _ = live
    art = weekly_artifact(src)
    _, path, _ = wl.save_artifact(art, tmp_path)
    spec = importlib.util.spec_from_file_location("backtest_independent_projections",
                                                  SCRIPTS / "backtest_independent_projections.py")
    sys.path.insert(0, str(SCRIPTS))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPTS))
    args = SimpleNamespace(model=PROD, weekly_dir=tmp_path, season=2026, week=6, out=tmp_path)
    got, data, _, got_path = mod._project_artifact(args, SCORING)
    assert got_path == path and hashlib.sha256(data).hexdigest() == wl.register_saved_artifact(con, got, got_path,
                                                                                               tmp_path)[0]


# ----------------------------------------------------------------------------- the table is write-once


def test_registration_never_updates_and_rejects_malformed_input_loudly(live):  # noqa: F811
    con, _, _ = live
    kw = dict(model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=6)
    assert register_approved_artifact(con, source="learning_cycle", **kw) is True
    first = rows_in(con)
    assert register_approved_artifact(con, source="manual_pin", **{**kw, "target_week": 9}) is False   # kept as it was
    assert rows_in(con) == first
    assert register_approved_artifact(con, source="manual_pin", **{**kw, "artifact_sha256": "b" * 64}) is True
    assert len(rows_in(con)) == 2
    for bad in ({"artifact_sha256": "dict:" + "a" * 64}, {"artifact_sha256": "A" * 64}, {"artifact_sha256": "abc"},
                {"model_version": ""}, {"source": "somebody"}):
        with pytest.raises(ValueError):
            register_approved_artifact(con, **{**kw, "source": "manual_pin", **bad})
    assert len(rows_in(con)) == 2 and set(APPROVED_SOURCES) == {"learning_cycle", "manual_pin"}
    src = inspect.getsource(register_approved_artifact).upper()
    assert "ON CONFLICT DO NOTHING" in src and "UPDATE " not in src and "DELETE " not in src


def test_the_table_is_create_only_and_never_rebuilt(live):  # noqa: F811
    con, _, _ = live
    assert table_exists(con, "approved_artifacts") and schema_version(con) == SCHEMA_VERSION == 8
    assert "approved_artifacts" not in {t for tables in _REBUILD.values() for t in tables}
    assert not any(w in (Path(inspect.getsourcefile(register_approved_artifact)).read_text().split("def write_live")[0]
                         .split("def register_approved_artifact")[1].upper()) for w in ("DROP ", "DELETE FROM"))


# ----------------------------------------------------------------------------- the evidence readers


def test_evidence_counts_only_registered_production_artifacts(live):  # noqa: F811
    con, src, _ = live
    good, _, _ = store(con, src, BEFORE_ALL, sha="a" * 64, meta_over={"model_version": PROD})
    hand, _, _ = store(con, src, BEFORE_ALL, sha="b" * 64, meta_over={"model_version": PROD})
    register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=6,
                               source="learning_cycle")
    rows, excluded = pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)
    assert {r["run_id"] for r in rows} == {good["run_id"]}
    assert [(e["run_id"], e["reason"]) for e in excluded] == [(hand["run_id"], NOT_REGISTERED)]
    assert NOT_REGISTERED == "artifact not registered as approved"
    ids, excluded = eligible_run_ids(con, 2026, PROD, FP)
    assert ids == [good["run_id"]] and excluded[0]["reason"] == NOT_REGISTERED
    assert latest_run(con, 2026, 6, model_version=PROD)["run_id"] in {good["run_id"]}
    assert latest_run(con, 2026, 6, model_version=PROD)["artifact_check"] == "verified"
    # another week's registration does not approve this week; other model versions are not constrained at all
    con.execute("DELETE FROM approved_artifacts")
    register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=7,
                               source="learning_cycle")
    assert pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)[0] == []
    assert artifact_gate(con, "ifp-0.0.5")({"artifact_id": "zzz"}) == ("unchecked", None)


def test_without_the_table_the_evidence_readers_fail_open(live):  # noqa: F811
    con, src, _ = live
    run, _, _ = store(con, src, BEFORE_ALL, sha="b" * 64, meta_over={"model_version": PROD})
    assert pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)[0] == []   # empty table
    con.execute("DROP TABLE approved_artifacts")
    rows, excluded = pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)
    assert {r["run_id"] for r in rows} == {run["run_id"]} and not excluded                # today's behaviour
    assert latest_run(con, 2026, 6, model_version=PROD)["artifact_check"] == "unverified"
    assert artifact_gate(con, PROD)({"artifact_id": "x"})[0] == "unverified" and not gate_active(con, PROD)


def test_the_single_sha_pin_leaves_ifp1_unchanged_when_unset_and_filters_when_set(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL, sha="a" * 64)
    b, _, _ = store(con, src, BEFORE_ALL, sha="b" * 64)
    assert production_pin.APPROVED_ARTIFACT_SHA256 is None and not gate_active(con, "ifp-1.0.0")
    assert len(eligible_run_ids(con, 2026, "ifp-1.0.0", FP)[0]) == 2
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", "a" * 64)
    assert gate_active(con, "ifp-1.0.0")
    assert eligible_run_ids(con, 2026, "ifp-1.0.0", FP)[0] == [a["run_id"]]
    assert latest_run(con, 2026, 6, model_version="ifp-1.0.0")["artifact_check"] == "verified"
    assert b["run_id"] not in eligible_run_ids(con, 2026, "ifp-1.0.0", FP)[0]

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
    assert len(rows_in(con)) == 2 and set(APPROVED_SOURCES) == {"learning_cycle", "manual_pin", "learning_cycle_backfill"}
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
    rows, excluded = pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)
    assert {r["run_id"] for r in rows} == {run["run_id"]} and not excluded           # empty table: also fail open
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


# ----------------------------------------------------------------------------- upgrade transition and backfill


def test_zero_registered_rows_is_unverified_and_the_first_registration_starts_enforcement(live):  # noqa: F811
    con, src, _ = live
    old, _, _ = store(con, src, BEFORE_ALL, sha="b" * 64, meta_over={"model_version": PROD})   # stored before the upgrade
    assert rows_in(con) == []
    assert artifact_gate(con, PROD)({"artifact_id": "b" * 64})[0] == "unverified"
    w = ps.weekly(con, 2026, 6, "independent")                 # the owner's view is not blanked after the upgrade
    assert w["available"] and w["run"]["run_id"] == old["run_id"] and w["run"]["artifact_verified"] is False
    assert "could not be verified" in w["reason"]
    assert latest_run(con, 2026, 6, model_version=PROD)["artifact_check"] == "unverified"
    # one registration (of ANY artifact) switches this model to enforcement: the old unregistered run is out
    register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=6,
                               source="learning_cycle")
    assert artifact_gate(con, PROD)({"artifact_id": "b" * 64}) == ("rejected", NOT_REGISTERED)
    assert pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)[0] == []
    assert ps.weekly(con, 2026, 6, "independent")["available"] is False
    # enforcement is per model: the ifp-1.0.0 rule is unaffected
    assert artifact_gate(con, "ifp-1.0.0")({"artifact_id": "x"}) == ("unchecked", None)


def test_backfill_registers_the_stores_own_files_and_nothing_else(live, tmp_path):  # noqa: F811
    con, src, _ = live
    arts = [weekly_artifact(src, w) for w in (4, 5, 6)]
    saved = [wl.save_artifact(a, tmp_path) for a in arts]
    store_dir = tmp_path / "artifacts" / PROD
    stray = store_dir / "s2026w06-stray.json"                                 # a hand copy under a store-like name
    stray.write_bytes(saved[2][1].read_bytes())
    (tmp_path / "copy.json").write_bytes(saved[0][1].read_bytes())             # outside the store: never even looked at
    (store_dir / "s2026w09-broken.json").write_text("{not json")               # unreadable
    retro = weekly_artifact(src, 7)
    retro.config["provenance"]["retrospective"] = True
    wl.save_artifact(retro, tmp_path)                                          # a retrospective replay is never production
    res = wl.backfill_store_artifacts(con, tmp_path, PROD)
    assert res["registered"] == 3 and res["existing"] == 0
    skipped = {name for name, _ in res["skipped"]}
    assert skipped == {"s2026w06-stray.json", "s2026w09-broken.json", saved_name(tmp_path, retro)}
    got = rows_in(con)
    assert {r[1] for r in got} == {sha(p) for _, p, _ in saved} and {r[4] for r in got} == {"learning_cycle_backfill"}
    assert {(r[2], r[3]) for r in got} == {(2026, 4), (2026, 5), (2026, 6)}
    # idempotent
    again = wl.backfill_store_artifacts(con, tmp_path, PROD)
    assert again["registered"] == 0 and again["existing"] == 3 and rows_in(con) == got


def saved_name(root, art):
    s, w = art.config["provenance"]["target"]
    return f"s{s}w{w:02d}-{wl.artifact_id(art)}.json"


def test_backfill_covers_existing_runs_so_the_first_cycle_after_the_upgrade_keeps_them(live, tmp_path):  # noqa: F811
    """The run was made from a store artifact before approved_artifacts existed: backfill registers that artifact,
    so enforcement (started by the registration) still serves the run."""
    con, src, _ = live
    art = weekly_artifact(src)
    _, path, _ = wl.save_artifact(art, tmp_path)
    run, _, _ = store(con, src, BEFORE_ALL, sha=sha(path), meta_over={"model_version": PROD})
    hand, _, _ = store(con, src, BEFORE_ALL, sha="e" * 64, meta_over={"model_version": PROD})
    assert ps.weekly(con, 2026, 6, "independent")["run"]["artifact_verified"] is False          # before the first cycle
    wl.backfill_store_artifacts(con, tmp_path, PROD)
    rows, excluded = pregame_forecasts(con, 2026, None, model_version=PROD, scoring_fingerprint=FP)
    assert {r["run_id"] for r in rows} == {run["run_id"]}
    assert [(e["run_id"], e["reason"]) for e in excluded] == [(hand["run_id"], NOT_REGISTERED)]
    assert ps.weekly(con, 2026, 6, "independent")["run"]["artifact_verified"] is True


# ----------------------------------------------------------------------------- review fixes


def test_a_weekly_artifact_also_covers_the_rest_of_the_season_runs_that_reuse_it(live):  # noqa: F811
    """Week W's artifact (data cutoff W-1) makes the week-W run and, reused, the W+1..18 rest-of-season runs. It
    cannot make a run of a week at or before its cutoff, nor of another season."""
    con, src, _ = live
    register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=6,
                               training_cutoff_key=202605, source="learning_cycle")
    gate = artifact_gate(con, PROD)

    def at(season, week, sha_="a" * 64):
        return gate({"artifact_id": sha_, "season": season, "week": week})

    assert [at(2026, w)[0] for w in (6, 7, 11, 18)] == ["verified"] * 4              # W, W+1, W+5, the end
    assert at(2026, 5)[0] == "rejected" and at(2026, 1)[0] == "rejected"             # key <= cutoff: it saw that week
    status, reason = at(2027, 8)                                                      # a different season
    assert status == "rejected" and reason.startswith(NOT_REGISTERED)
    assert at(2026, 7, "f" * 64) == ("rejected", NOT_REGISTERED)                     # an unregistered artifact
    # through the readers: the week-7 run of the week-6 artifact is served
    w7, _, _ = store(con, src, BEFORE_ALL, sha="a" * 64, week=7, meta_over={"model_version": PROD})
    assert latest_run(con, 2026, 7, model_version=PROD)["run_id"] == w7["run_id"]
    # no recorded cutoff: only the exact target week; no recorded target: any week
    register_approved_artifact(con, model_version=PROD, artifact_sha256="b" * 64, target_season=2026, target_week=6,
                               source="manual_pin")
    register_approved_artifact(con, model_version=PROD, artifact_sha256="c" * 64, target_season=None, target_week=None,
                               source="manual_pin")
    gate = artifact_gate(con, PROD)
    assert at(2026, 6, "b" * 64)[0] == "verified" and at(2026, 7, "b" * 64)[0] == "rejected"
    assert at(2031, 3, "c" * 64)[0] == "verified"
    # write-once: the same artifact cannot be re-registered for another week (it already covers them)
    assert register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026,
                                      target_week=7, training_cutoff_key=202605, source="manual_pin") is False


def test_a_rejected_production_run_is_explained_as_not_approved_not_as_missing(live):  # noqa: F811
    con, src, _ = live
    register_approved_artifact(con, model_version=PROD, artifact_sha256="a" * 64, target_season=2026, target_week=6,
                               training_cutoff_key=202605, source="learning_cycle")
    store(con, src, BEFORE_ALL, sha="b" * 64, meta_over={"model_version": PROD})      # unregistered ifp-2 run
    store(con, src, BEFORE_ALL, sha="c" * 64)                                           # ifp-1.0.0 run
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["model_version"] == "ifp-1.0.0"
    assert "not approved" in w["reason"] and "No ifp-2.0.0 run is stored" not in w["reason"]
    con.execute("DELETE FROM independent_projection_runs WHERE model_version = 'ifp-1.0.0'")
    con.execute("DELETE FROM independent_projection_rows "
                "WHERE run_id NOT IN (SELECT run_id FROM independent_projection_runs)")
    w = ps.weekly(con, 2026, 6, "independent")                       # nothing approved left: ESPN, and it says why
    assert w["available"] is False and "not approved" in w["reason"] and "ESPN projections are shown" in w["reason"]
    assert "is stored for week" not in w["reason"]                    # not the "no run is stored" wording


def test_the_two_unverified_notes_are_distinguished(live):  # noqa: F811
    con, src, _ = live
    store(con, src, BEFORE_ALL, sha="b" * 64, meta_over={"model_version": PROD})
    assert "no approved artifacts registered for this model yet" in ps.weekly(con, 2026, 6, "independent")["reason"]
    con.execute("DROP TABLE approved_artifacts")
    note = ps.weekly(con, 2026, 6, "independent")["reason"]
    assert "no approved_artifacts table yet" in note and "registered for this model" not in note


def test_register_saved_artifact_refuses_a_retrospective_replay(live, tmp_path):  # noqa: F811
    con, src, _ = live
    art = weekly_artifact(src)
    art.config["provenance"]["retrospective"] = True
    _, path, _ = wl.save_artifact(art, tmp_path)
    with pytest.raises(wl.NotReady, match="retrospective"):
        wl.register_saved_artifact(con, art, path, tmp_path)
    assert rows_in(con) == []


def test_backfill_leaves_the_skipped_file_to_the_caller(live, tmp_path):  # noqa: F811
    con, src, _ = live
    saved = [wl.save_artifact(weekly_artifact(src, w), tmp_path) for w in (5, 6)]
    res = wl.backfill_store_artifacts(con, tmp_path, PROD, skip=(saved[1][1],))
    assert res["registered"] == 1 and {r[3] for r in rows_in(con)} == {5}

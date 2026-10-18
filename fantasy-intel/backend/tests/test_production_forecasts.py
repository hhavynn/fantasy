"""Immutable, run-keyed production forecasts (app.analytics.production_forecasts, schema v8).
Temp DuckDB files and the synthetic league only; nothing here touches a real or hosted database."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from app.analytics.independent_projection_store import build_live, write_live, write_run
from app.analytics import production_pin
from app.analytics.production_forecasts import (NOT_APPROVED_ARTIFACT, ImmutableRunError, content_sha256,
                                                eligible_run_ids, latest_run, migrate_legacy, pregame_forecasts)
from app.db.connection import SCHEMA_VERSION, init_db, table_columns, table_exists

from test_independent_projections import SCORING, _artifact, live  # noqa: F401  (live is a fixture)

MODEL = "ifp-1.0.0"
PROD = "ifp-2.0.0"      # the approved production model: the only version the artifact pin applies to
FP = SCORING.fingerprint
FIT = "2026-09-01T00:00:00+00:00"           # before every week-6 kickoff below
# Week 6: CCC@AAA is a Thursday game (2026-09-03 20:20 ET = 2026-09-04 00:20 UTC); the rest kick off Sunday
# 2026-09-06 13:00 ET = 17:00 UTC. Players: 1 = AAA (Thursday), 6 = BBB, 9 = FFF, 11 = GGG (Sunday).
BEFORE_ALL = datetime(2026, 9, 3, 12, 0)
BETWEEN = datetime(2026, 9, 5, 12, 0)      # after the Thursday kickoff, before Sunday's
AFTER_ALL = datetime(2026, 9, 7, 12, 0)
ROW_KEYS = {"season", "week", "espn_player_id", "gsis_id", "position", "run_id", "run_created_at", "model_version",
            "artifact_id", "artifact_created_at", "scoring_fingerprint", "nfl_team", "kickoff", "pregame",
            "coverage_status", "projected_points", "p_play", "expected_points"}


@pytest.fixture(autouse=True)
def _unpinned(monkeypatch):
    """These tests exercise the storage, integrity and pregame rules with ifp-1.0.0 (MODEL) runs of synthetic
    artifacts, so the committed ifp-1.0.0 pin is unset here; the pin tests below set it explicitly."""
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", None)


def thursday(con):
    con.execute("UPDATE nfl_schedules SET gameday = '2026-09-03', gametime = '20:20' "
                "WHERE season = 2026 AND week = 6 AND home_team = 'AAA'")
    con.execute("UPDATE nfl_schedules SET gameday = '2026-09-03', gametime = '20:20' "
                "WHERE season = 2026 AND week = 6 AND away_team = 'AAA'")


def store(con, src, created, *, fit=FIT, mutate=None, meta_over=None, week=6, sha="a" * 64, from_week=None):
    meta, rows = build_live(con, src, _artifact(src, created_at=fit), 2026, week, artifact_sha256=sha,
                            projected_from_week=from_week)
    meta["created_at"] = created
    if mutate:
        mutate(rows)
    meta.update(meta_over or {})
    return write_run(con, meta, rows), meta, rows


def bump(pid, points):
    def fn(rows):
        next(r for r in rows if r["espn_player_id"] == pid)["projected_points"] = points
    return fn


def dump(con, run_id):
    return con.execute("SELECT * FROM independent_projection_rows WHERE run_id = ? ORDER BY espn_player_id",
                       [run_id]).fetchall()


def forecasts(con, **kw):
    return pregame_forecasts(con, 2026, kw.pop("weeks", None), model_version=kw.pop("model", MODEL),
                             scoring_fingerprint=kw.pop("fp", FP))


# ----------------------------------------------------------------------------- write-once, append-only


def test_two_runs_of_the_same_week_are_both_retained(live):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL)
    first = dump(con, a["run_id"])
    b, _, _ = store(con, src, BETWEEN, mutate=bump(1, 99.0))
    assert a["created"] and b["created"] and a["run_id"] != b["run_id"]
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 2
    n = a["row_count"]
    assert n == b["row_count"] and con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0] == 2 * n
    assert dump(con, a["run_id"]) == first                                           # the earlier run is untouched
    assert con.execute("SELECT projected_points FROM independent_projection_rows WHERE run_id = ? AND "
                       "espn_player_id = 1", [b["run_id"]]).fetchone()[0] == 99.0


def test_identical_retry_is_idempotent(live):  # noqa: F811
    con, src, _ = live
    a, meta, rows = store(con, src, BEFORE_ALL)
    before = (con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0],
              con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0])
    again, _, _ = store(con, src, BETWEEN)                    # new run_id and created_at, same content
    assert again["run_id"] == a["run_id"] and again["created"] is False
    same_id = write_run(con, meta, rows)                      # the very same run
    assert same_id["run_id"] == a["run_id"] and same_id["created"] is False
    assert (con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0],
            con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0]) == before
    assert write_live(con, meta, rows) == a["row_count"]


def test_source_correction_creates_a_new_run_and_leaves_old_rows_byte_identical(live):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL)
    old_rows = dump(con, a["run_id"])
    old_run = con.execute("SELECT * FROM independent_projection_runs WHERE run_id = ?", [a["run_id"]]).fetchall()
    b, _, _ = store(con, src, BETWEEN, mutate=bump(6, 50.0))
    assert b["created"] and b["run_id"] != a["run_id"] and b["content_sha256"] != a["content_sha256"]
    assert dump(con, a["run_id"]) == old_rows
    assert con.execute("SELECT * FROM independent_projection_runs WHERE run_id = ?", [a["run_id"]]).fetchall() == old_run
    # a different artifact file is different provenance even with identical forecasts
    c, _, _ = store(con, src, BETWEEN, sha="b" * 64)
    assert c["created"] and c["run_id"] not in (a["run_id"], b["run_id"])


def test_different_content_under_an_existing_run_id_raises(live):  # noqa: F811
    con, src, _ = live
    a, meta, rows = store(con, src, BEFORE_ALL)
    before = dump(con, a["run_id"])
    bump(1, 123.0)(rows)
    with pytest.raises(ImmutableRunError):
        write_run(con, meta, rows)
    assert dump(con, a["run_id"]) == before
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 1
    with pytest.raises(ValueError):
        write_run(con, {**meta, "run_id": "empty"}, [])


def test_run_provenance_columns(live):  # noqa: F811
    con, src, _ = live
    a, meta, rows = store(con, src, BEFORE_ALL, sha="c" * 64)
    run = con.execute("SELECT artifact_id, content_sha256, row_count, migrated_from_legacy "
                      "FROM independent_projection_runs").fetchone()
    assert run == ("c" * 64, a["content_sha256"], len(rows), False)
    assert a["content_sha256"] == content_sha256(meta, rows)
    # the id is the file digest when given, a clearly distinct dict digest otherwise
    meta2, _ = build_live(con, src, _artifact(src), 2026, 6)
    assert meta2["artifact_id"].startswith("dict:")


def test_a_reverted_change_is_a_new_newest_run_but_an_immediate_retry_is_idempotent(live):  # noqa: F811
    """A -> B -> A': the third computation equals the FIRST run's content but is the newest, so it must be stored
    (else consumers keep serving B). Only the newest run counts as 'the same' for the retry rule."""
    con, src, _ = live
    thursday(con)
    a, _, _ = store(con, src, BEFORE_ALL)
    b, _, _ = store(con, src, datetime(2026, 9, 4, 12, 0), mutate=bump(6, 0.5))
    c, _, _ = store(con, src, BETWEEN)                      # reverted: same content as A
    assert c["created"] and c["run_id"] not in (a["run_id"], b["run_id"]) and c["content_sha256"] == a["content_sha256"]
    assert latest_run(con, 2026, 6, model_version=MODEL, scoring_fingerprint=FP)["run_id"] == c["run_id"]
    p6 = next(r for r in forecasts(con)[0] if r["espn_player_id"] == 6)
    assert p6["run_id"] == c["run_id"] and p6["projected_points"] != 0.5
    again, _, _ = store(con, src, BETWEEN)                  # immediate retry of C
    assert again["run_id"] == c["run_id"] and again["created"] is False
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 3


def test_a_failed_readback_rolls_everything_back(live, monkeypatch):  # noqa: F811
    import app.analytics.independent_projection_store as st
    con, src, _ = live
    real, calls = st.content_sha256, []

    def flaky(meta, rows):
        calls.append(1)
        return "0" * 64 if len(calls) > 1 else real(meta, rows)      # the in-transaction re-read disagrees
    monkeypatch.setattr(st, "content_sha256", flaky)
    with pytest.raises(RuntimeError, match="reproduce"):
        store(con, src, BEFORE_ALL)
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0] == 0


def test_a_run_without_created_at_gets_its_own_reason(live):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL)
    con.execute("UPDATE independent_projection_runs SET created_at = NULL WHERE run_id = ?", [a["run_id"]])
    rows, excluded = forecasts(con)
    assert rows == [] and excluded[0]["reason"].startswith("run creation time unknown")


# ----------------------------------------------------------------------------- pregame_forecasts


def test_contract_row_keys_and_values(live):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL)
    rows, excluded = forecasts(con)
    assert excluded == [] and rows and all(set(r) == ROW_KEYS for r in rows)
    r = next(r for r in rows if r["espn_player_id"] == 1)
    assert r["run_id"] == a["run_id"] and r["pregame"] is True and r["week"] == 6 and r["season"] == 2026
    assert r["run_created_at"] == BEFORE_ALL.replace(tzinfo=timezone.utc)
    assert r["kickoff"] == datetime(2026, 9, 6, 17, 0, tzinfo=timezone.utc) and r["kickoff"].tzinfo is not None
    assert r["artifact_created_at"] == datetime(2026, 9, 1, tzinfo=timezone.utc) and r["artifact_id"] == "a" * 64
    assert r["model_version"] == MODEL and r["scoring_fingerprint"] == FP and r["coverage_status"] == "projected"
    assert r["projected_points"] > 0 and r["p_play"] is None and r["expected_points"] is None
    assert [(x["week"], x["espn_player_id"]) for x in rows] == sorted((x["week"], x["espn_player_id"]) for x in rows)
    assert forecasts(con, weeks=[5]) == ([], [])


def test_each_players_own_kickoff_decides(live):  # noqa: F811
    con, src, _ = live
    thursday(con)
    early, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 11.0))
    late, _, _ = store(con, src, BETWEEN, mutate=bump(1, 22.0))   # made after AAA's Thursday kickoff
    rows, excluded = forecasts(con)
    by = {r["espn_player_id"]: r for r in rows}
    assert excluded == []
    assert by[1]["run_id"] == early["run_id"] and by[1]["projected_points"] == 11.0     # Thursday player: earlier run
    assert by[1]["kickoff"] == datetime(2026, 9, 4, 0, 20, tzinfo=timezone.utc)
    for pid in (6, 9, 11):                                                              # Sunday players: later run
        assert by[pid]["run_id"] == late["run_id"]
    # only the between-run exists: Thursday player has no pregame forecast, Sunday players keep theirs
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ?", [early["run_id"]])
    con.execute("DELETE FROM independent_projection_runs WHERE run_id = ?", [early["run_id"]])
    rows, _ = forecasts(con)
    assert 1 not in {r["espn_player_id"] for r in rows} and {6, 9, 11} <= {r["espn_player_id"] for r in rows}
    assert all(r["run_id"] == late["run_id"] and r["pregame"] and r["run_created_at"] < r["kickoff"] for r in rows)


def test_post_kickoff_rows_are_never_returned(live):  # noqa: F811
    con, src, _ = live
    pre, _, _ = store(con, src, BEFORE_ALL, mutate=bump(6, 7.0))
    post, _, _ = store(con, src, AFTER_ALL, mutate=bump(6, 77.0))
    rows, excluded = forecasts(con)
    assert {r["run_id"] for r in rows} == {pre["run_id"]} and next(
        r for r in rows if r["espn_player_id"] == 6)["projected_points"] == 7.0
    [ex] = excluded
    assert ex["run_id"] == post["run_id"] and ex["week"] == 6 and ex["reason"].startswith("post-kickoff")


def test_unknown_kickoff_is_not_pregame(live):  # noqa: F811
    con, src, _ = live
    store(con, src, BEFORE_ALL)
    con.execute("UPDATE nfl_schedules SET gametime = NULL WHERE season = 2026 AND week = 6")
    con.execute("UPDATE independent_projection_rows SET kickoff = NULL, pregame = false")
    rows, excluded = forecasts(con)
    assert rows == [] and excluded[0]["reason"].startswith("artifact fit time or week kickoff unknown")


def test_later_flex_can_only_make_a_row_less_eligible(live):  # noqa: F811
    con, src, _ = live
    store(con, src, BETWEEN)                                  # pregame for Sunday players at write time
    assert {r["espn_player_id"] for r in forecasts(con)[0]} >= {6, 9, 11}
    con.execute("UPDATE nfl_schedules SET gameday = '2026-09-05', gametime = '07:00' "
                "WHERE season = 2026 AND week = 6 AND home_team = 'DDD'")      # BBB's game moved before the run
    rows, _ = forecasts(con)
    assert 6 not in {r["espn_player_id"] for r in rows} and 9 in {r["espn_player_id"] for r in rows}


def test_mismatched_runs_are_excluded_with_a_reason(live):  # noqa: F811
    con, src, _ = live
    good, _, _ = store(con, src, BEFORE_ALL)
    late_fit, _, _ = store(con, src, BEFORE_ALL, fit="2026-09-06T17:00:00+00:00", mutate=bump(1, 5.0))
    other_art, _, _ = store(con, src, BEFORE_ALL, sha="d" * 64, mutate=bump(1, 6.0))
    other_model, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 7.0), meta_over={"model_version": "ifp-0.0.0"})
    other_fp, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 8.0), meta_over={"scoring_fingerprint": "other"})
    rows, excluded = forecasts(con)
    why = {e["run_id"]: e["reason"] for e in excluded}
    assert why[late_fit["run_id"]].startswith("artifact fitted after the week's first kickoff")
    assert why[other_model["run_id"]].startswith("model ifp-0.0.0")
    assert why[other_fp["run_id"]] == "made under different league scoring"
    assert good["run_id"] not in why
    # a run from a different artifact file of the right model/scoring is a legitimate, later-created candidate
    assert other_art["run_id"] not in why
    assert {r["run_id"] for r in rows} <= {good["run_id"], other_art["run_id"]}
    assert not {late_fit["run_id"], other_model["run_id"], other_fp["run_id"]} & {r["run_id"] for r in rows}
    # asking for another model or scoring never returns this model's rows
    assert forecasts(con, model="ifp-9")[0] == [] and forecasts(con, fp="nope")[0] == []


def test_pinned_artifact_excludes_other_artifacts_with_a_reason(live, monkeypatch):  # noqa: F811
    """ifp-1.0.0 (MODEL here) is the single-sha pin's model."""
    con, src, _ = live
    thursday(con)       # run B (BETWEEN) is post-kickoff for the Thursday player 1, so run A still supplies his row
    a, _, _ = store(con, src, BEFORE_ALL, sha="a" * 64)
    b, _, _ = store(con, src, BETWEEN, sha="b" * 64, mutate=bump(1, 77.0))
    # unpinned (today): both artifacts are legitimate evidence and the latest eligible run wins per player
    assert production_pin.APPROVED_ARTIFACT_SHA256 is None
    rows, excluded = forecasts(con)
    assert {r["run_id"] for r in rows} == {a["run_id"], b["run_id"]} and not excluded
    assert sorted(eligible_run_ids(con, 2026, MODEL, FP)[0]) == sorted([a["run_id"], b["run_id"]])
    # pinned to artifact A: B's run is excluded with the reason and none of its rows is returned
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", "a" * 64)
    rows, excluded = forecasts(con)
    assert {r["run_id"] for r in rows} == {a["run_id"]} and {r["artifact_id"] for r in rows} == {"a" * 64}
    assert [(e["run_id"], e["reason"]) for e in excluded] == [(b["run_id"], NOT_APPROVED_ARTIFACT)]
    assert NOT_APPROVED_ARTIFACT == "artifact not the approved production artifact"
    run_ids, excluded = eligible_run_ids(con, 2026, MODEL, FP)
    assert run_ids == [a["run_id"]] and excluded[0]["reason"] == NOT_APPROVED_ARTIFACT
    assert latest_run(con, 2026, 6, model_version=MODEL)["run_id"] == a["run_id"]
    # pinned to an artifact nobody used: no evidence at all, every run excluded with the reason
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", "c" * 64)
    rows, excluded = forecasts(con)
    assert rows == [] and {e["reason"] for e in excluded} == {NOT_APPROVED_ARTIFACT} and len(excluded) == 2
    assert latest_run(con, 2026, 6, model_version=MODEL) is None


def test_the_pin_applies_to_its_model_version_only(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    store(con, src, BEFORE_ALL, sha="a" * 64, meta_over={"model_version": "ifp-0.0.5"})
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", "c" * 64)
    rows, excluded = forecasts(con, model="ifp-0.0.5")
    assert rows and not excluded
    assert latest_run(con, 2026, 6, model_version="ifp-0.0.5") is not None
    assert forecasts(con, model=MODEL)[0] == []                    # no ifp-1.0.0 run exists at all here


def test_runs_without_rows_or_with_altered_rows_are_excluded(live):  # noqa: F811
    con, src, _ = live
    zero = {"run_id": "zero-rows", "season": 2026, "week": 6, "model_version": MODEL, "scoring_fingerprint": FP,
            "artifact_created_at": FIT, "created_at": BEFORE_ALL, "row_count": 0, "migrated_from_legacy": True}
    con.execute("INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint, "
                "artifact_created_at, created_at, row_count, migrated_from_legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(zero.values()))
    con.execute("INSERT INTO independent_projection_runs (run_id, season, week, model_version, scoring_fingerprint, "
                "artifact_created_at, created_at) VALUES ('unsealed', 2026, 6, ?, ?, ?, ?)", [MODEL, FP, FIT, BEFORE_ALL])
    lost, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 1.0))
    edited, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 2.0))
    ok, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 3.0))
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ? AND espn_player_id = 1", [lost["run_id"]])
    con.execute("UPDATE independent_projection_rows SET projected_points = 40 WHERE run_id = ? AND espn_player_id = 1",
                [edited["run_id"]])
    rows, excluded = forecasts(con)
    why = {e["run_id"]: e["reason"] for e in excluded}
    assert why["zero-rows"].startswith("forecast rows missing") and why["unsealed"].startswith("forecast rows missing")
    assert why[lost["run_id"]].startswith("integrity: ") and "row_count" in why[lost["run_id"]]
    assert why[edited["run_id"]] == "integrity: stored rows do not match the run's content_sha256"
    assert {r["run_id"] for r in rows} == {ok["run_id"]}
    assert latest_run(con, 2026, 6)["run_id"] == ok["run_id"]       # never the empty, short or edited runs


# ----------------------------------------------------------------------------- v7 -> v8 migration


def downgrade_to_v7(con):
    """Turn a freshly initialised database back into the v7 layout (legacy table only, no run provenance)."""
    con.execute("DROP TABLE independent_projection_rows")
    for c in ("artifact_id", "content_sha256", "row_count", "migrated_from_legacy"):
        con.execute(f"ALTER TABLE independent_projection_runs DROP COLUMN {c}")
    con.execute("DELETE FROM schema_meta WHERE key = 'projection_rows_backfill'")
    con.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")


def test_v7_database_migrates_losslessly_and_keeps_the_legacy_table(live):  # noqa: F811
    con, src, _ = live
    thursday(con)
    meta, rows = build_live(con, src, _artifact(src, created_at=FIT), 2026, 6)
    meta["created_at"] = BETWEEN
    old_meta, old_rows = build_live(con, src, _artifact(src, created_at=FIT), 2026, 6)
    old_meta["created_at"] = BEFORE_ALL
    downgrade_to_v7(con)
    assert "row_count" not in table_columns(con, "independent_projection_runs")
    legacy_cols = [c for c in table_columns(con, "independent_player_projections")]
    import app.db.connection as db
    for m in (old_meta, meta):                                                    # both runs of week 6 existed ...
        db.upsert_rows(con, "independent_projection_runs", [{k: v for k, v in m.items() if k != "artifact_id"}],
                       ["run_id"])
    db.replace_where(con, "independent_player_projections", rows, "season = 2026 AND week = 6")   # ... the old bug kept one
    orphan = [{**rows[0], "run_id": "no-such-run", "week": 5, "espn_player_id": 999}]
    db.replace_where(con, "independent_player_projections", rows + orphan, "season = 2026")
    legacy_before = con.execute("SELECT * FROM independent_player_projections ORDER BY season, week, "
                                "espn_player_id").fetchall()
    assert init_db(con) is None                                                    # the migration runs here
    assert con.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    assert SCHEMA_VERSION == 8
    # legacy table untouched
    assert con.execute("SELECT * FROM independent_player_projections ORDER BY season, week, espn_player_id"
                       ).fetchall() == legacy_before
    # lossless copy under the original run id
    cols = ", ".join(legacy_cols)
    copied = con.execute(f"SELECT {cols} FROM independent_projection_rows WHERE run_id = ? ORDER BY espn_player_id",
                         [meta["run_id"]]).fetchall()
    assert copied == con.execute(f"SELECT {cols} FROM independent_player_projections WHERE run_id = ? "
                                 "ORDER BY espn_player_id", [meta["run_id"]]).fetchall() and len(copied) == len(rows)
    # kickoff / pregame computed from the player's own game (AAA plays Thursday, created between the games)
    ko = {r[0]: r[1:] for r in con.execute("SELECT espn_player_id, kickoff, pregame FROM independent_projection_rows "
                                           "WHERE run_id = ?", [meta["run_id"]]).fetchall()}
    assert ko[1] == (datetime(2026, 9, 4, 0, 20), False) and ko[6] == (datetime(2026, 9, 6, 17, 0), True)
    # run bookkeeping: sealed run vs the run whose rows the old bug destroyed
    runs = {r[0]: r[1:] for r in con.execute("SELECT run_id, row_count, migrated_from_legacy, content_sha256, artifact_id "
                                             "FROM independent_projection_runs").fetchall()}
    assert runs[meta["run_id"]][0] == len(rows) and runs[meta["run_id"]][1] is True and runs[meta["run_id"]][2]
    assert runs[old_meta["run_id"]] == (0, True, None, None)
    # the orphan legacy row is reported, not invented
    report = json.loads(con.execute("SELECT value FROM schema_meta WHERE key = 'projection_rows_backfill'").fetchone()[0])
    assert report["orphan_legacy_rows"] == 1 and report["orphan_legacy_run_ids"] == ["no-such-run"]
    assert report["runs_without_rows"] == [old_meta["run_id"]] and report["rows_copied"] == len(rows)
    assert con.execute("SELECT count(*) FROM independent_projection_rows WHERE run_id = 'no-such-run'").fetchone()[0] == 0
    # evidence: the migrated run is usable (artifact stamped before kickoff), the destroyed run is excluded
    out, excluded = forecasts(con)
    assert {r["run_id"] for r in out} == {meta["run_id"]} and 1 not in {r["espn_player_id"] for r in out}
    assert [e["run_id"] for e in excluded] == [old_meta["run_id"]]
    assert excluded[0]["reason"].startswith("forecast rows missing")
    # the consumer view skips the destroyed run even though it is the OLDER one here, and a newer destroyed run
    assert latest_run(con, 2026, 6)["run_id"] == meta["run_id"]
    # idempotent: a second pass changes nothing
    assert migrate_legacy(con)["rows_copied"] == 0
    assert con.execute("SELECT count(*) FROM independent_projection_rows").fetchone()[0] == len(rows)


def test_v6_database_keeps_its_runs_and_legacy_rows_through_the_upgrade(live):  # noqa: F811
    """The old v7 rebuild DROPPED both projection tables. A v6-layout database (no participation columns, no v8
    columns) now keeps every row and migrates it."""
    import app.db.connection as db
    con, src, _ = live
    meta, rows = build_live(con, src, _artifact(src, created_at=FIT), 2026, 6)
    meta["created_at"] = BEFORE_ALL
    downgrade_to_v7(con)
    db.upsert_rows(con, "independent_projection_runs", [{k: v for k, v in meta.items() if k != "artifact_id"}], ["run_id"])
    db.replace_where(con, "independent_player_projections", rows, "season = 2026 AND week = 6")
    for c in ("participation_version", "participation_status", "participation_evaluation"):
        con.execute(f"ALTER TABLE independent_projection_runs DROP COLUMN {c}")
    for c in ("p_play", "expected_points", "participation"):
        con.execute(f"ALTER TABLE independent_player_projections DROP COLUMN {c}")
    con.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
    n = con.execute("SELECT count(*) FROM independent_player_projections").fetchone()[0]
    assert n == len(rows)
    init_db(con)
    assert con.execute("SELECT count(*) FROM independent_player_projections").fetchone()[0] == n
    assert con.execute("SELECT count(*) FROM independent_projection_runs").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM independent_projection_rows WHERE run_id = ?",
                       [meta["run_id"]]).fetchone()[0] == n
    assert {"p_play", "expected_points", "participation"} <= set(table_columns(con, "independent_player_projections"))
    assert "participation_version" in table_columns(con, "independent_projection_runs")
    assert latest_run(con, 2026, 6)["run_id"] == meta["run_id"]


def test_new_tables_are_never_in_a_rebuild_entry():
    from app.db.connection import _REBUILD
    assert "independent_projection_rows" not in {t for tables in _REBUILD.values() for t in tables}
    assert _REBUILD[7] == [] and _REBUILD[8] == []


def test_reinitialising_a_v8_database_keeps_history(live):  # noqa: F811
    con, src, _ = live
    a, _, _ = store(con, src, BEFORE_ALL)
    before = dump(con, a["run_id"])
    init_db(con)
    init_db(con)
    assert dump(con, a["run_id"]) == before


# ----------------------------------------------------------------------------- hosted runtime, publish


def test_module_imports_without_polars_or_numpy():
    backend = Path(__file__).resolve().parents[1]
    code = ("import sys\nfor m in ('polars', 'numpy'):\n    sys.modules[m] = None\n"
            "import app.analytics.production_forecasts as pf, app.analytics.projection_source as ps\n"
            "print(pf.pregame_forecasts.__name__, ps.MODEL_VERSION)")
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["pregame_forecasts", "ifp-2.0.0"]   # the production model (projection_source)


def test_publish_copies_the_run_tables_as_is_and_cannot_touch_local_history(live, tmp_path):  # noqa: F811
    from app.db.publish import replace_tables
    con, src, path = live
    a, _, _ = store(con, src, BEFORE_ALL)
    store(con, src, BETWEEN, mutate=bump(1, 9.0))
    con.close()
    hosted = tmp_path / "hosted.duckdb"
    hcon = duckdb.connect(str(hosted))
    hcon.execute("CREATE TABLE schema_meta (key VARCHAR, value VARCHAR)")
    hcon.execute("CREATE TABLE independent_projection_rows AS SELECT 1 AS stale")   # old hosted copy is replaced
    hcon.close()
    hub = duckdb.connect()
    hub.execute(f"ATTACH '{path}' AS src (READ_ONLY)")
    hub.execute(f"ATTACH '{hosted}' AS dst")
    before = hub.execute("SELECT * FROM src.main.independent_projection_rows ORDER BY run_id, espn_player_id").fetchall()
    replace_tables(hub, "src", "dst", "stamp")
    assert hub.execute("SELECT * FROM dst.main.independent_projection_rows ORDER BY run_id, espn_player_id"
                       ).fetchall() == before
    assert hub.execute("SELECT * FROM dst.main.independent_projection_runs ORDER BY run_id").fetchall() == \
        hub.execute("SELECT * FROM src.main.independent_projection_runs ORDER BY run_id").fetchall()
    assert hub.execute("SELECT * FROM src.main.independent_projection_rows ORDER BY run_id, espn_player_id"
                       ).fetchall() == before                                       # publishing never writes the source
    hub.close()
    assert table_exists(duckdb.connect(str(hosted), read_only=True), "independent_projection_rows")


# ----------------------------------------------------------------------------- consumer selection integrity


def test_latest_run_rechecks_the_content_hash_not_just_the_row_count(live):  # noqa: F811
    """Same-row-count tampering: a stored value changes, the row count does not. The consumer view must skip
    the run exactly like the benchmark reader does, and fall back to the newest run that still verifies."""
    con, src, _ = live
    ok, _, _ = store(con, src, BEFORE_ALL, mutate=bump(1, 3.0))
    tampered, _, _ = store(con, src, BETWEEN, mutate=bump(1, 4.0))
    con.execute("UPDATE independent_projection_rows SET projected_points = 99 WHERE run_id = ? AND espn_player_id = 1",
                [tampered["run_id"]])
    assert con.execute("SELECT count(*) FROM independent_projection_rows WHERE run_id = ?",
                       [tampered["run_id"]]).fetchone()[0] == tampered["row_count"]
    assert latest_run(con, 2026, 6)["run_id"] == ok["run_id"]
    _, excluded = forecasts(con)
    assert {e["run_id"]: e["reason"] for e in excluded}[tampered["run_id"]] == \
        "integrity: stored rows do not match the run's content_sha256"

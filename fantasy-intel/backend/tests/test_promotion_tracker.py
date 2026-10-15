"""Promotion tracker (app.analytics.promotion_tracker): read-only, frozen criteria, pregame-only ESPN."""

import json

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.analytics.forecast_backtest import PROMOTION_CRITERIA
from app.analytics.forecast_features import load_panel
from app.analytics.independent_projection_store import build_live, write_live
from app.analytics.promotion_tracker import build_tracker, model_evidence, pregame_snapshot

from test_independent_projections import SCORING, _artifact, live  # noqa: F401  (live is a fixture)

# Synthetic week-5 games kick off 2026-09-05 13:00 Eastern = 17:00 UTC; week 6 on 2026-09-06.
PRE_W5, POST_W5 = "2026-09-05 16:59:00", "2026-09-05 17:00:00"
FIT_BEFORE_W5 = "2026-09-01T00:00:00+00:00"


def _snapshot(con, espn_id: int, week: int, points: float, synced_at: str) -> None:
    con.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, ?, ?, ?)",
                [synced_at[:10], espn_id, week, points, synced_at])


@pytest.fixture
def tracked(live):  # noqa: F811
    """Stored week-5 run (week 5 is final) plus ESPN snapshots: WR 1, WR 6 and TE 9 pregame;
    WR 11 only after kickoff (must not count). The synthetic artifact has no TE model, so TE 9 has
    a pregame ESPN snapshot and a completed game but no independent projection."""
    con, src, path = live
    meta, rows = build_live(con, src, _artifact(src), 2026, 5)
    write_live(con, meta, rows)
    # As if the artifact had been fitted before week 5 (the synthetic one is stamped 2026-10-14).
    con.execute("UPDATE independent_projection_runs SET artifact_created_at = ?", [FIT_BEFORE_W5])
    for pid, pts in ((1, 14.0), (6, 3.0), (9, 8.0)):
        _snapshot(con, pid, 5, pts, PRE_W5)
    _snapshot(con, 11, 5, 9.0, POST_W5)
    return con, src, path


def _manual(con, ids: list[int]) -> tuple[float, float]:
    panel = load_panel(con, SCORING, [2026]).filter(pl.col("week") == 5)
    label = {r["gsis_id"]: r["league_points"] for r in panel.iter_rows(named=True)}
    ours = dict(con.execute("SELECT espn_player_id, projected_points FROM independent_player_projections "
                            "WHERE week = 5").fetchall())
    espn = dict(con.execute("SELECT espn_player_id, projected_points FROM espn_projection_history "
                            "WHERE scoring_period = 5").fetchall())
    gs = dict(con.execute("SELECT espn_player_id, gsis_id FROM player_id_map").fetchall())
    a = np.array([label[gs[i]] for i in ids])
    return (float(np.abs(a - np.array([ours[i] for i in ids])).mean()),
            float(np.abs(a - np.array([espn[i] for i in ids])).mean()))


def test_matched_counts_use_pregame_snapshots_only(tracked):
    con, _, _ = tracked
    t = build_tracker(con)
    pos = {p["position"]: p for p in t["positions"]}
    assert pos["WR"]["espn"]["n"] == 2 and pos["WR"]["espn"]["pregame_completed"] == 2   # WR 11 is post-kickoff
    assert pos["TE"]["espn"]["n"] == 0 and pos["TE"]["espn"]["pregame_completed"] == 1   # no independent projection
    assert pos["QB"]["espn"]["n"] == 0 and pos["RB"]["espn"]["n"] == 0
    assert t["espn"]["matched_observations"] == 2
    assert t["espn"]["by_week"] == [{"week": 5, "pregame_completed": 3, "matched": 2}]
    mae_i, mae_e = _manual(con, [1, 6])
    assert pos["WR"]["espn"]["mae_independent"] == pytest.approx(mae_i, abs=1e-3)
    assert pos["WR"]["espn"]["mae_espn"] == pytest.approx(mae_e, abs=1e-3)
    assert pos["QB"]["espn"]["mae_independent"] is None and pos["QB"]["espn"]["mae_espn"] is None   # no rows, no MAE


def test_criteria_come_from_the_frozen_constant(tracked, monkeypatch):
    con, _, _ = tracked
    t = build_tracker(con)
    assert t["criteria"]["espn_min_matched_per_position"] == PROMOTION_CRITERIA["espn_min_matched_per_position"] == 300
    assert t["criteria"]["min_relative_mae_improvement_vs_best_trailing_baseline"] == 0.03
    assert not any(p["espn"]["criterion_met"] for p in t["positions"])
    # The tracker reads the constant at call time (it never keeps its own copy of the bar).
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 1)
    wr = next(p for p in build_tracker(con)["positions"] if p["position"] == "WR")["espn"]
    assert wr["required"] == 1 and wr["criterion_met"] == (wr["mae_independent"] <= wr["mae_espn"])


def test_stored_status_is_copied_not_recomputed(tracked):
    con, _, _ = tracked
    t = build_tracker(con)
    assert t["run"]["week"] == 5 and t["run_missing_reason"] is None
    stored = json.loads(con.execute("SELECT model_status FROM independent_projection_runs").fetchone()[0])
    assert {s["status"] for s in stored.values()} == {"experimental", "insufficient_data"}   # synthetic artifact
    for p in t["positions"]:
        st = stored[p["position"]]
        assert (p["model"]["status"], p["model"]["reasons"]) == (st["status"], st["reasons"])
        assert p["model"]["start_sit_ready"] is False
    run = {"model_status": {"QB": {"status": "validated", "start_sit_ready": False, "reasons": ["x"]}},
           "evaluation": {"QB": {"common_support_n": 834, "relative_mae_improvement": 0.0578,
                                 "best_trailing_baseline": "recent_avg",
                                 "metrics": {"primary": {"mae": 6.55}, "recent_avg": {"mae": 6.95}},
                                 "primary_minus_best_baseline_mae": {"ci95": [-0.57, -0.23]}}}}
    assert model_evidence(run, "QB") == {
        "status": "validated", "start_sit_ready": False, "reasons": ["x"], "heldout_n": 834,
        "relative_mae_improvement": 0.0578, "best_trailing_baseline": "recent_avg", "mae_primary": 6.55,
        "mae_best_baseline": 6.95, "ci95_primary_minus_baseline": [-0.57, -0.23]}


def test_other_model_versions_do_not_count(tracked):
    con, _, _ = tracked
    con.execute("UPDATE independent_projection_runs SET model_version = 'ifp-0.0.0'")
    t = build_tracker(con)
    assert t["espn"]["matched_observations"] == 0
    assert next(p for p in t["positions"] if p["position"] == "WR")["espn"]["pregame_completed"] == 2
    # Neither the evidence nor the held-out status of another model version is shown.
    assert t["run"] is None and "another model version" in t["run_missing_reason"]
    assert all(p["model"]["status"] is None for p in t["positions"])
    assert t["espn"]["excluded_runs"][0]["reason"].startswith("model ifp-0.0.0")


def test_artifact_fitted_after_the_week_does_not_count(tracked):
    """`fit` trains on every completed week: an artifact fitted after week 5 kicked off may have
    seen week 5's results, so its week-5 projections are not evidence."""
    con, _, _ = tracked
    con.execute("UPDATE independent_projection_runs SET artifact_created_at = ?", ["2026-09-05T17:00:00+00:00"])
    t = build_tracker(con)
    assert t["espn"]["matched_observations"] == 0
    [ex] = t["espn"]["excluded_runs"]
    assert ex["week"] == 5 and ex["reason"].startswith("artifact fitted after the week's first kickoff")
    assert t["run"] is not None   # the held-out status itself is still the current model's


def test_other_league_scoring_does_not_count(tracked):
    con, _, _ = tracked
    con.execute("UPDATE independent_projection_runs SET scoring_fingerprint = 'other-scoring'")
    t = build_tracker(con)
    assert t["espn"]["matched_observations"] == 0 and t["run"] is None
    assert t["espn"]["excluded_runs"][0]["reason"] == "made under different league scoring"


def test_no_run_means_no_status_but_espn_pool_is_still_counted(live):  # noqa: F811
    con, _, _ = live
    _snapshot(con, 1, 5, 14.0, PRE_W5)
    t = build_tracker(con)
    assert t["run"] is None and "project" in t["run_missing_reason"]
    wr = next(p for p in t["positions"] if p["position"] == "WR")
    assert wr["model"]["status"] is None and wr["espn"]["n"] == 0 and wr["espn"]["pregame_completed"] == 1


def test_current_week_pregame_snapshot(live):  # noqa: F811
    con, _, _ = live
    s = pregame_snapshot(con, 2026, 6)
    assert s["first_kickoff"] == "2026-09-06T17:00:00+00:00" and s["captured"] is False
    assert s["last_kickoff"] == "2026-09-06T17:00:00+00:00"
    _snapshot(con, 1, 6, 10.0, "2026-09-06 17:30:00")      # after kickoff: not a pregame snapshot
    s = pregame_snapshot(con, 2026, 6)
    assert s["captured"] is False and s["latest_snapshot_at"].startswith("2026-09-06T17:30")
    _snapshot(con, 6, 6, 10.0, "2026-09-05 23:00:00")
    s = pregame_snapshot(con, 2026, 6)
    assert s["captured"] is True and s["players"] == 1 and s["latest_pregame_snapshot_at"].startswith("2026-09-05T23:00")


def test_api_is_read_only(tracked, monkeypatch):
    con, _, path = tracked
    tables = ("independent_player_projections", "independent_projection_runs", "espn_projection_history")
    before = {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables}
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    body = TestClient(app).get("/api/projections/independent/promotion").json()
    assert body["schema_version"] == "independent-promotion/v1" and body["espn"]["matched_observations"] == 2
    assert [p["position"] for p in body["positions"]] == ["QB", "RB", "WR", "TE"]
    import duckdb
    after = duckdb.connect(str(path), read_only=True)
    assert {t: after.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables} == before
    after.close()


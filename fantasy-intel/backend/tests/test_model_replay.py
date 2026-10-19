"""Weekly model replay: the per-week aggregation of scripts/model_replay.py, the write-once store and the hosted reader
(app.analytics.model_replay), and the API route. Synthetic numbers only; nothing here is a real result."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

import duckdb
import polars as pl
import pytest

from app.analytics import model_replay as mr
from app.db.connection import init_db

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def cell(n: int, model: float, prior: float | None = None, avg: float | None = None) -> dict:
    return {"n": n, "mae": {"model": model, "prior_version": model + 0.1 if prior is None else prior,
                            "recent_avg": model + 0.3 if avg is None else avg}}


def week(w: int, maes: dict[str, float], n: int = 10) -> dict:
    groups = {p: cell(n, maes[p]) for p in mr.POSITIONS}
    groups["ALL"] = cell(n * 4, sum(maes.values()) / 4)
    return {"week": w, "groups": groups}


def snapshot(fp: str = "m1", data_fp: str = "d1", *, at: str = "2026-10-10T00:00:00+00:00",
             weeks: dict[int, dict[str, float]] | None = None) -> dict:
    weeks = weeks or {1: {"QB": 6.0, "RB": 4.0, "WR": 4.0, "TE": 3.0}, 2: {"QB": 7.0, "RB": 4.0, "WR": 4.0, "TE": 3.0}}
    wk = [week(w, m) for w, m in sorted(weeks.items())]
    totals = {g: {"n": sum(x["groups"][g]["n"] for x in wk),
                  "mae": {m: round(sum(x["groups"][g]["mae"][m] for x in wk) / len(wk), 4) for m in mr.METRICS}}
              for g in mr.GROUPS}
    return {"schema_version": mr.SCHEMA_VERSION, "season": 2026, "generated_at": at,
            "model": {"version": "ifp-2.2.0", "fingerprint": fp}, "prior_version": {"version": "ifp-2.1.0"},
            "data": {"fingerprint": data_fp, "last_week": max(weeks)}, "population": "players who played",
            "weeks": wk, "totals": totals, "notes": ["synthetic"]}


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "r.duckdb"))
    init_db(c)
    yield c
    c.close()


def test_snapshot_round_trip_and_unchanged_model_and_data_store_nothing(con):
    sha, created = mr.store_replay(con, snapshot())
    assert created
    same, created = mr.store_replay(con, snapshot(at="2026-10-11T00:00:00+00:00"))
    assert not created and same == sha
    _, created = mr.store_replay(con, snapshot(data_fp="d2", at="2026-10-12T00:00:00+00:00"))   # a new final week
    assert created
    assert [s["payload"]["data"]["fingerprint"] for s in mr.snapshots(con, 2026)] == ["d2", "d1"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda p: p["weeks"][0]["groups"]["ALL"].update(n=1), "ALL n is not the sum"),
    (lambda p: p["totals"]["QB"].update(n=999), "totals QB"),
    (lambda p: p["weeks"][0]["groups"]["QB"]["mae"].update(model=-1), "non-negative"),
    (lambda p: p["weeks"].reverse(), "ascending"),
    (lambda p: p["weeks"][0].update(gsis_id="00-1"), "identifier"),
    (lambda p: p["model"].update(fingerprint=""), "fingerprint"),
])
def test_invalid_snapshots_are_refused(con, mutate, reason):
    p = snapshot()
    mutate(p)
    with pytest.raises(ValueError, match=reason):
        mr.store_replay(con, p)


def test_damaged_row_is_skipped_never_served(con):
    mr.store_replay(con, snapshot())
    mr.store_replay(con, snapshot("m2", at="2026-10-12T00:00:00+00:00"))
    con.execute("UPDATE model_replays SET payload = replace(payload, '6.0', '1.0') WHERE model_fingerprint = 'm2'")
    assert [s["payload"]["model"]["fingerprint"] for s in mr.snapshots(con, 2026)] == ["m1"]


def test_response_compares_with_the_previous_model_week_by_week(con):
    assert mr.response(con, 2026)["available"] is False
    mr.store_replay(con, snapshot("m1"))
    first = mr.response(con, 2026)
    assert first["previous"] is None and first["vs_previous"] is None
    assert first["vs_prior"]["tally"]["QB"] == {"better": 2, "worse": 0, "same": 0}      # model 0.1 below prior
    # same model, a newer data snapshot: still nothing to compare against
    mr.store_replay(con, snapshot("m1", "d2", at="2026-10-11T00:00:00+00:00"))
    assert mr.response(con, 2026)["vs_previous"] is None
    # a model change: closer in week 1, further in week 2, three weeks where the old one had two
    mr.store_replay(con, snapshot("m2", "d3", at="2026-10-12T00:00:00+00:00",
                                  weeks={1: {"QB": 5.0, "RB": 4.0, "WR": 4.0, "TE": 3.0},
                                         2: {"QB": 7.5, "RB": 4.02, "WR": 4.0, "TE": 3.0},
                                         3: {"QB": 6.0, "RB": 4.0, "WR": 4.0, "TE": 3.0}}))
    res = mr.response(con, 2026)
    assert res["previous"]["model"]["fingerprint"] == "m1" and res["previous"]["data"]["fingerprint"] == "d2"
    weeks = {w["week"]: w["groups"] for w in res["vs_previous"]["weeks"]}
    assert set(weeks) == {1, 2}                                  # only weeks both replayed
    assert weeks[1]["QB"] == {"diff": -1.0, "direction": "better", "n_new": 10, "n_old": 10}
    assert weeks[2]["QB"]["direction"] == "worse" and weeks[2]["RB"]["direction"] == "same"
    assert res["vs_previous"]["tally"]["QB"] == {"better": 1, "worse": 1, "same": 0}
    assert [h["model"]["fingerprint"] for h in res["history"]] == ["m2", "m1", "m1"]


def test_summarize_scores_identical_player_weeks():
    mrs = importlib.import_module("model_replay")
    preds = pl.DataFrame({
        "week": [1, 1, 1, 2, 2],
        "position": ["QB", "QB", "RB", "RB", "WR"],
        "label": [10.0, 20.0, 5.0, None, 8.0],
        "pred_model": [12.0, 18.0, 6.0, 4.0, 8.0],
        "pred_prior": [13.0, 17.0, 7.0, 4.0, None],              # the WR row has no prior forecast: not paired
        "pred_A1": [10.0, 10.0, 5.0, 4.0, 8.0],
    })
    weeks, totals = mrs.summarize(preds)
    assert [w["week"] for w in weeks] == [1]                     # week 2 has no paired row
    assert weeks[0]["groups"]["QB"] == {"n": 2, "mae": {"model": 2.0, "prior_version": 3.0, "recent_avg": 5.0}}
    assert weeks[0]["groups"]["ALL"]["n"] == 3 and totals["WR"]["n"] == 0
    assert mr.validate(snapshot() | {"weeks": weeks, "totals": totals}) is None


def test_api_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db = tmp_path / "api.duckdb"
    c = duckdb.connect(str(db))
    init_db(c)
    mr.store_replay(c, snapshot())
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    monkeypatch.setenv("MOTHERDUCK_DATABASE", "")
    monkeypatch.setenv("FANTASY_API_KEY", "")
    from app.main import app

    body = TestClient(app).get("/api/projections/replay?season=2026").json()
    assert body["available"] is True and body["latest"]["model"]["fingerprint"] == "m1"
    assert body["vs_prior"]["tally"]["ALL"]["better"] == 2


def test_a_failed_replay_is_a_warning_never_a_failed_refresh(monkeypatch, tmp_path):
    import refresh_all as refresh

    assert refresh.REPLAY not in refresh.plan(publish=True)
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "tok")
    calls = []
    monkeypatch.setattr(refresh, "run_step", lambda cmd, log, timeout=None: calls.append(Path(cmd[1]).name) or (
        2 if Path(cmd[1]).name == refresh.REPLAY else 0))
    monkeypatch.setattr(refresh, "check_pregame", lambda log: None)
    monkeypatch.setattr(refresh, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py", "--skip-evidence"])
    assert refresh.main() == 0
    assert calls.index(refresh.REPLAY) == calls.index(refresh.REPORT) + 1 and calls[-1] == refresh.PUBLISH
    assert "WARNING: replay: model_replay.py exited 2" in next(tmp_path.glob("refresh-*.log")).read_text()

"""Floor / ceiling ranges around the served Ours projection (app.analytics.projection_ranges, GET
/api/projections/ranges, independent.range). Synthetic artifacts and runs; nothing here is a real result."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import duckdb
import pytest

from app.analytics import point_distributions as pd_
from app.analytics import projection_ranges as pr
from app.analytics.independent_projection_store import build_live, write_live

from test_independent_projections import PINNED, SCORING, _artifact, _client, live  # noqa: F401
from test_point_distributions import _synthetic_oos

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BACKEND = Path(__file__).resolve().parents[1]


def _dist_artifact(scoring_fingerprint=SCORING.fingerprint, qb_status="validated", rb_status="validated"):
    choice = {"QB": {"method": "Bz"}, "RB": {"method": "R@300"}, "WR": {"method": "R@300"}, "TE": {"method": "R@300"}}
    status = {"QB": {"status": qb_status}, "RB": {"status": rb_status}, "WR": {"status": "validated"},
              "TE": {"status": "validated"}}
    return json.loads(json.dumps(pd_.fit_artifact(
        _synthetic_oos(), choice, status, through_key=202605, fingerprint="f" * 16,
        scoring_fingerprint=scoring_fingerprint, selection_sha="s" * 16,
        participation_status={"QB": "experimental", "RB": "experimental", "WR": "validated", "TE": "validated"})))


@pytest.fixture(scope="module")
def artifact():
    return _dist_artifact()


def _pin(art, pin_id=1):
    return {"pin_id": pin_id, "content_hash": art["content_hash"], "artifact_sha256": "a" * 64,
            "pinned_at": "2026-10-20T15:00:00+00:00"}


def _tables(art, pin_id=1):
    con = duckdb.connect()
    pr.store(con, art, _pin(art, pin_id))
    return pr.load(con)


# ----------------------------------------------------------------------------- quantile serving


@pytest.mark.parametrize("pos", ["QB", "RB", "WR", "TE"])
def test_served_levels_reproduce_the_research_artifact_exactly(artifact, pos):
    """The numpy-free table at P10/P50/P90 equals point_distributions.artifact_quantiles at every mean, including
    between grid points, at 0 and beyond the grid's top (clamped like the artifact)."""
    tables = _tables(artifact)
    top = artifact["positions"][pos]["table"]["mean_grid"][-1]
    for mean in (0.0, 0.3, 2.25, 7.77, 12.0, 18.4, 25.1, top + 4.0):
        qf = pd_.artifact_quantiles(artifact, pos, mean)
        got = pr.quantiles(tables, pos, mean)
        assert got["floor"] == pytest.approx(qf.quantile(0.1), abs=0.006)
        assert got["median"] == pytest.approx(qf.quantile(0.5), abs=0.006)
        assert got["ceiling"] == pytest.approx(qf.quantile(0.9), abs=0.006)
        assert got["floor"] <= got["median"] <= got["ceiling"]


def test_ranges_widen_with_the_projection_and_keep_it_inside(artifact):
    tables = _tables(artifact)
    low, high = pr.quantiles(tables, "WR", 6.0), pr.quantiles(tables, "WR", 16.0)
    assert high["ceiling"] - high["floor"] > low["ceiling"] - low["floor"]
    for mean, r in ((6.0, low), (16.0, high)):
        assert r["floor"] < mean < r["ceiling"]


def test_store_is_append_only_per_pin_and_load_reads_the_newest(artifact):
    con = duckdb.connect()
    n = pr.store(con, artifact, _pin(artifact))
    assert n == sum(len(e["table"]["mean_grid"]) for e in artifact["positions"].values() if e.get("table"))
    assert pr.store(con, artifact, _pin(artifact)) == 0              # the same pin twice: a no-op
    other = _dist_artifact(scoring_fingerprint="0" * 16)
    pr.store(con, other, _pin(other, pin_id=2))
    t = pr.load(con)
    assert t["pin_id"] == 2 and t["scoring_fingerprint"] == "0" * 16
    assert con.execute(f"SELECT count(DISTINCT pin_id) FROM {pr.TABLE}").fetchone()[0] == 2


def test_store_refuses_an_artifact_that_is_not_the_pinned_one(artifact):
    with pytest.raises(ValueError, match="not the pinned one"):
        pr.store(duckdb.connect(), artifact, {**_pin(artifact), "content_hash": "x" * 16})
    with pytest.raises(ValueError, match="point-distribution artifact"):
        pr.table_rows({**artifact, "schema": "other"}, _pin(artifact))


def test_position_policy_ships_rb_wr_te_and_holds_qb(artifact):
    tables = _tables(artifact)
    assert [p for p in pr.POSITION_POLICY if pr.servable(tables, p)[0]] == ["RB", "WR", "TE"]
    ok, why = pr.servable(tables, "QB")
    assert not ok and "replay" in why                                # validated in the artifact, still held back
    assert not pr.servable(tables, "K")[0] and not pr.servable(tables, "D/ST")[0]
    held = _tables(_dist_artifact(rb_status="withheld"))
    ok, why = pr.servable(held, "RB")
    assert not ok and "withheld" in why                              # a position the artifact withholds is never served


def test_rows_need_a_projection_and_runs_need_horizon_zero_and_matching_scoring(artifact):
    tables = _tables(artifact)
    row = {"position": "WR", "coverage_status": "projected", "projected_points": 12.0}
    assert pr.row_range(tables, row) == pr.quantiles(tables, "WR", 12.0)
    assert pr.row_range(tables, {**row, "coverage_status": "withheld_unavailable"}) is None
    assert pr.row_range(tables, {**row, "projected_points": None}) is None
    assert pr.row_range(tables, {**row, "position": "QB"}) is None
    run = {"scoring_fingerprint": SCORING.fingerprint, "forecast_horizon": None}
    assert pr.run_problem(tables, run) is None                        # NULL horizon is the next-week forecast
    assert "rest-of-season" in pr.run_problem(tables, {**run, "forecast_horizon": 2})
    assert "scoring" in pr.run_problem(tables, {**run, "scoring_fingerprint": "0" * 16})
    assert "no pinned" in pr.run_problem(None, run)


# ----------------------------------------------------------------------------- API


def _served(live, artifact=None):
    con, src, path = live
    meta, rows = build_live(con, src, _artifact(src), 2026, 6, artifact_sha256=PINNED)
    write_live(con, meta, rows)
    if artifact is not None:
        pr.store(con, artifact, _pin(artifact))
    con.close()
    return path


def test_ranges_api_shape(live, monkeypatch, artifact):
    body = _client(monkeypatch, _served(live, artifact)).get("/api/projections/ranges", params={"week": 6}).json()
    assert body["schema_version"] == "projection-ranges/v1" and body["available"] is True and body["reason"] is None
    assert body["week"] == 6 and body["season"] == 2026 and body["run"]["forecast_horizon"] == 0
    assert body["levels"] == {"floor": 0.1, "median": 0.5, "ceiling": 0.9} and body["coverage"] == 0.8
    assert body["conditional_on"] == "appears" and body["version"] == "ifpd-0.1.0"
    assert {p for p, v in body["positions"].items() if v["served"]} == {"RB", "WR", "TE"}
    assert body["positions"]["QB"]["reason"]
    ranges = body["ranges"]
    assert ranges and "3" not in ranges and "5" not in ranges        # the QB and the kicker carry none
    for pid, r in ranges.items():
        assert set(r) == {"position", "projection", "floor", "median", "ceiling"}
        assert r["position"] in {"RB", "WR", "TE"}
        assert r["floor"] <= r["median"] <= r["ceiling"] and r["floor"] < r["projection"] < r["ceiling"]
    assert "1" in ranges                                             # the healthy WR


def test_independent_api_carries_the_same_range(live, monkeypatch, artifact):
    client = _client(monkeypatch, _served(live, artifact))
    body = client.get("/api/projections/independent", params={"week": 6}).json()
    ranges = client.get("/api/projections/ranges", params={"week": 6}).json()["ranges"]
    assert body["ranges"]["available"] is True
    for p in body["players"]:
        want = ranges.get(str(p["espn_player_id"]))
        got = p["independent"]["range"]
        assert got == (None if want is None else {k: want[k] for k in ("floor", "median", "ceiling")})
        if p["position"] in ("QB", "K", "D/ST") or p["independent"]["projected_points"] is None:
            assert got is None


def test_no_ranges_without_a_pinned_table_or_under_other_scoring(live, monkeypatch):
    path = _served(live)
    client = _client(monkeypatch, path)
    body = client.get("/api/projections/ranges", params={"week": 6}).json()
    assert body["available"] is False and "no pinned" in body["reason"] and body["ranges"] == {}
    assert all(p["independent"]["range"] is None for p in
               client.get("/api/projections/independent", params={"week": 6}).json()["players"])
    con = duckdb.connect(str(path))
    other = _dist_artifact(scoring_fingerprint="0" * 16)
    pr.store(con, other, _pin(other))
    con.close()
    body = client.get("/api/projections/ranges", params={"week": 6}).json()
    assert body["available"] is False and "scoring" in body["reason"]
    assert client.get("/api/projections/ranges", params={"week": 7}).json()["available"] is False   # no run


def test_publish_carries_the_range_table(live, artifact):
    """db/publish.replace_tables copies every main table, so the hosted database serves the same ranges."""
    from app.db.publish import replace_tables

    path = _served(live, artifact)
    con = duckdb.connect()
    con.execute(f"ATTACH '{path}' AS src (READ_ONLY)")
    con.execute("ATTACH ':memory:' AS dst")
    con.execute("CREATE TABLE dst.main.schema_meta (key VARCHAR, value VARCHAR)")
    replace_tables(con, "src", "dst", "2026-10-20T00:00:00")
    con.execute("USE dst")
    assert pr.load(con) is not None and pr.load(con)["content_hash"] == artifact["content_hash"]


# ----------------------------------------------------------------------------- the load script


@pytest.fixture(scope="module")
def script():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("projection_ranges_script", SCRIPTS / "projection_ranges.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_script_copies_the_ledger_pin_once(tmp_path, script, artifact):
    from test_distribution_ledger import write_artifact

    import distribution_ledger as dl
    from datetime import datetime, timezone

    db = tmp_path / "live.duckdb"
    argv = ["load", "--ifp-dir", str(tmp_path / "models"), "--live-db", str(db)]
    assert script.main(argv) == script.EXIT_NOTHING                  # no pin yet
    path = write_artifact(tmp_path / "models", artifact)
    args = script.parse(argv)
    dl.write_pin(args, dl.make_pin(args, path, how="manual", reason="test", now=datetime.now(timezone.utc)))
    assert script.main(argv) == script.EXIT_OK
    assert script.main(argv) == script.EXIT_OK                       # second load: already loaded
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute(f"SELECT count(DISTINCT pin_id), min(content_hash) FROM {pr.TABLE}").fetchone() == (
        1, artifact["content_hash"])
    con.close()
    path.write_text(path.read_text().replace('"R@300"', '"R@301"', 1))   # tampered after the pin
    assert script.main(argv) == script.EXIT_FAILED


def test_serving_never_imports_the_research_module():
    """The hosted API has no research packages: serving ranges reads the stored table only."""
    code = ("import sys; import app.analytics.projection_ranges, app.api.projection_ranges; "
            "assert 'app.analytics.point_distributions' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True)
    src = (BACKEND / "app" / "analytics" / "projection_ranges.py").read_text()
    assert "import numpy" not in src and "polars" not in src

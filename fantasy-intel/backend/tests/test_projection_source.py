"""Projection source switch (app.analytics.projection_source): ESPN by default, the independent
model only when asked, with every number's origin labeled."""

from datetime import timedelta

import pytest

from app.analytics import projection_source as ps
from app.analytics.independent_projection_store import build_live, write_live
from app.analytics.start_sit import load_start_sit_evaluation
from app.db.connection import table_columns

from test_independent_projections import _artifact, live  # noqa: F401  (live is a fixture)
from test_matchup_api import CURRENT_WEEK, league, serve  # noqa: F401  (fixtures)


# ----------------------------------------------------------------------------- weekly()


@pytest.fixture
def stored(live):  # noqa: F811
    """A week-6 run of the synthetic league; expected points set as if participation had run."""
    con, src, path = live
    meta, rows = build_live(con, src, _artifact(src), 2026, 6)
    for r in rows:
        if r["coverage_status"] == "projected":
            r["expected_points"] = round(r["projected_points"] * 0.9, 2)
    write_live(con, meta, rows)
    return con


def test_espn_is_the_default_and_unchanged(live):  # noqa: F811
    con, _, _ = live
    w = ps.weekly(con, 2026, 6, ps.normalize(None))
    espn = dict(con.execute("SELECT espn_player_id, projected_points FROM espn_player_weekly_points "
                            "WHERE season = 2026 AND scoring_period = 6").fetchall())
    assert w["source"] == "espn" and w["available"] and w["values"] == espn
    assert set(w["origin"].values()) == {"espn"}
    with pytest.raises(ValueError):
        ps.normalize("vegas")


def test_independent_without_a_run_falls_back_to_espn_and_says_so(live):  # noqa: F811
    con, _, _ = live
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and "No independent projection run" in w["reason"]
    assert set(w["origin"].values()) == {"espn"}


def test_independent_values_and_origins(stored):
    w = ps.weekly(stored, 2026, 6, "independent")
    assert w["available"] and w["run"]["model_version"] == ps.MODEL_VERSION
    rows = {r[0]: r[1:] for r in stored.execute(
        "SELECT espn_player_id, coverage_status, expected_points FROM independent_projection_rows").fetchall()}
    espn = dict(stored.execute("SELECT espn_player_id, projected_points FROM espn_player_weekly_points").fetchall())
    assert w["values"][1] == rows[1][1] and w["origin"][1] == "ours"            # projected WR
    assert w["values"][4] == 0.0 and w["origin"][4] == "ours_zero"              # ESPN OUT: withheld
    assert w["values"][8] == 0.0 and w["origin"][8] == "ours_zero"              # bye
    assert w["values"][5] == espn[5] and w["origin"][5] == "espn"               # K: no independent model
    assert w["values"][7] == espn[7] and w["origin"][7] == "espn"               # rookie: abstains
    assert sum(w["counts"].values()) == len(w["origin"])


@pytest.mark.parametrize("column,value", [("model_version", "ifp-0.0.0"), ("scoring_fingerprint", "other")])
def test_a_run_of_another_model_or_scoring_is_not_used(stored, column, value):
    stored.execute(f"UPDATE independent_projection_runs SET {column} = ?", [value])
    assert ps.weekly(stored, 2026, 6, "independent")["available"] is False


def test_latest_run_is_shown_and_the_earlier_run_survives(live):  # noqa: F811
    con, src, _ = live
    art = _artifact(src)
    first, rows1 = build_live(con, src, art, 2026, 6)
    write_live(con, first, rows1)
    second, rows2 = build_live(con, src, art, 2026, 6)
    next(r for r in rows2 if r["espn_player_id"] == 1)["projected_points"] = 31.0
    next(r for r in rows2 if r["espn_player_id"] == 1)["expected_points"] = 31.0
    second["created_at"] = first["created_at"] + timedelta(minutes=5)
    write_live(con, second, rows2)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["run_id"] == second["run_id"] and w["values"][1] == 31.0 and w["origin"][1] == "ours"
    assert con.execute("SELECT count(*) FROM independent_projection_rows WHERE run_id = ?",
                       [first["run_id"]]).fetchone()[0] == len(rows1)
    # a newer run whose rows were lost is skipped, not shown as an all-ESPN forecast
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ?", [second["run_id"]])
    assert ps.weekly(con, 2026, 6, "independent")["run"]["run_id"] == first["run_id"]
    espn = ps.weekly(con, 2026, 6, "espn")
    assert espn["run"] is None and set(espn["origin"].values()) == {"espn"}      # ESPN stays the default


def test_unmigrated_database_is_read_from_the_legacy_table(stored):
    """A database or hosted copy that predates schema v8 has only the legacy table: read it, read-only."""
    con = stored
    ids = [r[0] for r in con.execute("SELECT run_id FROM independent_projection_runs").fetchall()]
    expected = ps.weekly(con, 2026, 6, "independent")
    cols = ", ".join(table_columns(con, "independent_player_projections"))
    con.execute(f"INSERT INTO independent_player_projections ({cols}) SELECT {cols} FROM independent_projection_rows")
    con.execute("DROP TABLE independent_projection_rows")
    legacy = ps.weekly(con, 2026, 6, "independent")
    assert legacy["available"] and legacy["run"]["run_id"] == ids[0]
    assert legacy["values"] == expected["values"] and legacy["origin"] == expected["origin"]


# ----------------------------------------------------------------------------- consumers

BUMP = 5.0


@pytest.fixture
def ours(monkeypatch):
    """Stand-in independent source: ESPN + 5 for every player, labeled ours (shape of weekly())."""
    real = ps.weekly

    def fake(con, season, week, source):
        w = real(con, season, week, "espn")
        if source == "espn":
            return w
        values = {pid: None if v is None else v + BUMP for pid, v in w["values"].items()}
        return {**w, "source": "independent", "values": values, "origin": {pid: "ours" for pid in values},
                "run": {"run_id": "r", "model_version": ps.MODEL_VERSION, "created_at": "t"},
                "model_status": {"QB": "validated"}, "counts": {"ours": len(values), "ours_zero": 0, "espn": 0}}
    monkeypatch.setattr(ps, "weekly", fake)


def test_start_sit_uses_the_selected_source(league, ours):  # noqa: F811
    _, con, _ = league
    espn = load_start_sit_evaluation(con)
    mine = load_start_sit_evaluation(con, source="independent")
    assert espn["projection_source"] == "ESPN" and espn["projection_source_detail"]["source"] == "espn"
    assert mine["projection_source"] == "Independent model" and mine["projection_source_detail"]["available"]
    base = {r["player_id"]: r["projected_points"] for r in espn["recommended_lineup"] + espn["current_bench"]}
    for r in mine["recommended_lineup"] + mine["current_bench"]:
        if r["player_id"] is not None and base.get(r["player_id"]) is not None:
            assert r["projected_points"] == pytest.approx(base[r["player_id"]] + BUMP)
            assert r["projection_origin"] == "ours"


def test_matchup_recomputes_lineups_from_the_selected_source(league, serve, ours):  # noqa: F811
    client = serve(league)
    espn = client.get("/api/matchup").json()
    mine = client.get("/api/matchup", params={"source": "independent"}).json()
    assert espn["projection_source"]["source"] == "espn"
    assert all("projection_origin" not in s for s in espn["me"]["lineup"])        # ESPN contract unchanged
    assert mine["projection_source"]["available"] and mine["week"] == CURRENT_WEEK
    filled = [s for s in mine["me"]["lineup"] if s["player_id"] is not None]
    assert filled and all(s["projection_origin"] == "ours" for s in filled)
    assert mine["me"]["projected_points"] == pytest.approx(espn["me"]["projected_points"] + BUMP * len(filled), abs=0.05)
    set_lineup = [p for p in mine["me"]["actual_lineup"] if p["projected_points"] is not None]
    assert set_lineup and all(p["projection_origin"] == "ours" for p in set_lineup)


def test_bad_source_is_a_422(league, serve):  # noqa: F811
    client = serve(league)
    assert client.get("/api/matchup", params={"source": "vegas"}).status_code == 422
    assert client.get("/api/start-sit", params={"source": "vegas"}).status_code == 422

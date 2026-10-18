"""Projection source switch (app.analytics.projection_source): ESPN by default, the independent
model only when asked, with every number's origin labeled."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.analytics import production_forecasts as pf
from app.analytics import production_pin
from app.analytics import projection_source as ps
from app.analytics.independent_projection_store import build_live, register_approved_artifact, write_live
from app.analytics.start_sit import load_start_sit_evaluation
from app.db.connection import table_columns

from test_independent_projections import _artifact, live  # noqa: F401  (live is a fixture)
from test_matchup_api import CURRENT_WEEK, league, serve  # noqa: F401  (fixtures)


PINNED = production_pin.APPROVED_ARTIFACT_SHA256   # the registry-pinned ifp-1.0.0 artifact a fallback run must use
WEEKLY_SHA = "b" * 64                     # an ifp-2.0.0 run's weekly artifact file digest (registered by _store)


# ----------------------------------------------------------------------------- weekly()


@pytest.fixture
def stored(live):  # noqa: F811
    """A week-6 run of the synthetic league; expected points set as if participation had run."""
    con, src, path = live
    meta, rows = build_live(con, src, _artifact(src), 2026, 6, artifact_sha256=PINNED)
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
    # the fixture run is the previous model's: shown, and labeled as the fallback
    assert w["available"] and w["run"]["model_version"] == ps.FALLBACK_VERSION
    assert ps.MODEL_VERSION in w["reason"] and ps.FALLBACK_VERSION in w["reason"]
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


def test_production_run_is_preferred_over_a_newer_fallback_run(live):  # noqa: F811
    """An ifp-2.0.0 run (ridges named "ifp2") is shown without a fallback note, even when an ifp-1.0.0
    run of the same week is newer."""
    con, src, _ = live
    art = _artifact(src)
    prod = replace(art, version=ps.MODEL_VERSION,
                   models={p: replace(m, model="ifp2") for p, m in art.models.items()})
    meta2, rows2 = build_live(con, src, prod, 2026, 6, artifact_sha256="d" * 64)
    assert {r["model"] for r in rows2 if r["coverage_status"] == "projected"} == {"ifp2_ridge"}
    for r in rows2:
        if r["coverage_status"] == "projected":
            r["expected_points"] = 30.0
    write_live(con, meta2, rows2)
    register_approved_artifact(con, model_version=ps.MODEL_VERSION, artifact_sha256="d" * 64, target_season=2026,
                               target_week=6, source="learning_cycle")
    meta1, rows1 = build_live(con, src, art, 2026, 6, artifact_sha256=PINNED)
    meta1["created_at"] = meta2["created_at"] + timedelta(minutes=5)
    write_live(con, meta1, rows1)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["run_id"] == meta2["run_id"] and w["run"]["model_version"] == ps.MODEL_VERSION
    assert w["reason"] is None and w["values"][1] == 30.0 and w["origin"][1] == "ours"


def test_latest_run_is_shown_and_the_earlier_run_survives(live):  # noqa: F811
    con, src, _ = live
    art = _artifact(src)
    first, rows1 = build_live(con, src, art, 2026, 6, artifact_sha256=PINNED)
    write_live(con, first, rows1)
    second, rows2 = build_live(con, src, art, 2026, 6, artifact_sha256=PINNED)
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


def test_unmigrated_database_fails_closed(stored):
    """A database or copy that predates schema v8 has only the legacy table, whose rows cannot be checked against
    a content hash: nothing is shown as an independent projection, and the reason says so."""
    con = stored
    assert ps.weekly(con, 2026, 6, "independent")["available"]
    cols = ", ".join(table_columns(con, "independent_player_projections"))
    con.execute(f"INSERT INTO independent_player_projections ({cols}) SELECT {cols} FROM independent_projection_rows")
    con.execute("DROP TABLE independent_projection_rows")
    legacy = ps.weekly(con, 2026, 6, "independent")
    assert legacy["available"] is False and "schema v8" in legacy["reason"]
    assert set(legacy["origin"].values()) == {"espn"}


# ----------------------------------------------------------------------------- approved artifacts

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64


def _runs(con, src, version, specs, week=6):
    """Runs of `version` for the week, oldest first: specs = [(artifact sha, WR 1 points, minutes after the first)]."""
    out = []
    for sha, pts, minutes in specs:
        art = replace(_artifact(src), version=version)
        meta, rows = build_live(con, src, art, 2026, week, artifact_sha256=sha)
        row = next(r for r in rows if r["espn_player_id"] == 1)
        row["projected_points"] = row["expected_points"] = pts
        meta["created_at"] = meta["created_at"] + timedelta(minutes=minutes)
        write_live(con, meta, rows)
        out.append(meta)
    return out


def _approve(con, meta, source="learning_cycle", week=...):
    register_approved_artifact(con, model_version=meta["model_version"], artifact_sha256=meta["artifact_id"],
                               target_season=meta["season"], target_week=meta["week"] if week is ... else week,
                               source=source)


def test_unpinned_fallback_model_serves_the_newest_run_and_the_shape_is_unchanged(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", None)    # the committed pin is set; unset it here
    _, newer = _runs(con, src, ps.FALLBACK_VERSION, [(SHA_A, 21.0, 0), (SHA_B, 31.0, 5)])
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == newer["run_id"] and w["values"][1] == 31.0
    # no artifact_verified; the horizon fields are the only addition to the independent run object
    assert set(w["run"]) == {"run_id", "model_version", "created_at", "forecast_horizon", "projected_from_week",
                             "horizon_note"}
    assert w["run"]["forecast_horizon"] == 0 and w["run"]["horizon_note"] is None


def test_a_pinned_fallback_artifact_serves_only_its_own_runs(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    older, _ = _runs(con, src, ps.FALLBACK_VERSION, [(SHA_A, 21.0, 0), (SHA_B, 31.0, 5)])
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", SHA_A)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == older["run_id"] and w["run"]["artifact_verified"] is True
    assert w["values"][1] == 21.0 and w["origin"][1] == "ours"       # the newer run of artifact B is ignored
    assert ps.describe(w)["run"]["artifact_verified"] is True
    assert ps.weekly(con, 2026, 6, "espn")["run"] is None             # ESPN stays the default and is unchanged
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", SHA_C)    # neither stored run used artifact C
    espn = ps.weekly(con, 2026, 6, "espn")
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and w["run"] is None and w["counts"] is None
    assert "No independent projection run" in w["reason"] and "not approved" in w["reason"]
    assert "ESPN projections are shown" in w["reason"]
    assert w["values"] == espn["values"] and set(w["origin"].values()) == {"espn"}


def test_a_run_without_a_recorded_artifact_is_not_the_pinned_artifact(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    _runs(con, src, ps.FALLBACK_VERSION, [(SHA_A, 21.0, 0)])
    con.execute("UPDATE independent_projection_runs SET artifact_id = NULL")
    monkeypatch.setattr(production_pin, "APPROVED_ARTIFACT_SHA256", SHA_A)
    assert ps.weekly(con, 2026, 6, "independent")["available"] is False


def test_a_registered_production_artifact_is_served_and_an_unregistered_newer_one_is_not(live):  # noqa: F811
    con, src, _ = live
    older, newer = _runs(con, src, ps.MODEL_VERSION, [(SHA_A, 21.0, 0), (SHA_B, 31.0, 5)])
    _approve(con, older)                                  # the cycle registered A; B was written by hand
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == older["run_id"] and w["values"][1] == 21.0
    assert w["run"]["artifact_verified"] is True and w["reason"] is None
    _approve(con, newer, "manual_pin")                    # once B is registered too, the newest run wins again
    assert ps.weekly(con, 2026, 6, "independent")["run"]["run_id"] == newer["run_id"]


def test_an_unregistered_production_artifact_falls_back_per_the_existing_rules(live):  # noqa: F811
    con, src, _ = live
    [fallback] = _runs(con, src, ps.FALLBACK_VERSION, [(PINNED, 11.0, 0)])
    [hand_made] = _runs(con, src, ps.MODEL_VERSION, [(SHA_A, 99.0, 5)])
    register_approved_artifact(con, model_version=ps.MODEL_VERSION, artifact_sha256=SHA_C, target_season=2026,
                               target_week=6, source="learning_cycle")        # enforcement starts with a first row
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["run_id"] == fallback["run_id"] and w["values"][1] == 11.0           # ifp-1.0.0 fallback
    assert ps.MODEL_VERSION in w["reason"] and ps.FALLBACK_VERSION in w["reason"]
    con.execute("DELETE FROM independent_projection_rows WHERE run_id = ?", [fallback["run_id"]])
    w = ps.weekly(con, 2026, 6, "independent")                 # nothing approved is left: ESPN, with a reason
    assert w["available"] is False and w["run"] is None and "not approved" in w["reason"]
    # never shown: it appears only in the list of skipped runs, with the approval reason
    assert w["values"][1] != 99.0 and [k["reason"] for k in w["skipped"] if k["run_id"] == hand_made["run_id"]] == \
        ["artifact not registered as approved"]


def test_an_artifact_registered_for_another_week_is_not_approved_for_this_one(live):  # noqa: F811
    con, src, _ = live
    [run] = _runs(con, src, ps.MODEL_VERSION, [(SHA_A, 21.0, 0)])
    _approve(con, run, week=7)
    assert ps.weekly(con, 2026, 6, "independent")["available"] is False
    # the table is write-once: a later registration of the same artifact does not widen it to this week
    register_approved_artifact(con, model_version=ps.MODEL_VERSION, artifact_sha256=SHA_A, target_season=None,
                               target_week=None, source="manual_pin")
    assert ps.weekly(con, 2026, 6, "independent")["available"] is False
    [other] = _runs(con, src, ps.MODEL_VERSION, [(SHA_B, 41.0, 9)])
    register_approved_artifact(con, model_version=ps.MODEL_VERSION, artifact_sha256=SHA_B, target_season=None,
                               target_week=None, source="manual_pin")         # recorded without a target: any week
    assert ps.weekly(con, 2026, 6, "independent")["run"]["run_id"] == other["run_id"]


def test_without_the_table_the_view_fails_open_and_says_unverified(live):  # noqa: F811
    """An old local database or hosted copy has no approved_artifacts: behave as before, but say so."""
    con, src, _ = live
    [run] = _runs(con, src, ps.MODEL_VERSION, [(SHA_A, 21.0, 0)])
    w = ps.weekly(con, 2026, 6, "independent")           # table present but no row for the model yet: also unverified
    assert w["available"] and w["run"]["artifact_verified"] is False and "could not be verified" in w["reason"]
    con.execute("DROP TABLE approved_artifacts")
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == run["run_id"] and w["values"][1] == 21.0
    assert w["run"]["artifact_verified"] is False and "could not be verified" in w["reason"]


def test_the_pin_module_is_a_leaf_that_imports_nothing():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(production_pin))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


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


# ----------------------------------------------------------------------------- consumer selection: integrity,
# approval, fallback, live vs evidence (projection_source.select_run)

ESPN_KEYS = {"source", "available", "reason", "values", "origin", "run", "model_status", "counts"}


def _ifp2(src, fit="2026-10-14T00:00:00+00:00"):
    art = _artifact(src, created_at=fit)
    return replace(art, version=ps.MODEL_VERSION, models={p: replace(m, model="ifp2") for p, m in art.models.items()})


def _store(con, src, art, *, sha, created=None, points=None, register=True):
    """Store a week-6 run; a production (ifp-2.0.0) run's artifact is registered as the learning cycle would."""
    meta, rows = build_live(con, src, art, 2026, 6, artifact_sha256=sha)
    if register and art.version == ps.MODEL_VERSION:
        register_approved_artifact(con, model_version=art.version, artifact_sha256=meta["artifact_id"],
                                   target_season=2026, target_week=6, source="learning_cycle")
    if created is not None:
        meta["created_at"] = created
    for r in rows:
        if r["coverage_status"] == "projected":
            r["expected_points"] = round(r["projected_points"] * 0.9, 2)
    if points is not None:
        one = next(r for r in rows if r["espn_player_id"] == 1)
        one["projected_points"] = one["expected_points"] = points
    write_live(con, meta, rows)
    return meta


def test_espn_default_is_unchanged_in_shape_and_values(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    w = ps.weekly(con, 2026, 6, "espn")
    assert set(w) == ESPN_KEYS and w["run"] is None and set(w["origin"].values()) == {"espn"}
    assert ps.describe(w) == {k: w[k] for k in ("source", "available", "reason", "run", "model_status", "counts")}
    assert ps.normalize(None) == "espn"


def test_valid_production_run_carries_honest_governance(live):  # noqa: F811
    con, src, _ = live
    meta = _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == meta["run_id"] and w["reason"] is None
    g = w["governance"]
    assert g["served_version"] == ps.MODEL_VERSION and g["is_fallback"] is False
    assert g["production_approval"] == "owner_override" and g["espn_superiority_established"] is False
    assert g["start_sit_ready"] is False and "Historical research only" in g["production_evidence"]
    # the run's stored status was frozen at training time; consumers see the override, never "validated"/"shadow"
    assert w["model_status"] == {p: ps.OVERRIDE_STATUS for p in ("QB", "RB", "WR", "TE")}
    assert "owner override" in ps.NOTE and "not shown to be better than ESPN" in ps.NOTE


def test_valid_fallback_is_labeled_and_needs_the_pinned_artifact(live):  # noqa: F811
    con, src, _ = live
    _store(con, src, _artifact(src), sha="d" * 64, points=21.0)           # ifp-1 label, but other (refit) bytes
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and "artifact not the approved production artifact" in w["reason"]
    assert w["skipped"][0]["model_version"] == ps.FALLBACK_VERSION
    ok = _store(con, src, _artifact(src), sha=PINNED, created=datetime(2026, 9, 1), points=22.0)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] and w["run"]["run_id"] == ok["run_id"] and w["values"][1] == 22.0
    assert w["governance"]["is_fallback"] is True and "fallback" in w["reason"] and "not the production model" in w["reason"]


@pytest.mark.parametrize("sha", [None, "e" * 64])
def test_production_run_of_an_unregistered_artifact_is_not_shown(live, sha):  # noqa: F811
    """Once approved_artifacts has a row for ifp-2.0.0, a run whose artifact is not registered (None: build_live
    stamps an in-memory "dict:" digest) is skipped with the approval reason, never shown."""
    con, src, _ = live
    register_approved_artifact(con, model_version=ps.MODEL_VERSION, artifact_sha256=WEEKLY_SHA, target_season=2026,
                               target_week=6, source="learning_cycle")
    _store(con, src, _ifp2(src), sha=sha, register=False)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and w["skipped"][0]["reason"].startswith("artifact not registered as approved")
    assert "not approved" in w["reason"]


def test_same_row_count_tampering_is_skipped_by_consumers(live):  # noqa: F811
    con, src, _ = live
    early = _store(con, src, _ifp2(src), sha=WEEKLY_SHA, created=datetime(2026, 9, 1), points=10.0)
    late = _store(con, src, _ifp2(src), sha=WEEKLY_SHA, created=datetime(2026, 9, 2), points=11.0)
    con.execute("UPDATE independent_projection_rows SET expected_points = 99 WHERE run_id = ? AND espn_player_id = 1",
                [late["run_id"]])
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["run_id"] == early["run_id"] and w["values"][1] == 10.0
    assert w["skipped"] == [{"run_id": late["run_id"], "model_version": ps.MODEL_VERSION, "created_at": str(late["created_at"]),
                             "reason": "integrity: stored rows do not match the run's content_sha256"}]


@pytest.mark.parametrize("damage,needle", [
    ("UPDATE independent_projection_runs SET content_sha256 = NULL", "no content hash"),
    ("DELETE FROM independent_projection_rows WHERE espn_player_id = 1", "row_count"),
    ("DELETE FROM independent_projection_rows", "forecast rows missing"),
])
def test_unsealed_short_or_empty_runs_are_never_shown(live, damage, needle):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    con.execute(damage)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and needle in w["reason"] and set(w["origin"].values()) == {"espn"}


def test_unsupported_scoring_shows_espn_and_says_why(live, monkeypatch):  # noqa: F811
    con, src, _ = live
    _store(con, src, _ifp2(src), sha=WEEKLY_SHA)
    monkeypatch.setattr(ps, "_scoring", lambda con, season: (None, "the league's scoring is not supported by the "
                                                                   "independent model"))
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["available"] is False and "not supported" in w["reason"]


def test_late_update_is_live_but_never_rewrites_or_becomes_pregame_evidence(live):  # noqa: F811
    """Mixed kickoffs: AAA plays Thursday, everyone else Sunday. A run made between them is the live view for
    everyone, but only its Sunday rows can be evidence; the Thursday player's frozen pregame row stays the
    earlier run's, byte for byte."""
    from test_production_forecasts import BEFORE_ALL, BETWEEN, FIT, thursday
    from app.analytics.production_forecasts import pregame_forecasts

    con, src, _ = live
    thursday(con)
    early = _store(con, src, _ifp2(src, FIT), sha=WEEKLY_SHA, created=BEFORE_ALL, points=10.0)
    frozen = con.execute("SELECT * FROM independent_projection_rows WHERE run_id = ? ORDER BY ALL",
                         [early["run_id"]]).fetchall()
    late = _store(con, src, _ifp2(src, FIT), sha=WEEKLY_SHA, created=BETWEEN, points=12.0)
    w = ps.weekly(con, 2026, 6, "independent")
    assert w["run"]["run_id"] == late["run_id"] and w["values"][1] == 12.0          # live view: newest verified run
    assert w["timing"]["rows_not_pregame"] >= 1 and w["timing"]["rows_pregame"] >= 1
    rows, _ = pregame_forecasts(con, 2026, [6], model_version=ps.MODEL_VERSION,
                                scoring_fingerprint=ps._scoring_fingerprint(con, 2026))
    by = {r["espn_player_id"]: r for r in rows}
    assert by[1]["run_id"] == early["run_id"] and by[1]["projected_points"] == 10.0  # Thursday: the pregame forecast
    assert by[6]["run_id"] == late["run_id"]                                        # Sunday: the later pregame run
    assert con.execute("SELECT * FROM independent_projection_rows WHERE run_id = ? ORDER BY ALL",
                       [early["run_id"]]).fetchall() == frozen


def test_hosted_selection_needs_no_research_packages():
    """The consumer selection, the independent API and the promotion endpoint import with polars, numpy, sklearn
    and scipy unavailable (Vercel's bundle), and the endpoint still reports governance there."""
    import subprocess
    import sys
    from pathlib import Path

    code = ("import sys\nfor m in ('polars', 'numpy', 'sklearn', 'scipy'):\n    sys.modules[m] = None\n"
            "from app.analytics import projection_source as ps\nimport app.api.independent_projections\n"
            "from app.api.projection_promotion import projection_promotion\n"
            "body = projection_promotion()\n"
            "assert body['available'] is False and body['governance']['production_approval'] == 'owner_override'\n"
            "assert body['governance']['espn_superiority_established'] is False\n"
            "assert 'research packages' in body['unavailable_reason']\n")
    subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], check=True)


# ----------------------------------------------------------------------------- rest-of-season runs in the views


def _fake_selection(monkeypatch, con, *, horizon, from_week, week):
    """Make select_run return a verified run of the given horizon with an ours value for every ESPN-projected player."""
    from datetime import datetime

    season = con.execute("SELECT max(season) FROM leagues").fetchone()[0]
    espn = ps._espn(con, season, week)
    run = {"run_id": f"ros-{horizon}", "season": season, "week": week, "model_version": ps.MODEL_VERSION,
           "created_at": datetime(2026, 9, 1), "model_status": {}, "artifact_check": "verified",
           "artifact_note": None, "forecast_horizon": horizon, "projected_from_week": from_week,
           "horizon": horizon, "horizon_note": pf.horizon_note({"forecast_horizon": horizon})}
    rows = [{"espn_player_id": pid, "coverage_status": "projected", "expected_points": (v or 0.0) + BUMP, "nfl_team": None}
            for pid, v in espn.items()]
    sel = {"run": run, "rows": rows, "fallback": False, "scoring_fingerprint": "fp", "skipped": [],
           "unavailable_reason": None}
    monkeypatch.setattr(ps, "select_run", lambda con_, season_, week_, **kw: sel)
    return run


@pytest.mark.parametrize("horizon", [0, 3])
def test_the_run_a_view_reports_carries_its_horizon_and_experimental_note(league, serve, monkeypatch, horizon):  # noqa: F811
    _, con, _ = league
    season = con.execute("SELECT max(season) FROM leagues").fetchone()[0]
    _fake_selection(monkeypatch, con, horizon=horizon, from_week=CURRENT_WEEK - horizon, week=CURRENT_WEEK)
    note = f"rest-of-season forecast (horizon {horizon}): experimental, not validated beyond the next week"
    w = ps.weekly(con, season, CURRENT_WEEK, "independent")
    expected = {"forecast_horizon": horizon, "projected_from_week": CURRENT_WEEK - horizon,
                "horizon_note": note if horizon else None}
    assert {k: w["run"][k] for k in expected} == expected
    assert {k: ps.describe(w)["run"][k] for k in expected} == expected
    client = serve(league)
    for path in ("/api/matchup", "/api/start-sit"):
        body = client.get(path, params={"source": "independent"}).json()
        detail = body.get("projection_source") if path == "/api/matchup" else body["projection_source_detail"]
        assert detail["available"] and {k: detail["run"][k] for k in expected} == expected, path


def test_the_espn_response_is_unchanged_by_the_horizon_fields(league, serve):  # noqa: F811
    client = serve(league)
    for path, key in (("/api/matchup", "projection_source"), ("/api/start-sit", "projection_source_detail")):
        detail = client.get(path).json()[key]
        assert detail["source"] == "espn" and detail["run"] is None
        assert "forecast_horizon" not in detail and "horizon_note" not in detail


# ----------------------------------------------------------------------------- kicker overlay (kfp)


def _kicker_run(con, pid: int, status: str = "projected", expected: float | None = 7.3):
    import json as _json

    from app.analytics import kicker_store as ks
    from app.analytics.kicker_scoring import resolve_kicker_scoring

    items = _json.loads(con.execute("SELECT scoring_items FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()[0])
    items += [{"statId": 80, "points": 3.0}, {"statId": 77, "points": 4.0}, {"statId": 198, "points": 5.0},
              {"statId": 201, "points": 6.0}, {"statId": 85, "points": -1.0}, {"statId": 86, "points": 1.0}]
    con.execute("UPDATE leagues SET scoring_items = ?", [_json.dumps(items)])
    meta = {"run_id": "kfp-test", "season": 2026, "week": 6, "model_version": "kfp-1.0.0",
            "scoring_fingerprint": resolve_kicker_scoring(items).fingerprint, "artifact_id": "c" * 64,
            "forecast_horizon": 0, "created_at": datetime(2026, 10, 10)}
    row = {"season": 2026, "week": 6, "espn_player_id": pid, "gsis_id": "K", "nfl_team": "AAA", "coverage_status": status,
           "projected_points": expected, "p_play": 1.0 if expected is not None else None, "expected_points": expected}
    ks.write_run(con, meta, [row])
    return items


def test_kicker_overlay_fills_only_kickers_and_leaves_the_skill_run_untouched(stored):
    before = ps.weekly(stored, 2026, 6, "independent")
    assert before["origin"][5] == "espn" and before["kicker_run"] is None
    run = stored.execute("SELECT run_id, content_sha256 FROM independent_projection_runs").fetchall()
    _kicker_run(stored, 5)
    after = ps.weekly(stored, 2026, 6, "independent")
    assert after["origin"][5] == "ours" and after["values"][5] == 7.3
    assert after["kicker_run"]["run_id"] == "kfp-test" and after["kicker_run"]["model_version"] == "kfp-1.0.0"
    others = {k: v for k, v in after["values"].items() if k != 5}
    assert others == {k: v for k, v in before["values"].items() if k != 5}          # every skill value unchanged
    assert {k: v for k, v in after["origin"].items() if k != 5} == {k: v for k, v in before["origin"].items() if k != 5}
    assert after["run"] == before["run"]
    assert stored.execute("SELECT run_id, content_sha256 FROM independent_projection_runs").fetchall() == run
    assert pf.latest_run(stored, 2026, 6) is not None                                 # the ifp run still verifies
    assert ps.weekly(stored, 2026, 6, "espn")["origin"][5] == "espn"                  # ESPN mode never overlays
    assert "kicker_run" in ps.describe(after)


def test_kicker_overlay_never_replaces_a_skill_estimate_and_needs_kicker_scoring(stored):
    before = ps.weekly(stored, 2026, 6, "independent")
    _kicker_run(stored, 1, expected=99.0)            # a kfp row for a player the skill run projects
    assert ps.weekly(stored, 2026, 6, "independent")["values"][1] == before["values"][1]
    from app.analytics import kicker_store as ks

    items = [i for i in __import__("json").loads(stored.execute("SELECT scoring_items FROM leagues").fetchone()[0])
             if i["statId"] not in (80, 77, 198, 201, 85, 86)]
    stored.execute("UPDATE leagues SET scoring_items = ?", [__import__("json").dumps(items)])   # no kicker rules
    w = ps.weekly(stored, 2026, 6, "independent")
    assert w["available"] and w["kicker_run"] is None and w["origin"][5] == "espn"
    assert "kicker scoring" in ks.select_run(stored, 2026, 6)["unavailable_reason"]


def test_the_independent_api_rebuilds_every_comparison_field_for_an_overlaid_kicker(stored):
    """Codex review on #96: the K row's comparison was built from the unsupported ifp row (no estimate) and only the
    expected-points fields were updated, so independent_minus_espn/comparable/evidence contradicted the overlay."""
    from app.api import independent_projections as ip

    _kicker_run(stored, 5)
    k = next(p for p in ip.build_response(stored, 6, None, None)["players"] if p["espn_player_id"] == 5)
    espn, c = k["espn"]["projected_points"], k["comparison"]
    assert k["independent"]["projected_points"] == 7.3 and espn is not None
    assert c["independent_minus_espn"] == round(7.3 - espn, 2) and c["expected_minus_espn"] == round(7.3 - espn, 2)
    assert c["comparable"] is (espn != 0) and c["expected_comparable"] is True
    assert c["comparability_note"] is None or espn == 0

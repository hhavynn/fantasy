"""Forecast inputs (#225): a projected row's full point-model inputs, stored beside its run (production_forecasts
"forecast inputs"). They must rebuild the stored point, never change a run's seal, stay write-once, sit only beside
the row they were captured for, and be ignored the moment a digest or the rebuild fails."""
import numpy as np
import polars as pl

from app.analytics import production_forecasts as pf
from app.analytics.independent_projection_store import build_live, write_run
from app.analytics.independent_projections import RidgeModel
from app.db.connection import fetch_dicts

from test_independent_projections import _artifact, live  # noqa: F401  (live is a fixture)

SHA = "c" * 64


def _rows(con, run_id):
    return {run_id: {int(r["espn_player_id"]): r for r in fetch_dicts(
        con, f"SELECT * FROM {pf.ROWS} WHERE run_id = ?", [run_id])}}


def _build(con, src):
    captured: dict = {}
    meta, rows = build_live(con, src, _artifact(src), 2026, 6, artifact_sha256=SHA, inputs_out=captured)
    return meta, rows, captured


def _verified(con, run_id):
    rows = _rows(con, run_id)
    return pf.verified_inputs(con, [run_id], sorted(rows[run_id]), rows), rows


def test_captured_inputs_rebuild_every_projected_point(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    stored = write_run(con, meta, rows, inputs=captured)
    got, back = _verified(con, stored["run_id"])
    projected = {pid for pid, r in back[stored["run_id"]].items() if r["coverage_status"] == "projected"}
    assert projected and set(pid for _, pid in got) == projected
    for (_, pid), v in got.items():
        assert abs(v["rebuilt"]["points"] - back[stored["run_id"]][pid]["projected_points"]) <= pf.REPRODUCE_TOLERANCE
        assert len(v["inputs"]["input_values"]) == len(v["model"]["columns"])
    # one model per (artifact, position, kind), shared by every row that used it
    n = con.execute(f"SELECT count(*), count(DISTINCT position) FROM {pf.MODELS}").fetchone()
    assert n[0] == n[1]


def test_inputs_never_change_the_seal_and_are_write_once(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    plain = pf.content_sha256(meta, rows)
    first = write_run(con, meta, rows, inputs=captured)
    assert first["content_sha256"] == plain
    n = con.execute(f"SELECT count(*) FROM {pf.INPUTS}").fetchone()[0]
    again = write_run(con, meta, rows, inputs=captured)                  # the very same run: nothing written
    assert (again["created"], again["run_id"]) == (False, first["run_id"])
    assert con.execute(f"SELECT count(*) FROM {pf.INPUTS}").fetchone()[0] == n
    # a run stored without inputs keeps its earlier behaviour: none stored, none read
    m2, r2, _ = _build(con, src)
    r2 = [{**r, "flags": [*r["flags"], "x"]} if r["coverage_status"] == "projected" else r for r in r2]
    other = write_run(con, m2, r2)
    assert con.execute(f"SELECT count(*) FROM {pf.INPUTS} WHERE run_id = ?", [other["run_id"]]).fetchone()[0] == 0


def test_a_row_changed_after_capture_keeps_no_inputs(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    target = next(r for r in rows if r["coverage_status"] == "projected")
    rows = [{**r, "projected_points": r["projected_points"] + 1} if r is target else r for r in rows]
    stored = write_run(con, meta, rows, inputs=captured)
    got, _ = _verified(con, stored["run_id"])
    assert (stored["run_id"], target["espn_player_id"]) not in got and got


def test_tampered_inputs_or_models_are_ignored(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    stored = write_run(con, meta, rows, inputs=captured)
    got, _ = _verified(con, stored["run_id"])
    pid = sorted(pid for _, pid in got)[0]
    con.execute(f"UPDATE {pf.INPUTS} SET input_values = list_transform(input_values, x -> x + 1) "
                "WHERE espn_player_id = ?", [pid])
    after, _ = _verified(con, stored["run_id"])
    assert (stored["run_id"], pid) not in after and len(after) == len(got) - 1
    con.execute(f"UPDATE {pf.MODELS} SET intercept = intercept + 0.5")
    assert _verified(con, stored["run_id"])[0] == {}


def test_inputs_that_do_not_rebuild_the_point_are_ignored_even_with_valid_digests(live):  # noqa: F811
    con, src, _ = live
    meta, rows, captured = _build(con, src)
    stored = write_run(con, meta, rows, inputs=captured)
    got, back = _verified(con, stored["run_id"])
    pid = sorted(pid for _, pid in got)[0]
    con.execute(f"DELETE FROM {pf.INPUTS} WHERE espn_player_id = ?", [pid])
    inp = dict(got[(stored["run_id"], pid)]["inputs"])
    inp["input_values"] = [v * 1.5 for v in inp["input_values"]]       # re-sealed, but no longer the forecast's inputs
    pf.write_inputs(con, stored["run_id"], [{k: v for k, v in inp.items() if k not in ("run_id", "inputs_digest")}])
    assert (stored["run_id"], pid) not in _verified(con, stored["run_id"])[0]


def test_the_quarterback_mixture_is_rebuilt_from_the_start_share():
    m = RidgeModel(position="QB", model="ifp22", features=["s_start", "pass_att_pg"], columns=["s_start", "pass_att_pg"],
                   means=[0.6, 30.0], stds=[0.4, 6.0], coef=[4.0, 2.5], intercept=12.0, lam=1.0, n_train=100)
    rec = {"artifact_id": "a", "position": "QB", "model_kind": "main", "model_version": "v", "columns": m.columns,
           "means": m.means, "stds": m.stds, "coef": m.coef, "intercept": m.intercept}
    for pi, att in ((0.8, 34.0), (0.1, 5.0), (0.5, 1.0)):
        f1 = m.predict(pl.DataFrame({"s_start": [1.0], "pass_att_pg": [att]}))[0]
        f0 = m.predict(pl.DataFrame({"s_start": [0.0], "pass_att_pg": [att]}))[0]
        out = pf.reproduce(rec, {"input_values": [pi, att], "imputed": [False, False], "mixture_column": "s_start"})
        assert np.isclose(out["points"], pi * f1 + (1 - pi) * f0)
        # linear in the share exactly when neither scenario is floored
        assert out["floored"] == (min(m.predict(pl.DataFrame({"s_start": [1.0, 0.0], "pass_att_pg": [att, att]}))) <= 0
                                  and (f1 == 0 or f0 == 0))


def test_league_builds_copy_the_inputs_and_rescored_runs_still_rebuild_from_them(live):  # noqa: F811
    import json

    from app.analytics.rescore import apply_reference
    from test_rescore import HALF, _publish_reference

    con, src, _ = live
    meta, rows, captured = _build(con, src)
    stored = write_run(con, meta, rows, inputs=captured)
    ref = _verified(con, stored["run_id"])[0]
    _publish_reference(con)
    apply_reference(con, 2026)                               # the reference scoring: the run and its inputs verbatim
    assert _verified(con, stored["run_id"])[0].keys() == ref.keys()
    con.execute("UPDATE leagues SET scoring_items = ?", [json.dumps(HALF)])
    apply_reference(con, 2026)                               # another scoring: re-scored, the inputs carry the factor
    rid = con.execute(f"SELECT run_id FROM {pf.RUNS}").fetchone()[0]
    assert rid != stored["run_id"]
    got, back = _verified(con, rid)
    rescored = {pid for pid, r in back[rid].items() if r["coverage_status"] == "projected"}
    assert rescored and {pid for _, pid in got} == rescored
    assert any(v["inputs"]["scale"] not in (None, 1.0) for v in got.values())
    assert all(v["inputs"]["input_values"] == ref[(stored["run_id"], pid)]["inputs"]["input_values"]
               for (_, pid), v in got.items())


def _cycle(con, src, weeks=(6, 7, 8)):
    """A rest-of-season cycle with cutoff week 6: one stored run per target week, each with its inputs."""
    art, ids = _artifact(src), {}
    for w in weeks:
        captured: dict = {}
        meta, rows = build_live(con, src, art, 2026, w, artifact_sha256=SHA, projected_from_week=6, inputs_out=captured)
        ids[w] = write_run(con, meta, rows, inputs=captured)["run_id"]
    return ids


def test_later_target_weeks_store_only_their_changes_and_still_rebuild(live):  # noqa: F811
    con, src, _ = live
    ids = _cycle(con, src)
    stored = fetch_dicts(con, f"SELECT run_id, base_run_id, changed, len(input_values) AS n FROM {pf.INPUTS}")
    whole = [r for r in stored if r["base_run_id"] is None]
    deltas = [r for r in stored if r["base_run_id"] is not None]
    assert {r["run_id"] for r in whole} >= {ids[6]} and deltas
    whole_keys = {(r["run_id"]) for r in whole}
    # a delta names an earlier run of the same cycle that stored this player's whole row
    assert all(r["base_run_id"] in whole_keys and r["base_run_id"] != r["run_id"] and r["n"] == len(r["changed"])
               for r in deltas)
    assert any(r["base_run_id"] == ids[6] for r in deltas)
    for w, rid in ids.items():
        got, back = _verified(con, rid)
        projected = {pid for pid, r in back[rid].items() if r["coverage_status"] == "projected"}
        assert {pid for _, pid in got} == projected, w
        assert all(len(v["inputs"]["input_values"]) == len(v["model"]["columns"]) for v in got.values())
    ncols = con.execute(f"SELECT max(len(columns)) FROM {pf.MODELS}").fetchone()[0]
    assert max(r["n"] for r in deltas) <= pf.DELTA_MAX_SHARE * ncols


def test_a_changed_base_row_invalidates_the_rows_stored_against_it(live):  # noqa: F811
    con, src, _ = live
    ids = _cycle(con, src)
    before = _verified(con, ids[7])[0]
    pid = next(pid for _, pid in before if con.execute(
        f"SELECT base_run_id FROM {pf.INPUTS} WHERE run_id = ? AND espn_player_id = ?", [ids[7], pid]).fetchone()[0])
    con.execute(f"UPDATE {pf.INPUTS} SET input_values = list_transform(input_values, x -> x + 0.5) "
                "WHERE run_id = ? AND espn_player_id = ?", [ids[6], pid])
    after = _verified(con, ids[7])[0]
    assert (ids[7], pid) not in after and len(after) == len(before) - 1


def test_rescored_cycles_keep_their_changes_against_the_rescored_base(live):  # noqa: F811
    import json

    from app.analytics.rescore import apply_reference
    from test_rescore import HALF, _publish_reference

    con, src, _ = live
    ids = _cycle(con, src)
    _publish_reference(con)
    con.execute("UPDATE leagues SET scoring_items = ?", [json.dumps(HALF)])
    apply_reference(con, 2026)
    derived = {r[0]: r[1] for r in con.execute(f"SELECT week, run_id FROM {pf.RUNS}").fetchall()}
    assert derived[7] != ids[7] and derived[7].startswith(ids[7])
    bases = {r[0] for r in con.execute(f"SELECT base_run_id FROM {pf.INPUTS} WHERE run_id = ? AND base_run_id IS NOT NULL",
                                       [derived[7]]).fetchall()}
    assert bases == {derived[6]}
    got, back = _verified(con, derived[7])
    assert {pid for _, pid in got} == {pid for pid, r in back[derived[7]].items() if r["coverage_status"] == "projected"}


def test_capture_follows_the_production_qb_mixture_and_its_fallback():
    """ifp-2.2.0: a QB with a start candidate is the start/no-start mixture (context_model.mix_points), one without goes
    through the fallback model; the captured inputs rebuild both exactly as predict_frame applied them."""
    from types import SimpleNamespace

    from app.analytics import context_model as cm
    from app.analytics.independent_projection_store import capture_inputs

    main = RidgeModel(position="QB", model="ifp22", features=["s_start", "pass_att_pg"],
                      columns=["s_start", "pass_att_pg", "pass_att_pg__missing"], means=[0.6, 30.0, 0.1],
                      stds=[0.4, 6.0, 0.3], coef=[4.0, 2.5, -1.0], intercept=14.0, lam=1.0, n_train=100)
    fb = RidgeModel(position="QB", model="ifp22", features=["pass_att_pg"], columns=["pass_att_pg"], means=[29.0],
                    stds=[7.0], coef=[3.0], intercept=13.0, lam=1.0, n_train=100)
    art = SimpleNamespace(version="ifp-2.2.0", models={"QB": main},
                          config={cm.CONFIG_KEY: {"components": {"QB": list(cm.SHIPPED["QB"])},
                                                  "fallback_models": {"QB": fb.to_dict()}}})
    assert cm.uses_scenarios("QB", tuple(cm.SHIPPED["QB"]))
    feats = pl.DataFrame({"gsis_id": ["q1", "q2", "q3"], "position": ["QB"] * 3, "pi_start": [0.8, None, 0.3],
                          "s_start": [None, None, None], "pass_att_pg": [34.0, 28.0, None]},
                         schema_overrides={"pi_start": pl.Float64, "s_start": pl.Float64})
    mixed = cm.mix_points(main, feats)
    expect = {"q1": mixed[0], "q2": fb.predict(feats.slice(1, 1))[0], "q3": mixed[2]}
    rows = [{"gsis_id": g, "espn_player_id": i + 1, "position": "QB", "coverage_status": "projected",
             "projected_points": round(float(expect[g]), 2)} for i, g in enumerate(("q1", "q2", "q3"))]
    out = capture_inputs(art, feats, rows, cm, "art-x")
    models = {(m["position"], m["model_kind"]): m for m in out["models"]}
    assert set(models) == {("QB", "main"), ("QB", "fallback")}
    by = {r["espn_player_id"]: r for r in out["rows"]}
    assert (by[1]["model_kind"], by[1]["mixture_column"]) == ("main", "s_start")
    assert (by[2]["model_kind"], by[2]["mixture_column"]) == ("fallback", None)
    assert by[3]["imputed"] == [False, True, False]          # pass attempts unknown: the training mean, marked
    for pid, g in ((1, "q1"), (2, "q2"), (3, "q3")):
        r = by[pid]
        got = pf.reproduce(models[("QB", r["model_kind"])], r)
        assert np.isclose(got["points"], expect[g]), g

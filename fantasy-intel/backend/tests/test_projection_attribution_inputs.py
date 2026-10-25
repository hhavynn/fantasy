"""Why a stored forecast changed, with the full stored inputs (projection-attribution/v2; issue #225).

Synthetic only. Invented models and design rows go through production_forecasts.reproduce exactly as the reader's
verified inputs do; `decompose` then splits the per-game change with the two-factor midpoint convention. Without
verified inputs on both sides a block must be byte-identical to v1."""

import json
import random

import pytest

from app.analytics import production_forecasts as pf
from app.analytics import projection_attribution as pa

from test_projection_attribution import KICK, LATE_, mover, parts, row, run, total_of

COLS = ["targets_pg", "snap_pct", "opp_pos_allowed"]


def model(art, coef, *, cols=COLS, means=None, stds=None, intercept=10.0, position="RB", kind="main"):
    m = {"artifact_id": art, "position": position, "model_kind": kind, "model_version": "ifp-2.2.0", "columns": cols,
         "means": means or [5.0, 0.6, 0.0][: len(cols)] + [1.0] * max(0, len(cols) - 3),
         "stds": stds or [2.0, 0.2, 1.0][: len(cols)] + [1.0] * max(0, len(cols) - 3), "coef": coef,
         "intercept": intercept}
    m["model_sha256"] = pf.model_sha256(m)
    return m


def side(m, values, *, imputed=None, mixture=None, scale=None, shift=None, p=1.0):
    inp = {"input_values": values, "imputed": imputed or [False] * len(values), "mixture_column": mixture,
           "scale": scale, "shift": shift}
    rebuilt = pf.reproduce(m, inp)
    x = round(rebuilt["points"], 2)
    return {"model": m, "inputs": inp, "rebuilt": rebuilt}, row(p, x)


def split(weeks, *, position="RB", inputs=True, before_run=None, after_run=None):
    """decompose() over {week: (before_side, after_side)} where a side is side(...)'s (verified, row) pair."""
    before_runs = {w: {**(before_run or run("b")), "run_id": f"b{w}", "week": w} for w in weeks}
    after_runs = {w: {**(after_run or run("a", 7, created=LATE_)), "run_id": f"a{w}", "week": w} for w in weeks}
    rows, verified = {}, {}
    for w, ((vb, rb), (va, ra)) in weeks.items():
        rows.setdefault(f"b{w}", {})[1] = rb
        rows.setdefault(f"a{w}", {})[1] = ra
        if vb is not None:
            verified[(f"b{w}", 1)] = vb
        if va is not None:
            verified[(f"a{w}", 1)] = va
    m = mover({w: (rb, ra) for w, ((_, rb), (_, ra)) in weeks.items()}, position)
    return pa.decompose(m, sorted(weeks), before_runs, after_runs, rows, KICK, verified if inputs else None), m


def per_game_parts(block):
    return {k: p["points"] for k, p in parts(block).items() if k != "availability"}


# ----------------------------------------------------------------------------- exactness


def test_a_retrain_is_split_exactly_between_inputs_and_the_model_over_a_grid():
    rng = random.Random(7)
    for _ in range(60):
        m0 = model("art-1", [rng.uniform(-2, 3) for _ in COLS], intercept=rng.uniform(8, 14))
        m1 = model("art-2", [rng.uniform(-2, 3) for _ in COLS], intercept=rng.uniform(8, 14),
                   means=[rng.uniform(4, 6), rng.uniform(0.5, 0.7), rng.uniform(-0.2, 0.2)])
        b = side(m0, [rng.uniform(3, 9), rng.uniform(0.4, 0.9), rng.uniform(-1, 1)], p=rng.choice([0.9, 1.0]))
        a = side(m1, [rng.uniform(3, 9), rng.uniform(0.4, 0.9), rng.uniform(-1, 1)], p=rng.choice([0.8, 1.0]))
        if b[0]["rebuilt"]["floored"] or a[0]["rebuilt"]["floored"]:
            continue
        block, mv = split({7: (b, a)}, after_run=run("a", 7, created=LATE_, artifact="art-2"))
        assert block["contract"] == pa.CONTRACT_V2 and block["method"] == "exact_midpoint"
        assert block["reconciles"] and total_of(block) == mv["delta"]
        assert block["status"] == "explained", block
        assert "model:coefficients" in parts(block) and not block["model"]["changed"]
        # the measured parts are p1 x the unrounded rebuilt change exactly
        p1 = a[1]["p_play"]
        exact = p1 * (a[0]["rebuilt"]["linear"] - b[0]["rebuilt"]["linear"])
        assert sum(per_game_parts(block).values()) == pytest.approx(exact, abs=0.006 * (1 + len(COLS)))


def test_swapping_the_two_forecasts_negates_every_part():
    m0 = model("art-1", [1.5, 4.0, -0.8], intercept=11.0)
    m1 = model("art-2", [1.1, 5.0, -1.2], intercept=12.5, means=[5.5, 0.62, 0.1])
    b, a = side(m0, [6.0, 0.7, 0.3]), side(m1, [8.0, 0.8, -0.5])
    fwd, _ = split({7: (b, a)})
    back, _ = split({7: (a, b)})
    f, r = per_game_parts(fwd), per_game_parts(back)
    assert f.keys() == r.keys()
    for k in f:
        assert f[k] == pytest.approx(-r[k], abs=0.011), k


def test_the_same_model_has_no_model_part_and_every_input_is_measured():
    m = model("art-1", [1.5, 4.0, -0.8])
    block, mv = split({7: (side(m, [6.0, 0.7, 0.3]), side(m, [8.0, 0.75, -0.5]))})
    keys = set(per_game_parts(block))
    assert keys == {"input:targets_pg", "input:snap_pct", "input:opp_pos_allowed"}
    assert parts(block)["input:targets_pg"]["points"] == pytest.approx(1.5 / 2.0 * 2.0, abs=0.01)
    assert {r["key"] for r in block["residual"]["reasons"]} <= {"rounding"}
    assert block["status"] == "explained" and total_of(block) == mv["delta"]
    assert parts(block)["input:targets_pg"]["before"] == 6.0 and parts(block)["input:targets_pg"]["after"] == 8.0


def test_an_unknown_input_is_residual_never_a_measured_change():
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.5, 4.0, -0.8], means=[5.0, 0.55, 0.0])
    b = side(m0, [6.0, 0.7, 0.3], imputed=[False, False, False])         # snap share measured at 0.7 ...
    a = side(m1, [8.0, 0.55, 0.3], imputed=[False, True, False])         # ... then unknown: the training mean
    block, mv = split({7: (b, a)})
    assert "input:snap_pct" not in parts(block)
    imputed = next(r for r in block["residual"]["reasons"] if r["key"] == "imputed")
    # its centred term went from 4.0 / 0.2 * (0.7 - 0.6) = 2.0 to 0 (unknown): residual, never measured
    assert imputed["points"] == pytest.approx(-2.0, abs=0.01)
    assert parts(block)["input:targets_pg"]["points"] == pytest.approx(1.5 / 2.0 * 2.0, abs=0.01)
    assert "model:coefficients" not in parts(block)          # same weights; only the unknown input's mean moved
    assert total_of(block) == mv["delta"]
    listed = next(i for i in block["inputs"] if i["feature"] == "snap_pct")
    assert listed["after"] is None and listed["after_missing"] == pa.IMPUTED and listed["before"] == 0.7


def test_an_input_unknown_in_both_forecasts_never_reaches_the_model_part():
    """A retrain that only moved an unknown input's training mean: nothing changed, nothing is measured."""
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.5, 4.0, -0.8], means=[5.0, 0.55, 0.0])
    b = side(m0, [6.0, 0.6, 0.3], imputed=[False, True, False])
    a = side(m1, [6.0, 0.55, 0.3], imputed=[False, True, False])
    assert round(b[0]["rebuilt"]["linear"], 9) == round(a[0]["rebuilt"]["linear"], 9)
    block, mv = split({7: (b, a)})
    assert mv["delta"] == 0 and total_of(block) == 0
    assert all(abs(p["points"]) < 0.005 for p in block["parts"])
    assert all(abs(r["points"]) < 0.005 for r in block["residual"]["reasons"])
    assert block["status"] == "explained"


def test_a_changed_availability_model_keeps_the_points_split_and_says_so():
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.1, 5.0, -1.2])
    b, a = side(m0, [6.0, 0.7, 0.3], p=0.9), side(m1, [8.0, 0.8, -0.5], p=0.95)
    block, _ = split({7: (b, a)}, after_run=run("a", 7, created=LATE_, participation_version="ipm-2"))
    assert "model:availability" in parts(block) and "model:coefficients" in parts(block)
    assert block["method"] == "exact_midpoint" and block["status"] == "model_changed"
    assert block["method_note"].startswith(pa.METHOD_V2) and pa.AVAILABILITY_UNSPLIT in block["method_note"]


def test_an_input_only_one_model_has_is_a_model_change_only():
    m0 = model("art-1", [1.5, 4.0], cols=COLS[:2])
    m1 = model("art-2", [1.5, 4.0, -0.8], intercept=10.5)
    block, mv = split({7: (side(m0, [6.0, 0.7]), side(m1, [6.0, 0.7, 0.9]))})
    assert "input:opp_pos_allowed" not in parts(block)
    assert parts(block)["model:coefficients"]["points"] == pytest.approx(0.5 + (-0.8) * 0.9, abs=0.01)
    assert total_of(block) == mv["delta"]


def test_a_quarterback_mixture_is_split_exactly_not_approximately():
    cols = ["s_start", "pass_att_pg"]
    m0 = model("art-1", [4.0, 2.5], cols=cols, means=[0.6, 30.0], stds=[0.4, 6.0], intercept=14.0, position="QB")
    m1 = model("art-2", [4.5, 2.0], cols=cols, means=[0.6, 31.0], stds=[0.4, 6.0], intercept=14.5, position="QB")
    b, a = side(m0, [0.7, 32.0], mixture="s_start"), side(m1, [0.9, 35.0], mixture="s_start")
    block, mv = split({7: (b, a)}, position="QB")
    assert block["method"] == "exact_midpoint" and block["approximation_note"] is None
    assert "input:s_start" in parts(block) and total_of(block) == mv["delta"]
    assert block["status"] == "explained"


def test_a_changed_league_rescore_factor_is_its_own_measured_part():
    m = model("art-1", [1.5, 4.0, -0.8])
    b, a = side(m, [6.0, 0.7, 0.3], scale=0.9), side(m, [6.0, 0.7, 0.3], scale=0.95)
    block, mv = split({7: (b, a)})
    assert set(per_game_parts(block)) <= {"model:rescoring"} | {f"input:{c}" for c in COLS}
    assert parts(block)["model:rescoring"]["points"] == pytest.approx(mv["delta"], abs=0.02)
    assert block["status"] == "explained"


def test_a_floored_forecast_is_not_split():
    m = model("art-1", [3.0, 4.0, -0.8], intercept=1.0)
    b, a = side(m, [0.5, 0.2, 0.3]), side(m, [8.0, 0.75, -0.5])
    assert b[0]["rebuilt"]["floored"]
    block, _ = split({7: (b, a)})
    assert not [k for k in per_game_parts(block) if k.startswith("input:")]
    assert any(r["key"] == "floored" for r in block["residual"]["reasons"])


def test_many_small_inputs_are_shown_together_and_still_add_up():
    cols = [f"f{i}" for i in range(12)]
    m0 = model("art-1", [0.004 * (i + 1) for i in range(12)], cols=cols)
    m1 = model("art-2", [0.004 * (i + 1) for i in range(12)], cols=cols, intercept=11.0)
    block, mv = split({7: (side(m0, [1.0] * 12), side(m1, [1.5] * 12))})
    small = parts(block)["inputs:small"]
    assert small["label"].startswith("12 other inputs") and len(small["features"]) == 12
    assert total_of(block) == mv["delta"]


# ----------------------------------------------------------------------------- the v1 fallback, byte for byte


def _v1(weeks, **kw):
    return split(weeks, inputs=False, **kw)[0]


@pytest.mark.parametrize("drop", ["before", "after", "both"])
def test_without_verified_inputs_on_both_sides_the_block_is_v1_byte_for_byte(drop):
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.1, 5.0, -1.2])
    b, a = side(m0, [6.0, 0.7, 0.3]), side(m1, [8.0, 0.8, -0.5])
    nb = (None, b[1]) if drop in ("before", "both") else b
    na = (None, a[1]) if drop in ("after", "both") else a
    got, _ = split({7: (nb, na)})
    assert json.dumps(got, sort_keys=True, default=str) == json.dumps(_v1({7: (b, a)}), sort_keys=True, default=str)
    assert got["contract"] == pa.CONTRACT and "full_inputs" not in got


def test_a_window_mixing_weeks_with_and_without_inputs_says_which_weeks_used_them():
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.1, 5.0, -1.2])
    w7 = (side(m0, [6.0, 0.7, 0.3]), side(m1, [8.0, 0.8, -0.5]))
    w8 = ((None, side(m0, [6.0, 0.7, 0.3])[1]), side(m1, [7.0, 0.8, -0.5]))
    block, mv = split({7: w7, 8: w8}, after_run=run("a", 7, created=LATE_, artifact="art-2"))
    assert block["full_inputs"]["weeks"] == [7]
    assert block["status"] == "model_changed"          # week 8 still cannot be split: v1's "model changed"
    assert total_of(block) == mv["delta"]


def test_the_v2_contract_keeps_every_v1_field():
    m0, m1 = model("art-1", [1.5, 4.0, -0.8]), model("art-2", [1.1, 5.0, -1.2])
    b, a = side(m0, [6.0, 0.7, 0.3]), side(m1, [8.0, 0.8, -0.5])
    v2, _ = split({7: (b, a)})
    assert set(_v1({7: (b, a)})) <= set(v2) and set(v2) - set(_v1({7: (b, a)})) == {"full_inputs"}
    for p in v2["parts"]:
        assert {"key", "kind", "label", "points", "weeks", "exact"} <= set(p)


# ----------------------------------------------------------------------------- stored runs, through attach


@pytest.fixture(autouse=True)
def _fingerprint(monkeypatch):
    from app.analytics import projection_source as ps

    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


def _identity_inputs(con, pid=1):
    """Store, for every run of player `pid`, a one-input model whose output is the stored point itself (v = x), so
    the inputs verify and his whole per-game change is that one measured input."""
    from app.db.connection import fetch_dicts

    for r in fetch_dicts(con, f"SELECT r.run_id, r.position, r.projected_points, u.artifact_id FROM {pf.ROWS} r "
                              f"JOIN {pf.RUNS} u USING (run_id) WHERE r.espn_player_id = ? "
                              "AND r.coverage_status = 'projected'", [pid]):
        m = {"artifact_id": r["artifact_id"], "position": r["position"], "model_kind": "main",
             "model_version": "ifp-2.0.0", "columns": ["targets_pg"], "means": [0.0], "stds": [1.0], "coef": [1.0],
             "intercept": 0.0}
        pf.write_models(con, [m])
        pf.write_inputs(con, r["run_id"], [{"espn_player_id": pid, "artifact_id": r["artifact_id"],
                                            "position": r["position"], "model_kind": "main",
                                            "input_values": [float(r["projected_points"])], "imputed": [False]}])


def _cycles(con):
    import test_projection_movers as tpm

    tpm.seed(con)
    tpm.cycle(con, 6, tpm.C6)
    tpm.cycle(con, 7, tpm.C7, tweak=lambda pid, week: {"x": tpm.base(pid) + 1.0} if pid == 1 else None)


def _block(con, pid=1):
    import test_projection_movers as tpm
    from app.analytics import projection_movers as pm

    result = pa.attach(con, tpm.SEASON, pm.movement(con, tpm.SEASON, source="independent"), player=pid)
    m = next(m for k in ("risers", "fallers", "players") for m in result[k] if m["espn_player_id"] == pid
             and m.get("attribution"))
    return m["attribution"], m


def test_stored_full_inputs_split_the_change_through_the_route_reader(con):
    _cycles(con)
    v1, _ = _block(con)
    _identity_inputs(con)
    v2, m = _block(con)
    assert v1["contract"] == pa.CONTRACT and v2["contract"] == pa.CONTRACT_V2
    assert v2["status"] == "explained" and total_of(v2) == m["delta"]
    assert parts(v2)["input:targets_pg"]["points"] == pytest.approx(m["delta"], abs=0.05)
    assert v2["full_inputs"]["weeks"] == [w["week"] for w in m["weeks"] if w["before"] or w["after"]]


@pytest.mark.parametrize("break_it", ["model digest", "input digest", "one side only", "another artifact"])
def test_inputs_that_fail_verification_give_the_v1_block_byte_for_byte(con, break_it):
    _cycles(con)
    v1, _ = _block(con)
    _identity_inputs(con)
    if break_it == "model digest":
        con.execute(f"UPDATE {pf.MODELS} SET intercept = 0.25")
    elif break_it == "input digest":
        con.execute(f"UPDATE {pf.INPUTS} SET imputed = [true]")
    elif break_it == "one side only":          # an old run (no inputs) against a new one
        con.execute(f"DELETE FROM {pf.INPUTS} WHERE run_id IN (SELECT run_id FROM {pf.RUNS} WHERE projected_from_week = 6)")
    else:                                     # inputs (and a model) that name another artifact than their run's
        con.execute(f"UPDATE {pf.MODELS} SET artifact_id = 'other'")
        con.execute(f"UPDATE {pf.MODELS} SET model_sha256 = NULL")
        for m in [dict(zip([d[0] for d in con.description], r)) for r in con.execute(f"SELECT * FROM {pf.MODELS}").fetchall()]:
            con.execute(f"UPDATE {pf.MODELS} SET model_sha256 = ? WHERE position = ?", [pf.model_sha256(m), m["position"]])
        old = con.execute(f"SELECT run_id, espn_player_id, position, input_values FROM {pf.INPUTS}").fetchall()
        con.execute(f"DELETE FROM {pf.INPUTS}")
        for rid, pid, pos, vals in old:
            pf.write_inputs(con, rid, [{"espn_player_id": pid, "artifact_id": "other", "position": pos,
                                        "model_kind": "main", "input_values": vals, "imputed": [False]}])
    got, _ = _block(con)
    assert json.dumps(got, sort_keys=True, default=str) == json.dumps(v1, sort_keys=True, default=str)

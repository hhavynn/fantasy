"""Why a stored forecast changed (analytics/projection_attribution.py; issue #225).

Synthetic only. The pure tests feed `decompose` invented run metadata and rows; the database tests store two sealed
rest-of-season cycles the way test_projection_movers.py does, with invented explanations, and read them through
GET /api/projections/movers."""

import itertools
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import test_projection_movers as tpm
from app.analytics import projection_attribution as pa
from app.analytics import projection_movers as pm
from app.analytics import projection_source as ps
from app.db.connection import connect, init_db

UTC = timezone.utc
KICK = {7: datetime(2026, 10, 22, 17, 0, tzinfo=UTC), 8: datetime(2026, 10, 29, 17, 0, tzinfo=UTC)}
EARLY, LATE_ = datetime(2026, 10, 14, 12, 0), datetime(2026, 10, 21, 12, 0)


def run(rid, cutoff=6, *, created=EARLY, artifact="art-1", version="ifp-2.2.0", week=7, **kw):
    return {"run_id": rid, "season": 2026, "week": week, "projected_from_week": cutoff, "created_at": created,
            "model_version": version, "artifact_id": artifact, "participation_version": "ipm-1",
            "data_through_season": 2026, "data_through_week": cutoff - 1, "evaluation": {}, **kw}


def term(feature, contribution, value=1.0, mean=0.5):
    return {"feature": feature, "value": value, "training_mean": mean, "contribution": contribution, "text": "x"}


def row(p, x, expl=(), status="projected", **kw):
    exp = None if status != "projected" else round(p * x, 2)
    return {"coverage_status": status, "p_play": p, "projected_points": x, "expected_points": exp,
            "explanation": json.dumps(list(expl)), "missing_inputs": json.dumps(kw.pop("missing", [])),
            "participation": None, "kickoff": None, **kw}


def value(r):
    return 0.0 if r["coverage_status"] in pa.ZERO_STATUSES else r["expected_points"]


def mover(rows_by_week, position="RB", pid=1):
    """A movers-shaped mover over {week: (before_row, after_row)}: per-week stored values and the rounded delta."""
    weeks = [{"week": w, "before": value(b), "after": value(a)} for w, (b, a) in sorted(rows_by_week.items())]
    b_raw, a_raw = sum(w["before"] for w in weeks), sum(w["after"] for w in weeks)
    return {"espn_player_id": pid, "position": position, "weeks": weeks, "delta": round(a_raw - b_raw, 2)}


def attribute(rows_by_week, *, before=None, after=None, position="RB", kick=KICK):
    """decompose() over {week: (before_row, after_row)} with one run per side per week."""
    before_runs = {w: {**(before or run("b")), "run_id": f"b{w}", "week": w} for w in rows_by_week}
    after_runs = {w: {**(after or run("a", 7, created=LATE_)), "run_id": f"a{w}", "week": w} for w in rows_by_week}
    rows = {}
    for w, (b, a) in rows_by_week.items():
        rows.setdefault(f"b{w}", {})[1] = b
        rows.setdefault(f"a{w}", {})[1] = a
    m = mover(rows_by_week, position)
    return pa.decompose(m, sorted(rows_by_week), before_runs, after_runs, rows, kick), m


def total_of(block):
    return round(sum(p["points"] for p in block["parts"]) + block["residual"]["points"], 2)


def parts(block, kind=None):
    return {p["key"]: p for p in block["parts"] if kind is None or p["kind"] == kind}


# ----------------------------------------------------------------------------- reconciliation


def test_same_artifact_input_terms_split_the_per_game_change_exactly():
    b = row(0.9, 10.0, [term("targets_pg", 1.0, 5.0, 4.0), term("snap_pct", 0.5, 0.6, 0.5)])
    a = row(0.9, 12.0, [term("targets_pg", 2.0, 6.0, 4.0), term("snap_pct", 1.5, 0.8, 0.5)])
    block, m = attribute({7: (b, a)})
    assert block["status"] == "explained" and block["method"] == "exact_linear" and block["reconciles"]
    ins = parts(block, "input")
    assert ins["input:targets_pg"]["points"] == 0.9 and ins["input:snap_pct"]["points"] == 0.9   # p1 x (c1 - c0)
    assert ins["input:targets_pg"]["before"] == 5.0 and ins["input:targets_pg"]["after"] == 6.0
    assert ins["input:snap_pct"]["unit"] == "share" and ins["input:targets_pg"]["exact"] is True
    assert parts(block)["availability"]["points"] == 0.0
    assert total_of(block) == m["delta"] == block["total"] == 1.8 and block["residual"]["points"] == 0.0
    assert block["model"]["changed"] is False and block["source_label"] == pa.OURS_LABEL


def test_contributions_and_residual_reconcile_to_the_total_over_a_grid_of_changes():
    """Parts + residual equal the mover's change to the cent, and the residual's reasons add up to the residual."""
    for p0, p1, x0, x1, c0, c1, extra in itertools.product((0.55, 0.9), (0.4, 0.97), (3.333, 11.0), (2.71, 14.2),
                                                           (-0.37, 1.11), (0.06, 2.49), (0.0, 0.83)):
        b = row(p0, x0, [term("targets_pg", c0), term("is_home", 0.12, 1.0, 0.5)])
        a = row(p1, x1, [term("targets_pg", c1), term("pts_l3", extra)] if extra else [term("targets_pg", c1)])
        a2 = row(p1, x1 + 0.4, [term("targets_pg", c1 + 0.1)])
        block, m = attribute({7: (b, a), 8: (b, a2)})
        assert block["reconciles"] and total_of(block) == m["delta"], (p0, p1, x0, x1, c0, c1, extra)
        assert round(sum(r["points"] for r in block["residual"]["reasons"]), 2) == block["residual"]["points"]
        assert block["status"] in ("explained", "partial")


def test_inputs_stored_in_only_one_forecast_and_unstored_inputs_stay_in_the_residual():
    b = row(1.0, 10.0, [term("targets_pg", 1.0), term("pts_l3", 0.8)])
    a = row(1.0, 13.0, [term("targets_pg", 2.0), term("carries_pg", 0.6)])
    block, _ = attribute({7: (b, a)})
    assert set(parts(block, "input")) == {"input:targets_pg"}
    assert block["status"] == "partial" and block["residual"]["points"] == 2.0
    assert [r["key"] for r in block["residual"]["reasons"]] == ["one_sided"]
    listed = {r["feature"]: r for r in block["inputs"]}
    assert listed["pts_l3"]["after"] is None and listed["pts_l3"]["after_missing"] == pa.NOT_STORED
    assert listed["carries_pg"]["before_missing"] == pa.NOT_STORED


# ----------------------------------------------------------------------------- model / source changes


def test_a_retrained_model_is_flagged_model_changed_and_never_attributed_to_inputs():
    b = row(0.9, 10.0, [term("targets_pg", 1.0, 5.0)])
    a = row(0.9, 12.0, [term("targets_pg", 2.0, 6.0)])
    block, m = attribute({7: (b, a)}, after=run("a", 7, created=LATE_, artifact="art-2"))
    assert block["status"] == "model_changed" and block["method"] == "model_changed"
    assert not parts(block, "input") and parts(block)["model:points"]["points"] == 1.8
    assert block["model"]["changed"] and "retrained" in block["model"]["reasons"][0]["text"]
    listed = {r["feature"]: r for r in block["inputs"]}
    assert listed["targets_pg"]["before"] == 5.0 and listed["targets_pg"]["after"] == 6.0 and "points" not in listed["targets_pg"]
    assert total_of(block) == m["delta"]


@pytest.mark.parametrize("after, reason", [
    (run("a", 7, created=LATE_, version="ifp-2.1.0"), "model version ifp-2.2.0 → ifp-2.1.0"),
    (run("a", 7, created=LATE_, artifact=None), "artifact not recorded"),
    (run("a", 7, created=LATE_, evaluation={"rescored_from": {"run_id": "r"}}), "re-scored"),
])
def test_a_model_or_scoring_swap_is_flagged_model_changed(after, reason):
    b, a = row(0.9, 10.0, [term("targets_pg", 1.0)]), row(0.9, 11.0, [term("targets_pg", 2.0)])
    block, _ = attribute({7: (b, a)}, after=after)
    assert block["status"] == "model_changed" and any(reason in r["text"] for r in block["model"]["reasons"])
    assert not parts(block, "input")


def test_a_changed_participation_model_moves_the_availability_part_to_model_changed():
    b = row(0.9, 10.0, [term("targets_pg", 1.0)])
    a = row(0.5, 10.0, [term("targets_pg", 1.0)], participation=json.dumps({"model_version": "return-rate-table"}))
    block, m = attribute({7: (b, a)})
    assert "availability" not in parts(block) and parts(block)["model:availability"]["points"] == -4.0
    assert block["status"] == "model_changed" and total_of(block) == m["delta"]


def test_espn_movers_are_unavailable_and_never_labelled_as_fantasy_intel():
    result = {"available": True, "source": "espn", "window": {"weeks": [7]},
              "risers": [{"espn_player_id": 1, "delta": 2.0}], "fallers": [], "players": []}
    out = pa.attach(None, 2026, result)
    block = out["risers"][0]["attribution"]
    assert block["status"] == "unavailable" and block["source_label"] == "ESPN" and "ESPN publishes" in block["reason"]
    assert "Fantasy Intel" not in json.dumps(block)


# ----------------------------------------------------------------------------- as-of and kickoff cutoffs


def test_a_forecast_made_at_or_after_kickoff_is_refused_not_attributed():
    b, a = row(0.9, 10.0, [term("targets_pg", 1.0)]), row(0.9, 12.0, [term("targets_pg", 2.0)])
    at_kickoff = run("a", 7, created=datetime(2026, 10, 22, 17, 0))
    block, m = attribute({7: (b, a)}, after=at_kickoff)
    assert not parts(block) and block["status"] == "not_decomposed"
    assert block["residual"]["reasons"][0]["key"] == "as_of" and block["residual"]["points"] == m["delta"]
    assert block["as_of"]["refused"] == [{"week": 7, "forecast": "latest", "reason": "forecast made after week 7's first kickoff"}]


def test_the_players_own_kickoff_and_an_unknown_kickoff_also_refuse():
    b = row(0.9, 10.0, [term("targets_pg", 1.0)], kickoff=datetime(2026, 10, 14, 11, 0))   # Thursday game, before the run
    a = row(0.9, 12.0, [term("targets_pg", 2.0)])
    block, _ = attribute({7: (b, a)})
    assert block["as_of"]["refused"][0]["reason"] == "forecast made after the player's own kickoff"
    block, _ = attribute({7: (row(0.9, 10.0), row(0.9, 11.0))}, kick={})
    assert block["as_of"]["refused"][0]["reason"] == "week 7 kickoff time unknown"


def test_inputs_that_run_past_the_cutoff_week_are_post_game_leakage_and_refused():
    b, a = row(0.9, 10.0, [term("targets_pg", 1.0)]), row(0.9, 12.0, [term("targets_pg", 2.0)])
    leaky = run("a", 7, created=LATE_, data_through_week=7)      # claims week 7's games, before week 7 kicked off
    block, _ = attribute({7: (b, a)}, after=leaky)
    assert not parts(block, "input") and "run through week 7" in block["as_of"]["refused"][0]["reason"]
    unrecorded = run("a", 7, created=LATE_, data_through_week=None, data_through_season=None)
    block, _ = attribute({7: (b, a)}, after=unrecorded)
    assert parts(block, "input") and block["as_of"]["after"]["data_through"] is None


def test_as_of_metadata_names_both_forecasts():
    block, _ = attribute({7: (row(0.9, 10.0), row(0.9, 11.0))})
    assert block["as_of"]["before"]["data_through"] == {"season": 2026, "week": 5}
    assert block["as_of"]["after"]["cutoff_week"] == 7 and block["as_of"]["after"]["created_at"] == "2026-10-21T12:00:00Z"


# ----------------------------------------------------------------------------- missing inputs and history


def test_missing_history_leaves_the_per_game_change_unexplained():
    block, m = attribute({7: (row(0.9, 10.0, []), row(0.9, 12.0, [term("targets_pg", 2.0)]))})
    assert not parts(block, "input") and block["status"] == "partial"
    assert block["residual"]["reasons"][0]["key"] == "no_inputs" and total_of(block) == m["delta"]


def test_a_missing_stored_row_is_residual_never_zero():
    b, a = row(0.9, 10.0), row(0.9, 12.0)
    m = mover({7: (b, a)})
    block = pa.decompose(m, [7], {7: run("b7")}, {7: run("a7", 7, created=LATE_)}, {"b7": {1: b}}, KICK)
    assert not block["parts"] and block["residual"]["reasons"][0]["key"] == "missing_row"
    assert block["residual"]["points"] == m["delta"]


def test_missing_inputs_are_shown_as_missing_with_reasons_never_zero():
    b = row(0.9, 10.0, [term("snap_pct", -0.4, None), term("snap_pct__missing", 0.2, None)],
            missing=["snap counts unavailable in recent games"])
    a = row(0.9, 11.0, [term("snap_pct", 0.3, 0.7), term("snap_pct__missing", -0.1, 0.7)])
    block, _ = attribute({7: (b, a)})
    snap = parts(block)["input:snap_pct"]
    assert snap["before"] is None and snap["before_missing"] == pa.IMPUTED and snap["after"] == 0.7
    ind = parts(block)["input:snap_pct__missing"]
    assert ind["unit"] == "flag" and ind["before"] == 1.0 and ind["after"] == 0.0
    assert block["missing_inputs"]["before"] == [{"text": "snap counts unavailable in recent games", "weeks": [7]}]
    assert block["missing_inputs"]["after"] == []


def test_a_status_change_cites_only_the_reason_stored_with_the_forecast():
    b = row(None, None, status="withheld_unavailable", missing=["ESPN status OUT"])
    a = row(0.9, 12.0, [term("targets_pg", 2.0)])
    block, m = attribute({7: (b, a)})
    st = parts(block, "status")["status:withheld_unavailable>projected"]
    assert st["points"] == m["delta"] == 10.8 and st["exact"]
    assert st["events"] == [{"forecast": "earlier", "text": "ESPN status OUT", "source": "stored with the forecast"}]
    assert block["status"] == "explained" and block["missing_inputs"]["before"] == []   # a reason, not a missing input


def test_a_floored_forecast_is_not_split():
    block, _ = attribute({7: (row(0.9, 0.0, [term("targets_pg", -1.0)]), row(0.9, 3.0, [term("targets_pg", 1.0)]))})
    assert not parts(block, "input") and block["residual"]["reasons"][0]["key"] == "floored"


def test_quarterback_shares_are_labelled_approximate():
    block, _ = attribute({7: (row(0.9, 15.0, [term("s_start", 1.0)]), row(0.9, 17.0, [term("s_start", 3.0)]))},
                         position="QB")
    assert block["method"] == "approximate" and block["approximation_note"] == pa.QB_NOTE
    assert parts(block)["input:s_start"]["exact"] is False


# ----------------------------------------------------------------------------- determinism and copies


def test_the_block_is_deterministic_whatever_the_stored_order():
    expl = [term("targets_pg", 1.0), term("snap_pct", 0.5), term("pts_l3", 0.3), term("is_home", 0.1)]
    a = row(0.8, 12.0, [term("targets_pg", 2.0), term("snap_pct", 0.2), term("pts_l3", 0.9), term("is_home", -0.1)])
    one, _ = attribute({7: (row(0.9, 10.0, expl), a)})
    two, _ = attribute({7: (row(0.9, 10.0, list(reversed(expl))), a)})
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert [p["key"] for p in one["parts"]] == sorted((p["key"] for p in one["parts"]),
                                                      key=lambda k: (-abs(parts(one)[k]["points"]), k))


def test_label_and_status_tables_mirror_the_model():
    from app.analytics.independent_projections import LABELS
    assert dict(pa.LABELS) == LABELS and pa.ZERO_STATUSES == ps.ZERO_STATUSES


# ----------------------------------------------------------------------------- stored runs through the route


def explained_runs(con):
    """Two cycles (week 6 and week 7, same artifact): Ace's targets term rises by 1.0 a week, Cy is withheld OUT in
    the earlier forecast. Rows get invented explanations and data cutoffs."""
    tpm.seed(con)

    def tweak6(pid, week):
        return {"status": "withheld_unavailable", "p": None, "x": None} if pid == 3 else None

    tpm.cycle(con, 6, tpm.C6, tweak=tweak6, sealed=False)
    tpm.cycle(con, 7, tpm.C7, tweak=lambda pid, week: {"x": tpm.base(pid) + 1.0} if pid == 1 else None, sealed=False)
    for cutoff in (6, 7):
        bump = 1.0 if cutoff == 7 else 0.0
        con.execute("UPDATE independent_projection_runs SET data_through_season = 2026, data_through_week = ? "
                    "WHERE projected_from_week = ?", [cutoff - 1, cutoff])
        con.execute("""UPDATE independent_projection_rows SET explanation = ? WHERE espn_player_id = 1
                       AND run_id IN (SELECT run_id FROM independent_projection_runs WHERE projected_from_week = ?)""",
                    [json.dumps([term("targets_pg", 1.5 + bump, 6.0 + bump, 5.0), term("snap_pct", 0.4, 0.7, 0.6)]),
                     cutoff])
        con.execute("""UPDATE independent_projection_rows SET missing_inputs = '["ESPN status OUT"]'
                       WHERE espn_player_id = 3 AND coverage_status = 'withheld_unavailable'""")
    for r in con.execute("SELECT run_id FROM independent_projection_runs").fetchall():
        tpm.seal(con, r[0])


@pytest.fixture
def fp(monkeypatch):
    monkeypatch.setattr(ps, "_scoring", lambda con, season: ("fp", None))


def test_stored_runs_attach_to_risers_fallers_and_the_requested_player(con, fp):
    explained_runs(con)
    result = pa.attach(con, tpm.SEASON, pm.movement(con, tpm.SEASON, source="independent"), player=5)
    risers = {m["espn_player_id"]: m for m in result["risers"]}
    ace = risers[1]["attribution"]
    assert ace["status"] == "explained" and ace["reconciles"] and total_of(ace) == risers[1]["delta"]
    assert parts(ace)["input:targets_pg"]["weeks"] == [w for w in result["window"]["weeks"] if w != 11]   # KC bye
    assert parts(ace)["input:targets_pg"]["before"] == 6.0 and parts(ace)["input:targets_pg"]["after"] == 7.0
    cy = risers[3]["attribution"]
    assert parts(cy, "status") and cy["status"] == "explained"
    assert any(e["text"] == "ESPN status OUT" for p in cy["parts"] for e in p.get("events", []))
    players = {m["espn_player_id"]: m for m in result["players"]}
    assert players[5]["attribution"]["status"] in ("explained", "partial")     # requested: flat but explained
    assert players[2]["attribution"] is None and players[1]["attribution"] is None   # list copies stay untouched


def test_the_route_returns_the_attribution_and_validates_player(tmp_path, monkeypatch, fp):
    path = tmp_path / "attr.duckdb"
    c = connect(path)
    init_db(c)
    explained_runs(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/projections/movers", params={"season": tpm.SEASON, "source": "independent", "player": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["risers"][0]["attribution"]["contract"] == pa.CONTRACT
    assert next(m for m in body["players"] if m["espn_player_id"] == 5)["attribution"] is not None
    assert client.get("/api/projections/movers", params={"season": tpm.SEASON, "player": 0}).status_code == 422
    espn = client.get("/api/projections/movers", params={"season": tpm.SEASON}).json()
    assert all(m["attribution"] is None or m["attribution"]["source_label"] == "ESPN"
               for m in espn["risers"] + espn["fallers"])


def test_outcomes_are_never_read(con, fp):
    explained_runs(con)
    con.executemany("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, "
                    "projected_points, synced_at) VALUES (?, ?, ?, NULL, 5.0, '2026-10-14 00:00:00')",
                    [(tpm.SEASON, pid, w) for pid in tpm.PLAYERS for w in range(6, 18)])

    def snap():
        out = pa.attach(con, tpm.SEASON, pm.movement(con, tpm.SEASON, source="independent"), player=2)
        return json.dumps(out, sort_keys=True, default=str)
    before = snap()
    con.execute("UPDATE espn_player_weekly_points SET actual_points = 77.0, projected_points = 99.0")
    assert snap() == before


def test_the_number_of_queries_does_not_grow_with_the_number_of_movers(tmp_path, fp):
    def queries(extra):
        c = connect(tmp_path / f"q{extra}.duckdb")
        init_db(c)
        players = {**tpm.PLAYERS, **{100 + i: (f"Extra {i:03d}", "WR", "BUF") for i in range(extra)}}
        tpm.seed(c, players)
        tpm.cycle(c, 6, tpm.C6, players=players)
        tpm.cycle(c, 7, tpm.C7, players=players, tweak=lambda pid, week: {"x": tpm.base(pid) + 1.0})
        result = pm.movement(c, tpm.SEASON, source="independent", limit=50)
        counter = tpm.Counting(c)
        pa.attach(counter, tpm.SEASON, result)
        c.close()
        return counter.n
    assert queries(0) == queries(30)


# ----------------------------------------------------------------------------- review follow-ups (#286)


@pytest.mark.parametrize("marker", [{"calibration": "ipm-cal-0.1.0", "uncalibrated_p_play": 0.8},
                                    {"availability_news": "an-1", "news_p_play": 0.5, "base_p_play": 0.9},
                                    {"base_p_play_source": "median P(plays) of this run's projected RB rows"}])
def test_a_participation_modifier_on_one_side_only_is_an_availability_method_change(marker):
    """Codex P2 on #286: the QB calibration map (horizon 0 only), availability news or a borrowed median applied to one
    forecast only is a change of method, never a measured availability change."""
    q = {"model_version": "ipm-1", "p_play": 0.6}
    b = row(0.9, 10.0, [term("targets_pg", 1.0)], participation=json.dumps({"model_version": "ipm-1", "p_play": 0.9}))
    a = row(0.6, 10.0, [term("targets_pg", 1.0)], participation=json.dumps({**q, **marker}))
    block, m = attribute({7: (b, a)})
    assert "availability" not in parts(block) and parts(block)["model:availability"]["points"] == -3.0
    assert block["status"] == "model_changed" and total_of(block) == m["delta"]
    assert any(r["text"].startswith("availability method ipm-1 → ipm-1 + ") for r in block["model"]["reasons"])
    same, _ = attribute({7: (row(0.9, 10.0, participation=json.dumps({**q, **marker})),
                             row(0.6, 10.0, participation=json.dumps({**q, **marker})))})
    assert parts(same)["availability"]["points"] == -3.0 and same["model"]["changed"] is False   # same method both sides


def test_a_requested_player_beyond_the_cap_is_kept_and_explained(con, fp, monkeypatch):
    """Codex P2 on #286: a requested mover outside the MAX_PLAYERS largest changes used to be dropped, so a stale link
    showed someone else. He is kept (last) and attributed; one who cannot be shown is reported with the reason."""
    explained_runs(con)
    monkeypatch.setattr(pm, "MAX_PLAYERS", 2)
    plain = pm.movement(con, tpm.SEASON, source="independent")
    assert 5 not in [m["espn_player_id"] for m in plain["players"]] and "requested_player" not in plain
    kept = pa.attach(con, tpm.SEASON, pm.movement(con, tpm.SEASON, source="independent", player=5), player=5)
    assert [m["espn_player_id"] for m in kept["players"]][-1] == 5 and len(kept["players"]) == 3
    assert kept["requested_player"] == {"espn_player_id": 5, "status": "listed", "reason": None}
    assert kept["players"][-1]["attribution"]["contract"] == pa.CONTRACT
    wr_only = pm.movement(con, tpm.SEASON, source="independent", position="WR", player=3)
    assert wr_only["requested_player"]["status"] == "filtered" and "RB" in wr_only["requested_player"]["reason"]
    missing = pm.movement(con, tpm.SEASON, source="independent", player=999)
    assert missing["requested_player"]["status"] == "not_found"


def test_a_requested_player_without_a_comparable_forecast_says_why(con, fp):
    tpm.seed(con)
    tpm.cycle(con, 6, tpm.C6)
    tpm.cycle(con, 7, tpm.C7, tweak=lambda pid, week: {"status": "abstain_no_history", "p": None, "x": None}
              if pid == 1 and week == 8 else None)
    r = pm.movement(con, tpm.SEASON, source="independent", player=1)
    assert r["requested_player"]["status"] == "not_comparable"
    assert "week 8" in r["requested_player"]["reason"] and 1 not in [m["espn_player_id"] for m in r["players"]]

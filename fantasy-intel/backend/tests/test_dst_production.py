"""dfp-r1 in production (owner override): the write-once D/ST run store (dst_store), its scoring fingerprint, the
artifact gate, the weekly source switch / independent API / forward view overlays, the D/ST endpoint, the production
script (scripts/dst_projections.py) end to end on a synthetic league, and the learning cycle's non-fatal dst_project
stage. No test makes a network request."""

from __future__ import annotations

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import sys

import duckdb
import pytest

from app.analytics import dst_research as dr
from app.analytics import dst_store as st
from app.analytics import projection_source as ps
from test_dst_research import LEAGUE
from app.analytics.independent_projection_store import build_live as build_ifp, write_live
from test_dst_shadow import TEAMS, world, write_cache
from test_dst_shadow import build_live as build_dst_world
from test_independent_projections import _artifact, live  # noqa: F401  (live is a fixture)
from test_projection_source import PINNED

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SCHEMA = Path(__file__).resolve().parents[1] / "app" / "db" / "schema.sql"
DST = 12                                        # an ESPN D/ST id added to the synthetic skill league


def _load(name, file):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create(con, *tables):
    text = SCHEMA.read_text()
    for t in tables:
        con.execute(re.search(rf"CREATE TABLE IF NOT EXISTS {t} \(.*?\n\);", text, re.S).group(0))


def _league(con, items=LEAGUE):
    con.execute("DELETE FROM leagues")
    con.execute("INSERT INTO leagues (league_id, season, scoring_items, synced_at) VALUES (1, 2026, ?, now())",
                [json.dumps(items)])


def _meta(fp, **kw):
    return {"run_id": "dfp-test", "season": 2026, "week": 6, "model_version": st.MODEL_VERSION,
            "scoring_fingerprint": fp, "artifact_id": "d" * 64, "forecast_horizon": 0,
            "created_at": datetime(2026, 10, 10), **kw}


def _row(pid=DST, status="projected", points=6.4, team="AAA"):
    return {"season": 2026, "week": 6, "espn_player_id": pid, "nfl_team": team, "opponent": "BBB", "is_home": True,
            "coverage_status": status, "projected_points": points, "expected_points": points if points is not None
            else (0.0 if status == "bye" else None), "interval_low": None if points is None else points - 7,
            "interval_high": None if points is None else points + 8,
            "stat_line": {"sacks": 2.6, "interceptions": 0.8, "fumble_recoveries": 0.6, "points_allowed": 20.1,
                          "yards_allowed": 320.0, "p_any_td": 0.12},
            "context": {"spread_line": 3.0, "total_line": 44.0, "opponent_implied_total": 20.5}, "explanation": [
                {"feature": "headline", "contribution": None, "text": "BBB are expected to score 20.5 points."}],
            "flags": [], "missing_inputs": []}


# ----------------------------------------------------------------------------- store


def test_the_numpy_free_fingerprint_equals_the_research_model(con):
    fp, why = st.scoring_fingerprint(LEAGUE)
    assert why is None and fp == dr.resolve_dst_scoring(LEAGUE).fingerprint
    assert st.MODELED == dr.MODELED and st.DST_UNIVERSE == dr.DST_UNIVERSE and st.MODEL_VERSION == dr.MODEL_VERSION
    assert st.scoring_fingerprint(LEAGUE + [{"statId": 107, "points": 0.1}])[0] is None      # unmodeled: abstain
    assert st.scoring_fingerprint(None)[0] is None


def test_runs_are_write_once_verified_and_selected_under_the_league_scoring(con):
    _league(con)
    fp = st.scoring_fingerprint(LEAGUE)[0]
    first = st.write_run(con, _meta(fp), [_row(), _row(13, "bye", None, "CCC")])
    assert first["created"] and st.write_run(con, _meta(fp, run_id="dfp-again"), [_row(), _row(13, "bye", None, "CCC")]
                                               )["created"] is False          # identical rerun: no-op
    with pytest.raises(st.ImmutableRunError):
        st.write_run(con, _meta(fp), [_row(points=9.9)])
    sel = st.select_run(con, 2026, 6)
    assert sel["run"]["run_id"] == "dfp-test" and sel["run"]["artifact_check"] == "unverified"   # fail open
    assert sel["rows"][0]["stat_line"]["sacks"] == 2.6                                          # JSON decoded
    o = st.overlay(con, 2026, 6)
    assert o["values"] == {DST: 6.4, 13: 0.0} and o["origin"] == {DST: "ours", 13: "ours_zero"}
    # an edited row is never shown
    con.execute("UPDATE dst_projection_rows SET projected_points = 99 WHERE espn_player_id = ?", [DST])
    sel = st.select_run(con, 2026, 6)
    assert sel["run"] is None and "content_sha256" in sel["skipped"][0]["reason"]
    assert st.overlay(con, 2026, 6)["values"] == {}


def test_the_artifact_gate_and_the_scoring_gate(con):
    _league(con)
    fp = st.scoring_fingerprint(LEAGUE)[0]
    st.write_run(con, _meta(fp), [_row()])
    assert st.register_artifact(con, model_version="dfp-r1", artifact_sha256="e" * 64, target_season=2026,
                                training_cutoff_key=202600, artifact_created_at=None)
    sel = st.select_run(con, 2026, 6)                       # another artifact is approved: this run's is not
    assert sel["run"] is None and sel["skipped"][0]["reason"] == st.NOT_REGISTERED
    st.register_artifact(con, model_version="dfp-r1", artifact_sha256="d" * 64, target_season=2026,
                         training_cutoff_key=202600, artifact_created_at=None)
    assert st.select_run(con, 2026, 6)["run"]["artifact_check"] == "verified"
    with pytest.raises(ValueError):
        st.register_artifact(con, model_version="kfp-1.0.0", artifact_sha256="d" * 64, target_season=2026,
                             training_cutoff_key=None, artifact_created_at=None)
    _league(con, LEAGUE + [{"statId": 107, "points": 0.1}])  # the league now scores tackles: the model abstains
    sel = st.select_run(con, 2026, 6)
    assert sel["run"] is None and "does not implement" in sel["unavailable_reason"]


# ----------------------------------------------------------------------------- consumers (synthetic skill league)


@pytest.fixture
def stored(live):  # noqa: F811
    """test_projection_source's week-6 run of the synthetic league, with a D/ST in the league (as every real league
    has: the ifp run carries it as unsupported_position, and ESPN projects it)."""
    con, src, _ = live
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, on_team_id) "
                "VALUES (?, 'AAA D/ST', 'D/ST', 'AAA', 1)", [DST])
    con.execute("INSERT INTO espn_player_weekly_points VALUES (2026, ?, 6, NULL, 5.0, '2026-10-13 12:00:00')", [DST])
    meta, rows = build_ifp(con, src, _artifact(src), 2026, 6, artifact_sha256=PINNED)
    for r in rows:
        if r["coverage_status"] == "projected":
            r["expected_points"] = round(r["projected_points"] * 0.9, 2)
    write_live(con, meta, rows)
    return con


def _dst_run(con, pid=DST, expected=6.4):
    items = json.loads(con.execute("SELECT scoring_items FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()[0])
    fp, why = st.scoring_fingerprint(items)
    assert why is None
    st.write_run(con, _meta(fp), [_row(pid, points=expected)])


def test_the_weekly_switch_shows_our_dst_and_leaves_every_other_value_untouched(stored):  # noqa: F811
    before = ps.weekly(stored, 2026, 6, "independent")
    assert before["origin"][DST] == "espn" and before["values"][DST] == 5.0 and before["dst_run"] is None
    _dst_run(stored)
    after = ps.weekly(stored, 2026, 6, "independent")
    assert after["origin"][DST] == "ours" and after["values"][DST] == 6.4
    assert after["dst_run"]["run_id"] == "dfp-test" and after["dst_run"]["model_version"] == "dfp-r1"
    assert {k: v for k, v in after["values"].items() if k != DST} == {k: v for k, v in before["values"].items() if k != DST}
    assert after["counts"]["ours"] == before["counts"]["ours"] + 1
    assert ps.weekly(stored, 2026, 6, "espn")["origin"][DST] == "espn"          # ESPN mode never overlays
    assert "dst_run" in ps.describe(after)


def test_the_dst_overlay_never_replaces_a_skill_estimate(stored):  # noqa: F811
    before = ps.weekly(stored, 2026, 6, "independent")
    _dst_run(stored, pid=1, expected=99.0)               # a dfp row for a player the skill run projects
    assert ps.weekly(stored, 2026, 6, "independent")["values"][1] == before["values"][1]


def test_the_independent_api_describes_the_dst_model(stored):  # noqa: F811
    from app.api import independent_projections as ip

    _dst_run(stored)
    body = ip.build_response(stored, 6, None, None)
    d = next(p for p in body["players"] if p["espn_player_id"] == DST)
    i, c = d["independent"], d["comparison"]
    assert i["projected_points"] == 6.4 and i["expected_points"] == 6.4 and i["model_version"] == "dfp-r1"
    assert i["interval_80"] == {"low": 6.4 - 7, "high": 6.4 + 8, "nominal_coverage": 0.8}
    assert i["stat_line"]["sacks"] == 2.6 and i["dst_context"]["opponent_implied_total"] == 20.5
    assert c["independent_minus_espn"] == round(6.4 - 5.0, 2) and c["expected_minus_espn"] == round(6.4 - 5.0, 2)
    assert body["dst_run"]["available"] and body["dst_run"]["run_id"] == "dfp-test"
    others = ip.build_response(stored, 6, "WR", None)                # no D/ST in the response: nothing to describe
    assert others["dst_run"] is None


def test_the_forward_view_shows_our_dst_for_the_projected_week(stored):  # noqa: F811
    from app.analytics import forward_ours as fo

    _dst_run(stored)
    got = fo.player_weeks(stored, 2026, DST, [6, 7])
    assert got[6]["value"] == 6.4 and got[6]["origin"] == "ours" and got[6]["dst_run"]["run_id"] == "dfp-test"
    assert got[7]["dst_run"] is None                                        # one week only: later weeks stay ESPN


def test_the_dst_endpoint(stored):  # noqa: F811
    from app.api import dst_projections as api

    assert api.build_response(stored, 6, None)["available"] is False
    _dst_run(stored)
    body = api.build_response(stored, 6, None)
    assert body["available"] and body["run"]["run_id"] == "dfp-test" and body["governance"]["model_version"] == "dfp-r1"
    p = body["players"][0]
    assert p["name"] == "AAA D/ST" and p["independent"]["expected_points"] == 6.4 and p["espn"]["projected_points"] == 5.0
    assert p["comparison"]["expected_minus_espn"] == 1.4
    text = json.dumps(body).lower()
    assert "experimental" not in text and "not shown to be better" not in text


# ----------------------------------------------------------------------------- the production script, end to end


@pytest.fixture(scope="module")
def dp():
    return _load("dst_projections", "dst_projections.py")


@pytest.fixture
def prod(dp, tmp_path):
    sched, espn = world()
    scoring = dr.resolve_dst_scoring(LEAGUE)
    build_dst_world(tmp_path / "live.duckdb", sched, espn, scoring)
    con = duckdb.connect(str(tmp_path / "live.duckdb"))
    _create(con, "approved_artifacts", "dst_projection_runs", "dst_projection_rows")
    con.execute("INSERT INTO fantasy_players VALUES (-16009, 'GB D/ST', 'D/ST', 'GB'), (-16099, 'Old D/ST', 'D/ST', NULL)")
    con.close()
    write_cache(tmp_path / "cache", espn)
    args = dp.parse(["project", "--season", "2026", "--week", "7", "--live-db", str(tmp_path / "live.duckdb"),
                     "--history-db", str(tmp_path / "absent.duckdb"), "--cache", str(tmp_path / "cache"),
                     "--out", str(tmp_path / "dst"), "--no-fetch"])
    return args


def test_fit_register_and_project_store_a_verified_run(dp, prod):
    fit = dp.cmd_fit(prod)
    art = json.loads(Path(fit["artifact"]).read_text())
    assert art["version"] == "dfp-r1" and art["config"] == dp.ds.FROZEN.to_dict()
    assert art["training_seasons"] == [2020, 2025] and len(art["interval_bands"]["bands"]) == 4
    assert "registered" not in fit                                     # fit without --register approves nothing
    assert dp.cmd_register(prod)["registered"] is True and dp.cmd_register(prod)["registered"] is False   # idempotent
    with pytest.raises(SystemExit, match="write-once"):
        dp.cmd_fit(prod)                                                 # never refitted
    out = dp.cmd_project(prod)
    run = out["runs"][0]
    assert run["created"] and run["week"] == 7
    assert run["coverage"] == {"bye": 1, "no_nfl_team": 1, "projected": 8}
    con = duckdb.connect(str(prod.live_db), read_only=True)
    sel = st.select_run(con, 2026, 7)
    con.close()
    assert sel["run"]["artifact_check"] == "verified" and sel["run"]["artifact_id"] == out["artifact_sha256"]
    rows = {r["nfl_team"]: r for r in sel["rows"]}
    assert rows["GB"]["coverage_status"] == "bye" and rows["GB"]["expected_points"] == 0.0
    atl = rows["ATL"]
    assert atl["coverage_status"] == "projected" and atl["expected_points"] == atl["projected_points"]
    assert atl["interval_low"] < atl["projected_points"] < atl["interval_high"]
    assert atl["context"]["has_line"] and atl["explanation"][0]["feature"] == "headline"
    assert sel["run"]["data_through_week"] == 6 and sel["run"]["inputs"]["lines"]["sha256"]
    # the stored projection is exactly the shadow's dfp-r1 forecast (one implementation)
    shadow = dp.ds.forecast(dp._load_rows(prod, dr.resolve_dst_scoring(LEAGUE)), dr.resolve_dst_scoring(LEAGUE),
                            2026, 7, set(TEAMS), last=7)
    assert {r["team"]: r["pred"] for r in shadow["rows"]} == {t: rows[t]["projected_points"] for t in TEAMS}
    assert dp.cmd_project(prod)["runs"][0]["created"] is False          # identical rerun: no-op


def test_a_moved_line_makes_a_new_run_which_is_the_one_shown(dp, prod):
    prod.register = True
    dp.cmd_fit(prod)
    first = dp.cmd_project(prod)["runs"][0]["run_id"]
    con = duckdb.connect(str(prod.live_db))
    con.execute("UPDATE nfl_schedules SET total_line = 38 WHERE season = 2026 AND week = 7")
    con.close()
    second = dp.cmd_project(prod)["runs"][0]
    assert second["created"] and second["run_id"] != first
    con = duckdb.connect(str(prod.live_db), read_only=True)
    assert st.select_run(con, 2026, 7)["run"]["run_id"] == second["run_id"]
    con.close()


def test_a_missing_line_projects_from_ratings_and_says_so(dp, prod):
    prod.register = True
    dp.cmd_fit(prod)
    con = duckdb.connect(str(prod.live_db))
    con.execute("UPDATE nfl_schedules SET spread_line = NULL WHERE season = 2026 AND week = 7 AND gametime = '20:15'")
    con.close()
    dp.cmd_project(prod)
    con = duckdb.connect(str(prod.live_db), read_only=True)
    rows = st.select_run(con, 2026, 7)["rows"]
    con.close()
    flagged = [r for r in rows if r["flags"]]
    assert len(flagged) == 2 and all("ratings only" in r["flags"][0] for r in flagged)
    assert all(not r["context"]["has_line"] for r in flagged)


def test_project_refuses_without_an_artifact_or_with_unsupported_scoring(dp, prod):
    with pytest.raises(SystemExit, match="fit --register"):
        dp.cmd_project(prod)
    con = duckdb.connect(str(prod.live_db))
    con.execute("UPDATE leagues SET scoring_items = ?", [json.dumps(LEAGUE + [{"statId": 107, "points": 0.1}])])
    con.close()
    with pytest.raises(SystemExit, match="abstains"):
        dp.cmd_fit(prod)


# ----------------------------------------------------------------------------- the learning cycle's stage


@pytest.fixture(scope="module")
def lc():
    return _load("learning_cycle_dst_project", "learning_cycle.py")


class _Proc:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_stage_fits_once_then_registers_and_projects_the_target_week(lc, tmp_path, monkeypatch):
    ns = lc.parse(["run", "--season", "2026", "--live-db", str(tmp_path / "l.duckdb"), "--dst-dir", str(tmp_path / "dst")])
    ns.week = 7
    seen = []
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Proc(0, json.dumps(
        {"artifact": "a", "sha256": "s", "registered": True, "runs": [{"week": 7, "run_id": "r", "created": True}]})))
    out = lc.stage_dst_project(ns, tmp_path / "log.txt")
    assert [Path(c[1]).name for c in seen] == ["dst_projections.py"] * 2
    assert seen[0][2:4] == ["fit", "--register"] and seen[1][2:5] == ["project", "--week", "7"]
    assert seen[1][seen[1].index("--out") + 1] == str(tmp_path / "dst") and out["runs"][0]["run_id"] == "r"
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / "dfp-r1_2026.json").write_text("{}")
    seen.clear()
    lc.stage_dst_project(ns, tmp_path / "log.txt")
    assert seen[0][2] == "register"                                       # never refitted mid-season
    monkeypatch.setattr(lc.subprocess, "run", lambda cmd, **kw: _Proc(1, "", "SystemExit: abstains"))
    with pytest.raises(RuntimeError, match="dst_projections.py register exited 1"):
        lc.stage_dst_project(ns, tmp_path / "log.txt")

"""Weekly learning pipeline on synthetic nflverse-shaped data (invented values: code paths and
leakage guards only, never evidence of forecasting accuracy)."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import polars as pl
import pytest

from app.analytics import challenger as ch
from app.analytics import weekly_learning as wl
from app.analytics.forecast_features import load_sources
from app.analytics.independent_projection_store import league_scoring
from app.db.connection import connect, init_db
from synthetic_forecast import add_league, add_participation, generate, write

SEASONS = [2024, 2025]
UTC = timezone.utc
BEFORE_W10 = datetime(2025, 9, 9, 12, tzinfo=UTC)      # synthetic week W kicks off 2025-09-W 17:00 UTC
AFTER_W10 = datetime(2025, 9, 10, 18, tzinfo=UTC)


def _db(path, unfinished):
    con = connect(path)
    init_db(con)
    write(con, add_participation(generate(SEASONS, unfinished=unfinished)))
    add_league(con, 2025, 10)
    return con


def _src(con):
    _, scoring = league_scoring(con, 2025)
    return load_sources(con, scoring), scoring


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    con = _db(tmp_path_factory.mktemp("wl") / "w.duckdb", {2025: 10})
    src, scoring = _src(con)
    yield con, src, scoring
    con.close()


def _ifp_selection():
    return {"positions": {p: {"primary": "C", "lambda": {"B": 0.01, "C": 0.01}, "intervals": {}}
                          for p in ("QB", "RB", "WR", "TE")}, "protocol": {"train_start_season": 2020}}


def _train(src, scoring, week=10, created="2025-09-27T12:00:00+00:00", retrospective=False):
    return wl.train(src, 2025, week, ifp_selection=_ifp_selection(), ifp_report={"status": {}, "positions": {}},
                    cfg=ch.ChallengerConfig.load(), scoring_desc=scoring.describe(), created_at=created,
                    retrospective=retrospective)


# ----------------------------------------------------------------------------- readiness / finality


def test_readiness_ok_before_kickoff_and_not_pregame_after(env):
    _, src, _ = env
    r = wl.readiness(src, 2025, 10, BEFORE_W10)
    assert r["ok"], r["errors"]
    assert r["pregame"] and r["completed_weeks"][9]["teams_with_box_scores"] == 8
    assert not wl.readiness(src, 2025, 10, AFTER_W10)["pregame"]


def test_update_before_and_after_a_week_becomes_final(env, tmp_path):
    """Week 10 is unfinished: a week-11 model must refuse; once week 10 is final it trains, and a
    week whose games are only partly final never enters training."""
    _, src, _ = env
    r = wl.readiness(src, 2025, 11, BEFORE_W10)
    assert not r["ok"] and any("not final" in e for e in r["errors"])
    assert wl.training_store(src, 2025, 11)["key"].max() == 202509       # week 10 is not a training week

    con = _db(tmp_path / "later.duckdb", {2025: 11})
    later, _ = _src(con)
    assert wl.readiness(later, 2025, 11, datetime(2025, 9, 10, tzinfo=UTC))["ok"]
    assert wl.training_store(later, 2025, 11)["key"].max() == 202510
    # one week-10 game not final -> readiness error and week 10 excluded entirely
    con.execute("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL WHERE season = 2025 AND week = 10 "
                "AND game_id = (SELECT min(game_id) FROM nfl_schedules WHERE season = 2025 AND week = 10)")
    partial, _ = _src(con)
    assert not wl.readiness(partial, 2025, 11, datetime(2025, 9, 10, tzinfo=UTC))["ok"]
    assert wl.training_store(partial, 2025, 11)["key"].max() == 202509
    con.close()


def test_missing_box_scores_or_xfp_block_training(env):
    _, src, _ = env
    no_stats = type(src)(src.panel.filter(~((pl.col("week") == 9) & (pl.col("team") == "AAA") & (pl.col("season") == 2025))),
                         src.games, src.defense, src.rosters, src.injuries)
    assert any("box scores" in e for e in wl.readiness(no_stats, 2025, 10, BEFORE_W10)["errors"])
    no_xfp = type(src)(src.panel.with_columns(pl.when((pl.col("season") == 2025) & (pl.col("week") == 9))
                                              .then(None).otherwise(pl.col("xfpl")).alias("xfpl")),
                       src.games, src.defense, src.rosters, src.injuries)
    assert any("expected points" in e for e in wl.readiness(no_xfp, 2025, 10, BEFORE_W10)["errors"])


# ----------------------------------------------------------------------------- training


def test_training_never_uses_the_target_week_or_later(env):
    _, src, scoring = env
    champ, chal = _train(src, scoring)
    for art in (champ, chal):
        prov = art.config["provenance"]
        assert prov["data_cutoff_key"] < 202510 and prov["latest_included_week"] == [2025, 9]
        assert all(m.train_window[1] < 202510 for m in art.models.values())
        assert prov["config_hash"] and prov["data_hash"] and prov["scoring_fingerprint"] == scoring.fingerprint
    assert chal.version == ch.CHALLENGER_VERSION and champ.version == wl.MODEL_VERSION
    assert not any(s["start_sit_ready"] for s in chal.status.values())
    assert all(chal.status[p]["status"] == "shadow" for p in chal.models)
    assert all(chal.status[p]["status"] == "insufficient_data" for p in set(chal.status) - set(chal.models))


def test_repeated_weekly_training_is_identical(env, tmp_path):
    _, src, scoring = env
    a1, b1 = _train(src, scoring)
    a2, b2 = _train(src, scoring, created="2025-09-27T13:00:00+00:00")     # later timestamp, same inputs
    assert wl.artifact_id(a1) == wl.artifact_id(a2) and wl.artifact_id(b1) == wl.artifact_id(b2)
    id1, p1, new1 = wl.save_artifact(b1, tmp_path)
    id2, p2, new2 = wl.save_artifact(b2, tmp_path)
    assert (id1, p1, new1, new2) == (id2, p2, True, False)
    run1 = wl.predict(src, a1, b1, 2025, 10, BEFORE_W10, artifact_ids={"x": 1})
    run2 = wl.predict(src, a2, b2, 2025, 10, BEFORE_W10, artifact_ids={"x": 1})
    assert run1["inputs_hash"] == run2["inputs_hash"] and run1["rows"] == run2["rows"]


def test_artifact_scoring_and_cutoff_mismatch_are_refused(env, tmp_path):
    _, src, scoring = env
    _, chal = _train(src, scoring)
    wl.save_artifact(chal, tmp_path)
    art, _, _ = wl.latest_artifact(tmp_path, ch.CHALLENGER_VERSION, 2025, 10, scoring.fingerprint)
    with pytest.raises(wl.NotReady, match="scoring"):
        wl.latest_artifact(tmp_path, ch.CHALLENGER_VERSION, 2025, 10, "another-scoring")
    with pytest.raises(wl.NotReady, match="cutoff"):
        wl.check_artifact(art, 2025, 9, scoring.fingerprint)               # trained through week 9
    with pytest.raises(wl.NotReady, match="no ifp-2.0.0 artifact"):
        wl.latest_artifact(tmp_path, ch.CHALLENGER_VERSION, 2025, 11, scoring.fingerprint)


# ----------------------------------------------------------------------------- ledger


def test_ledger_is_write_once_and_null_without_participation(env, tmp_path):
    _, src, scoring = env
    champ, chal = _train(src, scoring)
    run = wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={"c": "x"})
    assert run["rows"] and all(r["pregame"] for r in run["rows"])
    assert all(r["p_play"] is None and r["challenger"]["expected_points"] is None for r in run["rows"])
    assert any(r["challenger"]["points"] is not None for r in run["rows"])
    p1, new1 = wl.write_run(run, tmp_path)
    p2, new2 = wl.write_run({**run, "run_id": run["run_id"] + "-again"}, tmp_path)
    assert new1 and not new2 and p1 == p2
    before = p1.read_text()
    with pytest.raises(FileExistsError):
        with open(p1, "x"):
            pass
    assert p1.read_text() == before


def test_unavailable_players_and_participation_semantics(env):
    """Only ACT/INA roster players on a team playing in W are forecast; with a participation
    artifact expected points = p_play * conditional points."""
    _, src, scoring = env
    from app.analytics.participation import ParticipationArtifact, build_store, fit_final, live_population
    champ, chal = _train(src, scoring)
    pstore = build_store(src, [(s, w) for s in SEASONS for w in range(1, 10 if s == 2025 else 18)])
    sel = {"positions": {p: {"lambda": 0.01} for p in ("QB", "RB", "WR", "TE")}, "conditional_model": {}}
    part = fit_final(pstore, sel, {"status": {}, "positions": {}}, "2025-09-27", 2020)
    assert isinstance(part, ParticipationArtifact)
    run = wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={}, participation=part)
    pop = set(live_population(src, 2025, 10)["gsis_id"].to_list())
    assert {r["gsis_id"] for r in run["rows"]} == pop
    for r in run["rows"]:
        if r["challenger"]["points"] is not None and r["p_play"] is not None:
            assert r["challenger"]["expected_points"] == pytest.approx(r["p_play"] * r["challenger"]["points"], abs=0.01)


def test_evaluation_counts_only_pregame_rows_from_earlier_artifacts(tmp_path):
    con = _db(tmp_path / "e.duckdb", {2025: 11})
    src, scoring = _src(con)
    champ, chal = _train(src, scoring)
    pre = wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={})
    late = wl.predict(src, champ, chal, 2025, 10, AFTER_W10, artifact_ids={})
    retro = wl.predict(src, champ, chal, 2025, 10, AFTER_W10, artifact_ids={}, kind="retrospective")
    assert not any(r["pregame"] for r in late["rows"])
    # make the late run look different so a careless evaluator would pick it
    for r in late["rows"]:
        r["challenger"]["points"] = 999.0
    for run in (pre, late, retro):
        wl.write_run(run, tmp_path / "ledger_root")
    runs = wl.load_ledger(tmp_path / "ledger_root")
    rep = wl.evaluate(runs, src)
    assert rep["weeks"] == [(2025, 10)]
    assert rep["positions"]["ALL"]["metrics"]["challenger"]["mae"] < 100
    assert wl.frozen_rows(runs)["run_id"].unique().to_list() == [pre["run_id"]]
    assert wl.frozen_rows(runs, "retrospective")["run_id"].unique().to_list() == [retro["run_id"]]
    assert all(s["status"] == "shadow" for s in rep["shadow_status"].values())     # 1 week << 8
    con.close()


def test_evaluation_waits_for_final_games(env, tmp_path):
    _, src, scoring = env
    champ, chal = _train(src, scoring)
    wl.write_run(wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={}), tmp_path)
    rep = wl.evaluate(wl.load_ledger(tmp_path), src)
    assert rep["weeks"] == [] and rep["positions"] == {}


def test_weekly_pipeline_never_imports_into_consumers():
    """The shadow pipeline must stay out of lineups, start/sit, trades, waivers and the API."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name in ("weekly_learning.py", "challenger.py"):
            continue
        text = path.read_text()
        assert "weekly_learning" not in text and "from .challenger" not in text, path
    assert json.loads(json.dumps(ch.SHADOW_PROMOTION_CRITERIA))["min_prospective_weeks"] == 8

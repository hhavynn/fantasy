"""Weekly learning pipeline on synthetic nflverse-shaped data (invented values: code paths and
leakage guards only, never evidence of forecasting accuracy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
# Synthetic data has no sync record: tests state when the nflverse schedule was fetched. Every synthetic
# game kicked off well before this, so a scored game is presumed final (wl.game_presumed_final).
FETCHED = datetime(2025, 12, 1, tzinfo=UTC)


def _ready(*a, fetched_at=FETCHED):
    return wl.readiness(*a, fetched_at=fetched_at)


def _evaluate(*a, fetched_at=FETCHED, **k):
    return wl.evaluate(*a, fetched_at={2025: fetched_at}, **k)


def _label(*a, fetched_at=FETCHED):
    return wl.label_outcomes(*a, fetched_at={2025: fetched_at})


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
    r = _ready(src, 2025, 10, BEFORE_W10)
    assert r["ok"], r["errors"]
    assert r["pregame"] and r["completed_weeks"][9]["teams_with_box_scores"] == 8
    assert not _ready(src, 2025, 10, AFTER_W10)["pregame"]


def test_update_before_and_after_a_week_becomes_final(env, tmp_path):
    """Week 10 is unfinished: a week-11 model must refuse; once week 10 is final it trains, and a
    week whose games are only partly final never enters training."""
    _, src, _ = env
    r = _ready(src, 2025, 11, BEFORE_W10)
    assert not r["ok"] and any("not final" in e for e in r["errors"])
    assert wl.training_store(src, 2025, 11)["key"].max() == 202509       # week 10 is not a training week

    con = _db(tmp_path / "later.duckdb", {2025: 11})
    later, _ = _src(con)
    assert _ready(later, 2025, 11, datetime(2025, 9, 10, tzinfo=UTC))["ok"]
    assert wl.training_store(later, 2025, 11)["key"].max() == 202510
    # one week-10 game not final -> readiness error and week 10 excluded entirely
    con.execute("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL WHERE season = 2025 AND week = 10 "
                "AND game_id = (SELECT min(game_id) FROM nfl_schedules WHERE season = 2025 AND week = 10)")
    partial, _ = _src(con)
    assert not _ready(partial, 2025, 11, datetime(2025, 9, 10, tzinfo=UTC))["ok"]
    assert wl.training_store(partial, 2025, 11)["key"].max() == 202509
    con.close()


def test_missing_box_scores_or_xfp_block_training(env):
    _, src, _ = env
    no_stats = type(src)(src.panel.filter(~((pl.col("week") == 9) & (pl.col("team") == "AAA") & (pl.col("season") == 2025))),
                         src.games, src.defense, src.rosters, src.injuries)
    assert any("box scores" in e for e in _ready(no_stats, 2025, 10, BEFORE_W10)["errors"])
    no_xfp = type(src)(src.panel.with_columns(pl.when((pl.col("season") == 2025) & (pl.col("week") == 9))
                                              .then(None).otherwise(pl.col("xfpl")).alias("xfpl")),
                       src.games, src.defense, src.rosters, src.injuries)
    assert any("expected points" in e for e in _ready(no_xfp, 2025, 10, BEFORE_W10)["errors"])


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
    rep = _evaluate(runs, src)
    assert rep["weeks"] == [(2025, 10)]
    assert rep["positions"]["ALL"]["metrics"]["challenger"]["mae"] < 100
    assert wl.frozen_rows(runs)["run_id"].unique().to_list() == [pre["run_id"]]
    assert wl.frozen_rows(runs, "retrospective")["run_id"].unique().to_list() == [retro["run_id"]]
    assert all(s["status"] == "insufficient_evidence" and s["lifecycle"] == "research_shadow"
               for s in rep["shadow_status"].values())                         # 1 week << 8
    con.close()


def test_evaluation_waits_for_final_games(env, tmp_path):
    _, src, scoring = env
    champ, chal = _train(src, scoring)
    wl.write_run(wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={}), tmp_path)
    rep = _evaluate(wl.load_ledger(tmp_path), src)
    assert rep["weeks"] == [] and rep["positions"] == {}


def test_weekly_pipeline_never_imports_into_consumers():
    """The shadow pipeline must stay out of lineups, start/sit, trades, waivers and the API."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        # adaptive_workload.py: research-only ifp-aw estimator built on the challenger (never imported, see
        # tests/test_adaptive_workload.py::test_research_module_is_never_imported_by_the_app)
        # hgb_research.py: research-only gradient-boosting challenger built on the challenger (never imported,
        # see tests/test_hgb_research.py::test_research_model_cannot_silently_become_production)
        # opportunity_efficiency.py: research-only two-target model built on the challenger (never imported, see
        # tests/test_opportunity_efficiency.py::test_research_module_is_never_imported_by_the_app)
        # model_registry.py: research-only dry-run promotion gate that reads weekly_learning's criteria (never
        # imported, see tests/test_model_registry.py::test_hosted_app_never_reads_the_registry_or_research_packages)
        # usage_signals.py: research-only FTN read / depth-chart signals built on the challenger (never imported, see
        # tests/test_usage_signals.py::test_research_module_is_never_imported_by_the_app)
        # context_challenger.py: research-only QB/teammate context challenger built on the challenger (never
        # imported, see tests/test_roster_context.py::test_app_never_imports_the_research_context_modules)
        if path.name in ("weekly_learning.py", "challenger.py", "source_provenance.py", "adaptive_workload.py",
                         "hgb_research.py", "opportunity_efficiency.py",
                         "espn_benchmark.py",   # research-only ESPN benchmark; reads label_outcomes (never imported)
                         "model_registry.py", "usage_signals.py", "context_challenger.py",
                         "injury_challenger.py"):   # research-only workload handoff challenger (never imported, see
            # tests/test_injury_workload.py::test_app_never_imports_the_injury_research_modules)
            continue
        text = path.read_text()
        assert "weekly_learning" not in text and "from .challenger" not in text, path
        assert "source_provenance" not in text, path
    assert json.loads(json.dumps(ch.SHADOW_PROMOTION_CRITERIA))["min_prospective_weeks"] == 8


# ----------------------------------------------------------------------------- outcome finality (partial weeks)
#
# Week 10 of the synthetic 2025 season is fully final in `final_env`. Each test derives a Sources
# view in which some week-10 games have not finished yet (no score, no box scores, no snaps), as
# when Thursday or Sunday games are final and later games are still pending.


def _expected(run: dict, factor: float = 0.9) -> dict:
    """Give every row an unconditional expected score (as a participation artifact would)."""
    for r in run["rows"]:
        for m in ("champion", "challenger"):
            pts = r[m]["points"]
            r[m]["expected_points"] = None if pts is None else round(pts * factor, 2)
    return run


@pytest.fixture(scope="module")
def final_env(tmp_path_factory):
    con = _db(tmp_path_factory.mktemp("fin") / "f.duckdb", {2025: 11})
    src, scoring = _src(con)
    champ, chal = _train(src, scoring)
    run = _expected(wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids={}))
    yield src, scoring, champ, chal, run
    con.close()


def _week10_games(src) -> list[tuple[str, str]]:
    g = src.games.filter((pl.col("season") == 2025) & (pl.col("week") == 10) & pl.col("is_home"))
    return sorted((r["team"], r["opponent"]) for r in g.iter_rows(named=True))


def _pending(src, teams: set[str]):
    """Week-10 games of `teams` not final: no score, and nflverse has not published their stats."""
    from dataclasses import replace
    wk = (pl.col("season") == 2025) & (pl.col("week") == 10) & pl.col("team").is_in(sorted(teams))
    games = src.games.with_columns([pl.when(wk).then(None).otherwise(pl.col(c)).alias(c)
                                    for c in ("points_for", "points_against")])
    return replace(src, games=games, panel=src.panel.filter(~wk))


def _teams(games) -> set[str]:
    return {t for g in games for t in g}


def _assert_never_zero_filled(rows: pl.DataFrame) -> None:
    unresolved = rows.filter(~pl.col("outcome_status").is_in(wl.RESOLVED))
    assert unresolved["label"].null_count() == unresolved.height, "an unresolved outcome received a label"
    absent = rows.filter(pl.col("outcome_status") == wl.CONFIRMED_ABSENT)
    assert (absent["label"] == 0.0).all()


def test_thursday_final_rest_pending_is_not_evidence(final_env):
    src, *_, run = final_env
    games = _week10_games(src)
    thursday, rest = games[:1], games[1:]
    part = _pending(src, _teams(rest))
    rows = _label(wl.frozen_rows([run]), part)
    _assert_never_zero_filled(rows)
    by_team = {t: set(rows.filter(pl.col("team") == t)["outcome_status"].to_list()) for t in _teams(games)}
    assert all(by_team[t] <= set(wl.RESOLVED) for t in _teams(thursday))
    assert all(by_team[t] == {wl.PENDING_GAME} for t in _teams(rest))

    rep = _evaluate([run], part)
    assert rep["weeks"] == [] and rep["positions"] == {}
    assert not rep["finality"]["2025-10"]["fully_final"]
    assert rep["finality"]["2025-10"]["pending_teams"] == sorted(_teams(rest))
    pre = rep["preliminary"]
    n_thu = rows.filter(pl.col("team").is_in(sorted(_teams(thursday)))).height
    assert pre["weeks"] == [(2025, 10)] and pre["n_resolved"] == n_thu
    assert pre["n_unresolved"] == rows.height - n_thu
    # The old evaluator zero-filled every pending player into the unconditional metrics.
    assert pre["positions"]["ALL"]["unconditional"]["n"] < rows.height
    assert pre["positions"]["ALL"]["unconditional"]["n"] == rows.filter(
        pl.col("outcome_status").is_in(wl.RESOLVED) & pl.col("exp_champion").is_not_null()
        & pl.col("exp_challenger").is_not_null()).height
    # promotion: an incomplete week counts as zero prospective weeks
    assert rep["outcomes"]["evidence"] == {s: 0 for s in wl.OUTCOME_STATUSES}
    for st in rep["shadow_status"].values():
        assert st["status"] != "eligible_for_review"
        assert any(r.startswith("0 prospective weeks") for r in st["reasons"])


def test_sunday_final_monday_pending(final_env):
    src, *_, run = final_env
    games = _week10_games(src)
    monday = games[-1:]
    part = _pending(src, _teams(monday))
    rep = _evaluate([run], part)
    assert rep["weeks"] == [] and rep["preliminary"]["weeks"] == [(2025, 10)]
    assert rep["outcomes"]["not_yet_evidence"][wl.PENDING_GAME] == sum(
        r["team"] in _teams(monday) for r in run["rows"])
    rows = _label(wl.frozen_rows([run]), part)
    _assert_never_zero_filled(rows)
    # a player whose own game is final is resolved even though another team's game is not
    sunday_player = rows.filter(pl.col("team") == games[0][0]).row(0, named=True)
    monday_player = rows.filter(pl.col("team") == monday[0][0]).row(0, named=True)
    assert sunday_player["outcome_status"] in wl.RESOLVED and sunday_player["label"] is not None
    assert monday_player["outcome_status"] == wl.PENDING_GAME and monday_player["label"] is None


def test_fully_completed_week_is_evidence(final_env):
    src, *_, run = final_env
    rep = _evaluate([run], src)
    assert rep["weeks"] == [(2025, 10)] and "preliminary" not in rep
    assert rep["finality"]["2025-10"] == {"scheduled_teams": 8, "final_teams": 8, "complete_teams": 8,
                                          "pending_teams": [], "missing_ingestion_teams": [],
                                          "forecast_teams_missing_from_schedule": [], "fully_final": True}
    ev = rep["outcomes"]["evidence"]
    assert ev[wl.APPEARED] + ev[wl.CONFIRMED_ABSENT] == len(run["rows"])
    assert rep["positions"]["ALL"]["n"] == ev[wl.APPEARED] - sum(
        r["champion"]["points"] is None or r["challenger"]["points"] is None for r in run["rows"]
        if r["gsis_id"] in set(src.panel.filter((pl.col("season") == 2025) & (pl.col("week") == 10))["gsis_id"]))


def test_confirmed_inactive_player_in_final_game_is_zero(final_env):
    src, *_, run = final_env
    rows = _label(wl.frozen_rows([run]), src)
    ina = set(src.rosters.filter((pl.col("season") == 2025) & (pl.col("week") == 10)
                                 & (pl.col("status") == "INA"))["gsis_id"].to_list())
    absent = rows.filter(pl.col("outcome_status") == wl.CONFIRMED_ABSENT)
    assert ina and ina <= set(absent["gsis_id"].to_list())
    assert (absent["label"] == 0.0).all()
    # unconditional includes the confirmed zeros, conditional does not
    rep = _evaluate([run], src)["positions"]["ALL"]
    with_exp = rows.filter(pl.col("exp_champion").is_not_null() & pl.col("exp_challenger").is_not_null())
    assert rep["unconditional"]["n"] == with_exp.height
    assert rep["n"] <= rows.filter(pl.col("outcome_status") == wl.APPEARED).height


def test_final_schedule_without_player_stats_is_missing_ingestion(final_env):
    from dataclasses import replace
    src, *_, run = final_env
    team = _week10_games(src)[0][0]
    wk = (pl.col("season") == 2025) & (pl.col("week") == 10) & (pl.col("team") == team)
    no_box = replace(src, panel=src.panel.filter(~wk))                         # score final, stats not synced
    no_snaps = replace(src, panel=src.panel.with_columns(
        pl.when(wk).then(None).otherwise(pl.col("offense_pct")).alias("offense_pct")))
    for view in (no_box, no_snaps):
        rows = _label(wl.frozen_rows([run]), view)
        _assert_never_zero_filled(rows)
        assert set(rows.filter(pl.col("team") == team)["outcome_status"].to_list()) == {wl.MISSING_INGESTION}
        rep = _evaluate([run], view)
        assert rep["weeks"] == [] and rep["finality"]["2025-10"]["missing_ingestion_teams"] == [team]


def test_bye_week_player_is_excluded_not_zero(final_env):
    """A forecast row with no game (no kickoff at forecast time) for a team the schedule does not list."""
    import copy
    from dataclasses import replace
    src, *_, run = final_env
    home, away = _week10_games(src)[0]
    run = copy.deepcopy(run)
    for r in run["rows"]:
        if r["team"] in (home, away):
            r["kickoff"] = None                                   # no game when the forecast was made
    wk = (pl.col("season") == 2025) & (pl.col("week") == 10) & pl.col("team").is_in([home, away])
    bye = replace(src, games=src.games.filter(~wk), panel=src.panel.filter(~wk))   # and none now
    rows = _label(wl.frozen_rows([run]), bye)
    _assert_never_zero_filled(rows)
    assert set(rows.filter(pl.col("team").is_in([home, away]))["outcome_status"].to_list()) == {wl.BYE}
    rep = _evaluate([run], bye)
    assert rep["weeks"] == [(2025, 10)]                       # the remaining games are all final
    assert rep["outcomes"]["evidence"][wl.BYE] == rows.filter(pl.col("team").is_in([home, away])).height
    assert rep["positions"]["ALL"]["unconditional"]["n"] == rows.filter(
        pl.col("outcome_status").is_in(wl.RESOLVED) & pl.col("exp_champion").is_not_null()
        & pl.col("exp_challenger").is_not_null()).height


def test_game_dropped_from_the_schedule_keeps_the_week_open(final_env):
    """The forecasts saw a scheduled game (kickoff) that the current schedule no longer lists (an
    incomplete schedule download): its players are missing data, never byes, and the week is not
    evidence even though every listed game is final."""
    from dataclasses import replace
    src, *_, run = final_env
    home, away = _week10_games(src)[0]
    wk = (pl.col("season") == 2025) & (pl.col("week") == 10) & pl.col("team").is_in([home, away])
    partial_schedule = replace(src, games=src.games.filter(~wk))
    rows = _label(wl.frozen_rows([run]), partial_schedule)
    _assert_never_zero_filled(rows)
    assert set(rows.filter(pl.col("team").is_in([home, away]))["outcome_status"].to_list()) == {wl.MISSING_INGESTION}
    rep = _evaluate([run], partial_schedule)
    assert rep["weeks"] == [] and rep["preliminary"]["weeks"] == [(2025, 10)]
    assert rep["finality"]["2025-10"]["forecast_teams_missing_from_schedule"] == sorted([home, away])
    assert all(st["status"] != "eligible_for_review" and any(r.startswith("0 prospective weeks") for r in st["reasons"])
               for st in rep["shadow_status"].values())


def test_live_score_is_not_presumed_final(final_env):
    """nflverse has no game-status field and a score may be live: a score fetched soon after kickoff,
    or at an unknown time, never makes a game final (schedule_intelligence's rule)."""
    src, *_, run = final_env
    kick = datetime(2025, 9, 10, 17, tzinfo=UTC)                 # every synthetic week-10 game
    assert wl.game_presumed_final(21, 17, kick, kick + timedelta(hours=9, minutes=1)) == (True, None)
    for fetched in (kick + timedelta(hours=3), None):
        ok, why = wl.game_presumed_final(21, 17, kick, fetched)
        assert not ok and why
        rows = _label(wl.frozen_rows([run]), src, fetched_at=fetched)
        _assert_never_zero_filled(rows)
        assert set(rows["outcome_status"].to_list()) == {wl.PENDING_GAME}
        rep = _evaluate([run], src, fetched_at=fetched)
        assert rep["weeks"] == [] and rep["outcomes"]["evidence"] == {s: 0 for s in wl.OUTCOME_STATUSES}
    # the same rule gates training: a week-9 score fetched mid-game blocks a week-10 model
    r = _ready(src, 2025, 10, BEFORE_W10, fetched_at=datetime(2025, 9, 9, 19, tzinfo=UTC))
    assert not r["ok"] and any("cannot be presumed final" in e for e in r["errors"])
    assert not _ready(src, 2025, 10, BEFORE_W10, fetched_at=None)["ok"]


def test_unresolved_identity_is_excluded_not_zero(final_env):
    import copy
    src, *_, run = final_env
    run = copy.deepcopy(run)
    games = _week10_games(src)
    rows = [r for r in run["rows"] if r["team"] == games[0][0]]
    rows[0]["gsis_id"] = "00-NOT-A-PLAYER"                       # id not on any roster or box score
    rows[1]["team"] = games[1][0]                                # forecast says another (final) team
    out = _label(wl.frozen_rows([run]), src)
    _assert_never_zero_filled(out)
    bad = out.filter(pl.col("gsis_id").is_in([rows[0]["gsis_id"], rows[1]["gsis_id"]]))
    assert set(bad["outcome_status"].to_list()) == {wl.UNRESOLVED_IDENTITY}


def test_latest_pregame_snapshot_per_player_and_retrospective_excluded(final_env):
    import copy
    src, scoring, champ, chal, run = final_env
    early = copy.deepcopy(run)
    later = _expected(wl.predict(src, champ, chal, 2025, 10, datetime(2025, 9, 10, 9, tzinfo=UTC), artifact_ids={}))
    for r in later["rows"]:                                      # e.g. a late injury report changed the forecast
        if r["challenger"]["points"] is not None:
            r["challenger"]["points"] += 1.0
    retro = wl.predict(src, champ, chal, 2025, 10, AFTER_W10, artifact_ids={}, kind="retrospective")
    for r in retro["rows"]:
        r["challenger"]["points"] = 999.0
    runs = [later, early, retro]                                 # order does not matter
    fr = wl.frozen_rows(runs)
    assert fr["run_id"].unique().to_list() == [later["run_id"]]
    rep = _evaluate(runs, src)
    alone = _evaluate([later], src)
    assert rep["positions"] == alone["positions"] and rep["weeks"] == [(2025, 10)]
    assert _evaluate([retro], src, "pregame")["weeks"] == []     # retrospective is never pregame evidence
    assert _evaluate(runs, src, "retrospective")["kind"] == "retrospective"


def test_reevaluation_after_the_last_game_is_final(final_env):
    src, *_, run = final_env
    monday = _teams(_week10_games(src)[-1:])
    before = _evaluate([run], _pending(src, monday))
    after = _evaluate([run], src)
    again = _evaluate([run], src)
    assert before["weeks"] == [] and after["weeks"] == [(2025, 10)]
    assert after == again                                        # deterministic, ledger untouched
    pre, fin = before["preliminary"]["positions"]["ALL"], after["positions"]["ALL"]
    assert pre["unconditional"]["n"] < fin["unconditional"]["n"]
    assert len(run["rows"]) == sum(after["outcomes"]["evidence"].values())


def test_evaluation_reports_criteria_and_starters_pool_on_identical_rows(final_env):
    src, scoring, champ, chal, run = final_env
    rep = _evaluate([run], src)
    for pos in ("QB", "RB", "WR", "TE", "ALL"):
        r = rep["positions"][pos]
        assert r["metrics"]["champion"]["n"] == r["metrics"]["challenger"]["n"] == r["n"]
        assert r["starters_pool"]["n"] <= r["n"] and (r["starters_pool"]["n"] > 0) == (r["n"] > 0)
    # synthetic TE has no challenger model: no paired rows, so its starters guard is missing evidence
    assert rep["positions"]["TE"]["n"] == 0
    te = {c["name"]: c["result"] for c in rep["shadow_status"]["TE"]["criteria"]}
    assert te["starters_pool_mae"] == te["mae_vs_champion"] == te["bias_guard"] == wl.INSUFFICIENT
    st = rep["shadow_status"]["WR"]
    assert {c["name"]: c["result"] for c in st["criteria"]}["prospective_weeks"] == wl.INSUFFICIENT
    assert st["status"] == "insufficient_evidence" and rep["promotion_blockers"]["WR"]["blocking"]
    retro = wl.predict(src, champ, chal, 2025, 10, AFTER_W10, artifact_ids={}, kind="retrospective")
    rr = _evaluate([retro], src, "retrospective")
    assert all(s["status"] != "eligible_for_review" for s in rr["shadow_status"].values())
    assert all(s["criteria"][0]["result"] == wl.FAIL for s in rr["shadow_status"].values())


def test_starters_pool_is_ranked_before_appearance_is_known(monkeypatch):
    """The pool is the pregame top-N by A1: an inactive top player leaves a hole, he is never
    replaced by the next-ranked player who happened to appear."""
    from app.analytics import forecast_backtest as fb
    monkeypatch.setitem(fb.PAIR_POOL, "WR", 2)
    rows = pl.DataFrame({
        "season": [2025] * 3, "week": [10] * 3, "key": [202510] * 3, "gsis_id": ["a", "b", "c"], "position": ["WR"] * 3,
        "outcome_status": [wl.CONFIRMED_ABSENT, wl.APPEARED, wl.APPEARED], "label": [0.0, 12.0, 8.0],
        "pred_A1": [20.0, 15.0, 10.0], "pred_A2": [20.0, 15.0, 10.0], "pred_champion": [18.0, 14.0, 9.0],
        "pred_challenger": [17.0, 13.0, 9.5], "low_champion": [None] * 3, "high_champion": [None] * 3,
        "low_challenger": [None] * 3, "high_challenger": [None] * 3, "exp_champion": [None] * 3,
        "exp_challenger": [None] * 3, "last_injury_exit": [0, 0, 0]},
        schema_overrides={c: pl.Float64 for c in ("low_champion", "high_champion", "low_challenger", "high_challenger",
                                                   "exp_champion", "exp_challenger")})
    sp = wl._position_reports(rows, 1, bootstrap=False)["WR"]["starters_pool"]
    assert sp["n"] == 1 and sp["mae"] == {"champion": 2.0, "challenger": 1.0}      # only "b", never "c"


# ----------------------------------------------------------------------------- injury retrieval warnings


def _log_obs(con, source, team, retrieved, scope=None):
    from app.analytics import injury_freshness as inj
    week = None if source == inj.SOURCE_ESPN else 10
    scope = scope or (inj.SCOPE_TEAM_CURRENT if source == inj.SOURCE_ESPN else inj.SCOPE_TEAM_WEEK)
    inj.record_observations(con, [inj.Observation(source, scope, 2025, week, team, retrieved, "a" * 64, 3, 0, None,
                                                  retrieved - timedelta(seconds=30), "r1")])


def test_readiness_carries_injury_freshness_warnings_without_changing_anything_else(tmp_path):
    """Week 10 kicks off 2025-09-10 17:00 UTC for every synthetic team; BEFORE_W10 is 29 h earlier."""
    from app.analytics import injury_freshness as inj
    con = _db(tmp_path / "w.duckdb", {2025: 10})
    src, _ = _src(con)
    plain = _ready(src, 2025, 10, BEFORE_W10)
    con.execute("INSERT INTO nfl_injuries (season, game_type, team, week, gsis_id, report_status) "
                "VALUES (2025, 'REG', 'AAA', 10, 'g-aaa', 'Questionable')")       # AAA has a designation ...
    _log_obs(con, inj.SOURCE_NFLVERSE, "AAA", BEFORE_W10 - timedelta(hours=40))        # ... retrieved 69 h pregame: stale
    _log_obs(con, inj.SOURCE_ESPN, "BBB", BEFORE_W10 - timedelta(hours=1))             # fresh ESPN fallback, nobody listed
    with_log = wl.readiness(src, 2025, 10, BEFORE_W10, fetched_at=FETCHED, con=con)
    for key in plain:                                          # status, errors, gates: identical
        if key != "warnings":
            assert with_log[key] == plain[key], key
    assert with_log["ok"] and with_log["pregame"] and with_log["errors"] == plain["errors"]
    assert with_log["warnings"][:len(plain["warnings"])] == plain["warnings"]
    extra = with_log["warnings"][len(plain["warnings"]):]
    text = "\n".join(extra)
    assert "stale injury evidence" in text and "AAA" in text
    assert "no injury evidence retrieved" in text and "CCC" in text
    assert "ESPN status fallback adds no designations for BBB" in text
    assert "informational only" in text
    info = with_log["injury_freshness"]
    assert info["log_available"] and info["fallback_adds_no_designations"] == ["BBB"]
    assert {i["team"] for i in info["upcoming_issues"]} == {"AAA", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"}
    assert "injury_freshness" not in plain
    con.close()


def test_readiness_survives_an_absent_injury_retrieval_log(tmp_path):
    con = _db(tmp_path / "old.duckdb", {2025: 10})
    src, _ = _src(con)
    plain = _ready(src, 2025, 10, BEFORE_W10)
    con.execute("DROP TABLE injury_retrievals")                 # an old database from before the log existed
    rep = wl.readiness(src, 2025, 10, BEFORE_W10, fetched_at=FETCHED, con=con)
    assert rep["ok"] == plain["ok"] and rep["errors"] == plain["errors"] and rep["pregame"] == plain["pregame"]
    assert rep["warnings"][len(plain["warnings"])].startswith("injury retrieval log unavailable")
    assert rep["injury_freshness"]["log_available"] is False and rep["injury_freshness"]["upcoming_issues"] == []
    con.close()


def test_injury_warnings_never_change_a_forecast(env, tmp_path):
    """The same inputs give the same ledger rows and inputs hash whatever the readiness warnings say."""
    _, src, scoring = env
    champ, chal = _train(src, scoring)
    ids = {"champion": {"version": champ.version, "id": wl.artifact_id(champ)},
           "challenger": {"version": chal.version, "id": wl.artifact_id(chal)}}
    base = wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids=ids,
                      readiness_report=wl.readiness(src, 2025, 10, BEFORE_W10, fetched_at=FETCHED))
    con = _db(tmp_path / "noisy.duckdb", {2025: 10})
    noisy = wl.predict(src, champ, chal, 2025, 10, BEFORE_W10, artifact_ids=ids,
                       readiness_report=wl.readiness(src, 2025, 10, BEFORE_W10, fetched_at=FETCHED, con=con))
    con.close()
    assert noisy["inputs_hash"] == base["inputs_hash"] and noisy["rows"] == base["rows"]
    assert noisy["readiness"]["warnings"] != base["readiness"]["warnings"]

"""Prospective ESPN benchmark (app.analytics.espn_benchmark). Synthetic data only, temporary DuckDB files.

World: 8 NFL teams x 7 skill players, season 2026. Weeks 1-6 are final (an NFL sync fetched long after
kickoff is on record), week 7 is scheduled but unplayed. Every player-week has an ESPN snapshot taken 48 h
before the kickoff (error exactly 4 points) and a production run created 24 h before it (error exactly
1 point), so MAE is 4 for ESPN and 1 for the model unless a test changes something."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import duckdb
import numpy as np
import polars as pl
import pytest

from app.analytics import espn_benchmark as eb
from app.analytics import weekly_learning as wl
from app.analytics.forecast_backtest import PROMOTION_CRITERIA
from app.analytics.forecast_features import load_panel
from app.analytics.independent_projections import MODEL_VERSION
from app.analytics.league_scoring import resolve_skill_scoring

from synthetic_forecast import SCORING_ITEMS, SLOTS, TEAMS, add_league, database, gsis
from synthetic_nfl import record_fetch

UTC = timezone.utc
SCORING = resolve_skill_scoring(SCORING_ITEMS)
FP = SCORING.fingerprint
FETCHED = "2026-12-30 00:00:00"
ARTIFACT = "2026-08-01T00:00:00+00:00"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "espn_benchmark.py"
BACKEND = Path(__file__).resolve().parents[1]
FINAL_WEEKS, ALL_WEEKS = range(1, 7), range(1, 8)
PLAYERS = [(1000 + i, team, slot, pos) for i, (team, (slot, pos)) in
           enumerate((t, s) for t in TEAMS for s in SLOTS)]
ESPN_ID = {gsis(t, s): pid for pid, t, s, _ in PLAYERS}


def kickoff(week: int) -> datetime:
    """Synthetic week W: 2026-09-0W 13:00 US Eastern = 17:00 UTC (weeks 1-9)."""
    return datetime(2026, 9, week, 17, tzinfo=UTC)


def naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path / "bench.duckdb")


def make_world(path, absent=(), skip_stats_teams=()):
    con = database(path, [2026], unfinished={2026: 7})
    add_league(con, 2026, 7)
    record_fetch(con, FETCHED, season=2026)
    roster = [{"season": 2026, "week": w, "gsis_id": gsis(t, s), "team": t, "game_type": "REG", "position": pos,
               "status": "ACT", "status_description_abbr": "A01"} for w in range(1, 8) for _, t, s, pos in PLAYERS]
    con.register("_r", pl.DataFrame(roster).to_arrow())
    con.execute("CREATE TABLE nfl_rosters_weekly AS SELECT * FROM _r")
    for pid, t, s, pos in PLAYERS:
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence, "
                    "espn_position) VALUES (?, ?, 'espn_id:nfl_players', 'high', ?)", [pid, gsis(t, s), pos])
    actual = {(r["week"], r["gsis_id"]): r["league_points"] for r in load_panel(con, SCORING, [2026]).iter_rows(named=True)}
    for w in ALL_WEEKS:
        con.execute("INSERT INTO independent_projection_runs (run_id, season, week, model_version, "
                    "scoring_fingerprint, artifact_created_at, created_at) VALUES (?, 2026, ?, ?, ?, ?, ?)",
                    [f"run-w{w}", w, MODEL_VERSION, FP, ARTIFACT, naive(kickoff(w) - timedelta(hours=24))])
    for i, (pid, t, s, pos) in enumerate(PLAYERS):
        g = gsis(t, s)
        for w in ALL_WEEKS:
            a = actual.get((w, g), 8.0)
            sign = 1 if (i + w) % 2 else -1
            espn, ours = a + 4 * sign, a + 1 * sign
            synced = kickoff(w) - timedelta(hours=48)
            con.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, ?, ?, ?)",
                        [synced.date(), pid, w, espn, naive(synced)])
            con.execute("INSERT INTO independent_player_projections (season, week, espn_player_id, gsis_id, run_id, "
                        "position, nfl_team, coverage_status, projected_points, p_play, expected_points) "
                        "VALUES (2026, ?, ?, ?, ?, ?, ?, 'projected', ?, 0.9, ?)",
                        [w, pid, g, f"run-w{w}", pos, t, ours / 0.9, ours])
    for w, g in absent:
        con.execute("DELETE FROM nfl_weekly_stats WHERE player_id = ? AND week = ?", [g, w])
        con.execute("DELETE FROM nfl_snap_counts WHERE pfr_player_id = ? AND week = ?", [f"pfr-{g}", w])
    for w, team in skip_stats_teams:
        con.execute("DELETE FROM nfl_weekly_stats WHERE team = ? AND week = ?", [team, w])
    return con


def rows_of(con, **kw):
    kw.setdefault("scoring", SCORING)
    rows, meta = eb.evaluation_rows(con, 2026, **kw)
    return rows, meta


def one(rows, week, gsis_id):
    [r] = [r for r in rows if r["week"] == week and r["gsis_id"] == gsis_id]
    return r


def pid_row(rows, week, pid):
    [r] = [r for r in rows if r["week"] == week and r["espn_player_id"] == pid]
    return r


def mutating_reader(fn):
    def reader(con, season, weeks=None, *, model_version, scoring_fingerprint):
        rows, ex = eb.legacy_forecast_reader(con, season, weeks, model_version=model_version,
                                             scoring_fingerprint=scoring_fingerprint)
        return [fn(dict(r)) for r in rows], ex
    return reader


@pytest.fixture
def easy_gate(monkeypatch):
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 1)


G_WR = gsis("AAA", "WR1")


# ----------------------------------------------------------------------------- row schema and baseline


def test_rows_have_exactly_the_contract_keys_and_horizons(world):
    rows, meta = rows_of(world)
    assert len(rows) == 56 * 7 and all(tuple(r) == eb.ROW_KEYS for r in rows)
    r = one(rows, 3, G_WR)
    assert r["exclusion_reason"] is None and r["outcome_status"] == wl.APPEARED
    assert r["ours_origin"] == "ours" and r["run_id"] == "run-w3"
    assert r["espn_horizon_h"] == 48.0 and r["ours_horizon_h"] == 24.0
    assert r["kickoff"] == kickoff(3) and r["espn_captured_at"] == kickoff(3) - timedelta(hours=48)
    assert abs(r["espn_pred"] - r["actual_points"]) == pytest.approx(4)
    assert abs(r["ours_independent_pred"] - r["actual_points"]) == pytest.approx(1)
    assert r["ours_consumer_pred"] == r["ours_independent_pred"]
    assert r["ours_conditional_pred"] == pytest.approx(r["ours_independent_pred"] / 0.9)
    assert meta["scoring_fingerprint"] == FP and meta["weeks"] == list(ALL_WEEKS)


def test_pending_game_is_never_scored(world):
    rows, _ = rows_of(world)
    wk7 = [r for r in rows if r["week"] == 7]
    assert {r["outcome_status"] for r in wk7} == {wl.PENDING_GAME}
    assert {r["exclusion_reason"] for r in wk7} == {"outcome_pending_game"}
    assert all(r["actual_points"] is None for r in wk7)


def test_missing_ingestion_is_never_scored(tmp_path):
    con = make_world(tmp_path / "m.duckdb", skip_stats_teams=[(2, "CCC")])
    rows, _ = rows_of(con)
    ccc = [r for r in rows if r["week"] == 2 and r["team"] == "CCC"]
    assert len(ccc) == 7 and {r["outcome_status"] for r in ccc} == {wl.MISSING_INGESTION}
    assert {r["exclusion_reason"] for r in ccc} == {"outcome_missing_ingestion"}
    assert all(r["actual_points"] is None for r in ccc)
    card = eb.scorecard(con, 2026, scoring=SCORING)
    # a week with a team still missing its data is not fully final: the whole week is preliminary, not evidence
    assert card["comparisons"]["pure_independent"]["n"] == 56 * 5
    assert {r["exclusion_reason"] for r in rows if r["week"] == 2 and r["team"] != "CCC"} == {"week_not_fully_final"}
    assert card["partial_weeks"] == [2, 7] and card["fully_final_weeks"] == [1, 3, 4, 5, 6]


# ----------------------------------------------------------------------------- temporal eligibility


def test_post_kickoff_espn_snapshot_is_not_used(world):
    world.execute("DELETE FROM espn_projection_history WHERE espn_player_id = ? AND scoring_period = 3",
                  [ESPN_ID[G_WR]])
    after = kickoff(3) + timedelta(minutes=1)
    world.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, 3, 99.0, ?)",
                  [after.date(), ESPN_ID[G_WR], naive(after)])
    rows, _ = rows_of(world)
    r = one(rows, 3, G_WR)
    assert r["exclusion_reason"] == "espn_not_pregame" and r["espn_pred"] is None
    # the instant of kickoff is not pregame either (strict inequality)
    world.execute("UPDATE espn_projection_history SET synced_at = ? WHERE espn_player_id = ? AND scoring_period = 3",
                  [naive(kickoff(3)), ESPN_ID[G_WR]])
    assert one(rows_of(world)[0], 3, G_WR)["exclusion_reason"] == "espn_not_pregame"
    # an earlier snapshot is preferred over a later post-kickoff one: the latest PREGAME snapshot wins
    early = kickoff(3) - timedelta(hours=30)
    world.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, 3, 7.5, ?)",
                  [early.date(), ESPN_ID[G_WR], naive(early)])
    r = one(rows_of(world)[0], 3, G_WR)
    assert r["exclusion_reason"] is None and r["espn_pred"] == 7.5 and r["espn_horizon_h"] == 30.0


def test_no_espn_snapshot_is_counted_when_a_forecast_exists(world):
    world.execute("DELETE FROM espn_projection_history WHERE espn_player_id = ? AND scoring_period = 3",
                  [ESPN_ID[G_WR]])
    assert one(rows_of(world)[0], 3, G_WR)["exclusion_reason"] == "no_espn_snapshot"


def test_post_kickoff_ours_is_excluded_by_the_reader_and_by_the_benchmark(world):
    # (a) the legacy reader never returns a forecast made at or after the player's own kickoff
    world.execute("UPDATE independent_projection_runs SET created_at = ? WHERE run_id = 'run-w3'",
                  [naive(kickoff(3))])
    rows, meta = rows_of(world)
    r = one(rows, 3, G_WR)
    assert r["exclusion_reason"] == "no_independent_forecast_for_week" and r["ours_independent_pred"] is None
    assert any(e["run_id"] == "run-w3" and "at or after" in e["reason"] and e["players"] == 56
               for e in meta["reader_exclusions"])
    # (b) a misbehaving reader that hands over a post-kickoff row is caught here (defence in depth)
    late = lambda r: {**r, "run_created_at": r["kickoff"] + timedelta(minutes=5)} if r["week"] == 4 else r  # noqa: E731
    rows, _ = rows_of(world, forecast_reader=mutating_reader(late))
    r = one(rows, 4, G_WR)
    assert r["exclusion_reason"] == "independent_not_pregame" and r["ours_independent_pred"] is None
    flagged = lambda r: {**r, "pregame": False} if r["week"] == 5 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(flagged))[0], 5, G_WR)[
        "exclusion_reason"] == "independent_not_pregame"


def test_thursday_and_sunday_kickoffs_use_each_players_own_game(world):
    # week 5: the AAA game moves to Thursday 2026-09-03 00:20 UTC; the Sunday games stay at 09-05 17:00 UTC
    world.execute("UPDATE nfl_schedules SET gameday = '2026-09-02', gametime = '20:20' WHERE week = 5 "
                  "AND (home_team = 'AAA' OR away_team = 'AAA')")
    [opp] = [t for (t,) in world.execute("SELECT CASE WHEN home_team = 'AAA' THEN away_team ELSE home_team END FROM "
                                         "nfl_schedules WHERE week = 5 AND (home_team = 'AAA' OR away_team = 'AAA')"
                                         ).fetchall()]
    early = datetime(2026, 9, 1, 12, tzinfo=UTC)     # before the Thursday game
    world.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, 5, 123.0, ?)",
                  [early.date(), ESPN_ID[G_WR], naive(early)])
    rows, _ = rows_of(world)
    thu_kick = datetime(2026, 9, 3, 0, 20, tzinfo=UTC)
    aaa = one(rows, 5, G_WR)
    # ESPN: the 48 h snapshot (Sep 3 17:00 UTC) is after his Thursday kickoff, the Sep 1 one is not
    assert aaa["kickoff"] == thu_kick and aaa["espn_pred"] == 123.0
    assert aaa["espn_horizon_h"] == pytest.approx((thu_kick - early).total_seconds() / 3600, abs=1e-3)
    # ours: the Friday run (Sep 4 17:00 UTC) is after the Thursday kickoff: the model has no pregame forecast
    assert aaa["ours_origin"] == "espn" and aaa["ours_independent_pred"] is None and aaa["ours_consumer_pred"] == 123.0
    # the opponent has no pregame ESPN snapshot at all
    assert one(rows, 5, gsis(opp, "WR1"))["exclusion_reason"] == "espn_not_pregame"
    # a Sunday team keeps both forecasts, with Sunday horizons
    sunday_team = next(t for t in TEAMS if t not in ("AAA", opp))
    sun = one(rows, 5, gsis(sunday_team, "WR1"))
    assert sun["kickoff"] == kickoff(5) and sun["ours_origin"] == "ours"
    assert (sun["espn_horizon_h"], sun["ours_horizon_h"]) == (48.0, 24.0)
    # every week-5 artifact check uses the week's FIRST kickoff (Thursday): Wednesday fit is eligible, Friday is not
    world.execute("UPDATE independent_projection_runs SET artifact_created_at = '2026-09-02T00:00:00+00:00' WHERE week = 5")
    assert one(rows_of(world)[0], 5, gsis(sunday_team, "WR1"))["ours_origin"] == "ours"
    world.execute("UPDATE independent_projection_runs SET artifact_created_at = '2026-09-03T12:00:00+00:00' WHERE week = 5")
    rows, meta = rows_of(world)
    assert one(rows, 5, gsis(sunday_team, "WR1"))["exclusion_reason"] == "no_independent_forecast_for_week"
    assert any(e["week"] == 5 and "after the week's first kickoff" in e["reason"] for e in meta["reader_exclusions"])


def test_scoring_fingerprint_mismatch_is_excluded(world):
    world.execute("UPDATE independent_projection_runs SET scoring_fingerprint = 'other-scoring' WHERE week = 3")
    rows, meta = rows_of(world)
    assert {r["exclusion_reason"] for r in rows if r["week"] == 3} == {"no_independent_forecast_for_week"}
    assert {"run_id": "run-w3", "week": 3, "reason": "made under different league scoring"} in meta["reader_exclusions"]
    card = eb.scorecard(world, 2026, scoring=SCORING)
    assert 3 not in {c["week"] for c in card["coverage"]["by_week_position"] if c["scored"]}
    # a reader that returns the mismatching row anyway is caught
    wrong = lambda r: {**r, "scoring_fingerprint": "x"} if r["week"] == 4 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(wrong))[0], 4, G_WR)[
        "exclusion_reason"] == "independent_scoring_mismatch"
    other_model = lambda r: {**r, "model_version": "ifp-9"} if r["week"] == 4 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(other_model))[0], 4, G_WR)[
        "exclusion_reason"] == "independent_model_mismatch"


def test_artifact_fitted_at_or_after_first_kickoff_is_excluded(world):
    world.execute("UPDATE independent_projection_runs SET artifact_created_at = ? WHERE week = 2",
                  [kickoff(2).isoformat()])      # exactly at the first kickoff: not strictly before
    rows, meta = rows_of(world)
    assert {r["exclusion_reason"] for r in rows if r["week"] == 2} == {"no_independent_forecast_for_week"}
    assert any(e["week"] == 2 and e["reason"].startswith("artifact fitted after the week's first kickoff")
               for e in meta["reader_exclusions"])
    late = lambda r: {**r, "artifact_created_at": kickoff(4) + timedelta(hours=1)} if r["week"] == 4 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(late))[0], 4, G_WR)[
        "exclusion_reason"] == "independent_artifact_after_first_kickoff"
    unknown = lambda r: {**r, "artifact_created_at": None} if r["week"] == 4 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(unknown))[0], 4, G_WR)[
        "exclusion_reason"] == "independent_artifact_time_unknown"


# ----------------------------------------------------------------------------- identity


def test_uncertain_id_mapping_is_excluded_never_guessed(world):
    medium, conflict, dup = ESPN_ID[gsis("AAA", "RB1")], ESPN_ID[gsis("BBB", "RB1")], ESPN_ID[gsis("CCC", "RB1")]
    world.execute("UPDATE player_id_map SET match_confidence = 'medium' WHERE espn_player_id = ?", [medium])
    world.execute("UPDATE player_id_map SET notes = 'conflicting crosswalks' WHERE espn_player_id = ?", [conflict])
    world.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence, espn_position) "
                  "VALUES (8888, ?, 'high', 'RB')", [gsis("CCC", "RB1")])    # a second ESPN id for one player
    world.execute("INSERT INTO espn_projection_history VALUES (CAST('2026-09-03' AS DATE), 2026, 8888, 3, 5.0, "
                  "'2026-09-03 17:00:00')")
    rows, _ = rows_of(world)
    for pid in (medium, conflict, dup, 8888):
        r = pid_row(rows, 3, pid)
        assert r["exclusion_reason"] == "uncertain_id_mapping" and r["gsis_id"] is None
        assert r["ours_independent_pred"] is None and r["actual_points"] is None
    # a forecast row that names another player than the crosswalk is excluded too
    wrong = lambda r: {**r, "gsis_id": "00-OTHER"} if r["week"] == 4 else r  # noqa: E731
    assert one(rows_of(world, forecast_reader=mutating_reader(wrong))[0], 4, G_WR)[
        "exclusion_reason"] == "independent_id_mismatch"


def test_unsupported_positions_and_byes_are_counted(world):
    world.execute("INSERT INTO player_id_map (espn_player_id, match_confidence, espn_position) VALUES (9000, 'none', 'K')")
    world.execute("INSERT INTO espn_projection_history VALUES (CAST('2026-09-03' AS DATE), 2026, 9000, 3, 8.0, "
                  "'2026-09-03 17:00:00')")
    world.execute("DELETE FROM nfl_schedules WHERE week = 4 AND (home_team = 'AAA' OR away_team = 'AAA')")
    rows, _ = rows_of(world)
    assert pid_row(rows, 3, 9000)["exclusion_reason"] == "unsupported_position"
    aaa4 = [r for r in rows if r["week"] == 4 and r["team"] == "AAA"]
    assert aaa4 and {r["exclusion_reason"] for r in aaa4} == {"bye"} and {r["outcome_status"] for r in aaa4} == {wl.BYE}


# ----------------------------------------------------------------------------- fallback and origins


FALLBACK = ESPN_ID[gsis("AAA", "WR1")]


def _make_fallback(con):
    con.execute("UPDATE independent_player_projections SET coverage_status = 'abstain_no_history', "
                "projected_points = NULL, expected_points = NULL WHERE espn_player_id = ?", [FALLBACK])


def test_espn_fallback_rows_give_zero_credit_and_leave_the_pure_comparison(world, easy_gate):
    _make_fallback(world)
    world.execute("DELETE FROM independent_player_projections WHERE espn_player_id = ?", [ESPN_ID[gsis("AAA", "WR2")]])
    rows, _ = rows_of(world)
    fb = [r for r in rows if r["gsis_id"] in (gsis("AAA", "WR1"), gsis("AAA", "WR2")) and r["week"] <= 6]
    assert len(fb) == 12 and all(r["exclusion_reason"] is None for r in fb)
    for r in fb:
        assert r["ours_origin"] == "espn" and r["ours_consumer_pred"] == r["espn_pred"]
        assert r["ours_independent_pred"] is None and r["ours_conditional_pred"] is None and r["ours_horizon_h"] is None
    pure_rows, _ = eb.population(rows, "pure_independent", eb.BenchmarkConfig())
    assert not {r["espn_player_id"] for r in pure_rows} & {FALLBACK, ESPN_ID[gsis("AAA", "WR2")]}
    assert len(pure_rows) == 56 * 6 - 12
    card = eb.scorecard(world, 2026, scoring=SCORING)
    pure, cons = card["comparisons"]["pure_independent"], card["comparisons"]["consumer_visible"]
    assert (pure["n"], cons["n"], cons["n_model"]) == (56 * 6 - 12, 56 * 6, 56 * 6 - 12)
    assert cons["fallback_share"] == pytest.approx(12 / 336, abs=1e-4)
    # the consumer comparison sees fallback rows with exactly zero difference ...
    only_fb = eb.compare(fb, "consumer_visible", eb.BenchmarkConfig())
    assert only_fb["by_position"]["ALL"]["mae_diff"] == 0.0 and only_fb["by_position"]["ALL"]["n_model"] == 0
    assert only_fb["verdict"] == "insufficient_evidence" and only_fb["fallback_share"] == 1.0
    # ... so the diluted consumer MAE gap is smaller than the pure one, never larger
    assert abs(cons["by_position"]["ALL"]["mae_diff"]) < abs(pure["by_position"]["ALL"]["mae_diff"])
    assert cons["by_position"]["ALL"]["ours"]["mae"] > pure["by_position"]["ALL"]["ours"]["mae"]


def test_ours_zero_and_participation_less_rows(world):
    pid = ESPN_ID[gsis("BBB", "WR1")]
    world.execute("UPDATE independent_player_projections SET coverage_status = 'withheld_unavailable', "
                  "projected_points = NULL, expected_points = NULL WHERE espn_player_id = ? AND week = 3", [pid])
    world.execute("UPDATE independent_player_projections SET expected_points = NULL, p_play = NULL "
                  "WHERE espn_player_id = ? AND week = 4", [pid])    # projected, but no participation estimate
    rows, _ = rows_of(world)
    z = pid_row(rows, 3, pid)
    assert z["ours_origin"] == "ours_zero" and z["ours_independent_pred"] == 0.0 and z["ours_consumer_pred"] == 0.0
    assert z["ours_conditional_pred"] is None and z["ours_horizon_h"] == 24.0
    conds, _ = eb.population(rows, "conditional", eb.BenchmarkConfig())
    assert z not in conds
    no_part = pid_row(rows, 4, pid)      # projection_source.weekly shows ESPN's number for it
    assert no_part["ours_origin"] == "espn" and no_part["ours_conditional_pred"] is None
    pure, _ = eb.population(rows, "pure_independent", eb.BenchmarkConfig())
    assert z in pure and no_part not in pure


# ----------------------------------------------------------------------------- outcomes: absence


def test_confirmed_absence_is_scored_zero_only_in_the_unconditional_comparison(tmp_path, easy_gate):
    con = make_world(tmp_path / "a.duckdb", absent=[(3, G_WR)])
    rows, _ = rows_of(con)
    r = one(rows, 3, G_WR)
    assert r["outcome_status"] == wl.CONFIRMED_ABSENT and r["actual_points"] == 0.0 and r["exclusion_reason"] is None
    cfg = eb.BenchmarkConfig()
    cond, _ = eb.population(rows, "conditional", cfg)
    uncond, col = eb.population(rows, "unconditional", cfg)
    pure, _ = eb.population(rows, "pure_independent", cfg)
    assert r not in cond and r in uncond and r in pure
    assert len(uncond) == len(cond) + 1 == 56 * 6
    assert all(x["outcome_status"] == wl.APPEARED for x in cond)
    assert col == "ours_independent_pred" and eb.population(rows, "conditional", cfg)[1] == "ours_conditional_pred"
    card = eb.scorecard(con, 2026, scoring=SCORING)
    assert card["comparisons"]["conditional"]["n"] == 56 * 6 - 1
    assert card["comparisons"]["unconditional"]["n"] == 56 * 6
    # conditional predictions are points-if-he-plays (expected / 0.9) so they differ from the unconditional ones
    assert card["comparisons"]["conditional"]["by_position"]["ALL"]["ours"]["mae"] != \
        card["comparisons"]["unconditional"]["by_position"]["ALL"]["ours"]["mae"]


# ----------------------------------------------------------------------------- equal populations


def test_every_comparison_runs_on_an_equal_population(world, easy_gate):
    _make_fallback(world)
    rows, _ = rows_of(world)
    cfg = eb.BenchmarkConfig()
    for name in eb.COMPARISONS:
        pop, col = eb.population(rows, name, cfg)
        assert pop, name
        eb.assert_equal_population(pop, col)
        c = eb.compare(rows, name, cfg)
        assert c["by_position"]["ALL"]["n"] == len(pop) == sum(c["by_position"][p]["n"] for p in ("QB", "RB", "WR", "TE"))
        assert c["by_position"]["ALL"]["ours"]["n"] == c["by_position"]["ALL"]["espn"]["n"] == len(pop)
    bad = [dict(pop[0], espn_pred=None)]
    with pytest.raises(AssertionError, match="unequal population"):
        eb.assert_equal_population(bad, col)
    with pytest.raises(AssertionError, match="unequal population"):
        eb.assert_equal_population([dict(pop[0], ours_independent_pred=None)], "ours_independent_pred")


def test_horizon_matched_subset_makes_information_timestamps_visible(world, easy_gate):
    # week 4: the ESPN snapshot of one player is 200 h old, ours 24 h: far apart
    world.execute("DELETE FROM espn_projection_history WHERE espn_player_id = ? AND scoring_period = 4", [FALLBACK])
    old = kickoff(4) - timedelta(hours=200)
    world.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, 4, 5.0, ?)",
                  [old.date(), FALLBACK, naive(old)])
    # week 5: ESPN 24 h before, ours 24 h before (equal), plus a 30 h gap for another player
    far = ESPN_ID[gsis("BBB", "WR1")]
    world.execute("DELETE FROM espn_projection_history WHERE espn_player_id = ? AND scoring_period = 5", [far])
    gap = kickoff(5) - timedelta(hours=54)
    world.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2026, ?, 5, 5.0, ?)",
                  [gap.date(), far, naive(gap)])
    rows, _ = rows_of(world)
    r = pid_row(rows, 4, FALLBACK)
    assert (r["espn_horizon_h"], r["ours_horizon_h"]) == (200.0, 24.0)
    cfg = eb.BenchmarkConfig(horizon_window_h=24.0)
    pure, _ = eb.population(rows, "pure_independent", cfg)
    matched, _ = eb.population(rows, "horizon_matched", cfg)
    assert r in pure and r not in matched
    assert pid_row(rows, 5, far) in pure and pid_row(rows, 5, far) not in matched    # 30 h > 24 h
    assert len(pure) - len(matched) == 2 and len(matched) == 56 * 6 - 2
    wide = eb.population(rows, "horizon_matched", eb.BenchmarkConfig(horizon_window_h=500))[0]
    assert len(wide) == len(pure)
    c = eb.compare(rows, "horizon_matched", cfg)
    assert c["all_rows_reference"] == {"n": len(pure), "of_which_matched": len(matched),
                                       "share_matched": round(len(matched) / len(pure), 4)}
    assert all(abs(x["ours_horizon_h"] - x["espn_horizon_h"]) <= 24.0 for x in matched)


# ----------------------------------------------------------------------------- statistics and the gate


def _fake(week, pos, actual, ours, espn, origin="ours"):
    r = dict.fromkeys(eb.ROW_KEYS)
    r.update(season=2026, week=week, position=pos, espn_player_id=week * 1000 + int(abs(actual) * 7), exclusion_reason=None,
             outcome_status=wl.APPEARED, actual_points=actual, espn_pred=espn, ours_independent_pred=ours,
             ours_conditional_pred=ours, ours_consumer_pred=ours, ours_origin=origin, espn_horizon_h=48.0,
             ours_horizon_h=24.0)
    return r


def _clustered_rows(weeks=10, per_week=30):
    """ESPN misses by 3 every time; the model's miss swings by week (1 or 5): a week-correlated shift."""
    rows = []
    for w in range(1, weeks + 1):
        shift = 1.0 if w % 2 else 5.0
        for i in range(per_week):
            actual = 10.0 + i * 0.01
            rows.append(_fake(w, "WR", actual, actual + shift, actual + 3.0))
    return rows


def test_bootstrap_is_deterministic_with_a_seed_and_clustered_by_week(easy_gate):
    rows = _clustered_rows()
    cfg = eb.BenchmarkConfig(resamples=2000, seed=11)
    a, b = eb.compare(rows, "pure_independent", cfg), eb.compare(rows, "pure_independent", cfg)
    assert a == b
    ci = a["by_position"]["WR"]["ci95"]
    assert a["by_position"]["WR"]["mae_diff"] == 0.0 and a["by_position"]["WR"]["weeks_covered"] == 10
    # iid bootstrap over the SAME player-weeks (the mistake week clustering avoids)
    d = np.array([abs(r["actual_points"] - r["ours_independent_pred"]) - abs(r["actual_points"] - r["espn_pred"])
                  for r in rows])
    rng = np.random.default_rng(11)
    means = d[rng.integers(0, len(d), size=(2000, len(d)))].mean(1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    assert (ci[1] - ci[0]) > 3 * (hi - lo)
    assert ci[0] < 0 < ci[1]
    assert a["verdict"] == "no_significant_difference"
    # a model that beats ESPN in every week is declared better
    shifted = [dict(r, ours_independent_pred=r["actual_points"] + (1.0 if r["week"] % 2 else 2.9)) for r in rows]
    c = eb.compare(shifted, "pure_independent", cfg)
    assert c["by_position"]["WR"]["ci95"][1] < 0 and c["verdict"] == "ours_better"


def test_verdict_is_insufficient_below_the_thresholds(monkeypatch):
    need = PROMOTION_CRITERIA["espn_min_matched_per_position"]
    assert eb.verdict(need - 1, 10, [-2, -1], need, 4)[0] == "insufficient_evidence"
    assert eb.verdict(need, 3, [-2, -1], need, 4)[0] == "insufficient_evidence"
    assert eb.verdict(need, 4, None, need, 4)[0] == "insufficient_evidence"
    assert eb.verdict(need, 4, [-2, -1], need, 4)[0] == "ours_better"
    assert eb.verdict(need, 4, [1, 2], need, 4)[0] == "espn_better"
    assert eb.verdict(need, 4, [-0.5, 0.5], need, 4)[0] == "no_significant_difference"
    assert eb.verdict(need, 4, [-1, 0], need, 4)[0] == "no_significant_difference"     # touching 0 is not excluding it
    # small synthetic world: per position 48 rows, so the default bar (300) leaves every position insufficient
    rows = [r for r in _clustered_rows(weeks=6, per_week=8)]
    c = eb.compare(rows, "pure_independent", eb.BenchmarkConfig())
    assert c["by_position"]["WR"]["required_n"] == need and c["verdict"] == "insufficient_evidence"
    assert c["by_position"]["WR"]["verdict_reason"] == f"48 model-covered rows < {need} required"
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 48)      # read at call time
    assert eb.compare(rows, "pure_independent", eb.BenchmarkConfig())["by_position"]["WR"]["required_n"] == 48
    few_weeks = eb.compare(rows, "pure_independent", eb.BenchmarkConfig(min_weeks=7))
    assert few_weeks["verdict"] == "insufficient_evidence"
    assert "6 distinct weeks < 7" in few_weeks["by_position"]["WR"]["verdict_reason"]
    assert eb.compare(rows, "pure_independent", eb.BenchmarkConfig(min_weeks=6))["verdict"] != "insufficient_evidence"
    assert eb.compare([], "pure_independent", eb.BenchmarkConfig())["verdict"] == "insufficient_evidence"


def test_the_gate_counts_model_covered_rows_not_fallback_rows(monkeypatch):
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 100)
    rows = [_fake(w, "WR", 10.0, 11.0, 14.0) for w in range(1, 7) for _ in range(4)]        # 24 model rows
    rows += [_fake(w, "WR", 10.0, 14.0, 14.0, origin="espn") for w in range(1, 7) for _ in range(30)]   # 180 fallback
    c = eb.compare(rows, "consumer_visible", eb.BenchmarkConfig())
    assert c["n"] == 204 and c["n_model"] == 24 and c["verdict"] == "insufficient_evidence"
    assert c["fallback_share"] == pytest.approx(180 / 204, abs=1e-4)
    assert eb.compare(rows, "pure_independent", eb.BenchmarkConfig())["n"] == 24


def test_scorecard_verdicts_on_the_synthetic_world(world, monkeypatch):
    card = eb.scorecard(world, 2026, scoring=SCORING)
    assert card["available"] and card["weeks"] == list(ALL_WEEKS)
    pure = card["comparisons"]["pure_independent"]
    # default bar: 48 rows per position, 6 weeks -> every position, hence the headline, is insufficient even though
    # the pooled 336 rows would clear a per-position bar (the pooled verdict is descriptive only)
    assert all(pure["by_position"][p]["verdict"] == "insufficient_evidence" for p in ("QB", "RB", "WR", "TE"))
    assert pure["verdict"] == "insufficient_evidence" and set(pure["position_verdicts"].values()) == {"insufficient_evidence"}
    assert pure["by_position"]["ALL"]["n"] == 336 and pure["descriptive_verdict"] == "ours_better"
    assert "verdict" not in pure["by_position"]["ALL"]
    assert pure["by_position"]["ALL"]["ours"]["mae"] == pytest.approx(1.0) and pure["by_position"]["ALL"]["espn"]["mae"] == pytest.approx(4.0)
    assert pure["by_position"]["ALL"]["mae_diff"] == pytest.approx(-3.0)
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 1)
    assert eb.scorecard(world, 2026, scoring=SCORING, config=eb.BenchmarkConfig(min_weeks=7))["comparisons"][
        "pure_independent"]["verdict"] == "insufficient_evidence"
    # swap the roles: ESPN becomes the accurate one
    world.execute("UPDATE independent_player_projections SET expected_points = expected_points + 6, "
                  "projected_points = projected_points + 6")
    assert eb.scorecard(world, 2026, scoring=SCORING)["comparisons"]["pure_independent"]["verdict"] == "espn_better"


def test_coverage_diagnostics_count_every_dropped_row(tmp_path):
    con = make_world(tmp_path / "c.duckdb", skip_stats_teams=[(2, "CCC")])
    con.execute("UPDATE player_id_map SET match_confidence = 'medium' WHERE espn_player_id = ?", [ESPN_ID[G_WR]])
    card = eb.scorecard(con, 2026, scoring=SCORING)
    cov = card["coverage"]
    assert cov["rows"] == 392 and sum(cov["by_exclusion_reason"].values()) + cov["scored"] == 392
    assert cov["by_exclusion_reason"] == {"uncertain_id_mapping": 7, "outcome_pending_game": 55,
                                          "outcome_missing_ingestion": 7, "week_not_fully_final": 48}
    # AAA WR1 weeks 1-7, week 7 rest, CCC week 2, the other 48 week-2 rows (week 2 is not fully final)
    assert cov["by_outcome_status"][wl.PENDING_GAME] == 55 and cov["by_outcome_status"][wl.MISSING_INGESTION] == 7
    assert cov["by_outcome_status"][wl.UNRESOLVED_IDENTITY] == 7
    cell = next(c for c in cov["by_week_position"] if c["week"] == 2 and c["position"] == "WR")
    assert cell["rows"] == 24 and cell["scored"] == 0 and cell["exclusions"]["outcome_missing_ingestion"] == 3
    assert cell["exclusions"]["week_not_fully_final"] == 24 - 3 - 1
    assert cov["scored"] == 275 and cov["scored_by_origin"] == {"ours": 275}


# ----------------------------------------------------------------------------- recording and the CLI


def test_record_is_write_once_and_content_addressed(world, tmp_path):
    out = tmp_path / "out"
    card = eb.scorecard(world, 2026, list(range(1, 7)), scoring=SCORING)
    p1, created = eb.record(card, out, 2026, 6)
    assert created and p1.parent == out and p1.name.startswith("s2026w06-")
    assert hashlib.sha256(p1.read_bytes()).hexdigest()[:16] == p1.stem.split("-")[1]
    before = p1.read_bytes()
    # identical inputs: same file, nothing new written
    p2, created2 = eb.record(eb.scorecard(world, 2026, list(range(1, 7)), scoring=SCORING), out, 2026, 6)
    assert p2 == p1 and not created2 and sorted(out.iterdir()) == [p1]
    # changed inputs (a new ESPN-error profile): a NEW file, the old one is untouched
    world.execute("UPDATE independent_player_projections SET expected_points = expected_points + 0.5 WHERE week = 2")
    p3, created3 = eb.record(eb.scorecard(world, 2026, list(range(1, 7)), scoring=SCORING), out, 2026, 6)
    assert created3 and p3 != p1 and p1.read_bytes() == before and len(list(out.iterdir())) == 2
    assert json.loads(p3.read_text())["schema_version"] == eb.SCHEMA_VERSION
    # a damaged file at the target name is reported, never replaced
    p3.write_text("{}")
    with pytest.raises(RuntimeError, match="different content"):
        eb.record(eb.scorecard(world, 2026, list(range(1, 7)), scoring=SCORING), out, 2026, 6)
    assert p3.read_text() == "{}"
    # no wall-clock value leaks into the file
    assert "generated_at" not in p1.read_text()


def test_scorecard_is_deterministic_and_json_safe(world):
    a, b = eb.scorecard(world, 2026, scoring=SCORING), eb.scorecard(world, 2026, scoring=SCORING)
    assert eb.to_json(a) == eb.to_json(b)
    json.loads(eb.to_json(a))


def test_unsupported_scoring_is_unavailable_not_an_error(tmp_path):
    con = database(tmp_path / "u.duckdb", [2026])
    add_league(con, 2026, 7, scoring_items=[{"statId": 3, "points": 0.04, "pointsOverrides": {}, "isReverseItem": True}])
    card = eb.scorecard(con, 2026)
    assert card["available"] is False and card["unavailable_reason"] and card["comparisons"] == {}


def test_scorecard_runs_on_a_read_only_connection_and_never_writes(tmp_path):
    path = tmp_path / "ro.duckdb"
    make_world(path).close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ro = duckdb.connect(str(path), read_only=True)
    try:
        card = eb.scorecard(ro, 2026, scoring=SCORING)
        assert card["available"] and card["comparisons"]["pure_independent"]["n"] == 336
        with pytest.raises(duckdb.Error):
            ro.execute("CREATE TABLE should_fail (x INT)")
    finally:
        ro.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_cli_score_and_record(tmp_path):
    path = tmp_path / "cli.duckdb"
    make_world(path).close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=BACKEND, capture_output=True, text=True)

    res = run("score", "--season", "2026", "--db", str(path), "--json")
    assert res.returncode == 0, res.stderr
    card = json.loads(res.stdout)
    assert card["comparisons"]["pure_independent"]["n"] == 336 and card["season"] == 2026
    table = run("score", "--season", "2026", "--db", str(path), "--weeks", "1-3")
    assert table.returncode == 0 and "pure_independent" in table.stdout and "insufficient_evidence" in table.stdout
    out = tmp_path / "rec"
    first = json.loads(run("record", "--season", "2026", "--week", "6", "--db", str(path), "--out", str(out)).stdout)
    second = json.loads(run("record", "--season", "2026", "--week", "6", "--db", str(path), "--out", str(out)).stdout)
    assert first["created"] is True and second["created"] is False and first["path"] == second["path"]
    assert len(list(out.iterdir())) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest          # the database was only read
    assert run("score", "--season", "2026", "--db", str(tmp_path / "missing.duckdb")).returncode == 3


def test_default_record_directory_is_git_ignored():
    repo = Path(__file__).resolve().parents[2]
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("not a git checkout")
    res = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "data/models/espn_benchmark/s2026w06-x.json"],
                         capture_output=True)
    assert res.returncode == 0
    assert 'data/models/espn_benchmark' in SCRIPT.read_text()


def test_origin_semantics_match_the_projection_switch():
    from app.analytics import projection_source as ps

    assert (eb.OURS, eb.OURS_ZERO, eb.ESPN) == (ps.OURS, ps.OURS_ZERO, ps.ESPN)
    assert eb.ZERO_STATUSES == ps.ZERO_STATUSES
    assert eb.MODEL_VERSION == ps.MODEL_VERSION


def test_a_partial_nfl_week_does_not_count_toward_the_evidence_gate(tmp_path, monkeypatch):
    """Only 1 of 4 week-4 games is final: week 4 must not satisfy min_weeks, the bootstrap or the verdict."""
    monkeypatch.setitem(PROMOTION_CRITERIA, "espn_min_matched_per_position", 1)
    con = make_world(tmp_path / "p.duckdb")
    others = con.execute("SELECT home_team, away_team FROM nfl_schedules WHERE season = 2026 AND week = 4 "
                         "AND 'AAA' NOT IN (home_team, away_team)").fetchall()
    for h, a in others:
        con.execute("UPDATE nfl_schedules SET home_score = NULL, away_score = NULL, result = NULL "
                    "WHERE week = 4 AND home_team = ?", [h])
        for tm in (h, a):
            con.execute("DELETE FROM nfl_weekly_stats WHERE week = 4 AND team = ?", [tm])
            con.execute("DELETE FROM nfl_snap_counts WHERE week = 4 AND team = ?", [tm])
    cfg = eb.BenchmarkConfig(min_weeks=4)
    three = eb.scorecard(con, 2026, [1, 2, 3], scoring=SCORING, config=cfg)
    four = eb.scorecard(con, 2026, [1, 2, 3, 4], scoring=SCORING, config=cfg)
    assert three["comparisons"]["pure_independent"]["weeks_covered"] == 3
    c = four["comparisons"]["pure_independent"]
    assert c["weeks_covered"] == 3 and c["verdict"] == "insufficient_evidence" and c["n"] == three["comparisons"]["pure_independent"]["n"]
    assert c["by_position"]["ALL"]["descriptive_verdict"] == "insufficient_evidence"
    assert "3 distinct weeks < 4" in c["by_position"]["WR"]["verdict_reason"]
    assert four["partial_weeks"] == [4] and four["fully_final_weeks"] == [1, 2, 3]
    rows, _ = rows_of(con, weeks=[1, 2, 3, 4])
    early = [r for r in rows if r["week"] == 4 and r["exclusion_reason"] == "week_not_fully_final"]
    assert early and all(r["outcome_status"] in wl.RESOLVED and r["actual_points"] is not None for r in early)
    # the resolved rows of the partial week are reported, descriptively, without any verdict
    pre = four["preliminary"]
    assert pre["weeks"] == [4] and pre["comparisons"]["pure_independent"]["n"] == len(early) - sum(
        1 for r in early if r["ours_origin"] == "espn")
    assert "verdict" not in json.dumps(pre["comparisons"]["pure_independent"])
    # once every game is final the week counts and the gate opens
    assert eb.scorecard(con, 2026, [1, 2, 3], scoring=SCORING, config=eb.BenchmarkConfig(min_weeks=3))[
        "comparisons"]["pure_independent"]["verdict"] == "ours_better"


def test_headline_is_the_per_position_summary():
    ok = lambda v: {"verdict": v}  # noqa: E731
    by = lambda **k: {p: ok(k.get(p, "insufficient_evidence")) for p in ("QB", "RB", "WR", "TE")}  # noqa: E731
    assert eb.headline(by())[0] == "insufficient_evidence"
    assert eb.headline(by(WR="ours_better"))[0] == "ours_better"
    assert eb.headline(by(WR="ours_better", RB="ours_better"))[0] == "ours_better"
    assert eb.headline(by(WR="ours_better", RB="espn_better"))[0] == "no_significant_difference"
    assert eb.headline(by(WR="ours_better", RB="no_significant_difference"))[0] == "no_significant_difference"
    assert eb.headline(by(TE="espn_better"))[0] == "espn_better"


def test_the_verdict_uses_unrounded_ci_bounds(monkeypatch, easy_gate):
    def fake(df, a, b, resamples, seed, exact=False):
        assert exact
        return {"diff": -0.0002, "ci95": [-0.0004, -0.0001]}
    monkeypatch.setattr(eb, "week_bootstrap_mae_diff", fake)
    rows = [_fake(w, "WR", 10.0, 11.0, 14.0) for w in range(1, 6) for _ in range(3)]
    c = eb.compare(rows, "pure_independent", eb.BenchmarkConfig())
    assert c["by_position"]["WR"]["ci95"] == [-0.0, -0.0] and c["by_position"]["WR"]["verdict"] == "ours_better"


def test_unconditional_is_exactly_the_conditional_rows_plus_confirmed_absences(tmp_path):
    con = make_world(tmp_path / "u.duckdb", absent=[(3, G_WR)])
    con.execute("UPDATE independent_player_projections SET projected_points = NULL WHERE espn_player_id = ? AND week = 4",
                [ESPN_ID[gsis("BBB", "WR1")]])       # expected points but no points-if-he-plays estimate
    rows, _ = rows_of(con)
    cfg = eb.BenchmarkConfig()
    cond = {(r["week"], r["espn_player_id"]) for r in eb.population(rows, "conditional", cfg)[0]}
    uncond = {(r["week"], r["espn_player_id"]) for r in eb.population(rows, "unconditional", cfg)[0]}
    absent = {(r["week"], r["espn_player_id"]) for r in rows if r["outcome_status"] == wl.CONFIRMED_ABSENT
              and r["exclusion_reason"] is None}
    assert uncond == cond | absent and not cond & absent and (4, ESPN_ID[gsis("BBB", "WR1")]) not in uncond


def test_app_modules_never_reference_the_benchmark():
    """Research-only: nothing in app/ other than the module itself may import or mention it."""
    app = BACKEND / "app"
    offenders = [p.relative_to(app).as_posix() for p in app.rglob("*") if p.suffix in (".py", ".sql")
                 and p.name != "espn_benchmark.py" and "espn_benchmark" in p.read_text()]
    assert offenders == []

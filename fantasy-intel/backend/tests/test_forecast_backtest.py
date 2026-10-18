"""Walk-forward validation: chronology, leakage sensitivity, selection/held-out separation, ESPN
matching. SYNTHETIC data only - nothing here measures real forecasting accuracy."""

import json
from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

import app.analytics.forecast_backtest as fb
import app.analytics.forecast_features as ff
from app.analytics.forecast_backtest import (Protocol, build_store, completed_weeks, espn_comparison, espn_pregame,
                                             evaluate, metrics, pair_decisions, select, walk_forward)
from app.analytics.forecast_features import features_for_week, load_sources, played_targets
from app.analytics.independent_projections import baseline_predictions
from app.analytics.league_scoring import resolve_skill_scoring

from synthetic_forecast import SCORING_ITEMS, database, gsis

SCORING = resolve_skill_scoring(SCORING_ITEMS)
PROTO = Protocol(first_feature_season=2021, train_start_season=2022, validation_seasons=(2023,),
                 heldout_seasons=(2024,), lambdas=(0.01, 0.1))


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    con = database(tmp_path_factory.mktemp("bt") / "s.duckdb", [2021, 2022, 2023, 2024])
    src = load_sources(con, SCORING)
    con.close()
    return build_store(src, completed_weeks(src, [2022, 2023, 2024]))


def _mae(df, col):
    return metrics(df["label"].to_numpy(), df[col].cast(pl.Float64).fill_null(np.nan).to_numpy())["mae"]


def test_deliberately_leaky_scenario_is_detected_and_pipeline_is_not_leaky(tmp_path, monkeypatch):
    """Box scores are pure noise (no player skill, no persistence), so honest history cannot
    predict a week. A feature builder that sees the target week's own usage looks unrealistically
    accurate; the real pipeline must not."""
    monkeypatch.setattr(ff, "RECENT_GAMES", 1)
    con = database(tmp_path / "noise.duckdb", [2022, 2023], iid_noise=True)
    src = load_sources(con, SCORING)
    weeks = completed_weeks(src, [2023])
    honest = build_store(src, weeks)

    leaky_frames = []
    for s, w in weeks:   # WRONG ON PURPOSE: cutoff one week late, so "last game" is the target game
        t = played_targets(src, s, w)
        f = features_for_week(src, s, w + 1, t.drop("league_points"))
        leaky_frames.append(f.with_columns(pl.lit(w).alias("week")).join(
            t.select("gsis_id", pl.col("league_points").alias("label")), on="gsis_id"))
    leaky = baseline_predictions(pl.concat(leaky_frames, how="diagonal_relaxed")).with_columns(
        (pl.col("season") * 100 + pl.col("week")).alias("key"))

    test_weeks = [(2023, w) for w in range(9, 18)]
    grid = {p: {"B": [0.001]} for p in ("QB", "RB", "WR", "TE")}
    h = walk_forward(honest, test_weeks, grid, 2023).drop_nulls("pred_B_0.001")
    lk = walk_forward(leaky, test_weeks, grid, 2023).drop_nulls("pred_B_0.001")
    mean_mae = np.mean([np.abs(g["label"] - g["label"].mean()).mean() for _, g in h.group_by("position")])
    honest_mae = np.mean([_mae(g, "pred_B_0.001") for _, g in h.group_by("position")])
    leaky_mae = np.mean([_mae(g, "pred_B_0.001") for _, g in lk.group_by("position")])
    assert leaky_mae < 0.8 * honest_mae                  # the scenario exposes leakage (yardage/TD noise remains)
    assert honest_mae > 0.9 * mean_mae                   # honest pipeline: no better than knowing the mean


def test_training_rows_always_precede_the_test_week(store, monkeypatch):
    seen = []
    real = fb.fit_ridge

    def spy(train, position, model, lams, label="label"):
        seen.append(int(train["key"].max()))
        return real(train, position, model, lams, label)

    monkeypatch.setattr(fb, "fit_ridge", spy)
    walk_forward(store, [(2023, 5)], {"WR": {"C": [0.1]}}, 2022)
    assert seen and max(seen) < 202305


def test_protocol_rejects_overlap_and_misordering():
    with pytest.raises(ValueError):
        Protocol(validation_seasons=(2023, 2024), heldout_seasons=(2024, 2025)).check()
    with pytest.raises(ValueError):
        Protocol(train_start_season=2024, validation_seasons=(2023,), heldout_seasons=(2025,)).check()


def test_selection_uses_validation_only_and_is_reproducible(store, monkeypatch):
    weeks_used = []
    real = fb.walk_forward

    def spy(store_, test_weeks, penalties, train_start):
        weeks_used.extend(test_weeks)
        return real(store_, test_weeks, penalties, train_start)

    monkeypatch.setattr(fb, "walk_forward", spy)
    a = select(store, PROTO)
    assert weeks_used and {s for s, _ in weeks_used} == {2023}
    b = select(store, PROTO)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    for pos in ("QB", "RB", "WR", "TE"):
        assert a["positions"][pos]["primary"] in ("B", "C")


def test_heldout_refuses_selection_from_other_seasons(store):
    sel = select(store, PROTO)
    other = Protocol(first_feature_season=2021, train_start_season=2022, validation_seasons=(2022,),
                     heldout_seasons=(2024,), lambdas=(0.01, 0.1))
    with pytest.raises(ValueError):
        evaluate(store, sel, other)


def test_heldout_report_statuses_and_no_espn_claim(store):
    sel = select(store, PROTO)
    rep = evaluate(store, sel, PROTO, espn=None)
    assert rep["heldout_seasons"] == [2024]
    for pos, st in rep["status"].items():
        assert st["status"] in ("validated", "experimental", "insufficient_data")
        assert st["start_sit_ready"] is False                       # never without an ESPN comparison
    assert rep["espn_comparison"]["matched_observations"] == 0
    wr = rep["positions"]["WR"]
    assert wr["common_support_n"] > 0 and set(wr["metrics"]) == {"recent_avg", "season_avg", "opportunity",
                                                                    "context", "primary"}
    json.dumps(rep, allow_nan=False)                                # aggregate, serializable


def test_insufficient_heldout_coverage(store):
    small = store.filter(~((pl.col("season") == 2024) & (pl.col("week") > 2)))
    rep = evaluate(small, select(small, PROTO), PROTO)
    assert all(s["status"] == "insufficient_data" for s in rep["status"].values())


def test_week_bootstrap_is_deterministic(store):
    df = store.filter(pl.col("season") == 2024).drop_nulls(["pred_A1", "pred_A2"])
    a = fb.week_bootstrap_mae_diff(df, "pred_A1", "pred_A2", 200, 1)
    assert a == fb.week_bootstrap_mae_diff(df, "pred_A1", "pred_A2", 200, 1)
    assert a["ci95"][0] <= a["diff"] <= a["ci95"][1]


def test_pair_decisions_counts_correct_choices():
    df = pl.DataFrame({"season": [2024] * 3, "week": [1] * 3, "position": ["WR"] * 3, "gsis_id": ["a", "b", "c"],
                       "label": [20.0, 10.0, 10.0], "pred_A1": [5.0, 6.0, 7.0], "pred_X": [15.0, 9.0, 9.0]})
    r = pair_decisions(df, ["pred_A1", "pred_X"])
    assert r["actual_ties_skipped"] == 1                     # b vs c scored the same
    assert r["all"]["pred_X"] == {"pairs": 2, "accuracy": 1.0, "mean_regret": 0.0}
    assert r["all"]["pred_A1"] == {"pairs": 2, "accuracy": 0.0, "mean_regret": 10.0}
    only = pair_decisions(df, ["pred_X"], restrict={(2024, 1, "a", "b")})
    assert only["all"]["pred_X"]["pairs"] == 1


def _espn_db(tmp_path, synced_at: str):
    con = database(tmp_path / "e.duckdb", [2024])
    con.execute("CREATE TABLE nfl_rosters_weekly AS SELECT 2024 AS season, 3 AS week, ? AS gsis_id, 'AAA' AS team, "
                "'WR' AS position", [gsis("AAA", "WR1")])
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) "
                "VALUES (11, ?, 'espn_id:nfl_players', 'high')", [gsis("AAA", "WR1")])
    con.execute("INSERT INTO espn_projection_history VALUES (CAST(? AS DATE), 2024, 11, 3, 12.5, ?)",
                [synced_at[:10], synced_at])
    return con


def test_espn_snapshot_before_kickoff_is_used(tmp_path):
    # Synthetic week-3 games kick off 2024-09-03 13:00 Eastern = 17:00 UTC.
    con = _espn_db(tmp_path, "2024-09-03 16:59:00")
    e = espn_pregame(con, 2024)
    assert e.height == 1 and e["espn"][0] == 12.5


def test_post_kickoff_espn_snapshot_is_excluded(tmp_path):
    con = _espn_db(tmp_path, "2024-09-03 17:00:00")
    assert espn_pregame(con, 2024).height == 0


def test_no_pregame_snapshot_means_no_comparison(store):
    out = espn_comparison(store.with_columns(pl.col("pred_A1").alias("pred_primary")), None)
    assert out["matched_observations"] == 0 and not any(v["criterion_met"] for v in out["by_position"].values())


def test_history_ingestion_guards(tmp_path):
    from app.nfl.history import is_history_db, validate_request

    live = tmp_path / "fantasy.duckdb"
    assert validate_request([2024, 2022, 2023], 2026, tmp_path / "h.duckdb", live) == [2022, 2023, 2024]
    with pytest.raises(ValueError, match="finished seasons"):
        validate_request([2025, 2026], 2026, tmp_path / "h.duckdb", live)
    with pytest.raises(ValueError, match="live database"):
        validate_request([2024], 2026, live, live)
    with pytest.raises(ValueError):
        validate_request([], 2026, tmp_path / "h.duckdb", live)
    con = database(tmp_path / "x.duckdb", [2024])
    assert is_history_db(con) is False


def test_metrics_sign_convention():
    m = metrics(np.array([10.0, 20.0]), np.array([12.0, 16.0]))
    assert m == {"n": 2, "mae": 3.0, "rmse": round((20 / 2) ** 0.5, 3), "bias": 1.0}   # actual - predicted


def _roster_db(tmp_path, snapshots):
    import duckdb
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    con.execute("CREATE TABLE lineup_snapshots (league_id BIGINT, season INT, scoring_period INT, team_id INT, "
                "espn_player_id BIGINT)")
    con.execute("CREATE TABLE player_id_map (espn_player_id BIGINT, gsis_id VARCHAR, espn_position VARCHAR, "
                "match_confidence VARCHAR)")
    for lg, week, team, pid in snapshots:
        con.execute("INSERT INTO lineup_snapshots VALUES (?, 2025, ?, ?, ?)", [lg, week, team, pid])
    for pid, g in ((10, "A"), (11, "B"), (20, "C"), (21, "D")):
        con.execute("INSERT INTO player_id_map VALUES (?, ?, 'WR', 'high')", [pid, g])
    return con


def test_roster_pairs_do_not_cross_leagues_that_share_a_team_id(tmp_path):
    # team 1 exists in both leagues; only (A, B) and (C, D) sat on the same roster
    con = _roster_db(tmp_path, [(111, 5, 1, 10), (111, 5, 1, 11), (222, 5, 1, 20), (222, 5, 1, 21)])
    assert fb.league_roster_pairs(con, 2025) == {(2025, 5, "A", "B"), (2025, 5, "C", "D")}
    # a pair that is real in one league stays once, even if another league also rosters both players
    con.execute("INSERT INTO lineup_snapshots VALUES (222, 2025, 5, 2, 10), (222, 2025, 5, 2, 11)")
    assert fb.league_roster_pairs(con, 2025) == {(2025, 5, "A", "B"), (2025, 5, "C", "D")}


def test_roster_pairs_single_league_is_unchanged(tmp_path):
    con = _roster_db(tmp_path, [(111, 5, 1, 10), (111, 5, 1, 11), (111, 5, 2, 20), (111, 5, 2, 21),
                                (111, 6, 1, 10), (111, 6, 2, 11)])
    assert fb.league_roster_pairs(con, 2025) == {(2025, 5, "A", "B"), (2025, 5, "C", "D")}
    assert fb.league_roster_pairs(con, 2024) == set()

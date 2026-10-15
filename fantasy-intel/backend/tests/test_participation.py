"""Participation model: leakage, population/labels, calibration metrics, selection/held-out
discipline, injury-exit split, live wiring. SYNTHETIC data only - nothing here measures real accuracy."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

import app.analytics.participation as pm
from app.analytics.forecast_backtest import Protocol, build_store, completed_weeks
from app.analytics.forecast_features import Sources, features_for_week, load_injuries, load_sources
from app.analytics.league_scoring import resolve_skill_scoring

from synthetic_forecast import SCORING_ITEMS, gsis, participation_database

SCORING = resolve_skill_scoring(SCORING_ITEMS)
PROTO = Protocol(first_feature_season=2021, train_start_season=2022, validation_seasons=(2023,),
                 heldout_seasons=(2024,), lambdas=(0.01, 0.1))
IFP = {"positions": {p: {"primary": "C", "lambda": {"B": 0.1, "C": 0.1}} for p in ("QB", "RB", "WR", "TE")}}


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    con = participation_database(tmp_path_factory.mktemp("pm") / "p.duckdb", [2021, 2022, 2023, 2024])
    s = load_sources(con, SCORING)
    con.close()
    return s


@pytest.fixture(scope="module")
def stores(src):
    weeks = completed_weeks(src, [2022, 2023, 2024])
    return build_store(src, weeks), pm.build_store(src, weeks)


def _week(src, season=2024, week=9):
    return pm.week_rows(src, season, week).sort("gsis_id")


def _feature_frame(rows):
    return rows.select("gsis_id", *pm.FEATURES)


# ----------------------------------------------------------------------------- population / labels


def test_population_labels_and_lookback(src):
    rows = _week(src)
    pop = pm.roster_population(src, 2024, 9)
    assert set(rows["gsis_id"]) <= set(pop["gsis_id"])
    appeared = set(src.panel.filter((pl.col("season") == 2024) & (pl.col("week") == 9))["gsis_id"])
    for r in rows.iter_rows(named=True):
        assert r["played"] == (r["gsis_id"] in appeared)
        assert r["played"] or r["label"] == 0.0          # did not appear -> 0 league points
        assert r["games_recent"] > 0                       # >= 1 lookback game
    assert rows.filter(pl.col("played") == 0).height > 0


def test_inactive_players_never_appear_in_synthetic_and_real_semantics(src):
    """INA is the game-day inactive decision: in the population it always means played = 0."""
    r = src.rosters.filter((pl.col("season") == 2024) & (pl.col("week") == 9))
    rows = _week(src).join(r.select("gsis_id", "status"), on="gsis_id")
    assert rows.filter(pl.col("status") == "INA")["played"].sum() == 0


def test_bye_and_reserve_players_are_not_in_the_population(src):
    rosters = src.rosters.with_columns(
        pl.when((pl.col("gsis_id") == gsis("AAA", "WR1")) & (pl.col("season") == 2024) & (pl.col("week") == 9))
          .then(pl.lit("RES")).otherwise(pl.col("status")).alias("status"))
    s = Sources(src.panel, src.games.filter(~((pl.col("season") == 2024) & (pl.col("week") == 9)
                                              & pl.col("team").is_in(["BBB"]))), src.defense, rosters, src.injuries)
    ids = set(pm.roster_population(s, 2024, 9)["gsis_id"])
    assert gsis("AAA", "WR1") not in ids
    assert not any(i.startswith("00-BBB") for i in ids)


# ----------------------------------------------------------------------------- leakage


def test_week_w_roster_status_is_never_a_feature(src):
    """Flipping every week-W ACT <-> INA must not change a single feature (it would be the label)."""
    flip = src.rosters.with_columns(
        pl.when((pl.col("season") == 2024) & (pl.col("week") == 9) & pl.col("status").is_in(["ACT", "INA"]))
          .then(pl.when(pl.col("status") == "ACT").then(pl.lit("INA")).otherwise(pl.lit("ACT")))
          .otherwise(pl.col("status")).alias("status"))
    flipped = Sources(src.panel, src.games, src.defense, flip, src.injuries)
    assert _feature_frame(_week(flipped)).equals(_feature_frame(_week(src)))


def test_no_target_week_or_future_outcome_leakage(src):
    """Corrupt every box score / snap row at or after week W and every roster and injury row after
    week W: features are unchanged. The week-W injury report is pregame and may change them."""
    at = (pl.col("season") * 100 + pl.col("week")) >= 2024 * 100 + 9
    after = (pl.col("season") * 100 + pl.col("week")) > 2024 * 100 + 9
    panel = src.panel.with_columns(pl.when(at).then(pl.col("offense_pct") * 0 + 0.01).otherwise(pl.col("offense_pct"))
                                   .alias("offense_pct"),
                                   pl.when(at).then(pl.col("league_points") + 99).otherwise(pl.col("league_points"))
                                   .alias("league_points"))
    extra = panel.filter((pl.col("season") == 2024) & (pl.col("week") == 9)).with_columns(
        pl.lit(gsis("AAA", "QB2")).alias("gsis_id"))       # a fake week-W appearance by a backup
    rosters = src.rosters.with_columns(pl.when(after).then(pl.lit("RES")).otherwise(pl.col("status")).alias("status"))
    injuries = src.injuries.with_columns(pl.when(after).then(pl.lit("Out")).otherwise(pl.col("report_status"))
                                         .alias("report_status"))
    corrupt = Sources(pl.concat([panel, extra]), src.games, src.defense, rosters, injuries)
    base = _feature_frame(_week(src))
    assert _feature_frame(_week(corrupt)).equals(base)
    # sanity: the week-W report IS an input
    wk9 = src.injuries.with_columns(pl.when((pl.col("season") == 2024) & (pl.col("week") == 9))
                                    .then(pl.lit("Out")).otherwise(pl.col("report_status")).alias("report_status"))
    changed = _feature_frame(_week(Sources(src.panel, src.games, src.defense, src.rosters, wk9)))
    assert not changed.equals(base)


def test_injury_rows_modified_after_kickoff_are_dropped(tmp_path):
    con = participation_database(tmp_path / "k.duckdb", [2024])
    before = load_injuries(con).height
    gid, week = con.execute("SELECT gsis_id, week FROM nfl_injuries LIMIT 1").fetchone()
    con.execute("UPDATE nfl_injuries SET date_modified = date_modified + INTERVAL 3 DAY "
                "WHERE gsis_id = ? AND week = ?", [gid, week])          # now after kickoff
    after = load_injuries(con)
    assert after.height == before - 1
    assert after.filter((pl.col("gsis_id") == gid) & (pl.col("week") == week)).height == 0
    con.close()


def test_walk_forward_training_always_precedes_test(stores, monkeypatch):
    cond_store, store = stores
    seen = []
    real = pm.fit_logit

    def spy(train, pos, lams, **kw):
        seen.append(int(train["key"].max()))
        return real(train, pos, lams, **kw)

    monkeypatch.setattr(pm, "fit_logit", spy)
    preds = pm.walk_forward(store, cond_store, [(2024, 5), (2024, 6)], {p: [0.01] for p in pm.POSITIONS},
                            {p: ("C", 0.1) for p in pm.POSITIONS}, 2022)
    assert seen and max(seen) < 2024 * 100 + 6 and all(k < 2024 * 100 + 6 for k in seen)
    assert preds["p_0.01"].is_between(0, 1).all() and preds["cond"].min() >= 0


# ----------------------------------------------------------------------------- features


def test_depth_order_and_teammate_ahead_out(src):
    rows = _week(src)
    for team in ("AAA", "BBB"):
        qb = rows.filter((pl.col("team") == team) & (pl.col("position") == "QB")).sort("depth_rank")
        if qb.height == 2:
            assert qb["depth_rank"].to_list() == [1, 2]
            backup = qb.row(1, named=True)
            assert backup["ahead_unavailable"] == int(qb.row(0, named=True)["inj_out"] == 1
                                                      or qb.row(0, named=True)["inj_doubtful"] == 1)


def test_injury_exit_is_separated_from_role_drop(src):
    """Two players with the same last-game snap drop: the one on the next injury report is an
    injury exit (snap_trend_role null); the other is a role change (snap_trend_role = snap_trend)."""
    hurt, demoted = gsis("AAA", "WR2"), gsis("BBB", "WR2")
    last = (pl.col("season") == 2024) & (pl.col("week") == 8) & pl.col("gsis_id").is_in([hurt, demoted])
    panel = src.panel.with_columns(pl.when(last).then(pl.lit(0.1)).otherwise(pl.col("offense_pct")).alias("offense_pct"))
    listing = pl.DataFrame({"season": [2024], "week": [9], "gsis_id": [hurt], "team": ["AAA"],
                            "report_status": ["Questionable"], "practice_status": ["Limited Participation in Practice"],
                            "injury": ["Hamstring"], "not_injury": [False]}).cast(src.injuries.schema)
    injuries = pl.concat([src.injuries.filter(~pl.col("gsis_id").is_in([hurt, demoted])), listing])
    s = Sources(panel, src.games, src.defense, src.rosters, injuries)
    t = pl.DataFrame({"gsis_id": [hurt, demoted], "position": ["WR", "WR"], "team": ["AAA", "BBB"],
                      "opponent": ["CCC", "DDD"], "is_home": [True, True]})
    f = {r["gsis_id"]: r for r in features_for_week(s, 2024, 9, t).iter_rows(named=True)}
    assert f[hurt]["snap_trend"] <= -0.15 and f[demoted]["snap_trend"] <= -0.15
    assert f[hurt]["injury_exit_last"] == 1 and f[hurt]["snap_trend_role"] is None
    assert f[demoted]["injury_exit_last"] == 0 and f[demoted]["snap_trend_role"] == f[demoted]["snap_trend"]
    # a 'not injury related' (rest) listing is not an injury exit
    rest = listing.with_columns(pl.lit(True).alias("not_injury"))
    s2 = Sources(panel, src.games, src.defense, src.rosters, pl.concat([injuries.filter(pl.col("gsis_id") != hurt), rest]))
    assert features_for_week(s2, 2024, 9, t.head(1))["injury_exit_last"][0] == 0
    # without injury reports nothing is claimed
    s3 = Sources(panel, src.games, src.defense, src.rosters, None)
    g = features_for_week(s3, 2024, 9, t.head(1))
    assert g["injury_exit_last"][0] is None and g["snap_trend_role"][0] == g["snap_trend"][0]


def test_flag_text_distinguishes_exit_from_demotion():
    from app.analytics.independent_projections import flags_for
    assert "injury exit" in " ".join(flags_for({"snap_trend": -0.4, "injury_exit_last": 1}))
    assert "role demotion" in " ".join(flags_for({"snap_trend": -0.4, "injury_exit_last": 0}))


# ----------------------------------------------------------------------------- logistic + calibration


def test_logit_recovers_signal_and_is_calibrated_in_sample():
    rng = np.random.default_rng(3)
    n = 4000
    x = rng.normal(0, 1, n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(0.5 + 2.0 * x)))).astype(int)
    df = pl.DataFrame({"season": [2024] * n, "week": [1] * n, "x": x, "played": y})
    m = pm.fit_logit(df, "WR", [1e-6], features=["x"])[1e-6]
    assert m.converged
    assert abs(m.coef[0] / m.stds[0] - 2.0) < 0.2 and abs(m.intercept + m.coef[0] * -m.means[0] / m.stds[0] - 0.5) < 0.2
    p = m.predict(df)
    assert abs(p.mean() - y.mean()) < 1e-6                 # MLE with an intercept matches the base rate
    rel = pm.reliability(y.astype(float), p)
    assert rel["ece"] < 0.02


def test_logit_missing_values_are_imputed_with_indicator():
    rng = np.random.default_rng(4)
    n = 1000
    x = rng.normal(0, 1, n)
    y = (rng.random(n) < 0.7).astype(int)
    df = pl.DataFrame({"season": [2024] * n, "week": [1] * n, "x": [None if i % 4 == 0 else v for i, v in enumerate(x)],
                       "played": y})
    m = pm.fit_logit(df, "WR", [0.01], features=["x"])[0.01]
    assert "x__missing" in m.columns
    assert np.isfinite(m.predict(df)).all()


def test_reliability_and_brier_detect_miscalibration():
    rng = np.random.default_rng(5)
    p = rng.random(20000)
    y = (rng.random(20000) < p).astype(float)
    good = pm.reliability(y, p)
    bad = pm.reliability(y, np.clip(p + 0.15, 0, 1))
    assert good["ece"] < 0.01 and bad["ece"] > 0.1 and bad["max_gap_bins_n_ge_min"] > 0.1
    assert pm.brier(y, p) < pm.brier(y, np.clip(p + 0.15, 0, 1))
    assert sum(b["n"] for b in good["bins"]) == 20000
    assert pm.log_loss(np.array([1.0]), np.array([0.0])) < 10      # clipped, finite


def test_week_bootstrap_is_deterministic_and_signed():
    df = pl.DataFrame({"key": [1, 1, 2, 2, 3, 3], "a": [0.1, 0.2, 0.1, 0.0, 0.2, 0.1], "b": [0.3, 0.4, 0.2, 0.3, 0.5, 0.3]})
    one, two = pm.week_bootstrap(df, "a", "b", 500, 1), pm.week_bootstrap(df, "a", "b", 500, 1)
    assert one == two and one["diff"] < 0 and one["ci95"][1] < 0


def _report(**over):
    rep = {"n": 1000, "probability": {"model": {"brier": 0.05}, "designation_rate": {"brier": 0.07},
                                      "trailing_appearance": {"brier": 0.09}},
           "best_baseline": "designation_rate", "model_minus_best_baseline_brier": {"ci95": [-0.03, -0.01]},
           "reliability": {"ece": 0.01, "max_gap_bins_n_ge_min": 0.03},
           "relative_mae_improvement_vs_conditional": 0.1, "expected_minus_conditional_mae": {"ci95": [-0.5, -0.3]}}
    rep.update(over)
    return rep


def test_status_applies_frozen_criteria():
    assert pm.status_for(_report())["participation"] == "validated"
    assert pm.status_for(_report())["expected_points"] == "validated"
    st = pm.status_for(_report(reliability={"ece": 0.04, "max_gap_bins_n_ge_min": 0.03}))
    assert st["participation"] == "experimental" and st["expected_points"] == "experimental"
    assert any("ECE" in r for r in st["reasons"])
    st = pm.status_for(_report(relative_mae_improvement_vs_conditional=0.01))
    assert st["participation"] == "validated" and st["expected_points"] == "experimental"
    assert pm.status_for(_report(n=10))["participation"] == "insufficient_data"
    assert pm.PARTICIPATION_CRITERIA["frozen_before_heldout"] is True


# ----------------------------------------------------------------------------- selection / held-out


def test_selection_uses_validation_only_and_is_reproducible(stores, monkeypatch):
    cond_store, store = stores
    weeks_seen = []
    real = pm.walk_forward

    def spy(store_, cond_, test_weeks, *a, **kw):
        weeks_seen.extend(test_weeks)
        return real(store_, cond_, test_weeks, *a, **kw)

    monkeypatch.setattr(pm, "walk_forward", spy)
    a = pm.select(store, cond_store, IFP, PROTO)
    assert {s for s, _ in weeks_seen} == {2023}
    assert json.dumps(a, sort_keys=True) == json.dumps(pm.select(store, cond_store, IFP, PROTO), sort_keys=True)
    assert set(a["positions"]) == set(pm.POSITIONS)
    assert all(v["lambda"] in pm.LOGIT_LAMBDAS for v in a["positions"].values())


def test_heldout_report_and_refusals(stores):
    cond_store, store = stores
    sel = pm.select(store, cond_store, IFP, PROTO)
    rep = pm.evaluate(store, cond_store, sel, IFP, PROTO)
    assert rep["heldout_seasons"] == [2024]
    for pos in pm.POSITIONS:
        r = rep["positions"][pos]
        assert set(r["probability"]) == {"always_plays", "designation_rate", "trailing_appearance", "model"}
        assert set(r["unconditional_points"]) == {"conditional_as_is", "out_rule", "best_baseline_x_conditional",
                                                  "expected_points"}
        assert rep["status"][pos]["participation"] in ("validated", "experimental", "insufficient_data")
    qb = rep["positions"]["QB"]
    # the synthetic backups rarely play: P(plays) must beat "always plays" by a wide margin
    assert qb["probability"]["model"]["brier"] < qb["probability"]["always_plays"]["brier"] / 2
    assert qb["unconditional_points"]["expected_points"]["mae"] < qb["unconditional_points"]["conditional_as_is"]["mae"]
    with pytest.raises(ValueError):
        pm.evaluate(store, cond_store, {**sel, "protocol": {**sel["protocol"], "validation_seasons": [2022]}}, IFP, PROTO)
    other = {"positions": {p: {"primary": "B", "lambda": {"B": 0.01, "C": 0.1}} for p in pm.POSITIONS}}
    with pytest.raises(ValueError):
        pm.evaluate(store, cond_store, sel, other, PROTO)


def test_artifact_round_trip(stores):
    cond_store, store = stores
    sel = {"positions": {p: {"lambda": 0.01} for p in pm.POSITIONS},
           "conditional_model": {p: {"model": "C", "lambda": 0.1} for p in pm.POSITIONS}}
    art = pm.fit_final(store, sel, {"status": {}}, "2026-10-14T00:00:00+00:00", 2022)
    back = pm.ParticipationArtifact.from_dict(json.loads(json.dumps(art.to_dict())))
    rows = store.filter(pl.col("position") == "QB").head(20)
    assert np.allclose(back.models["QB"].predict(rows), art.models["QB"].predict(rows))
    with pytest.raises(ValueError):
        pm.ParticipationArtifact.from_dict({**art.to_dict(), "schema": "other"})
    assert art.status["QB"]["participation"] == "insufficient_data"


def test_snap_drop_diagnostic_shapes(stores):
    _, store = stores
    d = pm.snap_drop_diagnostic(store)
    assert set(d) == {"injury_exit", "role_drop"}


def test_espn_status_fallback_only_for_teams_without_nflverse_designations(src, tmp_path):
    from app.analytics.independent_projection_store import espn_designation_fallback
    from app.db.connection import connect, init_db
    con = connect(tmp_path / "f.duckdb")
    init_db(con)
    synced_team_player = src.injuries.filter((pl.col("season") == 2024) & (pl.col("week") == 9)
                                             & pl.col("report_status").is_not_null()).row(0, named=True)
    synced_team = synced_team_player["team"]
    other = next(t for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH") if t != synced_team)
    # drop the other team's week-9 report entirely, then let ESPN say its WR1 is OUT and a synced-team WR is OUT
    inj = src.injuries.filter(~((pl.col("season") == 2024) & (pl.col("week") == 9) & (pl.col("team") == other)))
    s = Sources(src.panel, src.games, src.defense, src.rosters.filter(pl.col("season") <= 2024), inj)
    unsynced, synced = gsis(other, "WR1"), gsis(synced_team, "WR3")
    for pid, g in ((1, unsynced), (2, synced)):
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, injury_status) VALUES (?, 'x', 'WR', 'OUT')", [pid])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_method, match_confidence) VALUES (?, ?, 'espn_id', 'high')", [pid, g])
    out, teams = espn_designation_fallback(con, s, 2024, 9)
    wk = out.injuries.filter((pl.col("season") == 2024) & (pl.col("week") == 9))
    assert teams == {other}
    assert wk.filter(pl.col("gsis_id") == unsynced)["report_status"].to_list() == ["Out"]
    assert wk.filter(pl.col("gsis_id") == synced).height == inj.filter(
        (pl.col("season") == 2024) & (pl.col("week") == 9) & (pl.col("gsis_id") == synced)).height
    con.close()

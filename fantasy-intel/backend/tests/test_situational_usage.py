"""Research-only situational usage splits (ifp-su): situation flags, the two success definitions, split counts,
point-in-time features, shrinkage, expected script, component prices, walk-forward cutoffs, isolation."""

from __future__ import annotations

import pathlib
import random

import numpy as np
import polars as pl
import pytest

from app.analytics import situational_usage as su


def _play(**kw) -> dict:
    base = {"game_id": "g1", "play_id": 1.0, "season": 2025, "week": 1, "season_type": "REG", "posteam": "AAA",
            "defteam": "BBB", "down": 1.0, "ydstogo": 10.0, "yardline_100": 50.0, "score_differential": 0.0,
            "half_seconds_remaining": 900.0, "play_type": "run", "pass_attempt": 0.0, "rusher_player_id": "rb1",
            "receiver_player_id": None, "qb_kneel": 0.0, "qb_scramble": 0.0, "two_point_attempt": 0.0, "sack": 0.0,
            "qb_dropback": 0.0, "qb_hit": 0.0, "epa": 0.1, "success": 1.0, "yards_gained": 4.0, "first_down": 0.0,
            "touchdown": 0.0}
    base.update(kw)
    return base


def _pass(**kw) -> dict:
    return _play(**{"play_type": "pass", "pass_attempt": 1.0, "rusher_player_id": None, "receiver_player_id": "wr1",
                    "qb_dropback": 1.0, **kw})


def _pbp(plays) -> pl.DataFrame:
    return pl.DataFrame(plays, schema_overrides={"receiver_player_id": pl.String, "rusher_player_id": pl.String})


# ----------------------------------------------------------------------------- situations and success


def test_conventional_success_is_down_and_distance_and_kept_apart_from_epa_success():
    p = su.classify_situations(_pbp([
        _play(down=1.0, ydstogo=10.0, yards_gained=4.0, epa=-0.2, success=0.0),   # 40% on 1st: conv yes, EPA no
        _play(down=1.0, ydstogo=10.0, yards_gained=3.0, epa=0.3, success=1.0),    # 30% on 1st: conv no, EPA yes
        _play(down=2.0, ydstogo=10.0, yards_gained=6.0),                          # 60% on 2nd: yes
        _play(down=2.0, ydstogo=10.0, yards_gained=5.0),                          # no
        _play(down=3.0, ydstogo=4.0, yards_gained=3.0),                           # short of the sticks: no
        _play(down=3.0, ydstogo=4.0, yards_gained=1.0, first_down=1.0),           # first down (penalty-aided): yes
        _play(down=4.0, ydstogo=2.0, yards_gained=2.0),                           # yes
    ]))
    assert p["conv_success"].to_list() == [True, False, True, False, False, True, True]
    assert p["epa_success"].to_list()[:2] == [False, True]
    # two separate names, never one merged "success" feature
    assert "su_conv_success" in su.FEATURE_DOCS and "su_epa_success" in su.FEATURE_DOCS
    assert not [f for f in su.FEATURE_DOCS if f in ("su_success", "success", "success_rate")]
    assert "EPA" in su.FEATURE_DOCS["su_epa_success"] and "down-and-distance" in su.FEATURE_DOCS["su_conv_success"]


def test_situation_flags():
    p = su.classify_situations(_pbp([
        _pass(down=2.0, ydstogo=8.0),                                      # passing down
        _pass(down=2.0, ydstogo=7.0),                                      # early down, not passing down
        _pass(down=3.0, ydstogo=5.0, half_seconds_remaining=100.0),        # passing down, late, two-minute
        _play(yardline_100=5.0, score_differential=7.0),                   # inside 5, leading
        _play(yardline_100=9.0, score_differential=-7.0),                  # inside 10, trailing
        _play(down=1.0, score_differential=6.0, half_seconds_remaining=120.0),   # two-minute -> not early
    ]))
    assert p["pass_down"].to_list() == [True, False, True, False, False, False]
    assert p["early"].to_list() == [True, True, False, True, True, False]
    assert p["late"].to_list() == [False, False, True, False, False, False]
    assert p["two_min"].to_list() == [False, False, True, False, False, True]
    assert p["i5"].to_list() == [False, False, False, True, False, False]
    assert p["i10"].to_list() == [False, False, False, True, True, False]
    assert p["leading"].to_list() == [False, False, False, True, False, False]
    assert p["trailing"].to_list() == [False, False, False, False, True, False]


def test_week_splits_count_designed_rushes_and_targets_only():
    plays = [
        _play(yardline_100=3.0),                                             # designed carry inside 5
        _play(qb_scramble=1.0, rusher_player_id="qb1"),                       # scramble: not a designed carry
        _play(qb_kneel=1.0),                                                  # kneel: not a rush play
        _play(two_point_attempt=1.0),                                         # 2-pt: excluded
        _pass(down=3.0, ydstogo=6.0),                                         # passing-down target
        _pass(sack=1.0, receiver_player_id=None),                             # sack: not a target
        _play(posteam="BBB", defteam="AAA", rusher_player_id="rb9"),         # other team
        _play(season_type="POST"),                                            # postseason: excluded
    ]
    player, team = su.week_splits(_pbp(plays))
    rb = player.filter(pl.col("gsis_id") == "rb1").row(0, named=True)
    assert (rb["car"], rb["car_i5"], rb["car_i10"], rb["tgt"]) == (1, 1, 1, 0)
    assert player.filter(pl.col("gsis_id") == "qb1").height == 0
    wr = player.filter(pl.col("gsis_id") == "wr1").row(0, named=True)
    assert (wr["tgt"], wr["tgt_pd"], wr["tgt_late"]) == (1, 1, 1)
    aaa = team.filter(pl.col("team") == "AAA").row(0, named=True)
    assert (aaa["tm_car"], aaa["tm_car_i5"], aaa["tm_tgt"], aaa["tm_tgt_pd"]) == (1, 1, 1, 1)
    assert team.filter(pl.col("team") == "BBB")["tm_car"].to_list() == [1]


# ----------------------------------------------------------------------------- point-in-time features


def _world(weeks=4, seed=1):
    """Two RBs on AAA over `weeks` weeks: rb1 gets every inside-5 carry and the early-down work, rb2 the
    passing-down targets."""
    rng = random.Random(seed)
    plays, panel, games, pid = [], [], [], 0
    for w in range(1, weeks + 1):
        for _ in range(10):
            pid += 1
            plays.append(_play(week=w, play_id=float(pid), rusher_player_id="rb1", down=1.0))
        for _ in range(2):
            pid += 1
            plays.append(_play(week=w, play_id=float(pid), rusher_player_id="rb1", yardline_100=2.0))
        for _ in range(3):
            pid += 1
            plays.append(_play(week=w, play_id=float(pid), rusher_player_id="rb2", down=2.0, ydstogo=3.0))
        for _ in range(4):
            pid += 1
            plays.append(_pass(week=w, play_id=float(pid), receiver_player_id="rb2", down=3.0, ydstogo=7.0))
        pid += 1
        plays.append(_pass(week=w, play_id=float(pid), receiver_player_id="rb1", down=1.0))
        for g, c, t in (("rb1", 12, 1), ("rb2", 3, 4)):
            panel.append({"season": 2025, "week": w, "gsis_id": g, "position": "RB", "team": "AAA", "carries": c,
                          "targets": t, "offense_pct": 0.5, "team_plays": 60.0})
        margin = rng.choice([7, 10, 14])
        games += [{"season": 2025, "week": w, "team": "AAA", "points_for": 20 + margin, "points_against": 20},
                  {"season": 2025, "week": w, "team": "BBB", "points_for": 20, "points_against": 20 + margin}]
    return _pbp(plays), pl.DataFrame(panel), pl.DataFrame(games)


def _features(pbp, panel, games, week):
    ps, ts = su.week_splits(pbp)
    t = pl.DataFrame({"gsis_id": ["rb1", "rb2"], "position": ["RB", "RB"], "team": ["AAA", "AAA"],
                      "opponent": ["BBB", "BBB"]})
    return su.situational_features(panel, games, ps, ts, 2025, week, t).sort("gsis_id")


def test_features_use_only_games_before_the_target_week():
    pbp, panel, games = _world(weeks=5)
    a = _features(pbp, panel, games, 4)
    # rewrite week 4 and 5 (the target week and later): nothing may change
    pbp2 = pbp.with_columns(pl.when(pl.col("week") >= 4).then(pl.lit("rb2")).otherwise(pl.col("rusher_player_id"))
                            .alias("rusher_player_id"))
    games2 = games.with_columns(pl.when(pl.col("week") >= 4).then(99).otherwise(pl.col("points_for")).alias("points_for"))
    panel2 = panel.with_columns(pl.when(pl.col("week") >= 4).then(0.99).otherwise(pl.col("offense_pct")).alias("offense_pct"))
    b = _features(pbp2, panel2, games2, 4)
    assert a.equals(b)
    assert a.filter(pl.col("gsis_id") == "rb1")["su_games"].item() == 3


def test_split_shares_separate_goal_line_and_passing_down_roles():
    pbp, panel, games = _world(weeks=4)
    f = {r["gsis_id"]: r for r in _features(pbp, panel, games, 4).iter_rows(named=True)}
    rb1, rb2 = f["rb1"], f["rb2"]
    assert rb1["su_car_i5_share"] > 0.75 and rb2["su_car_i5_share"] < 0.25      # all inside-5 carries are rb1's
    assert rb2["su_tgt_pd_share"] > 0.7 and rb1["su_tgt_pd_share"] < 0.3
    assert rb1["su_car_early_share"] > rb2["su_car_early_share"]
    assert rb2["su_tgt_mix"] > rb1["su_tgt_mix"]
    # opportunity per snap: (carries + targets) / (snap share x team plays)
    assert rb1["su_opp_per_snap"] == pytest.approx(13 / 30)
    # AAA won every game, BBB lost every one: positive expected script for AAA
    assert rb1["su_exp_margin"] > 0
    assert rb1["su_script_car"] == pytest.approx(rb1["su_exp_margin"] * rb1["su_car_early_share"])
    assert rb2["su_script_tgt"] == pytest.approx(rb2["su_exp_margin"] * rb2["su_tgt_pd_share"])


def test_a_split_with_no_team_opportunities_falls_back_to_the_overall_share():
    pbp, panel, games = _world(weeks=3)
    pbp = pbp.filter(pl.col("yardline_100") > 10)          # no inside-10 plays at all
    f = _features(pbp, panel, games, 4).filter(pl.col("gsis_id") == "rb1").row(0, named=True)
    ps, ts = su.week_splits(pbp)
    p = ps.filter(pl.col("gsis_id") == "rb1").select(pl.col("car").sum()).item()
    t = ts.select(pl.col("tm_car").sum()).item()
    assert f["su_car_i5_share"] == pytest.approx(p / t)
    assert f["su_car_tilt"] == pytest.approx(0.0, abs=1e-9)     # never leading or trailing: no tilt evidence


def test_expected_margin_is_shrunk_and_zero_in_week_one():
    games = pl.DataFrame({"season": [2025] * 2, "week": [1, 1], "team": ["AAA", "BBB"],
                          "points_for": [30, 20], "points_against": [20, 30]})
    m = {r["team"]: r["margin_pg"] for r in su.team_margins(games, 2025, 2).iter_rows(named=True)}
    assert m["AAA"] == pytest.approx(10 / (1 + su.SCRIPT_SHRINK_GAMES))
    assert su.team_margins(games, 2025, 1).height == 0


# ----------------------------------------------------------------------------- components


def test_prices_recover_points_per_component_and_recombine_floors_and_caps():
    rng = np.random.default_rng(0)
    n = 3000
    car = rng.integers(0, 25, n).astype(float)
    gl = np.minimum(rng.integers(0, 3, n), car).astype(float)
    tgt = rng.integers(0, 8, n).astype(float)
    y = 0.5 + 0.6 * (car - gl) + 2.0 * gl + 1.4 * tgt + rng.normal(0, 0.5, n)
    train = pl.DataFrame({"season": [2025] * n, "week": rng.integers(1, 10, n), "y_car": car, "y_gl": gl,
                          "y_tgt": tgt, "label": y})
    b = su.fit_prices(train, None, 202511)
    assert b == pytest.approx([0.5, 0.6, 2.0, 1.4], abs=0.05)
    out = su.recombine(b, np.array([-1.0, 2.0]), np.array([0.5, 5.0]), np.array([-3.0, 1.0]))
    assert out[0] == pytest.approx(max(b[0], 0.0))                          # every component floored at 0
    assert out[1] == pytest.approx(b[0] + b[2] * 2.0 + b[3] * 1.0)        # inside-5 carries capped at carries


class _Cfg:
    """Minimal stand-in for ChallengerConfig: two base features, ifp-2-like spec fields."""
    positions = {p: {"lam": 0.01, "half_life_weeks": None} for p in ("QB", "RB", "WR", "TE")}

    def features(self, position):
        return ["x1", "x2"]


def _store(weeks=8, players=80, seed=5) -> pl.DataFrame:
    rng = random.Random(seed)
    rows = []
    for w in range(1, weeks + 1):
        for i in range(players):
            x1 = rng.uniform(0, 20)
            row = {"season": 2025, "week": w, "key": 202500 + w, "gsis_id": f"p{i}", "position": "RB",
                   "games_recent": 3, "x1": x1, "x2": rng.uniform(-1, 1), "pred_A1": x1}
            row.update({f: rng.uniform(0, 1) for f in su.FEATURE_DOCS})
            row["y_car"] = max(0.0, 0.8 * x1 + rng.gauss(0, 2))
            row["y_gl"] = float(rng.random() < 0.2)
            row["y_tgt"] = rng.uniform(0, 5)
            row["label"] = 0.6 * row["y_car"] + 2.0 * row["y_gl"] + 1.4 * row["y_tgt"] + rng.gauss(0, 2)
            rows.append(row)
    return pl.DataFrame(rows)


def test_walk_forward_never_trains_on_the_test_week_or_later():
    store = _store()
    a = su.walk_forward(store, [(2025, 8)], _Cfg())
    poisoned = store.with_columns(pl.when(pl.col("week") == 8).then(1e6).otherwise(pl.col("label")).alias("label"),
                                  pl.when(pl.col("week") == 8).then(1e6).otherwise(pl.col("y_car")).alias("y_car"))
    b = su.walk_forward(poisoned, [(2025, 8)], _Cfg())
    assert a.equals(b)
    assert {"pred_ifp2", "pred_split_add", "pred_script_only", "pred_rb_components",
            "wl_y_car_su", "wl_y_car_vol"} <= set(a.columns)
    assert a["pred_rb_components"].null_count() == 0


def test_a_non_adopted_position_keeps_the_ifp2_prediction():
    store = _store()
    p = su.walk_forward(store, [(2025, 8)], _Cfg(),
                        adopted={"split_add": {"RB": False}, "script_only": {"RB": False}, "rb_components": {"RB": False}})
    for c in su.CANDIDATES:
        assert p[f"pred_{c}"].to_list() == p["pred_ifp2"].to_list()


def test_too_few_training_rows_abstain_for_every_model():
    store = _store(weeks=3, players=60)   # weeks 1-2: 120 training rows < MIN_TRAIN_ROWS
    p = su.walk_forward(store, [(2025, 3)], _Cfg())
    assert all(p[f"pred_{c}"].null_count() == p.height for c in ("ifp2", *su.CANDIDATES))


# ----------------------------------------------------------------------------- rules, metrics, isolation


def test_candidates_and_groups_are_fixed():
    assert su.CANDIDATES == ("split_add", "script_only", "rb_components")
    assert not su.GROUPS["split_add"]["QB"] and not su.GROUPS["script_only"]["QB"]
    assert set(su.GROUPS["script_only"]["RB"]) < set(su.GROUPS["split_add"]["RB"])
    assert set(su.GROUPS["script_only"]["WR"]) < set(su.GROUPS["split_add"]["WR"])
    for fs in su.GROUPS["split_add"].values():
        assert not {"label", "y_car", "y_gl", "y_tgt"} & set(fs)
    assert set(su.DECISION_RULES) >= {"development", "confirmation", "outcome", "workload", "never"}


def test_bonferroni_interval_is_wider():
    rng = np.random.default_rng(1)
    n = 2000
    d = pl.DataFrame({"key": rng.integers(1, 40, n), "label": rng.normal(5, 3, n)}).with_columns(
        (pl.col("label") + pl.Series(rng.normal(0, 2, n))).alias("a"),
        (pl.col("label") + pl.Series(rng.normal(0, 2.1, n))).alias("b"))
    wide = su.week_bootstrap(d, "a", "b", alpha=0.05 / 6)
    narrow = su.week_bootstrap(d, "a", "b")
    assert wide["diff"] == narrow["diff"]
    assert wide["ci"][0] <= narrow["ci"][0] and wide["ci"][1] >= narrow["ci"][1]


def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "situational_usage.py":
            continue
        assert "situational_usage" not in path.read_text(), path

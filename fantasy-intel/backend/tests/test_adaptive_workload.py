"""Research-only adaptive workload estimator (app.analytics.adaptive_workload): exposure semantics,
regime blend, missing data, leakage and isolation. Hand-built panels; every number is checkable."""

from __future__ import annotations

import pathlib

import polars as pl
import pytest

from app.analytics import adaptive_workload as aw
from app.analytics import challenger as ch
from app.analytics.forecast_features import SHRINK_GAMES, USAGE_PRIOR_GAMES, Sources, _panel_schema

FULL, XF = 0.7, 15.0          # a normal game for the main player: 70% snaps, 15 expected league points
FILLER = 10.0                 # every game of the filler RB (sets the position mean)


def _panel(rows: list[dict]) -> pl.DataFrame:
    schema = _panel_schema()
    base = {c: (0.0 if t == pl.Float64 else None) for c, t in schema.items()}
    out = []
    for r in rows:
        d = {**base, "has_stats": True, "has_snaps": r.get("offense_pct") is not None, "opponent": "OPP", **r}
        out.append(d)
    return pl.DataFrame(out, schema=schema)


def _games(seasons: dict[int, int], teams=("AAA", "CCC"), margin: dict | None = None) -> pl.DataFrame:
    rows = []
    for season, last in seasons.items():
        for week in range(1, last + 1):
            for team in teams:
                m = (margin or {}).get((season, week, team), 3)
                rows.append({"season": season, "week": week, "game_id": f"{season}{week}{team}", "team": team,
                             "opponent": "OPP", "is_home": True, "gameday": None, "gametime": None,
                             "points_for": 20.0 + m, "points_against": 20.0})
    return pl.DataFrame(rows)


def _player(gid, season, weeks, team="AAA", snap=FULL, xfpl=XF, pos="RB"):
    return [{"season": season, "week": w, "gsis_id": gid, "position": pos, "team": team, "offense_pct": snap,
             "xfpl": xfpl} for w in weeks]


def _filler(seasons=(2024, 2025), last=(18, 6)):
    rows = []
    for s, n in zip(seasons, last):
        rows += _player("F1", s, range(1, n + 1), snap=0.6, xfpl=FILLER)
    return rows


def _sources(rows, exits=None, injuries=None, games=None) -> Sources:
    defense = pl.DataFrame(schema={"season": pl.Int64, "week": pl.Int64, "defteam": pl.String, "pass_plays": pl.Float64,
                                   "pass_epa_sum": pl.Float64, "rush_plays": pl.Float64, "rush_epa_sum": pl.Float64})
    ex = None
    if exits:
        ex = pl.DataFrame([{"season": s, "week": w, "gsis_id": g, "injured_in_game": 1, "injury_sec": 900.0,
                            "last_touch_sec": 800.0, "team_last_sec": 3500.0} for s, w, g in exits])
    inj = None
    if injuries is not None:
        inj = pl.DataFrame([{"season": s, "week": w, "gsis_id": g, "team": "AAA", "report_status": "Questionable",
                             "practice_status": None, "injury": "Hamstring", "not_injury": False}
                            for s, w, g in injuries],
                           schema={"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                                   "report_status": pl.String, "practice_status": pl.String, "injury": pl.String,
                                   "not_injury": pl.Boolean})
    return Sources(_panel(rows), games if games is not None else _games({2024: 18, 2025: 18}), defense, None, inj, ex)


def _role(src, week=None, gid="P1", team="AAA", season=2025, config=aw.RoleConfig()) -> dict:
    t = pl.DataFrame({"gsis_id": [gid], "position": ["RB"], "team": [team]})
    out = aw.role_features_for_week(src, season, week, t, config)
    return out.row(0, named=True)


def _prior(src, season=2025, week=1) -> float:
    p = src.panel.filter((pl.col("season") * 100 + pl.col("week") < season * 100 + week) & (pl.col("season") >= season - 1)
                         & (pl.col("position") == "RB"))
    return float(p["xfpl"].drop_nulls().mean())


def _established_2024(src, week) -> float:
    """Previous-season role of P1 with 17 normal 2024 games (prior from the same cutoff)."""
    return (17 * XF + _prior(src, 2025, week) * SHRINK_GAMES) / (17 + SHRINK_GAMES)


def _history(extra_2025, exits=None, injuries=None, games=None):
    rows = _player("P1", 2024, range(1, 18)) + _filler() + extra_2025
    return _sources(rows, exits=exits, injuries=injuries, games=games)


K = USAGE_PRIOR_GAMES


# ----------------------------------------------------------------------------- the twelve scenarios


def test_early_injury_exit_keeps_partial_information():
    rows = _player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.1, xfpl=2.0)
    src = _history(rows, exits=[(2025, 2, "P1")])
    r = _role(src, 3)
    p = _established_2024(src, 3)
    f = 0.1 / FULL
    assert r["aw_info"] == pytest.approx(1 + f)
    assert r["aw_eb"] == pytest.approx((K * p + XF + 2.0) / (K + 1 + f))
    healthy, raw = (K * p + XF) / (K + 1), (K * p + XF + 2.0) / (K + 2)
    assert abs(r["aw_eb"] - healthy) < abs(raw - healthy)          # not dragged down like a full game ...
    assert r["aw_eb"] != pytest.approx(healthy)                     # ... and not discarded either
    assert r["aw_last_class"] == "injury_exit_pbp"
    assert _role(src, 3, config=aw.RoleConfig(exposure=False))["aw_eb"] == pytest.approx(raw)


def test_genuine_role_drop_is_taken_at_face_value_and_opens_a_regime():
    rows = _player("P1", 2025, [1]) + _player("P1", 2025, [2, 3], snap=0.4, xfpl=5.0)
    src = _history(rows)
    r = _role(src, 4)
    p = _established_2024(src, 4)
    assert r["aw_eb"] == pytest.approx((K * p + XF + 10.0) / (K + 3))      # f = 1 for role_low games
    assert r["aw_eb"] == pytest.approx(_role(src, 4, config=aw.RoleConfig(exposure=False))["aw_eb"])
    assert r["aw_last_class"] == "role_low"
    assert r["aw_streak"] == -2 and r["aw_r"] == pytest.approx(-0.5)
    assert r["aw_role"] == pytest.approx(0.5 * r["aw_eb"] + 0.5 * 5.0)
    assert r["aw_role"] < r["aw_eb"]
    # one low game is not a regime
    one = _role(_history(_player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.4, xfpl=5.0)), 3)
    assert one["aw_streak"] == -1 and one["aw_r"] == 0 and one["aw_role"] == pytest.approx(one["aw_eb"])


def test_sudden_breakout_moves_toward_the_new_regime():
    rows = _player("P1", 2024, range(1, 18), snap=0.3, xfpl=5.0) + _filler()
    rows += _player("P1", 2025, [1, 2], snap=0.8, xfpl=18.0)
    src = _sources(rows)
    r = _role(src, 3)
    assert r["aw_streak"] == 2 and r["aw_r"] == pytest.approx(0.5)
    assert r["aw_role"] == pytest.approx(0.5 * r["aw_eb"] + 0.5 * 18.0)
    assert r["aw_eb"] < r["aw_role"] < 18.0


def test_a_two_game_regime_is_found_even_when_one_game_alone_is_not_enough():
    # prior role 10, two 13-point games: each is 30% above the role WITHOUT both games, while the
    # latest is only ~21% above a baseline that still contains the other one
    rows = _player("P1", 2024, range(1, 18), xfpl=10.0) + _filler() + _player("P1", 2025, [1, 2], xfpl=13.0)
    r = _role(_sources(rows), 3)
    assert r["aw_streak"] == 2 and r["aw_r"] == pytest.approx(0.5)
    assert r["aw_role"] == pytest.approx(0.5 * r["aw_eb"] + 0.5 * 13.0)


def test_repeated_injury_exits_do_not_create_a_decline_regime():
    rows = (_player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.1, xfpl=2.0) + _player("P1", 2025, [3])
            + _player("P1", 2025, [4], snap=0.05, xfpl=1.0))
    src = _history(rows, exits=[(2025, 2, "P1"), (2025, 4, "P1")])
    r = _role(src, 5)
    p = _established_2024(src, 5)
    f2, f4 = 0.1 / FULL, aw.MIN_EXPOSURE                  # 0.05 / 0.7 is below the floor
    assert r["aw_eb"] == pytest.approx((K * p + 2 * XF + 3.0) / (K + 2 + f2 + f4))
    assert r["aw_streak"] == 0 and r["aw_role"] == pytest.approx(r["aw_eb"])
    raw = (K * p + 2 * XF + 3.0) / (K + 4)
    assert r["aw_eb"] > raw + 3


def test_ambiguous_week18_drop_is_partially_discounted_and_never_a_regime():
    rows = _player("P1", 2024, range(1, 18)) + _player("P1", 2024, [18], snap=0.2, xfpl=3.0) + _filler()
    src = _sources(rows)
    r = _role(src, 1)
    prior = _prior(src, 2025, 1)
    e = 0.2 / FULL
    f = 1 - 0.5 * (1 - e)
    assert r["aw_last_class"] == "week18_low"
    assert r["aw_prev_role"] == pytest.approx((17 * XF + 3.0 + prior * SHRINK_GAMES) / (17 + f + SHRINK_GAMES))
    face = _role(src, 1, config=aw.RoleConfig(exposure=False))["aw_prev_role"]
    assert r["aw_prev_role"] > face
    # the same drop in week 10 is a role_low game at face value
    mid = _player("P1", 2025, range(1, 10)) + _player("P1", 2025, [10], snap=0.2, xfpl=3.0)
    m = _role(_history(mid), 11)
    assert m["aw_last_class"] == "role_low"


def test_missing_snaps_take_the_game_at_face_value():
    rows = _player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=None, xfpl=9.0)
    src = _history(rows)
    r = _role(src, 3)
    p = _established_2024(src, 3)
    assert r["aw_last_class"] == "unknown"
    assert r["aw_info"] == pytest.approx(2.0)
    assert r["aw_eb"] == pytest.approx((K * p + XF + 9.0) / (K + 2))


def test_missing_xfp_is_excluded_never_zero():
    with_missing = _history(_player("P1", 2025, [1]) + _player("P1", 2025, [2], xfpl=None))
    without = _history(_player("P1", 2025, [1]))
    a, b = _role(with_missing, 3), _role(without, 3)
    assert a["aw_xfpl_missing_games"] == 1 and b["aw_xfpl_missing_games"] == 0
    assert a["aw_eb"] == pytest.approx(b["aw_eb"], rel=1e-3)        # position prior moves by one filler week only
    assert a["aw_info"] == pytest.approx(1.0)


def test_new_player_without_previous_season():
    src = _sources(_filler() + _player("N1", 2025, [3], snap=0.5, xfpl=12.0))
    none = _role(src, 3, gid="N1")
    assert none["aw_eb"] is None and none["aw_role"] is None and none["aw_prev_role"] is None
    one = _role(src, 4, gid="N1")
    prior = _prior(src, 2025, 4)
    assert one["aw_prev_role"] is None
    assert one["aw_eb"] == pytest.approx((K * prior + 12.0) / (K + 1))


def test_team_change_weakens_the_previous_season_prior():
    rows = _player("P1", 2024, range(1, 18)) + _filler() + _player("P1", 2025, [1, 2], team="CCC", xfpl=8.0, snap=0.6)
    src = _sources(rows)
    p = _established_2024(src, 3)
    moved, stayed = _role(src, 3, team="CCC"), _role(src, 3, team="AAA")
    k2 = K * aw.TEAM_CHANGE_PRIOR_FACTOR
    assert moved["aw_eb"] == pytest.approx((k2 * p + 16.0) / (k2 + 2))
    assert stayed["aw_eb"] == pytest.approx((K * p + 16.0) / (K + 2))
    assert moved["aw_eb"] < stayed["aw_eb"]
    fixed = _role(src, 3, team="CCC", config=aw.RoleConfig(adaptive=False, team_change_factor=1.0))
    assert fixed["aw_eb"] == pytest.approx(stayed["aw_eb"])


def test_target_week_and_later_outcomes_cannot_leak():
    base = _player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.1, xfpl=2.0)
    later = _player("P1", 2025, [3, 4], snap=0.05, xfpl=0.5)
    a = _role(_history(base, exits=[(2025, 2, "P1")]), 3)
    b = _role(_history(base + later, exits=[(2025, 2, "P1"), (2025, 3, "P1"), (2025, 4, "P1")]), 3)
    assert a == b
    # flipping the target week's own play-by-play evidence changes nothing either
    c = _role(_history(base + later, exits=[(2025, 2, "P1")]), 3)
    assert a == c


def test_reproducible_estimates():
    rows = (_player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.1, xfpl=2.0)
            + _player("P1", 2025, [3, 4], snap=0.4, xfpl=6.0))
    src = _history(rows, exits=[(2025, 2, "P1")])
    shuffled = Sources(src.panel.sample(fraction=1.0, shuffle=True, seed=7), src.games, src.defense, None, None,
                       src.exits.sample(fraction=1.0, shuffle=True, seed=3))
    t = pl.DataFrame({"gsis_id": ["P1", "F1"], "position": ["RB", "RB"], "team": ["AAA", "AAA"]})
    a = aw.role_features_for_week(src, 2025, 5, t)
    assert a.equals(aw.role_features_for_week(src, 2025, 5, t))
    assert a.equals(aw.role_features_for_week(shuffled, 2025, 5, t))


def test_missing_injury_provenance_falls_back_to_face_value():
    rows = _player("P1", 2025, [1]) + _player("P1", 2025, [2], snap=0.1, xfpl=2.0)
    none = _role(_history(rows), 3)                       # no play-by-play, no injury reports
    assert none["aw_last_class"] == "role_low"
    p = _established_2024(_history(rows), 3)
    assert none["aw_eb"] == pytest.approx((K * p + XF + 2.0) / (K + 2))
    report = _role(_history(rows, injuries=[(2025, 3, "P1")]), 3)    # a NEW listing after the drop only
    assert report["aw_last_class"] == "injury_exit_report"
    e = 0.1 / FULL
    assert report["aw_info"] == pytest.approx(1 + (1 - 0.5 * (1 - e)))
    pbp = _role(_history(rows, exits=[(2025, 2, "P1")]), 3)
    assert pbp["aw_info"] < report["aw_info"] < none["aw_info"]


# ----------------------------------------------------------------------------- research integration


def test_candidates_keep_ifp2_frozen_and_qb_unchanged():
    cfg = ch.ChallengerConfig.load()
    before = cfg.hash()
    assert cfg.version == "ifp-2.0.0"
    for cand in aw.CANDIDATES:
        assert aw.candidate_features(cand, "QB", cfg) == cfg.features("QB")
        rb = aw.candidate_features(cand, "RB", cfg)
        assert "xfpl_eb" not in rb and set(cand.replace.values()) <= set(rb)
        assert set(cfg.features("RB")) - {"xfpl_eb"} <= set(rb)
    assert aw.candidate_half_life("QB", cfg) == cfg.positions["QB"].get("half_life_weeks")
    assert all(aw.candidate_half_life(p, cfg) == cfg.positions[p]["half_life_weeks"] for p in ("RB", "WR", "TE"))
    assert ch.ChallengerConfig.load().hash() == before
    rc = aw.research_config(aw.CANDIDATES[1], {"QB": 0.01, "RB": 0.01, "WR": 0.03, "TE": 0.03}, cfg)
    assert rc["version"] == aw.AW_VERSION != cfg.version and rc["status"] == "research"
    with pytest.raises(ValueError):
        aw.research_config(aw.CANDIDATES[1], {}, cfg, status="champion")


def _toy_report(mae_cand=4.0, mae_base=4.0, inj=(4.0, 4.0)):
    m = lambda mae: {"n": 100, "mae": mae, "rmse": 6.0, "bias": 0.0}   # noqa: E731
    pos = {p: {"minus_baseline_mae": {"X": {"diff": -0.01, "ci95": [-0.02, -0.001]}}} for p in ("QB", "RB", "WR", "TE")}
    coh = {c: {"X": m(4.0), "ifp2": m(4.0)} for c in ("outcome_genuine_demotion", "limited_history_lt3_games")}
    coh["injury_return"] = {"X": m(inj[0]), "ifp2": m(inj[1])}
    return {**pos, "ALL": {"metrics": {"X": m(mae_cand), "ifp2": m(mae_base)}, "cohorts": coh,
                           "minus_baseline_mae": {"X": {"diff": mae_cand - mae_base, "ci95": [-0.02, -0.001]}}},
            "pairs": {"all": {"pred_X": {"accuracy": 0.65}, "pred_ifp2": {"accuracy": 0.65}}}}


def test_decision_rules():
    ok = aw.decision(_toy_report(3.99), _toy_report(3.99), "X")
    assert ok["outcome"] == "research_candidate"
    worse_injury = aw.decision(_toy_report(3.99), _toy_report(3.99, inj=(4.2, 4.0)), "X")
    assert worse_injury["outcome"] == "rejected" and worse_injury["rejections"]["injury_return_mae"]
    no_gain = _toy_report(3.99)
    no_gain["ALL"]["minus_baseline_mae"]["X"]["ci95"] = [-0.02, 0.01]
    assert aw.decision(no_gain, _toy_report(3.99), "X")["outcome"] == "rejected"


def test_walk_forward_trains_only_on_earlier_weeks():
    rows = []
    for s in (2020, 2021):
        for w in range(1, 6):
            for i in range(60):
                rows.append({"season": s, "week": w, "key": s * 100 + w, "gsis_id": f"p{i}", "position": "RB",
                             "games_recent": 3, "label": float(i % 7), "x": float(i % 7)})
    store = pl.DataFrame(rows)
    out = aw.walk_forward(store, [(2021, 3)], "T", {p: ["x"] for p in ("QB", "RB", "WR", "TE")},
                          {p: [0.01] for p in ("QB", "RB", "WR", "TE")})
    assert out.height == 60 and out["pred_T@0.01"].is_not_null().all()
    # the target week's own labels do not move its predictions
    flipped = store.with_columns(pl.when(pl.col("key") >= 202103).then(-pl.col("label")).otherwise(pl.col("label"))
                                 .alias("label"))
    again = aw.walk_forward(flipped, [(2021, 3)], "T", {p: ["x"] for p in ("QB", "RB", "WR", "TE")},
                            {p: [0.01] for p in ("QB", "RB", "WR", "TE")})
    assert out.equals(again)


def test_research_module_is_never_imported_by_the_app():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        if path.name == "adaptive_workload.py":
            continue
        assert "adaptive_workload" not in path.read_text(), path

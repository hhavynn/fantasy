"""D/ST projection research (dfp-r1): league scoring, ESPN payloads, game table, point-in-time ratings, the stack,
metrics and the adoption gate, and the isolation of the model (research script + prospective shadow only). Synthetic,
deterministic fixtures only; no test makes a network request."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from app.analytics import dst_research as dr

# The owner league's D/ST items (slot 16 overrides), plus items a D/ST never accrues.
LEAGUE = [{"statId": 89, "points": 0.0, "pointsOverrides": {"16": 5.0}},
          {"statId": 90, "points": 0.0, "pointsOverrides": {"16": 4.0}},
          {"statId": 91, "points": 0.0, "pointsOverrides": {"16": 3.0}},
          {"statId": 92, "points": 0.0, "pointsOverrides": {"16": 1.0}},
          {"statId": 123, "points": 0.0, "pointsOverrides": {"16": -1.0}},
          {"statId": 124, "points": 0.0, "pointsOverrides": {"16": -3.0}},
          {"statId": 125, "points": 0.0, "pointsOverrides": {"16": -5.0}},
          {"statId": 128, "points": 0.0, "pointsOverrides": {"16": 5.0}},
          {"statId": 129, "points": 0.0, "pointsOverrides": {"16": 3.0}},
          {"statId": 130, "points": 0.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 132, "points": 0.0, "pointsOverrides": {"16": -1.0}},
          {"statId": 133, "points": 0.0, "pointsOverrides": {"16": -3.0}},
          {"statId": 136, "points": 0.0, "pointsOverrides": {"16": -7.0}},
          {"statId": 95, "points": 0.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 96, "points": 0.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 97, "points": 0.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 98, "points": 0.0, "pointsOverrides": {"16": 2.0}},
          {"statId": 99, "points": 0.0, "pointsOverrides": {"16": 1.0}},
          {"statId": 93, "points": 6.0, "pointsOverrides": {"16": 6.0}},
          {"statId": 101, "points": 6.0}, {"statId": 102, "points": 6.0}, {"statId": 103, "points": 6.0},
          {"statId": 104, "points": 6.0}, {"statId": 206, "points": 2.0}, {"statId": 209, "points": 1.0},
          {"statId": 3, "points": 0.04}, {"statId": 198, "points": 5.0}, {"statId": 53, "points": 1.0}]


# ----------------------------------------------------------------------------- scoring


def test_slot_16_override_beats_base_points_and_player_stats_are_ignored():
    s = dr.resolve_dst_scoring(LEAGUE)
    assert s.supported
    assert s.weights[89] == 5.0 and s.weights[101] == 6.0     # override; base points when no override
    assert 3 not in s.weights and 198 not in s.weights and 53 not in s.weights
    assert {i["stat_id"] for i in s.ignored} == {3, 198, 53}
    assert s.fingerprint.startswith("dst:")
    # base points without an override still apply to slot 16; an override of 0 removes the item
    t = dr.resolve_dst_scoring([{"statId": 99, "points": 1.0}, {"statId": 95, "points": 2.0, "pointsOverrides": {"16": 0}}])
    assert t.weights == {99: 1.0}


def test_unmodeled_dst_statistics_make_the_model_abstain():
    s = dr.resolve_dst_scoring(LEAGUE + [{"statId": 107, "points": 0.5}, {"statId": 187, "points": -0.1}])
    assert not s.supported
    assert {u["stat_id"] for u in s.unmodeled} == {107, 187}
    rev = dr.resolve_dst_scoring([{"statId": 99, "points": 1.0, "isReverseItem": True}])
    assert not rev.supported and rev.unmodeled[0]["reason"] == "reverse scoring item"
    assert not dr.resolve_dst_scoring(None).supported


def test_points_of_an_actual_week_and_of_projected_tier_probabilities():
    s = dr.resolve_dst_scoring(LEAGUE)
    # 3 sacks, 1 INT returned for a TD, 1 FR, 10 points allowed (7-13), 280 yards (200-299)
    actual = {"99": 3.0, "100": 6.0, "95": 1.0, "103": 1.0, "96": 1.0, "120": 10.0, "91": 1.0, "92": 0.0,
              "127": 280.0, "130": 1.0, "107": 40.0}
    assert s.points(actual) == 3 + 2 + 6 + 2 + 3 + 2
    assert s.td_points(actual) == 6
    # a projection is linear in the tier probabilities ESPN reports
    proj = {99: 2.5, 95: 0.8, 89: 0.01, 90: 0.04, 91: 0.2, 92: 0.15, 123: 0.2, 124: 0.05, 125: 0.0, 129: 0.05}
    assert s.points(proj) == pytest.approx(2.5 + 1.6 + 0.05 + 0.16 + 0.6 + 0.15 - 0.2 - 0.15 + 0.15)


@pytest.mark.parametrize("value,tier", [(0, 89), (1, 90), (6, 90), (7, 91), (13, 91), (14, 92), (17, 92),
                                        (18, 121), (27, 122), (28, 123), (45, 124), (46, 125), (63, 125)])
def test_points_allowed_tier_edges(value, tier):
    assert dr.tier_of(value, dr.PA_TIERS) == tier


@pytest.mark.parametrize("value,tier", [(-12, 128), (99, 128), (100, 129), (299, 130), (300, 131), (549, 135),
                                        (550, 136)])
def test_yards_allowed_tier_edges(value, tier):
    assert dr.tier_of(value, dr.YA_TIERS) == tier


# ----------------------------------------------------------------------------- ESPN payload


def _espn_entry(pro_team, stats, pos=16):
    return {"player": {"defaultPositionId": pos, "proTeamId": pro_team, "stats": stats}}


def test_parse_espn_keeps_weekly_actuals_and_projections_with_nflverse_team_codes():
    payload = {"players": [
        _espn_entry(14, [{"seasonId": 2024, "statSplitTypeId": 1, "scoringPeriodId": 3, "statSourceId": 0,
                          "stats": {"99": 2.0}},
                         {"seasonId": 2024, "statSplitTypeId": 1, "scoringPeriodId": 3, "statSourceId": 1,
                          "stats": {"99": 2.6}},
                         {"seasonId": 2024, "statSplitTypeId": 0, "scoringPeriodId": 0, "statSourceId": 0,
                          "stats": {"99": 40.0}},          # season total: ignored
                         {"seasonId": 2023, "statSplitTypeId": 1, "scoringPeriodId": 3, "statSourceId": 0,
                          "stats": {"99": 9.0}}]),          # other season: ignored
        _espn_entry(28, [{"seasonId": 2024, "statSplitTypeId": 1, "scoringPeriodId": 4, "statSourceId": 0,
                          "stats": {"95": 1.0}}]),
        _espn_entry(12, [{"seasonId": 2024, "statSplitTypeId": 1, "scoringPeriodId": 3, "statSourceId": 0,
                          "stats": {"99": 1.0}}], pos=1)]}   # a QB: ignored
    out = dr.parse_espn_dst(payload, 2024)
    assert out["actual"] == {("LA", 3): {99: 2.0}, ("WAS", 4): {95: 1.0}}
    assert out["espn"] == {("LA", 3): {99: 2.6}}


# ----------------------------------------------------------------------------- synthetic league


TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


def synthetic(seasons=(2020, 2021, 2022, 2023), weeks=8, seed=0, sack_prone="HHH"):
    """Round-robin seasons. Points allowed follow the closing line (with noise); `sack_prone`'s opponents
    record three extra sacks. ESPN projects the league mean of every component."""
    rng = np.random.default_rng(seed)
    strength = {t: rng.normal(0, 4) for t in TEAMS}
    sched, espn, tw = [], {}, []
    for s in seasons:
        espn[s] = {"actual": {}, "espn": {}}
        for w in range(1, weeks + 1):
            order = TEAMS[:1] + TEAMS[1:][w % 7:] + TEAMS[1:][:w % 7]
            for a, b in zip(order[:4], order[4:][::-1]):
                spread = round((strength[a] - strength[b]) * 2) / 2
                total = 44.0
                hs = max(0, int(rng.normal((total + spread) / 2, 7)))
                as_ = max(0, int(rng.normal((total - spread) / 2, 7)))
                sched.append({"season": s, "week": w, "game_type": "REG", "home_team": a, "away_team": b,
                              "home_score": hs, "away_score": as_, "spread_line": spread, "total_line": total,
                              "location": "Home"})
                for team, opp, pa in ((a, b, as_), (b, a, hs)):
                    sacks = float(rng.poisson(2.4) + (3 if opp == sack_prone else 0))
                    ya = float(max(0, rng.normal(330, 60)))
                    stats = {99: sacks, 100: 2 * sacks, 95: float(rng.poisson(0.8)), 96: float(rng.poisson(0.6)),
                             120: float(pa), 127: ya, dr.tier_of(pa, dr.PA_TIERS): 1.0,
                             dr.tier_of(ya, dr.YA_TIERS): 1.0}
                    if rng.random() < 0.1:
                        stats[103] = 1.0
                    if rng.random() < 0.04:
                        stats[101 if rng.random() < 0.75 else 102] = 1.0
                    espn[s]["actual"][(team, w)] = stats
                    espn[s]["espn"][(team, w)] = {99: 2.4, 95: 0.8, 96: 0.6, 91: 0.2, 92: 0.2, 122: 0.3, 130: 0.3}
                    tw.append({"season": s, "week": w, "team": opp, "dropbacks": 38.0, "hit_or_sack": sacks + 3})
    return sched, tw, espn


@pytest.fixture(scope="module")
def world():
    sched, tw, espn = synthetic()
    scoring = dr.resolve_dst_scoring(LEAGUE)
    rows = dr.build_games(sched, tw, espn, scoring)
    teams = sorted({r["team"] for r in rows})
    return rows, teams, scoring, dr.history_arrays(rows, teams)


def test_game_table_orients_the_line_to_each_defense():
    sched = [{"season": 2024, "week": 1, "game_type": "REG", "home_team": "OAK", "away_team": "SD", "home_score": 24,
              "away_score": 17, "spread_line": 3.0, "total_line": 44.0, "location": "Home"},
             {"season": 2024, "week": 1, "game_type": "REG", "home_team": "AAA", "away_team": "BBB",
              "home_score": None, "away_score": None, "spread_line": None, "total_line": None,
              "location": "Neutral"},
             {"season": 2024, "week": 19, "game_type": "WC", "home_team": "AAA", "away_team": "BBB"}]
    espn = {2024: {"actual": {("LV", 1): {120: 17.0, 127: 250.0, 92: 1.0, 130: 1.0, 99: 2.0}}, "espn": {}}}
    rows = {r["team"]: r for r in dr.build_games(sched, [], espn, dr.resolve_dst_scoring(LEAGUE))}
    assert set(rows) == {"LV", "LAC", "AAA", "BBB"}          # relocations normalised; playoffs dropped
    home, away = rows["LV"], rows["LAC"]
    assert (home["margin"], home["implied_opp"]) == (3.0, 20.5)     # LV allows LAC's implied 20.5
    assert (away["margin"], away["implied_opp"]) == (-3.0, 23.5)
    assert home["final"] and not away["final"]                 # no ESPN actuals for LAC: not a label
    assert home["points"] == 1 + 2 + 2 and home["pa_tier"] == 92 and home["ya_tier"] == 130
    assert rows["AAA"]["home"] == 0.5 and rows["AAA"]["implied_opp"] is None and not rows["AAA"]["final"]


def test_ratings_are_point_in_time(world):
    """A row's features never change when its own week or any later game changes."""
    rows, teams, scoring, hist = world
    cfg = dr.DConfig()
    target = [r for r in rows if r["season"] == 2022 and r["week"] == 5]
    before = dr.rating_features(target, hist, teams, cfg, 1)
    tampered = dict(hist)
    later = hist["key"] >= 2022 * 100 + 5
    for x in dr.RATED:
        tampered[x] = np.where(later, hist[x] * 3 + 7, hist[x])
    after = dr.rating_features(target, tampered, teams, cfg, 1)
    assert before == after
    # horizon 3 also ignores weeks 3 and 4
    mid = (hist["key"] >= 2022 * 100 + 3) & ~later
    tampered2 = dict(hist)
    for x in dr.RATED:
        tampered2[x] = np.where(mid, hist[x] * 3 + 7, hist[x])
    assert dr.rating_features(target, hist, teams, cfg, 3) == dr.rating_features(target, tampered2, teams, cfg, 3)
    assert dr.rating_features(target, hist, teams, cfg, 1) != dr.rating_features(target, tampered2, teams, cfg, 1)
    assert dr.cutoff_key(2022, 2, 4) == 202200


def test_opponent_adjustment_credits_the_offense_not_its_opponents(world):
    rows, teams, _, hist = world
    adj = dr.Ratings(hist, teams, 202308, dr.DConfig(iterations=3))
    raw = dr.Ratings(hist, teams, 202308, dr.DConfig(opponent_adjust=False))
    h = adj.teams["HHH"]
    assert adj.off["sacks"][h] > 1.25 and adj.off["sacks"][h] == adj.off["sacks"].max()   # HHH allows the most
    # HHH's own defense never faced HHH: unadjusted, it looks bad only because the league mean is inflated
    assert adj.def_["sacks"][h] > raw.def_["sacks"][h]
    assert adj.predict("sacks", "AAA", "HHH", 1.0) > adj.predict("sacks", "AAA", "BBB", 1.0)


def test_projection_is_the_league_score_of_expected_statistics(world):
    rows, teams, scoring, hist = world
    cfg = dr.DConfig(k=3.0)
    feats = dr.rating_features(rows, hist, teams, cfg, 1)
    tr = [i for i, r in enumerate(rows) if r["season"] in (2020, 2021, 2022)]
    te = [i for i, r in enumerate(rows) if r["season"] == 2023]
    preds = dr.project_season([rows[i] for i in tr], [feats[i] for i in tr], [rows[i] for i in te],
                              [feats[i] for i in te], scoring, cfg)
    p, f = preds[0], feats[te[0]]
    assert sum(p["p_pa_tier"].values()) == pytest.approx(1.0)
    assert sum(p["p_ya_tier"].values()) == pytest.approx(1.0)
    stats = {99: p["e_sacks"], 95: p["e_ints"], 96: p["e_fr"], 97: p["e_blk"], 98: p["e_saf"],
             103: p["e_ints"] * f["tau_int_td"], 104: p["e_fr"] * f["tau_fr_td"], 93: p["e_blk"] * f["tau_blk_td"],
             101: p["e_ret_td"] * f["tau_kr_share"], 102: p["e_ret_td"] * (1 - f["tau_kr_share"]),
             206: f["tau_ret2"], 209: f["tau_saf1"],
             **p["p_pa_tier"], **p["p_ya_tier"]}
    assert p["pred"] == pytest.approx(scoring.points(stats), abs=1e-5)
    assert p["p_any_td"] == pytest.approx(1 - math.exp(-p["lambda_td"]))
    # the market stack learns that the opponent's implied total drives points allowed
    imp = np.array([rows[i]["implied_opp"] for i in te])
    epa = np.array([q["e_pa"] for q in preds])
    assert np.corrcoef(imp, epa)[0, 1] > 0.8
    # and the sack-prone offense's opponents are projected for more sacks
    hhh = [q["e_sacks"] for i, q in zip(te, preds) if rows[i]["opp"] == "HHH"]
    rest = [q["e_sacks"] for i, q in zip(te, preds) if rows[i]["opp"] != "HHH"]
    assert np.mean(hhh) > np.mean(rest) + 0.5


def test_return_tds_keep_the_kickoff_vs_punt_mix(world):
    """A league that scores kickoff- and punt-return TDs differently gets the observed KR/PR mix, not 50/50."""
    rows, teams, _, hist = world
    rt = dr.Ratings(hist, teams, 202308, dr.DConfig())
    kr, pr = (hist[c][hist["key"] <= 202308] for c in ("kr_td", "pr_td"))
    assert kr.sum() and pr.sum() and kr.sum() != pr.sum()      # the fixture's mix is not 50/50
    assert abs(rt.td_rate["kr_share"] - 0.5) > 0.05
    kr_only = dr.resolve_dst_scoring([{"statId": 101, "points": 6.0}])
    pr_only = dr.resolve_dst_scoring([{"statId": 102, "points": 6.0}])
    cfg = dr.DConfig(market=False)
    feats = dr.rating_features(rows, hist, teams, cfg, 1)
    tr = [i for i, r in enumerate(rows) if r["season"] < 2023]
    te = [i for i, r in enumerate(rows) if r["season"] == 2023][:4]
    args = ([rows[i] for i in tr], [feats[i] for i in tr], [rows[i] for i in te], [feats[i] for i in te])
    for k, p in zip(dr.project_season(*args, kr_only, cfg), dr.project_season(*args, pr_only, cfg)):
        share = k["pred"] / (k["pred"] + p["pred"])
        assert share == pytest.approx(feats[te[0]]["tau_kr_share"], abs=1e-3)


def test_rows_without_a_line_fall_back_to_ratings(world):
    rows, teams, scoring, hist = world
    cfg = dr.DConfig()
    feats = dr.rating_features(rows, hist, teams, cfg, 1)
    tr = [i for i, r in enumerate(rows) if r["season"] < 2023]
    te = [i for i, r in enumerate(rows) if r["season"] == 2023][:8]
    no_line = [dict(rows[i], implied_opp=None, margin=None) for i in te]
    a = dr.project_season([rows[i] for i in tr], [feats[i] for i in tr], no_line, [feats[i] for i in te], scoring, cfg)
    b = dr.project_season([rows[i] for i in tr], [feats[i] for i in tr], no_line, [feats[i] for i in te], scoring,
                          dr.replace_cfg(cfg, market=False))
    assert [x["pred"] for x in a] == pytest.approx([x["pred"] for x in b])
    assert all(np.isfinite(x["pred"]) for x in a)


# ----------------------------------------------------------------------------- metrics


def test_rank_metrics_and_common_support():
    assert dr.spearman(np.array([1, 2, 3, 4.0]), np.array([10, 20, 30, 40.0])) == pytest.approx(1.0)
    assert dr.spearman(np.array([1, 2, 3, 4.0]), np.array([4, 3, 2, 1.0])) == pytest.approx(-1.0)
    assert dr.pairwise_accuracy(np.array([1, 2, 3.0]), np.array([1, 3, 2.0])) == (2, 3)
    assert dr.pairwise_accuracy(np.array([1, 1, 3.0]), np.array([1, 3, 3.0])) == (1, 1)   # ties skipped
    rows = [{"key": 202401 + (i // 4), "points": float(i % 4), "a": float(i % 4), "b": 1.5 if i % 2 else None}
            for i in range(16)]
    ev = dr.evaluate(rows, {"a": "a", "b": "b"}, baseline="b", resamples=50)
    assert ev["n"] == 8                                     # rows where every method has a prediction
    assert ev["methods"]["a"]["mae"] == 0.0
    assert ev["vs_b"]["a"]["mae_diff"] < 0


def test_interval_bands_and_td_calibration():
    rng = np.random.default_rng(1)
    pred = rng.uniform(2, 10, 4000)
    actual = pred + rng.normal(0, 3, 4000)
    bands = dr.residual_bands(pred[:2000], actual[:2000])
    cov = dr.coverage(bands, pred[2000:], actual[2000:])
    assert abs(cov["empirical"] - 0.8) < 0.04
    p = np.full(1000, 0.12)
    y = (rng.random(1000) < 0.12).astype(float)
    cal = dr.td_calibration(p, y)
    assert cal["mean_p"] == pytest.approx(0.12) and cal["brier"] < 0.12


# ----------------------------------------------------------------------------- script gate + isolation


def _script():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    path = scripts / "dst_research.py"
    spec = importlib.util.spec_from_file_location("dst_research_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report(mae_ci, rho_ci, bias, pro_diff):
    m = {"bias_actual_minus_pred": bias}
    vs = {"mae_diff": -0.1, "mae_diff_ci90": mae_ci, "spearman_diff": 0.03, "spearman_diff_ci90": rho_ci}
    return {"test": {"common_support": {"methods": {"ESPN": m, "model": m, "naive: x": m}, "vs_ESPN": {"model": vs}}},
            "prospective": {"common_support": {"n": 10, "vs_ESPN": {"model": {"mae_diff": pro_diff}}}}}


def test_adoption_gate_needs_every_preregistered_check():
    s = _script()
    assert s.decide(_report([-0.2, -0.01], [0.0, 0.05], 0.1, 0.0))["recommendation"].startswith("adopt")
    assert s.decide(_report([-0.2, 0.01], [0.0, 0.05], 0.1, 0.0))["recommendation"] == "do not adopt"
    assert s.decide(_report([-0.2, -0.01], [-0.03, 0.05], 0.1, 0.0))["recommendation"] == "do not adopt"
    assert s.decide(_report([-0.2, -0.01], [0.0, 0.05], 0.6, 0.0))["recommendation"] == "do not adopt"
    assert s.decide(_report([-0.2, -0.01], [0.0, 0.05], 0.1, 0.3))["recommendation"] == "do not adopt"


def test_research_module_is_never_imported_by_the_app():
    """The app serves dfp-r1 only from stored, verified runs (dst_store, SQL + stdlib): it never imports the numpy
    research model or the shadow."""
    app = Path(__file__).resolve().parents[1] / "app"
    for f in app.rglob("*.py"):
        if f.name != "dst_research.py":
            text = f.read_text()
            assert "import dst_research" not in text and "dst_research import" not in text, f
            assert "import dst_shadow" not in text and "dst_shadow import" not in text, f


def test_dfp_r1_runs_only_from_its_scripts_and_the_shadow_stays_separate():
    """Only the research, shadow and production scripts import the model. learning_cycle.py / refresh_all.py launch
    them as separate processes (never an import); projection_source reads D/ST only through dst_store."""
    root = Path(__file__).resolve().parents[2]
    scripts = root / "scripts"
    importers = {f.name for f in scripts.rglob("*.py") if "import dst_research" in f.read_text()
                 or "dst_research import" in f.read_text() or "import dst_shadow" in f.read_text()}
    assert importers == {"dst_research.py", "dst_shadow.py", "dst_projections.py"}
    for name in ("learning_cycle.py", "refresh_all.py"):
        text = (scripts / name).read_text()
        assert '"dst_shadow.py"' in text and "import dst_shadow" not in text and "from dst_shadow" not in text
    assert '"dst_projections.py"' in (scripts / "learning_cycle.py").read_text()
    source = (root / "backend" / "app" / "analytics" / "projection_source.py").read_text()
    assert "from . import dst_store" in source and "dst_research" not in source

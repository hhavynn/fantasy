"""Player usage derivation, trends and underlying-value classification."""

import pytest

from app.analytics.player_scores import classify, percentile_scores, score_players, value_gap
from app.analytics.trends import composite_trend, compute_trend, split_windows
from app.analytics.usage import derive_usage_row


def test_windows():
    assert split_windows(3) == (0, 0)
    assert split_windows(4) == (2, 2)
    assert split_windows(5) == (2, 3)
    assert split_windows(9) == (3, 5)


def test_trend_up_down_flat_and_ignores_missing_weeks():
    up = compute_trend("targets", [(1, 3), (2, 4), (3, None), (4, 8), (5, 9)], threshold=1.5)
    assert up.label == "UP"
    assert up.recent_weeks == [4, 5] and up.baseline_weeks == [1, 2]  # week 3 (no game) skipped
    assert up.delta == pytest.approx(8.5 - 3.5)
    down = compute_trend("offense_pct", [(1, .9), (2, .88), (3, .6), (4, .55)], threshold=0.08)
    assert down.label == "DOWN"
    flat = compute_trend("targets", [(1, 5), (2, 6), (3, 5), (4, 6)], threshold=1.5)
    assert flat.label == "FLAT"
    short = compute_trend("targets", [(1, 5), (2, 6), (3, 9)], threshold=1.5)
    assert short.label == "INSUFFICIENT"


def test_composite_trend_clamps_and_averages():
    a = compute_trend("a", [(1, 0), (2, 0), (3, 10), (4, 10)], threshold=1.0)   # +10/1 -> clamp 2 -> +1
    b = compute_trend("b", [(1, 5), (2, 5), (3, 5), (4, 5)], threshold=1.0)     # 0
    score, label = composite_trend([a, b])
    assert score == pytest.approx(0.5) and label == "UP"
    assert composite_trend([compute_trend("c", [(1, 1)], 1.0)]) == (None, "INSUFFICIENT")


def test_value_gap_thresholds():
    assert value_gap(20.0, 14.0, 4) == "PRODUCTION_EXCEEDS_USAGE"   # +6 >= max(2.5, 2.8)
    assert value_gap(10.0, 14.0, 4) == "USAGE_EXCEEDS_PRODUCTION"
    assert value_gap(15.0, 14.0, 4) == "ALIGNED"
    assert value_gap(20.0, 14.0, 2) == "INSUFFICIENT"


def test_classify_rules():
    assert classify(2, "UP", "ALIGNED", 9, 9)[0] == "MONITOR"
    assert classify(5, "UP", "ALIGNED", 6, 4)[0] == "BUY"
    assert classify(5, "FLAT", "USAGE_EXCEEDS_PRODUCTION", 6, 3)[0] == "BUY"
    assert classify(5, "FLAT", "USAGE_EXCEEDS_PRODUCTION", 2, 3)[0] == "HOLD"   # role too small to buy
    assert classify(5, "FLAT", "PRODUCTION_EXCEEDS_USAGE", 6, 9)[0] == "SELL"
    assert classify(5, "DOWN", "ALIGNED", 6, 7)[0] == "SELL"
    assert classify(5, "FLAT", "ALIGNED", 6, 6)[0] == "HOLD"


def test_percentiles():
    p = percentile_scores({"a": 1, "b": 2, "c": 3, "d": 3})
    assert p["a"] < p["b"] < p["c"] == p["d"]
    assert percentile_scores({"solo": 4}) == {"solo": 5.0}


def test_usage_row_distinguishes_missing_from_zero():
    played_no_stats = derive_usage_row({"gsis_id": "x", "week": 2, "has_stats": False, "has_snaps": True,
                                        "offense_snaps": 12, "offense_pct": 0.2, "team_carries": 25,
                                        "position": "WR"}, 2026)
    assert played_no_stats["targets"] == 0 and played_no_stats["xfp"] == 0.0
    assert played_no_stats["adot"] is None          # 0 targets -> undefined, not 0
    assert played_no_stats["stats_source"] == "snaps_only"
    stats_no_snaps = derive_usage_row({"gsis_id": "y", "week": 2, "has_stats": True, "has_snaps": False,
                                       "targets": 4, "air_yards": 40.0, "carries": 10, "rushing_yards": 50.0,
                                       "team_carries": 25, "position": "RB"}, 2026)
    assert stats_no_snaps["offense_pct"] is None    # unknown, not zero
    assert stats_no_snaps["adot"] == 10.0
    assert stats_no_snaps["rush_share"] == pytest.approx(0.4)
    assert stats_no_snaps["yards_per_carry"] == 5.0
    assert stats_no_snaps["opportunities"] == 14


def _row(g, week, pos, **kw):
    base = {"gsis_id": g, "week": week, "position": pos, "offense_snaps": 40, "offense_pct": .6, "targets": 5,
            "target_share": .15, "carries": 0, "rush_share": 0, "opportunities": 5, "xfp": 10.0,
            "fantasy_points_ppr": 10.0}
    base.update(kw)
    return base


def test_score_players_flags_rising_role_with_low_production():
    rows = []
    for w in range(1, 6):  # riser: snaps/targets/xfp climb while points stay low
        rows.append(_row("riser", w, "WR", offense_pct=.4 + .1 * w, targets=2 * w, target_share=.05 * w,
                         xfp=3.0 * w, fantasy_points_ppr=3.0 * w - 3))
        rows.append(_row("steady", w, "WR"))
        rows.append(_row("lucky", w, "WR", xfp=8.0, fantasy_points_ppr=18.0))
    out = {r["gsis_id"]: r for r in score_players(rows, 2026)}
    assert out["riser"]["trend_label"] == "UP" and out["riser"]["classification"] == "BUY"
    assert out["lucky"]["value_gap_label"] == "PRODUCTION_EXCEEDS_USAGE" and out["lucky"]["classification"] == "SELL"
    assert out["steady"]["classification"] == "HOLD"
    # components are stored, not just the label
    assert out["riser"]["components"]["trends"] and out["riser"]["reasons"]


def test_games_without_snaps_or_opportunities_do_not_count():
    rows = [_row("p", w, "RB") for w in range(1, 4)] + [_row("p", 4, "RB", offense_snaps=0, opportunities=0)]
    assert score_players(rows, 2026)[0]["games"] == 3

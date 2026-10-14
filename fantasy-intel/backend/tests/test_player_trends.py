"""Player usage derivation, trends and underlying-value classification."""

import pytest

from app.analytics.player_scores import classify, opportunity_tier, percentile_scores, score_players, value_gap
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


def test_classify_market_signals():
    # tiny role rising -> WATCH, never an automatic BUY
    assert classify(5, "UP", "ALIGNED", "LOW", "WR", 3.0)[0] == "WATCH"
    # elite role rising -> BUY
    assert classify(5, "UP", "ALIGNED", "HIGH", "WR", 16.0)[0] == "BUY"
    # high production on a weak role -> SELL (sell-high)
    assert classify(5, "FLAT", "PRODUCTION_EXCEEDS_USAGE", "LOW", "WR", 11.0)[0] == "SELL"
    # strong role, unlucky production -> BUY
    assert classify(5, "FLAT", "USAGE_EXCEEDS_PRODUCTION", "HIGH", "RB", 9.0)[0] == "BUY"
    # declining role with a strong box score -> SELL
    assert classify(5, "DOWN", "ALIGNED", "MEDIUM", "RB", 15.0)[0] == "SELL"
    # small role, unlucky -> not a buy
    assert classify(5, "FLAT", "USAGE_EXCEEDS_PRODUCTION", "LOW", "TE", 2.0)[0] == "HOLD"
    assert classify(2, "UP", "ALIGNED", "HIGH", "WR", 20.0)[0] == "MONITOR"
    assert classify(5, "UP", "ALIGNED", None, "QB", 20.0)[0] == "MONITOR"


def test_opportunity_tiers_are_absolute():
    assert opportunity_tier("xfp", "WR", 3.0) == "LOW"
    assert opportunity_tier("xfp", "WR", 8.0) == "MEDIUM"
    assert opportunity_tier("xfp", "WR", 14.0) == "HIGH"
    assert opportunity_tier("xfp", "TE", 9.5) == "HIGH"
    assert opportunity_tier("targets+carries", "RB", 10) == "MEDIUM"
    assert opportunity_tier("targets+carries", "QB", 10) is None


def test_percentiles():
    p = percentile_scores({"a": 1, "b": 2, "c": 3, "d": 3})
    assert p["a"] < p["b"] < p["c"] == p["d"]
    assert percentile_scores({"solo": 4}) == {"solo": 5.0}


def test_usage_row_distinguishes_missing_from_zero():
    played_no_stats = derive_usage_row({"gsis_id": "x", "week": 2, "has_stats": False, "has_snaps": True,
                                        "has_pbp": True, "offense_snaps": 12, "offense_pct": 0.2,
                                        "team_rush_attempts": 25, "team_targets": 30, "position": "WR"}, 2026)
    assert played_no_stats["targets"] == 0 and played_no_stats["xfp"] == 0.0
    assert played_no_stats["adot"] is None          # 0 targets -> undefined, not 0
    assert played_no_stats["stats_source"] == "snaps_only"
    assert played_no_stats["ngs_avg_separation"] is None   # missing NGS stays NULL
    stats_no_snaps = derive_usage_row({"gsis_id": "y", "week": 2, "has_stats": True, "has_snaps": False,
                                       "has_pbp": True, "targets": 4, "air_yards": 40.0, "carries": 10,
                                       "rushing_yards": 50.0, "team_targets": 40, "position": "RB",
                                       "pbp_rush_attempts": 10, "designed_rushes": 10, "team_rush_attempts": 25,
                                       "team_rb_carries": 16, "designed_rush_success": 4}, 2026)
    assert stats_no_snaps["offense_pct"] is None    # unknown, not zero
    assert stats_no_snaps["adot"] == 10.0
    assert stats_no_snaps["target_share"] == pytest.approx(0.1)          # targets / team TARGETS
    assert stats_no_snaps["team_rush_share"] == pytest.approx(0.4)
    assert stats_no_snaps["backfield_rush_share"] == pytest.approx(10 / 16)
    assert stats_no_snaps["rushing_success_rate"] == pytest.approx(0.4)
    assert stats_no_snaps["yards_per_carry"] == 5.0
    assert stats_no_snaps["opportunities"] == 14
    assert stats_no_snaps["ybc_per_att"] is None    # no PFR row -> unknown


def test_backfield_share_only_for_rbs_and_ngs_never_zero_filled():
    qb = derive_usage_row({"gsis_id": "q", "week": 1, "has_stats": True, "has_snaps": True, "has_pbp": True,
                           "position": "QB", "carries": 8, "pbp_rush_attempts": 7, "designed_rushes": 3,
                           "team_rush_attempts": 28, "team_rb_carries": 20}, 2026)
    assert qb["team_rush_share"] == pytest.approx(7 / 28)
    assert qb["backfield_rush_share"] is None
    assert qb["ngs_rush_yoe_per_att"] is None
    ngs = derive_usage_row({"gsis_id": "r", "week": 1, "has_stats": True, "has_snaps": True, "has_pbp": True,
                            "position": "RB", "ngs_rush_yoe_per_att": 0.0}, 2026)
    assert ngs["ngs_rush_yoe_per_att"] == 0.0       # a real 0 survives; only absence is NULL


def test_ftn_read1_share_null_when_game_not_charted():
    charted = derive_usage_row({"gsis_id": "w", "week": 1, "has_stats": True, "has_snaps": True, "has_pbp": True,
                                "position": "WR", "targets": 6, "ftn_read1_targets": None,
                                "team_ftn_read1_targets": 20}, 2026)
    assert charted["ftn_read1_targets"] == 0 and charted["ftn_read1_target_share"] == 0.0
    uncharted = derive_usage_row({"gsis_id": "w", "week": 1, "has_stats": True, "has_snaps": True,
                                  "has_pbp": True, "position": "WR", "targets": 6}, 2026)
    assert uncharted["ftn_read1_targets"] is None and uncharted["ftn_read1_target_share"] is None


def test_ftn_read1_metric_never_affects_scores_or_signals():
    """The unverified FTN code-"1" share must not move any score, tier, trend or signal."""
    base = [_row("p", w, "WR", offense_pct=.5 + .05 * w, targets=w + 3, target_share=.1 + .02 * w, xfp=4.0 + w,
                 fantasy_points_ppr=5.0 + w) for w in range(1, 6)]
    low = [{**r, "ftn_read1_targets": 0, "ftn_read1_target_share": 0.0} for r in base]
    high = [{**r, "ftn_read1_targets": 9, "ftn_read1_target_share": 0.9} for r in base]
    strip = lambda rows: [{k: v for k, v in r.items() if k != "computed_at"} for r in rows]  # noqa: E731
    assert strip(score_players(low, 2026)) == strip(score_players(high, 2026))
    from app.analytics.trends import TREND_METRICS
    assert not any("ftn" in m or "read" in m for ms in TREND_METRICS.values() for m, _ in ms)


def test_ftn_read1_kept_only_as_experimental_context():
    from app.analytics.context import headlines, player_recent
    rows = [{**_row("p", w, "WR"), "air_yards": 40.0, "air_yards_share": .2, "wopr": .3, "adot": 8.0,
             "team_rush_share": None, "backfield_rush_share": None, "rz_targets": 1, "rz_carries": 0,
             "gl_carries": 0, "designed_rushes": 0, "rushing_success_rate": None, "explosive_runs": 0,
             "ybc_per_att": None, "yac_per_att": None, "carries": 0, "broken_tackles": None,
             "designed_targets": 1, "ngs_rush_yoe_per_att": None, "ngs_avg_separation": None,
             "ftn_read1_targets": 4, "ftn_read1_target_share": 0.4} for w in range(1, 4)]
    ev = player_recent(rows)
    assert ev["experimental"]["ftn_read1_target_share"] == pytest.approx(0.4)
    assert ev["experimental"]["read_semantics_status"] == "UNVERIFIED"
    assert ev["experimental"]["assumed_primary_read_code"] == "1"
    assert "ftn_read1_target_share" not in ev                      # not a first-class evidence field
    lines = headlines("WR", ev, {"role_trend": "FLAT", "matchup": "NEUTRAL"})
    assert lines and not any("read" in h.lower() for h in lines)    # never surfaced as a claim


def test_team_snap_estimator():
    from app.analytics.usage import estimate_team_snaps
    assert estimate_team_snaps([(65, 1.0), (60, 0.92), (40, 0.62), (5, 0.08)]) == (65, None)
    est, warn = estimate_team_snaps([(65, 1.0), (40, 0.5)])     # implies 65 vs 80: inconsistent
    assert est is None and "inconsistent" in warn
    assert estimate_team_snaps([(10, 0.2)])[0] is None


def test_rb_trends_use_backfield_share():
    from app.analytics.trends import TREND_METRICS
    names = [m for m, _ in TREND_METRICS["RB"]]
    assert "backfield_rush_share" in names and "team_rush_share" in names and "rush_share" not in names


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

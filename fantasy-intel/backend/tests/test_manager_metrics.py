"""Manager tendency metrics + classification."""

from datetime import datetime, timedelta

import pytest

from app.analytics.manager_metrics import (ManagerInputs, classify_metrics, compute_metrics, confidence,
                                           hold_lengths, starter_turnover)

T0 = datetime(2026, 9, 10)


def add(team, pid, pos, day, period=1, **kw):
    return {"team_id": team, "espn_player_id": pid, "position": pos, "date": T0 + timedelta(days=day),
            "period": period, **kw}


def drop(team, pid, pos, day):
    return {"team_id": team, "espn_player_id": pid, "position": pos, "date": T0 + timedelta(days=day)}


def build(**kw):
    teams = [{"team_id": t, "faab_spent": 10 * t, "move_to_ir": 0} for t in (1, 2, 3, 4)]
    defaults = dict(league_id=1, season=2026, current_week=5, roster_spots=16, is_faab=True, faab_budget=100,
                    teams=teams, now=T0 + timedelta(days=40))
    defaults.update(kw)
    return ManagerInputs(**defaults)


def metric(rows, team, name):
    return next(r for r in rows if r["team_id"] == team and r["metric"] == name)


def test_rates_shares_and_churn():
    adds = [add(1, i, "RB", i) for i in range(6)] + [add(1, 10, "WR", 1), add(1, 11, "D/ST", 2), add(2, 20, "TE", 3)]
    rows = compute_metrics(build(adds=adds, drops=[drop(1, 0, "RB", 3)]))
    assert metric(rows, 1, "adds_total")["value"] == 8
    assert metric(rows, 1, "adds_per_week")["value"] == pytest.approx(8 / 5)
    assert metric(rows, 1, "rb_add_share")["value"] == pytest.approx(6 / 8)
    assert metric(rows, 1, "rb_add_share")["sample_size"] == 8
    assert metric(rows, 1, "roster_churn_rate")["value"] == pytest.approx(8 / 16)
    assert metric(rows, 1, "stream_dst_rate")["value"] == pytest.approx(1 / 5)
    assert metric(rows, 1, "transactions_per_week")["value"] == pytest.approx(9 / 5)
    assert metric(rows, 3, "adds_total")["value"] == 0
    assert metric(rows, 3, "rb_add_share")["value"] is None   # no adds -> unknown share, not 0


def test_hold_lengths_completed_and_still_held():
    h = hold_lengths([add(1, 5, "WR", 0), add(1, 6, "WR", 2), add(1, 5, "WR", 20)],
                     [drop(1, 5, "WR", 3), drop(1, 5, "WR", 25)])
    days = sorted(c["days"] for c in h[1]["completed"])
    assert days == [3.0, 5.0] and h[1]["still_held"] == 1


def test_quick_drop_rate_only_counts_mature_adds():
    inp = build(adds=[add(1, 1, "WR", 0), add(1, 2, "WR", 1), add(1, 3, "WR", 39)],
                drops=[drop(1, 1, "WR", 2)])
    rows = compute_metrics(inp)
    q = metric(rows, 1, "quick_drop_rate")
    assert q["numerator"] == 1 and q["denominator"] == 2   # the day-39 add is too recent to judge


def test_trades_and_two_for_one():
    trades = [{"trade_id": "t1", "legs": [
        {"espn_player_id": 1, "from_team_id": 1, "to_team_id": 2},
        {"espn_player_id": 2, "from_team_id": 1, "to_team_id": 2},
        {"espn_player_id": 3, "from_team_id": 2, "to_team_id": 1}]}]
    rows = compute_metrics(build(trades=trades))
    assert metric(rows, 1, "trade_count")["value"] == 1 and metric(rows, 2, "trade_count")["value"] == 1
    assert metric(rows, 1, "two_for_one_trade_count")["value"] == 1
    assert metric(rows, 1, "players_traded_away")["value"] == 2
    assert metric(rows, 2, "players_received_in_trades")["value"] == 2
    assert metric(rows, 3, "trade_count")["value"] == 0


def test_starter_turnover():
    lineups = [{"period": p, "team_id": 1, "espn_player_id": pid, "position": "TE", "is_starter": True}
               for p, pid in [(1, 10), (2, 10), (3, 11), (4, 12)]]
    assert starter_turnover(lineups, 1, "TE") == (2, 3)


def test_injury_and_rookie_rates_ignore_unknowns():
    adds = [add(1, 1, "RB", 0, injury_context=True, is_rookie=True), add(1, 2, "RB", 1, injury_context=False,
            is_rookie=None), add(1, 3, "WR", 2, injury_context=None, is_rookie=False)]
    rows = compute_metrics(build(adds=adds))
    inj = metric(rows, 1, "injury_replacement_rate")
    assert (inj["numerator"], inj["denominator"]) == (1, 2)
    rk = metric(rows, 1, "rookie_add_rate")
    assert (rk["numerator"], rk["denominator"]) == (1, 2)


def test_one_transaction_never_establishes_a_tendency():
    rows = compute_metrics(build(adds=[add(1, 1, "TE", 0)], current_week=1))
    classes = {(c["team_id"], c["dimension"]): c for c in classify_metrics(rows)}
    assert classes[(1, "te_interest")]["label"] == "INSUFFICIENT"
    assert classes[(1, "te_interest")]["confidence"] == "LOW"
    assert classes[(1, "waiver_aggression")]["label"] == "INSUFFICIENT"   # 1 week < 3 weeks minimum


def test_league_relative_labels_preserve_numbers():
    adds = [add(1, i, "RB", i % 30) for i in range(20)] + [add(2, 100 + i, "WR", i) for i in range(5)] + \
           [add(3, 200 + i, "WR", i) for i in range(5)] + [add(4, 300 + i, "WR", i) for i in range(4)]
    classes = {(c["team_id"], c["dimension"]): c for c in classify_metrics(compute_metrics(build(adds=adds)))}
    agg1 = classes[(1, "waiver_aggression")]
    assert agg1["label"] == "HIGH" and agg1["value"] == pytest.approx(4.0)
    assert agg1["league_mean"] is not None and agg1["z_score"] > 0.75 and agg1["sample_size"] == 5
    assert classes[(1, "rb_interest")]["label"] == "HIGH"
    assert classes[(2, "rb_interest")]["label"] == "LOW"     # zero share is always LOW
    assert classes[(4, "waiver_aggression")]["label"] in ("LOW", "MEDIUM")
    assert classes[(1, "trade_activity")]["label"] == "LOW"  # absolute: 0 trades


def test_confidence_levels():
    assert confidence(2, 5) == "LOW" and confidence(6, 5) == "MEDIUM" and confidence(10, 5) == "HIGH"

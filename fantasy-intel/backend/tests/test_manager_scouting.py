"""Read-only scouting contract; independent, hand-checked evidence fixtures."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

def insert(con, table, records):
    for row in records:
        cols = ','.join(row)
        placeholders = ','.join('?' for _ in row)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])


@pytest.fixture
def scouting_db(con):
    insert(con, "leagues", [dict(league_id=1, season=2026, name="Test", current_week=6,
                                 is_faab=False, synced_at=datetime(2026, 10, 14))])
    insert(con, "fantasy_teams", [dict(league_id=1, season=2026, team_id=t, name=f"Team {t}",
        owner_names=f"Owner {t}", wins=3, losses=2, ties=0, points_for=500, waiver_rank=t,
        acquisitions=16 if t == 1 else 24, drops=12, is_mine=t == 1) for t in (1, 2, 3)])
    players = [(101, "RB"), (102, "RB"), (103, "WR"), (104, "WR"), (105, "WR"),
               (201, "RB"), (202, "WR"), (203, "TE"), (204, "D/ST")]
    insert(con, "fantasy_players", [dict(espn_player_id=p, full_name=f"Player {p}", position=pos)
                                     for p, pos in players])
    insert(con, "lineup_snapshots", [dict(league_id=1, season=2026, scoring_period=6,
        team_id=1 if p < 200 else 2, espn_player_id=p, lineup_slot_id=21 if p == 102 else 20,
        lineup_slot="IR" if p == 102 else "BE", is_starter=False) for p, _ in players])
    metrics = []
    def m(t, key, value, n, num=None, den=None):
        metrics.append(dict(league_id=1, season=2026, team_id=t, metric=key, value=value,
                            sample_size=n, numerator=num, denominator=den,
                            computed_at=datetime(2026, 10, 14)))
    for t, num, den in [(1, 7, 16), (2, 3, 24), (3, 0, 0)]:
        m(t, "rb_add_share", num / den if den else None, den, num, den)
        m(t, "rb_adds", num, num)
    m(1, "avg_rbs_rostered", 5, 6, 30, 6)
    m(1, "adds_total", 16, 16)
    m(1, "drops_total", 12, 12)
    m(1, "adds_per_week", 16 / 6, 6, 16, 6)
    m(1, "roster_churn_rate", 1, 6, 16, 16)
    m(1, "quick_drop_rate", 0.25, 12, 3, 12)
    m(1, "starter_turnover_dst", 0.8, 5, 4, 5)
    m(1, "distinct_starters_dst", 4, 5)
    m(1, "dst_adds", 3, 3)
    m(1, "dst_pickups_per_week", 0.5, 6, 3, 6)
    insert(con, "manager_metrics", metrics)
    insert(con, "manager_classifications", [dict(league_id=1, season=2026, team_id=1,
        dimension=dim, label=label, metric=key, value=value, league_mean=mu,
        sample_size=n, min_sample=minimum, confidence=conf, rule="Stored engine rule")
        for dim, label, key, value, mu, n, minimum, conf in [
            ("waiver_aggression", "HIGH", "adds_per_week", 16 / 6, 1.2, 6, 3, "HIGH"),
            ("roster_churn", "MEDIUM", "roster_churn_rate", 1, 0.9, 6, 3, "HIGH"),
            ("quick_drops", "LOW", "quick_drop_rate", 0.25, 0.5, 12, 4, "HIGH"),
            ("rb_interest", "HIGH", "rb_add_share", 7 / 16, 0.25, 16, 5, "HIGH"),
            ("te_interest", "INSUFFICIENT", "te_add_share", None, 0.1, 0, 5, "LOW"),
            ("dst_streaming", "HIGH", "starter_turnover_dst", 0.8, 0.3, 5, 3, "MEDIUM"),
        ]])
    insert(con, "team_position_needs", [dict(league_id=1, season=2026, team_id=1, position="RB",
        starters_required=2, rostered=2, healthy=1, unavailable=1, effective_available=1.0,
        starter_value=4.3, need_score=0.82, details={"league_avg_starter_value": 7.2, "formula": "stored"})])
    return con


def report(con, team=1):
    from app.analytics.manager_scouting import build_manager_scouting
    return build_manager_scouting(con, team)


def by_position(items, pos):
    return next(r for r in items if r["position"] == pos)


def test_stored_high_medium_low_labels_and_raw_evidence(scouting_db):
    signals = {s["dimension"]: s for s in report(scouting_db)["waiver_style"]}
    assert [signals[k]["classification"] for k in ("waiver_aggression", "roster_churn", "quick_drops")] == ["HIGH", "MEDIUM", "LOW"]
    q = signals["quick_drops"]
    assert q["value"] == 0.25 and q["numerator"] == 3 and q["denominator"] == 12
    assert q["league_mean"] == 0.5 and q["sample_size"] == 12 and q["rule"] == "Stored engine rule"


def test_missing_and_insufficient_history_are_not_low(scouting_db):
    d = report(scouting_db)
    assert by_position(d["position_affinity"], "TE")["classification"] == "INSUFFICIENT"
    assert all(s["classification"] == "INSUFFICIENT" for s in report(scouting_db, 3)["waiver_style"])


def test_current_composition_includes_zero_positions_ir_and_league_context(scouting_db):
    c = report(scouting_db)["current_roster"]
    assert c["available"] and c["scoring_period"] == 6
    assert by_position(c["positions"], "RB")["count"] == 2
    assert by_position(c["positions"], "WR")["count"] == 3
    assert by_position(c["positions"], "QB")["count"] == 0
    assert by_position(c["positions"], "IR")["count"] == 1
    assert by_position(c["positions"], "WR")["league_average"] == 2
    assert c["league_team_samples"] == 2  # third team is missing, not an empty roster


def test_current_roster_and_historical_tendency_stay_separate(scouting_db):
    rb = by_position(report(scouting_db)["position_affinity"], "RB")
    assert rb["current_rostered"] == 2 and rb["average_rostered"] == 5
    assert rb["current_need_score"] == 0.82
    assert rb["classification"] == "HIGH"


def test_position_ratio_uses_weighted_league_share(scouting_db):
    rb = by_position(report(scouting_db)["position_affinity"], "RB")
    assert rb["add_count"] == 7 and rb["add_share"] == 7 / 16
    assert rb["league_add_share"] == 10 / 40  # not the unweighted mean of team shares
    assert rb["relative_ratio"] == 1.75
    assert rb["league_add_sample"] == 40


def test_absent_league_position_activity_never_divides_by_zero(scouting_db):
    scouting_db.execute("UPDATE manager_metrics SET numerator=0,value=0 WHERE metric='rb_add_share'")
    rb = by_position(report(scouting_db)["position_affinity"], "RB")
    assert rb["league_add_share"] == 0 and rb["relative_ratio"] is None


def test_ratio_requires_sufficient_team_and_league_samples(scouting_db):
    scouting_db.execute("UPDATE manager_metrics SET denominator=2,sample_size=2 WHERE metric='rb_add_share'")
    assert by_position(report(scouting_db)["position_affinity"], "RB")["relative_ratio"] is None


def test_current_needs_pass_through_existing_numbers_and_formula(scouting_db):
    rb = report(scouting_db)["current_needs"][0]
    assert (rb["need_score"], rb["rostered"], rb["healthy"], rb["effective_available"]) == (0.82, 2, 1, 1.0)
    assert rb["starter_value"] == 4.3 and rb["league_starter_average"] == 7.2
    assert rb["details"]["formula"] == "stored"


def test_streaming_exposes_starter_rotation_and_pickups_separately(scouting_db):
    s = by_position(report(scouting_db)["streaming_style"], "D/ST")
    assert s["classification"] == "HIGH" and s["distinct_starters"] == 4
    assert s["turnover_changes"] == 4 and s["turnover_transitions"] == 5 and s["pickups"] == 3
    assert by_position(report(scouting_db)["streaming_style"], "QB")["classification"] == "INSUFFICIENT"


def test_zero_trades_mean_unknown_behavior(scouting_db):
    t = report(scouting_db)["trade_history"]
    assert t["completed_trades"] == 0 and t["recent_trades"] == []
    assert t["trade_behavior_confidence"] == "INSUFFICIENT"
    assert t["behavior"] == "UNKNOWN"


def add_trade(con, key, status="EXECUTED", day=1, reverse=False, player_offset=0):
    insert(con, "transactions", [dict(league_id=1, season=2026, transaction_id=key,
        type="TRADE_ACCEPT", status=status, process_date=datetime(2026, 10, day))])
    legs = [(101 + player_offset, 1, 2), (103 + player_offset, 1, 2), (201 + player_offset, 2, 1)]
    if reverse:
        legs = [(p, b, a) for p, a, b in legs]
    insert(con, "transaction_items", [dict(transaction_id=key, item_index=i,
        item_type="TRADE", espn_player_id=p, from_team_id=a, to_team_id=b) for i, (p, a, b) in enumerate(legs)])


def test_completed_trade_facts_exclude_pending_and_deduplicate_sources(scouting_db):
    add_trade(scouting_db, "one")
    add_trade(scouting_db, "two", day=8, reverse=True)
    add_trade(scouting_db, "pending", status="PENDING", day=10)
    add_trade(scouting_db, "canceled", status="CANCELED", day=11)
    insert(scouting_db, "activity_messages", [dict(league_id=1, season=2026, message_id=f"msg-{i}",
        topic_id="topic-one", message_type_id=244, topic_date=datetime(2026, 10, 1),
        espn_player_id=p, from_team_id=a, to_team_id=b) for i, (p, a, b) in enumerate([(101, 1, 2), (103, 1, 2), (201, 2, 1)])])
    t = report(scouting_db)["trade_history"]
    assert (t["completed_trades"], t["players_sent"], t["players_received"], t["uneven_player_count_trades"]) == (2, 3, 3, 2)
    assert t["recent_trades"][0]["players_sent"][0]["espn_player_id"] == 201
    assert t["trade_behavior_confidence"] == "INSUFFICIENT"
    assert set(t["recent_trades"][1]["sources"]) == {"activity", "transactions"}


@pytest.mark.parametrize(("count", "expected"), [(4, "INSUFFICIENT"), (5, "LOW"), (9, "LOW"), (10, "MEDIUM"), (20, "HIGH")])
def test_trade_confidence_depends_on_completed_count_not_weeks(scouting_db, count, expected):
    for i in range(count):
        add_trade(scouting_db, f"trade-{i}", day=i + 1, reverse=bool(i % 2), player_offset=1000 * i)
    assert report(scouting_db)["trade_history"]["trade_behavior_confidence"] == expected


def test_headlines_have_evidence_and_do_not_confuse_present_with_history(scouting_db):
    heads = report(scouting_db)["headlines"]
    assert heads and all(h["evidence"] and h["confidence"] for h in heads)
    assert any(h["category"] == "historical" and "1.8x" in h["text"] for h in heads)
    assert any(h["category"] == "current" and "RB" in h["text"] for h in heads)
    assert any("unknown" in h["text"] for h in heads)


def test_missing_current_snapshot_is_unknown_and_never_falls_back_to_history(scouting_db):
    c = report(scouting_db, 3)["current_roster"]
    assert not c["available"] and all(r["count"] is None for r in c["positions"])


def test_partial_streaming_metrics_do_not_crash_or_create_unsupported_headlines(scouting_db):
    scouting_db.execute("DELETE FROM manager_metrics WHERE metric='distinct_starters_dst'")
    d = report(scouting_db)
    assert by_position(d["streaming_style"], "D/ST")["distinct_starters"] is None
    assert not any("rotated starting D/ST" in h["text"] for h in d["headlines"])


def test_faab_state_and_spending_evidence(scouting_db):
    scouting_db.execute("UPDATE leagues SET is_faab=true,faab_budget=100")
    scouting_db.execute("UPDATE fantasy_teams SET faab_spent=25 WHERE team_id=1")
    insert(scouting_db, "manager_metrics", [dict(league_id=1, season=2026, team_id=1,
        metric="faab_spent_pct", value=0.25, numerator=25, denominator=100, sample_size=6)])
    insert(scouting_db, "manager_classifications", [dict(league_id=1, season=2026, team_id=1,
        dimension="faab_spending", label="MEDIUM", metric="faab_spent_pct", value=0.25,
        league_mean=0.3, sample_size=6, min_sample=3, confidence="HIGH", rule="Stored spending rule")])
    d = report(scouting_db)
    assert d["identity"]["faab_remaining"] == 75
    s = next(s for s in d["waiver_style"] if s["dimension"] == "faab_spending")
    assert s["value"] == 0.25 and s["league_mean"] == 0.3 and s["classification"] == "MEDIUM"


def test_report_works_on_a_read_only_database_connection(scouting_db):
    from app.db.connection import connect
    path = scouting_db.execute("SELECT path FROM duckdb_databases() WHERE NOT internal").fetchone()[0]
    scouting_db.close()
    c = connect(path, read_only=True)
    try:
        assert report(c)["identity"]["team_id"] == 1
    finally:
        c.close()


@pytest.mark.parametrize("change", ["missing", "sample", "value", "timestamp"])
def test_stale_classification_cannot_override_missing_or_changed_metric_evidence(scouting_db, change):
    scouting_db.execute("UPDATE manager_classifications SET computed_at=TIMESTAMP '2026-10-14' WHERE dimension='waiver_aggression'")
    if change == "missing":
        scouting_db.execute("DELETE FROM manager_metrics WHERE metric='adds_per_week'")
    elif change == "sample":
        scouting_db.execute("UPDATE manager_metrics SET sample_size=3 WHERE metric='adds_per_week'")
    elif change == "value":
        scouting_db.execute("UPDATE manager_metrics SET value=1.5 WHERE metric='adds_per_week'")
    else:
        scouting_db.execute("UPDATE manager_metrics SET computed_at=TIMESTAMP '2026-10-15' WHERE metric='adds_per_week'")
    d = report(scouting_db)
    s = next(s for s in d["waiver_style"] if s["dimension"] == "waiver_aggression")
    assert s["classification"] == "INSUFFICIENT" and s["confidence"] == "INSUFFICIENT"
    assert s["stored_classification"]["label"] == "HIGH"
    assert s["stored_classification"]["sample_size"] == 6
    assert s["evidence_status"] in ("MISSING_METRIC", "MISMATCHED")
    assert not any(any("adds_per_week =" in evidence for evidence in h["evidence"]) for h in d["headlines"])


def test_no_probability_or_preference_predictions_in_payload(scouting_db):
    payload = json.dumps(report(scouting_db), default=str).lower()
    assert "acceptance" not in payload and "probability" not in payload
    assert "prefers stars" not in payload and "accepts 2-for-1" not in payload


def test_report_does_not_write_and_is_scoped_to_selected_league(scouting_db):
    insert(scouting_db, "manager_metrics", [dict(league_id=99, season=2025, team_id=1,
        metric="rb_add_share", value=1, numerator=100, denominator=100, sample_size=100)])
    before = scouting_db.execute("SELECT * FROM manager_metrics ORDER BY league_id,team_id,metric").fetchall()
    assert by_position(report(scouting_db)["position_affinity"], "RB")["league_add_share"] == 0.25
    assert scouting_db.execute("SELECT * FROM manager_metrics ORDER BY league_id,team_id,metric").fetchall() == before


def test_scouting_api_and_unknown_manager(scouting_db, monkeypatch):
    from app.main import app
    db_path = scouting_db.execute("SELECT path FROM duckdb_databases() WHERE NOT internal").fetchone()[0]
    scouting_db.close()
    monkeypatch.setenv("FANTASY_DB_PATH", db_path)
    client = TestClient(app)
    r = client.get("/api/managers/1/scouting")
    assert r.status_code == 200 and r.json()["identity"]["team_id"] == 1
    assert client.get("/api/managers/999/scouting").status_code == 404
    assert client.post("/api/managers/1/scouting").status_code == 405


def test_no_league_returns_503(con, monkeypatch):
    from app.main import app
    db_path = con.execute("SELECT path FROM duckdb_databases() WHERE NOT internal").fetchone()[0]
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", db_path)
    assert TestClient(app).get("/api/managers/1/scouting").status_code == 503

"""Trade counterparty scouting (app/analytics/trade_counterparty.py, GET /api/managers/{team_id}/counterparty, issue
#215) on synthetic leagues: every pattern carries its numerator, denominator and window; small samples are suppressed;
missing provider data is unknown, not zero; reused team ids and changed managers are never attributed to the current
manager; nothing predicts acceptance."""
import json
import re
from datetime import datetime, timedelta

import pytest

from app.analytics.trade_counterparty import (
    MIN_TRADES,
    NotSynced,
    build_trade_counterparty,
)

T0 = datetime(2026, 9, 10, 12, 0)
POS = {101: "RB", 102: "RB", 103: "WR", 104: "TE", 105: "WR", 201: "WR", 202: "RB", 203: "WR", 301: "QB",
       302: "RB", 401: "WR", 501: "RB", 502: "WR"}


def insert(con, table, rows):
    for row in rows:
        cols = ",".join(row)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' for _ in row)})",
                    [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])


def league(con, season=2026, owners=None, teams=(1, 2, 3, 4), current=True):
    owners = owners or {}
    if current:
        insert(con, "leagues", [dict(league_id=7, season=season, name="Synthetic", current_week=13,
                                     synced_at=datetime(2026, 12, 1))])
    insert(con, "fantasy_teams", [dict(league_id=7, season=season, team_id=t, name=f"Team {t} {season}",
                                       owner_names=f"Owner {t}", owner_ids=owners.get(t, [f"m_{t}"]),
                                       is_mine=t == 1) for t in teams])


def players(con):
    insert(con, "fantasy_players", [dict(espn_player_id=p, full_name=f"Player {p}", position=pos)
                                    for p, pos in POS.items()])


def trade(con, tid, team, week, legs, season=2026, ttype="TRADE_ACCEPT", status="EXECUTED", raw=None):
    when = T0.replace(year=season) + timedelta(days=7 * week)
    insert(con, "transactions", [dict(league_id=7, season=season, transaction_id=tid, type=ttype, status=status,
                                      team_id=team, scoring_period=week, proposed_date=when, process_date=when,
                                      raw=raw or {})])
    insert(con, "transaction_items", [dict(transaction_id=tid, item_index=i, item_type="TRADE", espn_player_id=p,
                                           from_team_id=f, to_team_id=t) for i, (p, f, t) in enumerate(legs)])


def pickup(con, tid, team, week, season=2026):
    when = T0 + timedelta(days=7 * week)
    insert(con, "transactions", [dict(league_id=7, season=season, transaction_id=tid, type="FREEAGENT",
                                      status="EXECUTED", team_id=team, scoring_period=week, proposed_date=when,
                                      process_date=when)])
    insert(con, "transaction_items", [dict(transaction_id=tid, item_index=0, item_type="ADD", espn_player_id=501,
                                           from_team_id=-1, to_team_id=team)])


def pattern(report, key):
    return next(p for p in report["patterns"] if p["key"] == key)


def rows(report, key):
    return {r["label"]: (r["numerator"], r["denominator"]) for r in pattern(report, key)["rows"]}


@pytest.fixture
def busy(con):
    """Team 1: five executed trades (one with no recorded legs, one three-team), plus noise that must not count."""
    league(con)
    players(con)
    trade(con, "t1", 1, 3, [(101, 1, 2), (201, 2, 1)])                       # 1-for-1 with team 2
    trade(con, "t2", 1, 6, [(102, 1, 2), (103, 1, 2), (202, 2, 1)])          # 2-for-1 with team 2
    trade(con, "t3", 3, 10, [(104, 1, 3), (301, 3, 1)])                      # 1-for-1 with team 3
    trade(con, "t4", 1, 11, [])                                              # provider omitted the legs
    trade(con, "t5", 4, 12, [(105, 1, 3), (302, 3, 4), (401, 4, 1)])         # three teams
    trade(con, "t6", 2, 4, [(203, 2, 3), (502, 3, 2)])                       # not team 1: league total only
    trade(con, "offer", 1, 5, [(101, 1, 4)], ttype="TRADE_PROPOSAL", status="PENDING")   # never counted
    trade(con, "veto", 1, 5, [(101, 1, 4)], ttype="TRADE_VETO")                         # never counted
    pickup(con, "fa", 2, 2)
    return con


def test_every_pattern_counts_observed_trades_against_its_denominator(busy):
    r = build_trade_counterparty(busy, 1)
    assert r["status"] == "recorded"
    s = r["summary"]
    assert (s["trades"], s["league_trades"], s["trades_with_player_legs"], s["trades_without_player_legs"]) == (5, 6, 4, 1)
    assert (s["multi_team_trades"], s["players_sent"], s["players_received"], s["recurring_partners"]) == (1, 5, 4, 2)
    assert rows(r, "trade_size") == {"1-for-1": (3, 4), "2-for-1": (1, 4)}
    assert pattern(r, "trade_size")["unknown"] == 1                          # the legless trade: size unknown, not 0
    assert rows(r, "positions_sent") == {"RB": (2, 4), "WR": (2, 4), "TE": (1, 4)}
    sent = {x["label"]: (x["players"], x["players_denominator"]) for x in pattern(r, "positions_sent")["rows"]}
    assert sent == {"RB": (2, 5), "WR": (2, 5), "TE": (1, 5)}
    assert rows(r, "positions_received") == {"WR": (2, 4), "RB": (1, 4), "QB": (1, 4)}
    assert rows(r, "timing_week") == {"Weeks 1–4": (1, 5), "Weeks 5–8": (1, 5), "Weeks 9–12": (3, 5)}
    partners = {x["team_id"]: (x["numerator"], x["denominator"], x["recurring"]) for x in pattern(r, "partners")["rows"]}
    # the three-team trade counts once for each partner; the legless trade has no recorded partner
    assert partners == {2: (2, 4, True), 3: (2, 4, True), 4: (1, 4, False)}
    assert pattern(r, "partners")["unknown"] == 1
    for p in r["patterns"]:
        assert not p["suppressed"] and p["window"]["seasons"] == [2026]
        assert all(0 < x["numerator"] <= x["denominator"] == p["denominator"] for x in p["rows"])
    assert r["window"]["first_trade_at"].startswith("2026-10-01") and r["window"]["last_trade_at"].startswith("2026-12-03")
    assert "timing_season" not in {p["key"] for p in r["patterns"]}          # one season: no season breakdown
    assert r["discovery"] == {"opponent_team_id": 1, "positions_sent": ["RB", "WR", "TE"]}
    t5 = next(t for t in r["trades"] if t["season"] == 2026 and len(t["partners"]) == 2)
    assert t5["shape"] == "1-for-1" and [p["espn_player_id"] for p in t5["players_received"]] == [401]


def test_low_counts_are_suppressed_with_the_stated_threshold(busy):
    r = build_trade_counterparty(busy, 4)                                    # one trade
    assert r["summary"]["trades"] == 1 and r["min_trades"] == MIN_TRADES == 3
    for p in r["patterns"]:
        assert p["suppressed"] and p["rows"] == [] and str(MIN_TRADES) in p["suppressed_reason"]
        assert p["denominator"] == 1
    assert r["discovery"]["positions_sent"] == []                           # no position claims from one trade
    assert str(MIN_TRADES) in r["suppression_rule"]


def test_an_empty_history_is_a_measured_zero_with_an_honest_empty_state(con):
    league(con)
    pickup(con, "fa", 2, 2)                                                  # transactions are stored: none is a trade
    r = build_trade_counterparty(con, 1)
    assert r["status"] == "recorded" and r["summary"]["trades"] == 0 and r["summary"]["league_trades"] == 0
    assert r["trades"] == [] and all(p["suppressed"] and p["denominator"] == 0 for p in r["patterns"])
    assert r["window"]["text"] == "Season 2026; no dated trade."


def test_a_trade_recorded_from_one_side_only_makes_the_other_teams_count_a_minimum(con):
    league(con)
    trade(con, "t1", 2, 3, [])               # a 2-and-3 trade stored on team 2's row, no legs, no activity entry
    r = build_trade_counterparty(con, 3)     # team 3 cannot see it: its zero is not a measured zero
    s = r["summary"]
    assert r["status"] == "recorded" and (s["trades"], s["league_trades"], s["league_trades_unattributed"]) == (0, 1, 1)
    s = build_trade_counterparty(con, 2)["summary"]   # the recorded side sees its own trade, partner unknown
    assert (s["trades"], s["league_trades_unattributed"]) == (1, 0)


def test_recurring_partners_is_withheld_with_the_partners_pattern(busy):
    trade(busy, "t7", 4, 13, [(501, 4, 3), (202, 3, 4)])                    # team 4's second trade with team 3
    r = build_trade_counterparty(busy, 4)                                    # two trades: below the threshold
    assert pattern(r, "partners")["suppressed"] and r["summary"]["recurring_partners"] is None
    assert build_trade_counterparty(busy, 1)["summary"]["recurring_partners"] == 2


def test_missing_provider_transactions_are_unknown_not_zero(con):
    league(con)                                                              # no transaction rows at all
    r = build_trade_counterparty(con, 1)
    assert r["status"] == "not_recorded" and r["patterns"] == []
    assert set(r["summary"].values()) == {None}
    assert r["seasons"] == [{"season": 2026, "status": "not_recorded", "team_trades": None, "league_trades": None,
                             "reason": "No transactions are stored for this season: trades are unknown, not zero."}]


def test_players_without_a_listed_position_are_unknown(con):
    league(con)
    players(con)
    trade(con, "x", 1, 3, [(999, 1, 2), (201, 2, 1)])
    r = build_trade_counterparty(con, 1)
    assert pattern(r, "positions_sent")["unknown"] == 1 and r["summary"]["players_sent"] == 1


def _prior(con, owners_1, owners_2=None):
    league(con)
    league(con, season=2025, current=False, owners={1: owners_1, **({2: owners_2} if owners_2 else {})})
    players(con)
    for i, week in enumerate((2, 5, 9)):
        trade(con, f"old{i}", 1, week, [(101 + i, 1, 2), (201, 2, 1)], season=2025)
    trade(con, "now", 1, 3, [(105, 1, 3), (301, 3, 1)])


def test_a_reused_team_id_with_another_owner_is_not_attributed_to_the_current_manager(con):
    _prior(con, ["m_previous_owner"])
    r = build_trade_counterparty(con, 1)
    assert r["summary"]["trades"] == 1 and r["window"]["seasons"] == [2026]
    old = next(s for s in r["seasons"] if s["season"] == 2025)
    assert old["status"] == "excluded" and old["team_trades"] == 3 and "another manager" in old["reason"]
    assert all(t["season"] == 2026 for t in r["trades"])


def test_unknown_owners_in_an_earlier_season_exclude_it(con):
    _prior(con, [])
    old = next(s for s in build_trade_counterparty(con, 1)["seasons"] if s["season"] == 2025)
    assert old["status"] == "excluded" and "missing" in old["reason"]


def test_the_same_manager_in_an_earlier_season_is_included_and_partners_are_matched_by_owner(con):
    _prior(con, ["m_1"], owners_2=["m_new_owner_of_2"])
    r = build_trade_counterparty(con, 1)
    assert r["window"]["seasons"] == [2026, 2025] and r["summary"]["trades"] == 4
    assert r["summary"]["league_trades"] == 4
    assert rows(r, "timing_season") == {"2026": (1, 4), "2025": (3, 4)}
    partners = pattern(r, "partners")["rows"]
    # team 2 changed owners after 2025: its 2025 trades are with that season's team, not today's team 2
    old2 = next(p for p in partners if p["team_id"] == 2)
    assert (old2["season"], old2["matched_to_current_team"], old2["name"]) == (2025, False, "Team 2 2025")
    assert next(p for p in partners if p["team_id"] == 3)["matched_to_current_team"] is True
    assert r["window"]["text"] == "Seasons 2025–2026; trades dated 2025-09-24 to 2026-10-01."


def test_sleeper_half_legs_and_asset_only_trades(con):
    league(con)
    players(con)
    # Sleeper stores each traded player as a drop from one roster and an add to the other
    insert(con, "transactions", [dict(league_id=7, season=2026, transaction_id="sleeper:1", type="TRADE_ACCEPT",
                                      status="EXECUTED", team_id=1, scoring_period=4, process_date=T0,
                                      raw={"roster_ids": [1, 2], "draft_picks": [], "waiver_budget": []}),
                                 dict(league_id=7, season=2026, transaction_id="sleeper:2", type="TRADE_ACCEPT",
                                      status="EXECUTED", team_id=1, scoring_period=5, process_date=T0 + timedelta(days=9),
                                      raw={"roster_ids": [1, 3], "draft_picks": [{"round": 1}], "waiver_budget": []})])
    insert(con, "transaction_items", [dict(transaction_id="sleeper:1", item_index=0, item_type="TRADE",
                                           espn_player_id=101, from_team_id=1, to_team_id=None),
                                      dict(transaction_id="sleeper:1", item_index=1, item_type="TRADE",
                                           espn_player_id=101, from_team_id=None, to_team_id=2)])
    r = build_trade_counterparty(con, 1)
    assert r["summary"]["trades"] == 2 and r["summary"]["trades_with_player_legs"] == 1
    assert r["summary"]["trades_with_unlisted_assets"] == 1
    assert pattern(r, "partners")["denominator"] == 2                       # the pick-only trade names its partner


def test_unknown_team_and_unsynced_league(con):
    with pytest.raises(NotSynced):
        build_trade_counterparty(con, 1)
    league(con)
    assert build_trade_counterparty(con, 99) is None


def test_no_acceptance_likelihood_or_personality_claim(busy):
    text = json.dumps(build_trade_counterparty(busy, 1), default=str)
    keys = set(re.findall(r'"([a-z_]+)":', text))
    assert not {k for k in keys if re.search(r"likel|probab|willing|accept|personality|preference", k)}
    assert not re.search(r"(?i)\b(willing|eager|reluctant|likes to|prefers|loves|hates|probability)\b", text)


def test_the_route_answers_and_is_league_scoped(busy, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.routing import iter_routes
    path = tmp_path / "test.duckdb"
    busy.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    for name in ("FANTASY_API_KEY", "MULTILEAGUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    from app.main import app
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/managers/1/counterparty").json()["summary"]["trades"] == 5
    assert c.get("/api/managers/99/counterparty").status_code == 404
    assert c.get("/api/managers/x/counterparty").status_code == 422
    paths = {r.path for r in iter_routes(app)}
    assert "/v2/leagues/{league_id}/api/managers/{team_id}/counterparty" in paths

"""Weekly League Recap: hand-computed scoreboard, awards and standings on a small league; ties and
co-winners, undecided matchups, empty seasons, missing vs. zero player points, multi-week periods,
standings without future leakage, trade placement, null timestamps and read-only API behaviour.
Synthetic inputs only: they say nothing about real Los Subies results."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics.weekly_recap import (NotFound, RecapData, build_weekly_recap, completed_periods, load_recap_data,
                                        place_trade, resolve_week)
from app.db.connection import connect, fetch_dicts, init_db

# 4 teams, regular season = 3 matchup periods.
#   P1: 1 (120.50) - 2 (100.25) HOME     3 (90.00) - 4 (130.00) AWAY
#   P2: 1 (110.00) - 3 (110.00) TIE      2 (150.00) - 4 (80.00) HOME
#   P3: undecided, live points only      P4: playoff (WINNERS_BRACKET), 1 (100) - 2 (95) HOME
GAMES = [(1, 1, 1, 2, 120.5, 100.25, "HOME"), (2, 1, 3, 4, 90.0, 130.0, "AWAY"),
         (3, 2, 1, 3, 110.0, 110.0, "TIE"), (4, 2, 2, 4, 150.0, 80.0, "HOME"),
         (5, 3, 1, 4, 50.0, 40.0, "UNDECIDED"), (6, 3, 2, 3, 30.0, 20.0, "UNDECIDED"),
         (7, 4, 1, 2, 100.0, 95.0, "HOME")]
TIERS = {7: "WINNERS_BRACKET"}
RECORDS = {1: (1, 0, 1), 2: (1, 1, 0), 3: (0, 1, 1), 4: (1, 1, 0)}


def matchups(games=GAMES):
    return [{"matchup_id": mid, "matchup_period": p, "home_team_id": h, "away_team_id": a, "home_points": hp,
             "away_points": ap, "winner": w, "playoff_tier": TIERS.get(mid, "NONE")} for mid, p, h, a, hp, ap, w in games]


def league(games=GAMES, week=None, sched=None, **kw) -> RecapData:
    teams = [{"team_id": t, "name": f"Team {t}", "abbrev": f"T{t}", "wins": RECORDS[t][0], "losses": RECORDS[t][1],
              "ties": RECORDS[t][2]} for t in range(1, 5)]
    ms = matchups(games)
    lg = {"league_id": 1, "season": 2026, "name": "Hand", "reg_season_matchups": 3, "final_scoring_period": 6}
    return RecapData(league=lg, schedule_settings=sched or {"matchupPeriodLength": 1}, teams=teams, matchups=ms,
                     week=resolve_week(ms, week), **kw)


def by_id(rows, key="team_id"):
    return {r[key]: r for r in rows}


# ------------------------------------------------------------- scoreboard --

def test_default_week_is_the_latest_completed_and_undecided_periods_never_count():
    assert completed_periods(matchups()) == [1, 2, 4]          # P3 has live points but no ESPN winner
    res = build_weekly_recap(league())
    assert res["selected_week"] == 4 and res["available_weeks"] == [1, 2, 4]
    with pytest.raises(NotFound):
        resolve_week(matchups(), 3)


def test_hand_computed_week_one_scoreboard_and_awards():
    res = build_weekly_recap(league(week=1))
    assert res["is_completed_week"] and res["scoring_periods"] == [1]
    games = by_id(res["matchups"], "matchup_id")
    assert games[1]["home_team"] == {"team_id": 1, "name": "Team 1", "abbrev": "T1", "points": 120.5}
    assert games[1]["margin"] == 20.25 and games[2]["margin"] == 40.0 and not games[1]["is_tie"]
    a = res["awards"]
    assert [w["team_id"] for w in a["highest_score"]["winners"]] == [4]
    assert a["biggest_blowout"]["winners"][0] == {
        "matchup_id": 2, "winner_team_id": 4, "winner_name": "Team 4", "loser_team_id": 3, "loser_name": "Team 3",
        "margin": 40.0, "is_tie": False, "score_summary": "130.00 - 90.00"}
    assert [w["matchup_id"] for w in a["closest_matchup"]["winners"]] == [1]
    assert [(w["team_id"], w["points"]) for w in a["highest_scoring_loser"]["winners"]] == [(2, 100.25)]
    assert not any(a[k]["is_tied"] for k in a)


def test_a_tie_is_the_closest_matchup_but_never_a_blowout_or_a_loss():
    res = build_weekly_recap(league(week=2))
    a = res["awards"]
    closest = a["closest_matchup"]["winners"]
    assert [(w["matchup_id"], w["margin"], w["is_tie"]) for w in closest] == [(3, 0.0, True)]
    assert [w["matchup_id"] for w in a["biggest_blowout"]["winners"]] == [4]
    assert [w["team_id"] for w in a["highest_scoring_loser"]["winners"]] == [4]     # tied teams did not lose
    results = {s["team_id"]: s["weekly_result"] for s in res["standings"]}
    assert results == {1: "T", 2: "W", 3: "T", 4: "L"}


def test_equal_values_share_an_award_sorted_by_id():
    games = [(1, 1, 3, 4, 140.0, 100.0, "HOME"), (2, 1, 2, 1, 140.0, 100.0, "HOME")]
    a = build_weekly_recap(league(games))["awards"]
    for k in ("highest_score", "highest_scoring_loser"):
        assert a[k]["is_tied"]
    assert [w["team_id"] for w in a["highest_score"]["winners"]] == [2, 3]
    assert [w["team_id"] for w in a["highest_scoring_loser"]["winners"]] == [1, 4]
    assert [w["matchup_id"] for w in a["biggest_blowout"]["winners"]] == [1, 2]
    assert a["closest_matchup"]["is_tied"]


def test_margins_compare_to_the_hundredth_despite_float_noise():
    games = [(1, 1, 1, 2, 100.1 + 0.2, 100.0, "HOME"), (2, 1, 3, 4, 100.3, 100.0, "HOME")]
    a = build_weekly_recap(league(games))["awards"]
    assert [w["matchup_id"] for w in a["closest_matchup"]["winners"]] == [1, 2]


def test_a_partly_final_period_lists_only_final_games_and_says_so():
    games = [(1, 1, 1, 2, 120.0, 100.0, "HOME"), (2, 1, 3, 4, 10.0, 70.0, "UNDECIDED")]
    res = build_weekly_recap(league(games))
    assert [g["matchup_id"] for g in res["matchups"]] == [1]
    assert not res["is_completed_week"] and res["notes"]["pending_matchups"] == 1
    assert [w["team_id"] for w in res["awards"]["highest_score"]["winners"]] == [1]   # 70 live points never count


def test_a_final_matchup_without_scores_is_listed_but_kept_out_of_awards():
    games = [(1, 1, 1, 2, None, None, "HOME"), (2, 1, 3, 4, 90.0, 80.0, "HOME")]
    res = build_weekly_recap(league(games))
    assert by_id(res["matchups"], "matchup_id")[1]["margin"] is None
    assert res["notes"]["missing_scores"] == 1
    assert [w["matchup_id"] for w in res["awards"]["biggest_blowout"]["winners"]] == [2]
    one = by_id(res["standings"])[1]
    assert (one["wins"], one["points_for"]) == (1, None)        # the result counts, unknown points stay unknown


def test_biggest_upset_is_omitted_with_a_reason():
    up = build_weekly_recap(league(week=1))["awards"]["biggest_upset"]
    assert up["winners"] == [] and "pregame" in up["omitted_reason"]


def test_an_early_season_with_no_completed_matchups_is_an_empty_recap():
    games = [(1, 1, 1, 2, 12.0, 3.0, "UNDECIDED"), (2, 1, 3, 4, 0.0, 0.0, "UNDECIDED")]
    res = build_weekly_recap(league(games))
    assert res["selected_week"] is None and res["available_weeks"] == []
    assert res["matchups"] == [] and res["awards"] is None and res["standings"] == []


# ---------------------------------------------------------------- standings --

def test_standings_use_only_games_through_the_selected_week():
    res = build_weekly_recap(league(week=1))
    s = by_id(res["standings"])
    assert (s[1]["wins"], s[1]["ties"], s[1]["games"]) == (1, 0, 1)        # the week-2 tie is not visible yet
    assert [r["team_id"] for r in res["standings"]] == [4, 1, 2, 3]        # 1-0 teams by points for
    assert all(r["rank_change"] is None for r in res["standings"])         # no earlier week to compare
    assert res["standings_through"] == 1


def test_standings_order_win_pct_then_points_for_then_team_id_and_rank_change():
    res = build_weekly_recap(league(week=2))
    rows = res["standings"]
    assert [(r["team_id"], r["wins"], r["losses"], r["ties"]) for r in rows] == [
        (1, 1, 0, 1), (2, 1, 1, 0), (4, 1, 1, 0), (3, 0, 1, 1)]
    one = rows[0]
    assert one["win_pct"] == 0.75 and one["points_for"] == 230.5 and one["points_against"] == 210.25
    assert {r["team_id"]: r["rank_change"] for r in rows} == {1: 1, 2: 1, 4: -2, 3: 0}


def test_playoff_weeks_keep_the_final_regular_season_standings():
    res = build_weekly_recap(league(week=4))
    assert res["standings_through"] == 2                       # P3 never finished; P4 is a playoff game
    assert by_id(res["standings"])[1]["games"] == 2
    assert by_id(res["matchups"], "matchup_id")[7]["playoff_tier"] == "WINNERS_BRACKET"
    assert by_id(res["standings"])[1]["weekly_result"] == "W"
    assert res["notes"]["espn_record_check"] == {"compared": True, "mismatched_team_ids": []}


def test_the_espn_record_check_flags_a_mismatch_only_on_the_latest_week():
    data = league(week=2)
    data.teams[0]["wins"] = 5
    assert build_weekly_recap(data)["notes"]["espn_record_check"]["mismatched_team_ids"] == [1]
    assert build_weekly_recap(league(week=1))["notes"]["espn_record_check"] is None


# ------------------------------------------------------------------ players --

PLAYERS = {p: {"espn_player_id": p, "full_name": f"Player {p}", "position": "WR", "pro_team": "KC"}
           for p in range(101, 108)}


def lineup(week, team, pid, starter=True, slot="WR"):
    return {"scoring_period": week, "team_id": team, "espn_player_id": pid, "is_starter": starter, "lineup_slot": slot}


def test_missing_points_stay_unknown_and_zero_is_a_real_score():
    data = league(week=1, players=PLAYERS,
                  player_points=[{"espn_player_id": 101, "scoring_period": 1, "actual_points": 30.0},
                                 {"espn_player_id": 102, "scoring_period": 1, "actual_points": 0.0},
                                 {"espn_player_id": 103, "scoring_period": 1, "actual_points": None},
                                 {"espn_player_id": 105, "scoring_period": 1, "actual_points": 12.0},
                                 {"espn_player_id": 106, "scoring_period": 1, "actual_points": 8.0}],
                  lineups=[lineup(1, 1, 101), lineup(1, 2, 102), lineup(1, 3, 103), lineup(1, 4, 104),
                           lineup(1, 1, 105, starter=False, slot="BE"), lineup(1, 2, 106, starter=False, slot="IR")])
    res = build_weekly_recap(data)
    pts = {p["espn_player_id"]: p for p in res["standout_players"]}
    assert pts[101]["points"] == 30.0 and pts[101]["lineup_status"] == "STARTER"
    assert pts[101]["fantasy_team_name"] == "Team 1"
    assert pts[102]["points"] == 0.0                                       # a measured zero is kept
    assert 103 not in pts and 104 not in pts                               # null and absent rows are not zero
    assert res["notes"]["rostered_without_points"] == 2
    assert [p["espn_player_id"] for p in res["bench_standouts"]] == [105]
    assert pts[106]["lineup_status"] == "IR"


def test_standouts_rank_by_points_then_id_and_split_unrostered():
    pp = [{"espn_player_id": p, "scoring_period": 1, "actual_points": v} for p, v in ((101, 20.0), (102, 25.0), (103, 20.0))]
    res = build_weekly_recap(league(week=1, players=PLAYERS, player_points=pp, lineups=[lineup(1, 1, 101)]))
    assert [p["espn_player_id"] for p in res["standout_players"]] == [102, 101, 103]
    assert [p["espn_player_id"] for p in res["unrostered_standouts"]] == [102, 103]
    assert res["unrostered_standouts"][0]["fantasy_team_id"] is None


SCHED_2WK = {"matchupPeriodLength": 1, "matchupPeriods": {"1": [1], "2": [2], "3": [3], "4": [4, 5]}}


def test_a_two_week_period_sums_both_weeks_and_drops_partial_totals():
    pp = [{"espn_player_id": 101, "scoring_period": 4, "actual_points": 10.0},
          {"espn_player_id": 101, "scoring_period": 5, "actual_points": 15.5},
          {"espn_player_id": 102, "scoring_period": 4, "actual_points": 40.0},            # week 5 missing
          {"espn_player_id": 103, "scoring_period": 4, "actual_points": 5.0},
          {"espn_player_id": 103, "scoring_period": 5, "actual_points": 5.0}]
    lu = [lineup(4, 1, 101), lineup(5, 1, 101), lineup(4, 2, 103), lineup(5, 3, 103)]   # 103 traded mid-period
    res = build_weekly_recap(league(week=4, sched=SCHED_2WK, players=PLAYERS, player_points=pp, lineups=lu))
    assert res["scoring_periods"] == [4, 5]
    assert res["available_periods"][-1] == {"matchup_period": 4, "weeks": [4, 5]}
    pts = {p["espn_player_id"]: p for p in res["standout_players"]}
    assert pts[101]["points"] == 25.5 and pts[101]["fantasy_team_id"] == 1
    assert 102 not in pts and res["notes"]["partial_period_players"] == 1
    assert pts[103]["lineup_status"] == "STARTER" and pts[103]["fantasy_team_id"] is None   # two teams: no single one


def test_a_two_week_period_takes_moves_from_both_weeks():
    moves = [{"transaction_id": "a", "type": "WAIVER", "scoring_period": 4, "bid_amount": 7.0,
              "processed_at": datetime(2026, 10, 1), "item_index": 0, "item_type": "ADD", "espn_player_id": 101,
              "team_id": 1},
             {"transaction_id": "b", "type": "FREEAGENT", "scoring_period": 5, "bid_amount": 0.0,
              "processed_at": None, "item_index": 0, "item_type": "ADD", "espn_player_id": 102, "team_id": 2},
             {"transaction_id": "b", "type": "FREEAGENT", "scoring_period": 5, "bid_amount": 0.0,
              "processed_at": None, "item_index": 1, "item_type": "DROP", "espn_player_id": 103, "team_id": 2}]
    act = build_weekly_recap(league(week=4, sched=SCHED_2WK, players=PLAYERS, moves=moves))["league_activity"]
    assert [(a["player_id"], a["bid_amount"]) for a in act["adds"]] == [(101, 7.0), (102, None)]   # bids only for waivers
    assert act["adds"][1]["processed_at"] is None                         # a missing timestamp stays missing
    assert [d["player_id"] for d in act["drops"]] == [103] and act["total_moves"] == 3


# ------------------------------------------------------------------- trades --

SPANS = {3: (datetime(2026, 9, 22, 7), datetime(2026, 9, 28, 6)), 4: (datetime(2026, 9, 29, 7), datetime(2026, 10, 5, 6))}


def trade(tid, date, legs, period=None):
    return {"trade_id": tid, "date": date, "scoring_period": period, "sources": ["activity"],
            "source_ids": {"activity": [tid]},
            "legs": [{"espn_player_id": p, "from_team_id": f, "to_team_id": t} for p, f, t in legs]}


def test_place_trade_needs_exactly_one_span():
    assert place_trade(datetime(2026, 9, 24), SPANS) == 3
    assert place_trade(datetime(2026, 9, 28, 20), SPANS) is None              # between periods
    assert place_trade(None, SPANS) is None
    overlap = {**SPANS, 5: (datetime(2026, 9, 20), datetime(2026, 9, 25))}
    assert place_trade(datetime(2026, 9, 24), overlap) is None


def test_trades_are_placed_by_transaction_then_by_date_otherwise_unplaced():
    trades = [trade("t1", datetime(2026, 10, 2), [(101, 1, 2), (102, 2, 1), (103, 2, 1)], period=4),
              trade("a1", datetime(2026, 9, 30), [(104, 3, 4), (105, 4, 3)]),
              trade("a2", datetime(2026, 9, 24), [(106, 1, 3)]),
              trade("a3", datetime(2026, 9, 28, 20), [(107, 1, 4)])]
    act = build_weekly_recap(league(week=2, sched={"matchupPeriods": {"1": [3], "2": [4]}}, players=PLAYERS,
                                    trades=trades, period_spans=SPANS))["league_activity"]
    assert [(t["trade_id"], t["placed_by"]) for t in act["trades"]] == [("a1", "activity_date"), ("t1", "transaction")]
    t1 = act["trades"][1]
    assert [(s["team_id"], [p["player_id"] for p in s["received"]]) for s in t1["sides"]] == [(1, [102, 103]), (2, [101])]
    assert act["unplaced_trades"] == 1


# ---------------------------------------------------------------------- API --

def seed(db_path, games=GAMES, with_league=True):
    con = connect(db_path)
    init_db(con)
    if with_league:
        settings = {"scheduleSettings": {"matchupPeriodLength": 1, "matchupPeriods": {str(p): [p] for p in range(1, 7)}}}
        con.execute("INSERT INTO leagues (league_id, season, name, reg_season_matchups, final_scoring_period, "
                    "settings_raw, synced_at) VALUES (1, 2026, 'Hand', 3, 6, ?, now())", [json.dumps(settings)])
        for t in range(1, 5):
            con.execute("INSERT INTO fantasy_teams (league_id, season, team_id, name, abbrev, wins, losses, ties) "
                        "VALUES (1, 2026, ?, ?, ?, ?, ?, ?)", [t, f"Team {t}", f"T{t}", *RECORDS[t]])
        for m in matchups(games):
            con.execute("INSERT INTO matchups VALUES (1, 2026, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [m["matchup_id"], m["matchup_period"], m["home_team_id"], m["away_team_id"], m["home_points"],
                         m["away_points"], m["winner"], m["playoff_tier"]])
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team) VALUES "
                    "(101, 'Ann Able', 'QB', 'KC'), (102, 'Bo Bell', 'RB', 'SF'), (103, 'Cy Cole', 'WR', 'NE')")
        con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, "
                    "projected_points) VALUES (2026, 101, 1, 31.5, 99.0), (2026, 102, 1, NULL, 12.0), "
                    "(2026, 103, 1, 0.0, 8.0), (2026, 101, 2, 3.0, 20.0)")
        con.execute("INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id, "
                    "lineup_slot, is_starter) VALUES (1, 2026, 1, 1, 101, 'QB', true), (1, 2026, 1, 2, 102, 'RB', true)")
        con.execute("INSERT INTO transactions (league_id, season, transaction_id, type, status, team_id, scoring_period, "
                    "proposed_date, process_date, bid_amount) VALUES "
                    "(1, 2026, 'w1', 'WAIVER', 'EXECUTED', 3, 1, '2026-09-09 07:00', '2026-09-10 07:00', 4), "
                    "(1, 2026, 'w2', 'WAIVER', 'FAILED_INVALIDPLAYERSOURCE', 4, 1, '2026-09-09 07:00', NULL, 9), "
                    "(1, 2026, 'f1', 'FREEAGENT', 'EXECUTED', 4, 1, NULL, NULL, 0), "
                    "(1, 2026, 'f2', 'FREEAGENT', 'EXECUTED', 4, 2, '2026-09-17 07:00', NULL, 0), "
                    "(1, 2026, 't1', 'TRADE_ACCEPT', 'EXECUTED', 1, 1, '2026-09-11 07:00', '2026-09-11 08:00', NULL)")
        con.execute("INSERT INTO transaction_items (transaction_id, item_index, item_type, espn_player_id, from_team_id, "
                    "to_team_id) VALUES ('w1', 0, 'ADD', 103, -1, 3), ('w2', 0, 'ADD', 102, -1, 4), "
                    "('f1', 0, 'DROP', 102, 4, -1), ('f2', 0, 'ADD', 102, -1, 4), "
                    "('t1', 0, 'TRADE', 101, 1, 2), ('t1', 1, 'TRADE', 102, 2, 1)")
    con.close()


@pytest.fixture
def serve(tmp_path, monkeypatch):
    def _serve(**kw):
        db_path = tmp_path / "recap.duckdb"
        seed(db_path, **kw)
        monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
        from app.main import app
        return TestClient(app), db_path
    return _serve


def test_api_returns_the_latest_completed_week_from_the_database(serve):
    api, _ = serve()
    res = api.get("/api/weekly-recap").json()
    assert res["selected_week"] == 4 and res["league_name"] == "Hand"


def test_api_week_one_players_activity_and_standings(serve):
    api, _ = serve()
    res = api.get("/api/weekly-recap", params={"week": 1}).json()
    pts = {p["espn_player_id"]: p for p in res["standout_players"]}
    assert pts[101]["points"] == 31.5 and pts[101]["full_name"] == "Ann Able"
    assert pts[103]["points"] == 0.0 and pts[103]["lineup_status"] == "UNROSTERED"
    assert 102 not in pts and res["notes"]["rostered_without_points"] == 1       # NULL actual points stay unknown
    act = res["league_activity"]
    assert [(a["player_id"], a["type"], a["bid_amount"]) for a in act["adds"]] == [(103, "WAIVER", 4.0)]   # failed claim left out
    assert [(d["player_id"], d["processed_at"]) for d in act["drops"]] == [(102, None)]
    assert [[(s["team_id"], [p["player_name"] for p in s["received"]]) for s in t["sides"]] for t in act["trades"]] == [
        [(1, ["Bo Bell"]), (2, ["Ann Able"])]]
    assert [s["team_id"] for s in res["standings"]] == [4, 1, 2, 3]


def test_api_errors(serve):
    api, _ = serve()
    assert api.get("/api/weekly-recap", params={"week": 3}).status_code == 404     # live, not final
    assert api.get("/api/weekly-recap", params={"week": 0}).status_code == 422
    assert api.get("/api/weekly-recap", params={"week": "x"}).status_code == 422


def test_api_empty_season_and_no_league(serve, tmp_path, monkeypatch):
    api, _ = serve(games=[(1, 1, 1, 2, 3.0, 1.0, "UNDECIDED")])
    res = api.get("/api/weekly-recap").json()
    assert res["selected_week"] is None and res["matchups"] == []
    empty = tmp_path / "empty.duckdb"
    seed(empty, with_league=False)
    monkeypatch.setenv("FANTASY_DB_PATH", str(empty))
    from app.main import app
    assert TestClient(app).get("/api/weekly-recap").status_code == 503


def test_api_is_read_only(serve):
    api, db_path = serve()
    tables = ("matchups", "fantasy_teams", "transactions", "transaction_items", "espn_player_weekly_points",
              "lineup_snapshots")
    con = connect(db_path, read_only=True)
    before = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables}
    con.close()
    mtime = db_path.stat().st_mtime_ns
    for week in (1, 2, 4, None):
        assert api.get("/api/weekly-recap", params={} if week is None else {"week": week}).status_code == 200
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == before
    assert load_recap_data(con, 2).week == 2
    con.close()

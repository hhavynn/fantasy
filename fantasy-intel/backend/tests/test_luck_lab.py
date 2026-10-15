"""Luck vs. Skill Lab: hand-computed expected wins, schedule luck, all-play, notable results and
counterfactual schedules on a small league; ties, observed zeros, missing scores, in-progress
matchups, incomplete coverage, multi-week periods, the regular-season/playoff boundary, duplicate or
missing ids, self-matchup exclusions, ranking consistency, determinism, PR #13 all-play consistency
and read-only API behaviour. Synthetic inputs only: they say nothing about real Los Subies results."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.analytics.luck_lab import build_luck_lab, counterfactual, load_luck_data, observed_periods, period_rows
from app.analytics.playoff_race import RaceData, build_playoff_race
from app.db.connection import connect, fetch_dicts

import synthetic_race as sr
from test_playoff_race import _db_league
from test_trade_data import _raw_snapshot

# Hand-computed league: 4 teams, regular season = 4 matchup periods.
#   P1: 1 (100) - 2 (120) L/W   3 (90) - 4 (80) W/L
#   P2: 1 (130) - 3 (140) L/W   2 (70) - 4 (70) TIE
#   P3: 1 (150) - 4 (110) W/L   2 (60) - 3 (0.0, an observed zero) W/L
#   P4: in progress (UNDECIDED, live points)      P5: playoff (WINNERS_BRACKET)
GAMES = [(1, 1, 1, 2, 100.0, 120.0, "AWAY"), (2, 1, 3, 4, 90.0, 80.0, "HOME"),
         (3, 2, 1, 3, 130.0, 140.0, "AWAY"), (4, 2, 2, 4, 70.0, 70.0, "TIE"),
         (5, 3, 1, 4, 150.0, 110.0, "HOME"), (6, 3, 2, 3, 60.0, 0.0, "HOME"),
         (7, 4, 1, 2, 40.0, 35.0, "UNDECIDED"), (8, 4, 3, 4, 20.0, 10.0, "UNDECIDED")]


def league(games=GAMES, extra=(), teams=4, reg=4, sched=None, mine=1) -> RaceData:
    matchups = [{"matchup_id": mid, "matchup_period": p, "home_team_id": h, "away_team_id": a, "home_points": hp,
                 "away_points": ap, "winner": w, "playoff_tier": "NONE"} for mid, p, h, a, hp, ap, w in games]
    matchups += [{"matchup_id": 9, "matchup_period": 5, "home_team_id": 1, "away_team_id": 2, "home_points": 1.0,
                  "away_points": 200.0, "winner": "AWAY", "playoff_tier": "WINNERS_BRACKET"}]
    matchups += list(extra)
    rec = {1: (1, 2, 0), 2: (2, 0, 1), 3: (2, 1, 0), 4: (0, 2, 1)}
    tms = [{"team_id": t, "name": f"Team {t}", "abbrev": f"T{t}", "is_mine": t == mine, "wins": rec.get(t, (0, 0, 0))[0],
            "losses": rec.get(t, (0, 0, 0))[1], "ties": rec.get(t, (0, 0, 0))[2], "points_for": None, "points_against": None}
           for t in range(1, teams + 1)]
    lg = {"league_id": 1, "season": 2026, "name": "Hand", "current_week": 4, "reg_season_matchups": reg,
          "final_scoring_period": 6}
    return RaceData(league=lg, schedule_settings=sched or {"matchupPeriodLength": 1}, rules_raw={}, teams=tms,
                    matchups=matchups, state=None, window={})


def summ(res, t):
    return res["summaries"][str(t)]


# ----------------------------------------------------------- hand-computed --

def test_hand_computed_expected_wins_luck_and_all_play():
    res = build_luck_lab(league())
    assert res["coverage"]["eligible_periods"] == [1, 2, 3]
    ex = {e["matchup_period"]: e for e in res["coverage"]["excluded_periods"]}
    assert list(ex) == [4] and ex[4]["reasons"] == ["2 matchup(s) not final (no ESPN winner yet)"]   # live points never count
    assert ex[4]["kind"] == "not_completed"
    assert res["coverage"]["data_notes"]["playoff_rows"] == 1                               # the playoff game stays out
    expected = {1: 2 / 3 + 2 / 3 + 1, 2: 1 + 0.5 / 3 + 1 / 3, 3: 1 / 3 + 1 + 0, 4: 0 + 0.5 / 3 + 2 / 3}
    actual = {1: 1.0, 2: 2.5, 3: 2.0, 4: 0.5}
    for t in range(1, 5):
        s = summ(res, t)
        assert s["periods"] == 3
        assert s["expected_wins"] == pytest.approx(expected[t], abs=1e-3)
        assert s["actual"]["win_equivalents"] == actual[t]
        assert s["schedule_luck"] == pytest.approx(actual[t] - expected[t], abs=1e-3)
    assert summ(res, 2)["actual"] == {"wins": 2, "losses": 0, "ties": 1, "win_equivalents": 2.5}
    assert sum(summ(res, t)["schedule_luck"] for t in range(1, 5)) == pytest.approx(0, abs=1e-2)   # zero-sum
    ap = {t: (summ(res, t)["all_play"]["wins"], summ(res, t)["all_play"]["losses"], summ(res, t)["all_play"]["ties"])
          for t in range(1, 5)}
    assert ap == {1: (7, 2, 0), 2: (4, 4, 1), 3: (4, 5, 0), 4: (2, 6, 1)}
    assert [x["team_id"] for x in res["leaderboard"]] == [2, 3, 4, 1]                     # most positive luck first
    assert [x["luck_rank"] for x in res["leaderboard"]] == [1, 2, 3, 4]
    assert summ(res, 3)["scoring"]["min"] == 0.0 and summ(res, 3)["scoring"]["last_place"] == 1   # the zero is real


def test_hand_computed_notable_results_and_difficulty():
    res = build_luck_lab(league())
    assert res["rules"]["band_size"] == 2                                                    # 4 teams: top/bottom two
    t1 = summ(res, 1)
    brutal = t1["notable"]["brutal_losses"]
    assert [b["matchup_period"] for b in brutal] == [2, 1]          # same weekly rank (2): the higher score first
    assert all(set(b["flags"]) == {"top_band_loss", "above_median_loss", "lost_to_top_scorer"} for b in brutal)
    assert brutal[0]["rank"] == 2 and brutal[0]["opponent_rank"] == 1 and brutal[0]["margin"] == -10.0
    assert brutal[1]["score"] == 100.0 and brutal[1]["margin"] == -20.0
    fortunate = summ(res, 3)["notable"]["fortunate_wins"]
    assert [f["matchup_period"] for f in fortunate] == [1] and set(fortunate[0]["flags"]) == {"bottom_band_win",
                                                                                              "below_median_win"}
    assert [f["matchup_period"] for f in summ(res, 2)["notable"]["fortunate_wins"]] == [3]   # 60 beat 0.0
    d = t1["difficulty"]
    assert d["opponent_points_total"] == 120 + 140 + 110 and d["opponent_points_mean"] == pytest.approx(370 / 3, abs=0.01)
    assert d["hardest"]["opponent_score"] == 140 and d["easiest"]["opponent_score"] == 110
    assert d["opponents_top_band"] == 3                                                     # ranks 1, 1, 2
    sc = t1["scoring"]
    assert (sc["mean"], sc["median"], sc["min"], sc["max"], sc["range"]) == (126.67, 130.0, 100.0, 150.0, 50.0)
    assert sc["first_place"] == 1 and sc["top_half_pct"] == 1.0
    assert (sc["q1"], sc["q3"], sc["iqr"]) == (115.0, 140.0, 25.0)                          # inclusive quartiles


def test_hand_computed_counterfactual_schedules_and_self_matchups():
    res = build_luck_lab(league(), team_id=1, schedule_of=2)
    cf = res["counterfactual"]
    assert cf["periods_compared"] == [2, 3]
    assert cf["excluded"] == [{"matchup_period": 1, "reason": "team 2 played team 1 this period (self-matchup)"}]
    assert cf["actual"]["win_equivalents"] == 1.0 and cf["counterfactual"]["win_equivalents"] == 2.0
    assert cf["win_equivalent_difference"] == 1.0 and cf["flips"]["loss_to_win"] == [2]
    row2 = next(r for r in cf["rows"] if r["matchup_period"] == 2)
    assert (row2["substituted_opponent_id"], row2["substituted_opponent_score"], row2["counterfactual_result"]) == (4, 70.0, "W")
    allc = {c["schedule_of"]: c for c in res["all_schedules"]}
    assert {b: (c["periods_compared"], c["actual"]["win_equivalents"], c["counterfactual"]["win_equivalents"])
            for b, c in allc.items()} == {2: ([2, 3], 1.0, 2.0), 3: ([1, 3], 1.0, 2.0), 4: ([1, 2], 0.0, 2.0)}
    assert all(c["same_periods_as_all_eligible"] is False for c in allc.values())          # each drops its self period
    assert allc[4]["flips"]["loss_to_win"] == [1, 2]
    with pytest.raises(ValueError):
        build_luck_lab(league(), team_id=1, schedule_of=1)


def test_timeline_cumulative_luck_and_biggest_contributors():
    res = build_luck_lab(league(), team_id=1)
    tl = res["timeline"]
    assert [x["matchup_period"] for x in tl] == [1, 2, 3]
    assert [x["result"] for x in tl] == ["L", "L", "W"]
    assert tl[-1]["cumulative_luck"] == pytest.approx(summ(res, 1)["schedule_luck"], abs=1e-3)
    assert res["biggest_contributors"][0]["matchup_period"] in (1, 2)                       # -2/3 each
    assert res["spotlight"]["luck_rank"] == 4 and res["spotlight"]["scoring_rank"] == 1


def test_matrix_and_rank_consistency():
    res = build_luck_lab(league())
    m = res["matrix"]
    assert [p["matchup_period"] for p in m["periods"]] == [1, 2, 3]
    t2 = next(r for r in m["teams"] if r["team_id"] == 2)
    assert [(c["rank"], c["result"]) for c in t2["cells"]] == [(1, "W"), (3, "T"), (3, "W")]  # P2 70-70 shares rank 3
    lb = res["leaderboard"]
    assert all(lb[i]["schedule_luck"] >= lb[i + 1]["schedule_luck"] for i in range(len(lb) - 1))
    assert sorted(x["scoring_rank"] for x in lb) == [1, 2, 3, 4]


# ---------------------------------------------------------------- edge cases --

def test_top_scorer_can_lose_on_an_espn_tiebreak_and_the_record_follows_espn():
    games = [(1, 1, 1, 2, 120.0, 120.0, "AWAY"), (2, 1, 3, 4, 90.0, 80.0, "HOME")]       # tied top score, ESPN: away
    res = build_luck_lab(league(games=games, reg=1))
    row = res["matrix"]["teams"][0]["cells"][0]
    assert row["rank"] == 1 and row["result"] == "L" and "top_band_loss" in row["flags"]
    assert summ(res, 1)["expected_wins"] == pytest.approx((2 + 0.5) / 3, abs=1e-3)         # tied for first in all-play
    assert "tiebreak" in res["rules"]["result_source"]


def test_missing_score_bye_incomplete_coverage_and_unknown_teams_exclude_the_period():
    games = list(GAMES[:6])
    games[0] = (1, 1, 1, 2, 100.0, None, "AWAY")                                         # a final with a missing score
    games[4] = (5, 3, 1, None, 150.0, None, "UNDECIDED")                                 # one-team row in P3
    res = build_luck_lab(league(games=games))
    ex = {e["matchup_period"]: e["reasons"] for e in res["coverage"]["excluded_periods"]}
    assert res["coverage"]["eligible_periods"] == [2]
    assert any("missing score" in r for r in ex[1])
    assert any("one-team row" in r for r in ex[3])
    assert all(summ(res, t)["periods"] == 1 for t in range(1, 5))                          # same periods for everyone
    # A period where one team has no result at all.
    res = build_luck_lab(league(games=[g for g in GAMES[:6] if g[0] != 6]))
    ex = {e["matchup_period"]: e for e in res["coverage"]["excluded_periods"]}
    assert ex[3]["teams_with_final_scores"] == 2 and ex[3]["kind"] == "incomplete"
    assert any("no completed result for team(s) 2, 3" in r for r in ex[3]["reasons"])
    # Five teams: every period has someone missing, so nothing is eligible, and that is said.
    res = build_luck_lab(league(teams=5))
    assert res["coverage"]["eligible_periods"] == [] and res["leaderboard"] == [] and res["timeline"] == []
    assert summ(res, 1)["periods"] == 0


def test_duplicate_and_missing_matchup_ids():
    dup_same = {"matchup_id": 1, "matchup_period": 1, "home_team_id": 1, "away_team_id": 2, "home_points": 100.0,
                "away_points": 120.0, "winner": "AWAY", "playoff_tier": "NONE"}
    res = build_luck_lab(league(extra=[dup_same]))
    assert res["coverage"]["data_notes"]["duplicate_ids_merged"] == [1] and res["coverage"]["eligible_periods"] == [1, 2, 3]
    conflict = {**dup_same, "home_points": 999.0}
    res = build_luck_lab(league(extra=[conflict]))
    assert res["coverage"]["data_notes"]["conflicting_ids"] == [1] and 1 not in res["coverage"]["eligible_periods"]
    no_id = [(None, *g[1:]) if g[0] == 2 else g for g in GAMES]
    res = build_luck_lab(league(games=no_id))
    assert res["coverage"]["data_notes"]["missing_ids"] == 1 and res["coverage"]["eligible_periods"] == [1, 2, 3]
    twice = {"matchup_id": 50, "matchup_period": 2, "home_team_id": 1, "away_team_id": 4, "home_points": 1.0,
             "away_points": 2.0, "winner": "AWAY", "playoff_tier": "NONE"}
    res = build_luck_lab(league(extra=[twice]))
    ex = {e["matchup_period"]: e["reasons"] for e in res["coverage"]["excluded_periods"]}
    assert any("more than one matchup" in r for r in ex[2])


def test_multi_week_periods_and_the_playoff_boundary():
    sched = {"matchupPeriodLength": 1, "matchupPeriods": {"1": [1], "2": [2], "3": [3, 4], "4": [5]}}
    res = build_luck_lab(league(sched=sched))
    assert [p["weeks"] for p in res["matrix"]["periods"]] == [[1], [2], [3, 4]]           # period totals, not weeks
    assert res["timeline"][2]["weeks"] == [3, 4]
    res = build_luck_lab(league(reg=2))                                                   # period 3 is now playoffs
    assert res["coverage"]["eligible_periods"] == [1, 2] and res["coverage"]["data_notes"]["playoff_rows"] == 5


def test_deterministic_and_inputs_unchanged():
    d = league()
    snap = copy.deepcopy(d)
    a, b = build_luck_lab(d, schedule_of=3), build_luck_lab(d, schedule_of=3)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert d.matchups == snap.matchups and d.teams == snap.teams
    from app.analytics.luck_lab import NotFound
    with pytest.raises(NotFound):
        build_luck_lab(d, team_id=99)
    with pytest.raises(NotFound):
        build_luck_lab(d, schedule_of=99)


def test_all_play_matches_the_playoff_race_lab_on_fully_observed_periods():
    d = sr.race(n_teams=6, reg_periods=8, start_week=5, final_week=10,
                scores={(2, 3): 0.0, (3, 1): 105.0, (3, 2): 105.0})
    race = build_playoff_race(d)
    luck = build_luck_lab(d)
    assert luck["coverage"]["eligible_periods"] == [1, 2, 3, 4]
    for t in race["teams"]:
        s = summ(luck, t["team_id"])
        assert {k: s["all_play"][k] for k in ("wins", "losses", "ties")} == \
            {k: t["all_play"][k] for k in ("wins", "losses", "ties")}
        assert s["all_play"]["pct"] == t["all_play"]["pct"]
    eligible, _, _ = observed_periods(d)
    per = period_rows(eligible)
    c = counterfactual(per, 1, 2)
    assert len(c["periods_compared"]) + len(c["excluded"]) == 4


# ------------------------------------------------------------- database & API --

def test_api_is_read_only_and_leaves_other_outputs_unchanged(tmp_path, monkeypatch):
    db_path, con = _db_league(tmp_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    con.execute("UPDATE matchups SET league_id = ?", [lid])
    data = load_luck_data(con)
    assert len(data.teams) == 4 and len(data.matchups) == 28
    raw = _raw_snapshot(con)
    tables = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in
              ("player_scores", "team_ros_lineup_projections", "waiver_recommendations", "fantasy_teams", "matchups")}
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    from test_schedule_intelligence import stable
    api = TestClient(app)
    trade = {"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304], "team_b_sends": [301]}
    before = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/playoff-race").json())
    mtime = db_path.stat().st_mtime_ns
    res = api.get("/api/luck-lab")
    assert res.status_code == 200
    body = res.json()
    assert body["focus_team_id"] == 2 and body["coverage"]["eligible_periods"] == [1, 2]
    race = before[1]
    for t in race["teams"]:
        assert {k: body["summaries"][str(t["team_id"])]["all_play"][k] for k in ("wins", "losses", "ties")} == \
            {k: t["all_play"][k] for k in ("wins", "losses", "ties")}
    cf = api.get("/api/luck-lab", params={"team_id": 2, "schedule_of": 3}).json()["counterfactual"]
    assert cf["schedule_of"] == 3 and len(cf["periods_compared"]) + len(cf["excluded"]) == 2
    assert api.get("/api/luck-lab", params={"team_id": 99}).status_code == 404
    assert api.get("/api/luck-lab", params={"team_id": 2, "schedule_of": 2}).status_code == 422
    after = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/playoff-race").json())
    assert stable(before) == stable(after)
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    assert {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in tables} == tables
    con.close()


def test_api_without_league_and_module_isolation(tmp_path, monkeypatch):
    from app.db.connection import init_db
    db_path = tmp_path / "empty.duckdb"
    con = connect(db_path)
    init_db(con)
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    assert TestClient(app).get("/api/luck-lab").status_code == 503
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "analytics"
    for f in ("player_scores.py", "waivers.py", "projections.py", "trades.py", "team_lineups.py", "trade_data.py",
              "trade_discovery.py", "pipeline.py", "context.py", "schedule_intelligence.py", "playoff_race.py"):
        assert "luck_lab" not in (root / f).read_text(), f


def test_ranks_use_exact_values_not_rounded_display_numbers():
    # Team 1 averages 100.0033, Team 2 exactly 100.00: both display as 100.00 but must not share a rank.
    games = [(1, 1, 1, 2, 100.01, 100.0, "HOME"), (2, 1, 3, 4, 50.0, 40.0, "HOME"),
             (3, 2, 1, 3, 100.0, 50.0, "HOME"), (4, 2, 2, 4, 100.0, 40.0, "HOME"),
             (5, 3, 1, 4, 100.0, 40.0, "HOME"), (6, 3, 2, 3, 100.0, 50.0, "HOME")]
    res = build_luck_lab(league(games=games, reg=3))
    lb = {r["team_id"]: r for r in res["leaderboard"]}
    assert lb[1]["scoring_mean"] == lb[2]["scoring_mean"] == 100.0
    assert (lb[1]["scoring_rank"], lb[2]["scoring_rank"]) == (1, 2)
    assert sum(r["schedule_luck"] for r in res["leaderboard"]) == pytest.approx(0.0)

"""Playoff Race Lab: standings and all-play, future head-to-head mapping, weekly optimized matchup
totals, missing vs zero projections, byes / ties / in-progress / unknown opponents, playoff spots and
boundaries, multi-week matchup periods, scenario overrides, leverage and pathways, determinism, and
read-only behaviour with no effect on the trade or projection engines. Synthetic inputs only
(tests/synthetic_race.py, plus the ESPN fixture for database and API tests); they say nothing about
real predictive accuracy."""

import copy
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.analytics.playoff_race import (CLOSE_SHARE, NotFound, build_playoff_race, load_race_data, parse_overrides,
                                        period_weeks)
from app.analytics.team_lineups import project_team
from app.db.connection import connect, fetch_dicts

import synthetic_race as sr
from espn_fixture import SEASON
from test_trade_data import _raw_snapshot, synced


def matchup(res, mid):
    return next(m for m in res["matchups"]["regular"] if m["matchup_id"] == mid)


def standing(rows, tid):
    return next(x for x in rows if x["team_id"] == tid)


def team(res, tid):
    return next(t for t in res["teams"] if t["team_id"] == tid)


def rec(x):
    return (x["record"]["wins"], x["record"]["losses"], x["record"]["ties"])


# --------------------------------------------------------------- standings & all-play --

def tie_league(spots=2):
    # 4 teams, periods 1-3 final (round robin: p1 1-4 3-2, p2 3-1 4-2, p3 1-2 4-3), periods 4-6 ahead.
    # Default score 100 + 10 * (4 - team). Period 1: team 1 scores 120 (all-play tie with team 2);
    # period 3: teams 3 and 4 both 110 (a real ESPN tie, and an all-play tie).
    return sr.race(n_teams=4, reg_periods=6, spots=spots, start_week=4, final_week=8,
                   scores={(1, 1): 120.0, (3, 3): 110.0, (3, 4): 110.0})


def test_current_standings_records_ties_and_all_play():
    res = build_playoff_race(tie_league(), base="actual")
    assert {t["team_id"]: rec(t) for t in res["teams"]} == {1: (3, 0, 0), 2: (2, 1, 0), 3: (0, 2, 1), 4: (0, 2, 1)}
    ap = {t["team_id"]: (t["all_play"]["wins"], t["all_play"]["losses"], t["all_play"]["ties"]) for t in res["teams"]}
    assert ap == {1: (8, 0, 1), 2: (6, 2, 1), 3: (2, 6, 1), 4: (0, 8, 1)}           # hand-computed
    assert team(res, 1)["all_play"]["periods"] == [1, 2, 3] and team(res, 1)["all_play"]["pct"] == round(8.5 / 9, 4)
    cur = res["scenario"]["current_standings"]
    assert [(x["team_id"], x["rank"], x["tied"], x["status"]) for x in cur] == [
        (1, 1, False, "inside"), (2, 2, False, "inside"), (3, 3, True, "outside"), (4, 3, True, "outside")]
    assert standing(cur, 3)["pct"] == round(1 / 6, 4)                                  # a tie counts half
    assert res["record_check"] == []                                                   # ESPN records match the finals
    # With 3 spots the two tied teams straddle the cutline: unresolved, no tiebreak applied, and
    # neither is named the last team in or the first team out.
    res3 = build_playoff_race(tie_league(spots=3), base="actual")
    cur3 = res3["scenario"]["current_standings"]
    assert standing(cur3, 3)["status"] == standing(cur3, 4)["status"] == "cutline_tie"
    cut = res3["scenario"]["cutline"]
    assert cut["cutline_tie"] == {"team_ids": [3, 4], "spots_left": 1}
    assert [x["team_id"] for x in cut["last_inside"]] == [2] and cut["first_outside"] == []
    assert {3, 4} <= set(cut["bubble"])
    # Every team tied (all period-1 games tied): the whole league straddles the cutline, nobody is in.
    every = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=2, final_week=8,
                    scores={(1, t): 100.0 for t in range(1, 5)})
    cut_all = build_playoff_race(every, base="actual")["scenario"]["cutline"]
    assert cut_all["cutline_tie"] == {"team_ids": [1, 2, 3, 4], "spots_left": 2} and cut_all["last_inside"] == []
    # No decided games at all: no status, so no cutline position.
    none = build_playoff_race(sr.race(n_teams=4, reg_periods=6, spots=2, start_week=1, final_week=8), base="actual")
    assert all(x["status"] is None for x in none["scenario"]["projected_standings"])
    assert none["scenario"]["cutline"]["cutline_tie"] is None and none["scenario"]["cutline"]["last_inside"] == []
    # A tied group that fits exactly is reported whole as the last group inside.
    cut4 = build_playoff_race(tie_league(spots=4), base="actual")["scenario"]["cutline"]
    assert [x["team_id"] for x in cut4["last_inside"]] == [3, 4] and cut4["cutline_tie"] is None


def test_espn_record_is_the_verified_start_and_mismatches_are_reported():
    d = tie_league()
    d.teams[0]["wins"] = 4                                       # ESPN says 4-0-0, stored finals say 3-0-0
    res = build_playoff_race(d, base="actual")
    assert rec(team(res, 1)) == (4, 0, 0)
    assert any("differs from the final matchups stored 3-0-0" in n for n in res["record_check"])
    d.teams[0]["wins"] = None                                    # missing ESPN record: derived, and said so
    res = build_playoff_race(d, base="actual")
    assert rec(team(res, 1)) == (3, 0, 0) and any("derived from final matchups" in n for n in res["record_check"])


# ------------------------------------------------------- future matchups & lineups --

def test_future_head_to_head_mapping_and_weekly_optimized_totals():
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8,
                strength={1: 30.0, 2: 20.0, 3: 25.0, 4: 22.0},
                projection_patch={404: {4: 6.0}})               # team 4's bench RB out-projects its WR in week 4
    res = build_playoff_race(d)
    pairs = sr.round_robin([1, 2, 3, 4], 6)
    upcoming = [m for m in res["matchups"]["regular"] if m["status"] == "upcoming"]
    assert sorted((m["matchup_period"], m["home_team_id"], m["away_team_id"]) for m in upcoming) == \
        sorted((p, h, a) for p in (4, 5, 6) for h, a in pairs[p])
    window = d.state.window()
    for m in upcoming:
        for side, tid in (("home_projection", m["home_team_id"]), ("away_projection", m["away_team_id"])):
            canonical = project_team(d.state.rosters[tid], d.state.slots, window, d.state.projections)
            expect = sum(w["projected_lineup_points"] for w in canonical["weeks"] if w["week"] in m["weeks"])
            assert m[side]["projected_points"] == pytest.approx(expect)                 # the canonical optimizer
    w4 = next(m for m in upcoming if m["matchup_period"] == 4 and 4 in (m["home_team_id"], m["away_team_id"]))
    side = "home_projection" if w4["home_team_id"] == 4 else "away_projection"
    assert w4[side]["projected_points"] == 22.0 + 10 + 6                               # FLEX takes the 6.0 RB
    other = next(m for m in upcoming if m["matchup_period"] == 5 and 1 in (m["home_team_id"], m["away_team_id"]))
    s1 = "home_projection" if other["home_team_id"] == 1 else "away_projection"
    assert other[s1]["projected_points"] == 30.0 + 15                                  # bench (1.0) never counts
    for m in upcoming:
        hp, ap = m["home_projection"]["projected_points"], m["away_projection"]["projected_points"]
        assert m["projected_margin"] == pytest.approx(hp - ap)
        assert m["projected_winner"] == ("home" if hp > ap else "away")
        assert m["close"] == (abs(hp - ap) <= CLOSE_SHARE * (hp + ap) / 2)
    assert team(res, 1)["regular_season_projected_points"] == 3 * 45.0                # weeks 4-6 only (regular)


def test_missing_projection_is_unresolved_and_a_projected_zero_is_real():
    pairs = sr.round_robin([1, 2, 3, 4], 6)
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8,
                projection_patch={101: {4: None}, 201: {4: 0.0}})     # team 1 QB unknown, team 2 QB a real 0
    res = build_playoff_race(d)
    m1 = next(m for m in res["matchups"]["regular"] if m["matchup_period"] == 4 and 1 in (m["home_team_id"], m["away_team_id"]))
    s = m1["home_projection"] if m1["home_team_id"] == 1 else m1["away_projection"]
    assert s["complete"] is False and s["projected_points"] is None and s["known_points"] == 15.0
    assert s["missing_players"] == ["QB 1"] and "without an ESPN projection" in s["gap"]
    assert m1["projected_winner"] is None and m1["projected_margin"] is None and m1["scenario_result"] is None
    assert "projection incomplete for Team 1" in m1["detail"]
    m2 = next(m for m in res["matchups"]["regular"] if m["matchup_period"] == 4 and 2 in (m["home_team_id"], m["away_team_id"]))
    s2 = m2["home_projection"] if m2["home_team_id"] == 2 else m2["away_projection"]
    assert s2["complete"] is True and s2["projected_points"] == 15.0                  # 0 + 10 + 5, not unknown
    assert m2["projected_winner"] is not None
    assert standing(res["scenario"]["projected_standings"], 1)["record"]["unresolved"] == 1
    assert team(res, 1)["regular_season_projected_points"] is None
    assert team(res, 1)["projection_weeks_incomplete"] == [4] and team(res, 1)["regular_season_known_points"] is not None
    assert any("unresolved in this scenario" in w for w in res["warnings"])
    assert len(pairs[4]) == 2


def test_team_without_roster_snapshot_is_a_gap_not_zero():
    res = build_playoff_race(sr.race(n_teams=4, reg_periods=6, start_week=4, final_week=8, rosters_missing={3}))
    for m in res["matchups"]["regular"]:
        if m["status"] == "upcoming" and 3 in (m["home_team_id"], m["away_team_id"]):
            s = m["home_projection"] if m["home_team_id"] == 3 else m["away_projection"]
            assert s["projected_points"] is None and s["gap"] == "no roster snapshot for this team"
            assert m["projected_winner"] is None
    assert team(res, 3)["projection_gap"] == "no roster snapshot for this team"
    assert team(res, 3)["regular_season_projected_points"] is None


# ----------------------------------------------- byes, ties, in progress, unknowns --

def test_byes_unknown_opponents_in_progress_and_equal_projections():
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8, in_progress={3},
                strength={1: 25.0, 2: 25.0, 3: 20.0, 4: 30.0},
                overrides_matchups=[{"matchup_id": 90, "matchup_period": 5, "home_team_id": 1, "away_team_id": None},
                                    {"matchup_id": 91, "matchup_period": 2, "home_team_id": None, "away_team_id": 3,
                                     "winner": "AWAY", "away_points": 99.0}])
    res = build_playoff_race(d)
    assert matchup(res, 90)["status"] == matchup(res, 91)["status"] == "bye"         # one team listed: no result
    live = [m for m in res["matchups"]["regular"] if m["matchup_period"] == 3]
    assert live and all(m["status"] == "in_progress" and m["scenario_result"] is None for m in live)
    assert all(m["home_points"] is not None for m in live)                           # live points shown, not final
    assert {t["team_id"]: sum(rec(t)) for t in res["teams"]} == {1: 2, 2: 2, 3: 2, 4: 2}   # periods 1-2 only
    eq = next(m for m in res["matchups"]["regular"] if m["status"] == "upcoming"
              and {m["home_team_id"], m["away_team_id"]} == {1, 2})
    assert eq["projected_margin"] == 0 and eq["projected_winner"] is None and "equal projected totals" in eq["detail"]
    with pytest.raises(ValueError, match="bye"):
        build_playoff_race(d, overrides={90: "home"})
    # Overriding the in-progress game decides it; nothing else changes.
    g = live[0]
    base = build_playoff_race(d)
    over = build_playoff_race(d, overrides={g["matchup_id"]: "home"})
    h = g["home_team_id"]
    assert standing(over["scenario"]["projected_standings"], h)["record"]["wins"] == \
        standing(base["scenario"]["projected_standings"], h)["record"]["wins"] + 1


def test_current_week_under_way_is_never_projected_and_no_remaining_weeks():
    d = sr.race(n_teams=4, reg_periods=6, start_week=4, final_week=8)
    d.state.start_week = 5                                       # week 4 has kicked off
    res = build_playoff_race(d)
    assert {m["status"] for m in res["matchups"]["regular"] if m["matchup_period"] == 4} == {"in_progress"}
    d.state.start_week = None                                    # every week kicked off
    res = build_playoff_race(d)
    assert not [m for m in res["matchups"]["regular"] if m["status"] == "upcoming"]
    assert any("No remaining fantasy scoring periods" in w for w in res["warnings"])


# ------------------------------------------------ playoff spots & season boundaries --

def test_playoff_spots_boundary_known_playoff_matchups_and_rule_warnings():
    d = sr.race(n_teams=6, reg_periods=5, spots=4, start_week=4, final_week=8,
                overrides_matchups=[{"matchup_id": 80, "matchup_period": 6, "home_team_id": 1, "away_team_id": 4,
                                     "playoff_tier": "WINNERS_BRACKET"},
                                    {"matchup_id": 81, "matchup_period": 7, "home_team_id": 2, "away_team_id": 3}],
                divisions={1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2})
    d.rules_raw = {"scheduleSettings.playoffSeedingRule": "TOTAL_POINTS_SCORED"}
    res = build_playoff_race(d)
    assert {m["matchup_id"] for m in res["matchups"]["playoff_known"]} == {80, 81}   # tier or past the boundary
    assert all(m["matchup_period"] <= 5 for m in res["matchups"]["regular"])
    cut = res["scenario"]["cutline"]
    proj = res["scenario"]["projected_standings"]
    inside = [x for x in proj if x["status"] == "inside"]
    assert cut["playoff_spots"] == 4 and cut["last_inside"] == [x for x in inside if x["rank"] == inside[-1]["rank"]]
    assert all(x["status"] == "outside" for x in cut["first_outside"])
    assert sum(1 for x in proj if x["status"] == "inside") <= 4
    assert any("2 divisions" in w for w in res["warnings"])
    assert res["rules"]["espn_rules_reported"] == {"scheduleSettings.playoffSeedingRule": "TOTAL_POINTS_SCORED"}
    assert "none is applied" in res["rules"]["espn_rules_note"] and "clinched or eliminated" in res["rules"]["clinch"]
    assert "clinch" not in json.dumps(res["teams"]).lower() and "eliminat" not in json.dumps(res["teams"]).lower()
    with pytest.raises(ValueError, match="not a regular-season matchup"):
        build_playoff_race(d, overrides={80: "home"})
    d.league["playoff_teams"] = None
    res = build_playoff_race(d)
    assert res["scenario"]["cutline"] is None and all(x["status"] is None for x in res["scenario"]["projected_standings"])
    assert any("playoff spots is not reported" in w for w in res["warnings"])


def test_espn_seed_order_that_disagrees_with_record_is_flagged():
    d = tie_league()
    d.teams[0]["playoff_seed"], d.teams[1]["playoff_seed"] = 2, 1          # ESPN seeds 2-1 ahead of 3-0
    assert any("seeds do not follow overall record order" in w for w in build_playoff_race(d)["warnings"])


def test_matchup_without_a_period_is_left_out_and_reported():
    d = sr.race(n_teams=4, reg_periods=6, start_week=4, final_week=8,
                overrides_matchups=[{"matchup_id": 77, "matchup_period": None, "home_team_id": 1, "away_team_id": 2,
                                     "home_points": 120.0, "away_points": 100.0, "winner": "HOME"}])
    res = build_playoff_race(d)                                                   # no TypeError sorting None
    assert 77 not in {m["matchup_id"] for m in res["matchups"]["regular"] + res["matchups"]["playoff_known"]}
    assert team(res, 1)["all_play"]["periods"] == [1, 2, 3]
    assert any("without a matchup period (ids 77)" in w for w in res["warnings"])
    assert res["record_check"] == []                       # its final result still counts toward derived records
    # With ESPN's record missing, the fallback derives it including the period-less final.
    d.teams[0]["wins"] = None
    res = build_playoff_race(d)
    expect = (sum(1 for m in d.matchups if m["winner"] == "HOME" and m["home_team_id"] == 1)
              + sum(1 for m in d.matchups if m["winner"] == "AWAY" and m["away_team_id"] == 1))
    assert team(res, 1)["record"]["wins"] == expect and any("derived from final matchups" in n for n in res["record_check"])


def test_schedule_coverage_reports_missing_regular_season_matchups():
    d = sr.race(n_teams=4, reg_periods=6, start_week=4, final_week=8)
    d.matchups = [m for m in d.matchups if m["matchup_period"] != 6]          # ESPN schedule incomplete
    res = build_playoff_race(d)
    cov = res["schedule_coverage"]
    assert cov["complete"] is False and all(x["periods"] == [6] for x in cov["missing"]) and len(cov["missing"]) == 4
    assert any("schedule is missing regular-season matchups" in w for w in res["warnings"])


def test_multi_week_matchup_periods_sum_weekly_lineups_and_partial_periods_stay_open():
    sched = {"matchupPeriodLength": 1, "matchupPeriods": {"1": [1], "2": [2], "3": [3], "4": [4], "5": [5, 6], "6": [7, 8]}}
    assert period_weeks(sched, 5) == [5, 6] and period_weeks({"matchupPeriodLength": 2}, 3) is None
    assert period_weeks({}, 3) == [3]
    d = sr.race(n_teams=4, reg_periods=5, spots=2, start_week=5, final_week=8, sched=sched,
                strength={1: 30.0, 2: 20.0, 3: 25.0, 4: 22.0}, projection_patch={101: {6: 40.0}})
    assert d.state.last_regular == 6                                             # boundary from the period map
    res = build_playoff_race(d)
    two = next(m for m in res["matchups"]["regular"] if m["matchup_period"] == 5 and 1 in (m["home_team_id"], m["away_team_id"]))
    s = two["home_projection"] if two["home_team_id"] == 1 else two["away_projection"]
    assert two["weeks"] == [5, 6] and [w["week"] for w in s["weekly"]] == [5, 6]
    assert s["projected_points"] == (30.0 + 15) + (40.0 + 15)                     # each week optimized separately
    assert len(two["weekly_margins"]) == 2
    assert res["league"]["matchup_periods"]["5"] == [5, 6]
    d.state.start_week = 6                                                       # week 5 of the period kicked off
    res = build_playoff_race(d)
    assert {m["status"] for m in res["matchups"]["regular"] if m["matchup_period"] == 5} == {"in_progress"}
    d2 = sr.race(n_teams=4, reg_periods=5, start_week=5, final_week=8, sched={"matchupPeriodLength": 2})
    res = build_playoff_race(d2)
    assert {m["status"] for m in res["matchups"]["regular"] if m["status"] != "final"} == {"unmapped"}
    assert res["schedule_coverage"]["unmapped_periods"]


# ---------------------------------------------------------------- scenario explorer --

def test_overrides_flip_tie_clear_and_are_reversible():
    d = sr.race(n_teams=6, reg_periods=8, spots=4, start_week=5, final_week=10)
    base = build_playoff_race(d)
    mine = base["focus_team_id"]
    assert mine == 1 and team(base, 1)["is_mine"]
    nxt = next(m for m in base["matchups"]["regular"] if m["status"] == "upcoming" and mine in (m["home_team_id"], m["away_team_id"]))
    win = "home" if nxt["home_team_id"] == mine else "away"
    lose = "away" if win == "home" else "home"
    before = standing(base["scenario"]["projected_standings"], mine)["record"]
    # "What if I win next week?"
    res = build_playoff_race(d, overrides={nxt["matchup_id"]: win})
    after = standing(res["scenario"]["projected_standings"], mine)["record"]
    delta = 0 if nxt["projected_winner"] == win else 1
    assert after["wins"] == before["wins"] + delta and matchup(res, nxt["matchup_id"])["override"] == win
    assert res["scenario"]["overrides"] == [{"matchup_id": nxt["matchup_id"], "outcome": win}]
    # A tie, and forcing a game unresolved.
    res = build_playoff_race(d, overrides={nxt["matchup_id"]: "tie"})
    assert standing(res["scenario"]["projected_standings"], mine)["record"]["ties"] == before["ties"] + 1
    res = build_playoff_race(d, overrides={nxt["matchup_id"]: "none"})
    assert standing(res["scenario"]["projected_standings"], mine)["record"]["unresolved"] == 1
    # Two flips at once, then removing them returns exactly the baseline (deterministic and reversible).
    others = [m for m in base["matchups"]["regular"] if m["status"] == "upcoming"
              and mine not in (m["home_team_id"], m["away_team_id"])][:2]
    flips = {m["matchup_id"]: ("away" if m["projected_winner"] == "home" else "home") for m in others}
    res = build_playoff_race(d, overrides=flips)
    for m in others:
        loser = m["home_team_id"] if flips[m["matchup_id"]] == "away" else m["away_team_id"]
        assert standing(res["scenario"]["projected_standings"], loser)["record"]["losses"] == \
            standing(base["scenario"]["projected_standings"], loser)["record"]["losses"] + 1
    assert build_playoff_race(d, overrides={}) == base
    # Baseline "actual": no projected winner applied, only overrides.
    act = build_playoff_race(d, base="actual", overrides={nxt["matchup_id"]: lose})
    assert act["scenario"]["decided_open_games"] == 1
    assert standing(act["scenario"]["projected_standings"], mine)["record"]["losses"] == \
        standing(act["scenario"]["current_standings"], mine)["record"]["losses"] + 1
    # Validation.
    with pytest.raises(ValueError, match="final"):
        build_playoff_race(d, overrides={1: "home"})
    with pytest.raises(ValueError):
        build_playoff_race(d, base="monte-carlo")
    with pytest.raises(NotFound):
        build_playoff_race(d, team_id=99)
    assert parse_overrides(["12:HOME", "15:away", "12:none"]) == {12: "none", 15: "away"}
    for bad in ("12", "x:home", "12:win", "12:"):
        with pytest.raises(ValueError):
            parse_overrides([bad])


def test_leverage_and_pathways_are_consistent_with_explicit_scenarios():
    d = sr.race(n_teams=6, reg_periods=8, spots=3, start_week=5, final_week=10,
                strength={1: 24.0, 2: 31.0, 3: 22.0, 4: 27.5, 5: 26.0, 6: 21.0})
    res = build_playoff_race(d, team_id=3)
    f = res["scenario"]["focus"]
    games = [m for m in res["matchups"]["regular"] if m["status"] == "upcoming" and 3 in (m["home_team_id"], m["away_team_id"])]
    side = {m["matchup_id"]: ("home" if m["home_team_id"] == 3 else "away") for m in games}
    opp = {"home": "away", "away": "home"}
    # Every leverage row matches the scenario it describes.
    for g in res["scenario"]["leverage"]["3"]:
        for key, outcome in (("if_win", side[g["matchup_id"]]), ("if_loss", opp[side[g["matchup_id"]]])):
            row = standing(build_playoff_race(d, team_id=3, overrides={g["matchup_id"]: outcome})
                           ["scenario"]["projected_standings"], 3)
            assert (row["rank"], row["status"]) == (g[key]["rank"], g[key]["status"])
        assert g["high_leverage"] == (g["if_win"]["status"] != g["if_loss"]["status"])
    # Win out / lose out are what they say.
    allw = build_playoff_race(d, overrides={mid: s for mid, s in side.items()})
    assert standing(allw["scenario"]["projected_standings"], 3)["record"]["wins"] == f["win_out"]["record"]["wins"]
    # Fewest wins to get inside: the named games suffice and no smaller set does.
    fw = f["fewest_wins_to_get_inside"]
    if fw is not None:
        k = fw["wins_needed"]
        chosen = {mid: (side[mid] if mid in fw["games"] else opp[side[mid]]) for mid in side}
        assert standing(build_playoff_race(d, overrides=chosen)["scenario"]["projected_standings"], 3)["status"] == "inside"
        for combo in itertools.combinations(side, max(0, k - 1)):
            trial = {mid: (side[mid] if mid in combo else opp[side[mid]]) for mid in side}
            st = standing(build_playoff_race(d, overrides=trial)["scenario"]["projected_standings"], 3)["status"]
            assert k == 0 or st != "inside"
    # Each helpful result elsewhere really improves team 3's place.
    now = standing(res["scenario"]["projected_standings"], 3)
    for h in f["helpful_other_results"]:
        row = standing(build_playoff_race(d, team_id=3, overrides={h["matchup_id"]: h["result"]})
                       ["scenario"]["projected_standings"], 3)
        assert (row["rank"], row["status"]) == (h["rank"], h["status"])
        assert row["rank"] < now["rank"] or (row["status"] == "inside" and now["status"] != "inside")
    # A league where results elsewhere do help (team 1 of the default synthetic league).
    d1 = sr.race()
    r1 = build_playoff_race(d1)
    helpful = r1["scenario"]["focus"]["helpful_other_results"]
    assert helpful
    now1 = standing(r1["scenario"]["projected_standings"], 1)
    for h in helpful:
        m = matchup(r1, h["matchup_id"])
        assert 1 not in (m["home_team_id"], m["away_team_id"]) and m["scenario_result"] != h["result"]
        row = standing(build_playoff_race(d1, overrides={h["matchup_id"]: h["result"]})["scenario"]["projected_standings"], 1)
        assert (row["rank"], row["status"]) == (h["rank"], h["status"]) and row["rank"] < now1["rank"]
    bubble = res["scenario"]["cutline"]["bubble"]
    assert all(any(g["high_leverage"] for g in res["scenario"]["leverage"][str(t)]) or
               standing(res["scenario"]["projected_standings"], t)["status"] == "cutline_tie" for t in bubble)


def test_results_are_deterministic_and_inputs_untouched():
    d = sr.race(n_teams=8, reg_periods=10, spots=4, start_week=6, final_week=13, in_progress={5})
    snapshot = copy.deepcopy(d)
    a = build_playoff_race(d, overrides={30: "home"})
    b = build_playoff_race(d, overrides={30: "home"})
    assert a == b and json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert d.matchups == snapshot.matchups and d.teams == snapshot.teams
    assert d.state.projections == snapshot.state.projections


# ------------------------------------------------------------- what-if (issue #192) --

def _flip(m):
    return "away" if m.get("projected_winner") == "home" else "home"


def test_whatif_is_absent_when_no_result_is_set_and_states_its_inputs_when_one_is():
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8)
    base = build_playoff_race(d)
    assert "whatif" not in base and build_playoff_race(d, overrides={}) == base          # scenarios off: unchanged
    assert "whatif" not in build_playoff_race(d, base="actual")
    nxt = next(m for m in base["matchups"]["regular"] if m["status"] == "upcoming" and m.get("projected_winner"))
    res = build_playoff_race(d, overrides={nxt["matchup_id"]: _flip(nxt)})
    w = res["whatif"]
    assert w["label"] == "hypothetical" and w["labels"]["current_standings"] == "actual"
    assert w["labels"]["projected_standings"] == "hypothetical"
    # A deterministic scenario: one scenario, no sampling, no seed, no probability anywhere.
    assert w["sampling"] == {"method": "deterministic", "scenarios": 1, "random_samples": 0, "seed": None,
                             "probabilities": None}
    assert w["inputs"]["base"] == "projected" and w["inputs"]["first_actionable_week"] == 4
    assert w["inputs"]["set_results"] == [{
        "matchup_id": nxt["matchup_id"], "matchup_period": nxt["matchup_period"], "weeks": nxt["weeks"],
        "home_team_id": nxt["home_team_id"], "away_team_id": nxt["away_team_id"], "status": "upcoming",
        "outcome": _flip(nxt), "baseline_result": nxt["projected_winner"], "changes_baseline": True}]
    assert any("hypothetical" in a for a in w["assumptions"]) and any("missing is not zero" in a for a in w["assumptions"])
    # The baseline is the same answer without the user's results; the changes compare the two exactly.
    assert w["baseline_standings"] == base["scenario"]["projected_standings"]
    for c in w["changes"]:
        b = standing(base["scenario"]["projected_standings"], c["team_id"])
        x = standing(res["scenario"]["projected_standings"], c["team_id"])
        assert (c["baseline"]["rank"], c["baseline"]["status"]) == (b["rank"], b["status"])
        assert (c["whatif"]["rank"], c["whatif"]["status"], c["whatif"]["record"]) == (x["rank"], x["status"], x["record"])
        assert c["rank_change"] == b["rank"] - x["rank"] and c["status_changed"] == (b["status"] != x["status"])
    winner = nxt["home_team_id"] if _flip(nxt) == "home" else nxt["away_team_id"]
    wc = next(c for c in w["changes"] if c["team_id"] == winner)
    assert wc["whatif"]["record"]["wins"] == wc["baseline"]["record"]["wins"] + 1
    # Setting the projected winner again changes nothing against the baseline, and says so.
    same = build_playoff_race(d, overrides={nxt["matchup_id"]: nxt["projected_winner"]})["whatif"]
    assert same["inputs"]["set_results"][0]["changes_baseline"] is False
    assert not any(c["status_changed"] or c["rank_change"] for c in same["changes"])


def test_whatif_completeness_never_counts_missing_projections_as_zero():
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=4, final_week=8, projection_patch={101: {5: None}})
    base = build_playoff_race(d)
    gap = next(m for m in base["matchups"]["regular"] if m["matchup_period"] == 5 and 1 in (m["home_team_id"], m["away_team_id"]))
    assert gap["projected_winner"] is None and gap["scenario_result"] is None
    other = next(m for m in base["matchups"]["regular"] if m["status"] == "upcoming" and m.get("projected_winner"))
    w = build_playoff_race(d, overrides={other["matchup_id"]: _flip(other)})["whatif"]
    assert w["completeness"] == {"status": "partial", "unresolved_games": 1, "by_reason": {"projection_incomplete": 1}}
    team1 = next(c for c in w["changes"] if c["team_id"] == 1)
    assert team1["whatif"]["record"]["unresolved"] == 1                  # left out, not scored as a loss
    # Setting the gap game's result resolves it; forcing another game unresolved is its own reason.
    w2 = build_playoff_race(d, overrides={gap["matchup_id"]: "home", other["matchup_id"]: "none"})["whatif"]
    assert w2["completeness"]["by_reason"] == {"set_unresolved": 1}
    assert next(x for x in w2["inputs"]["set_results"] if x["matchup_id"] == gap["matchup_id"])["baseline_result"] is None
    # The actual-results baseline leaves every other game unresolved, and says why.
    w3 = build_playoff_race(d, base="actual", overrides={other["matchup_id"]: "home"})["whatif"]
    assert set(w3["completeness"]["by_reason"]) == {"actual_baseline"}
    assert w3["completeness"]["unresolved_games"] == 5                 # periods 4-6: six games, one set


def _all_outcomes(d, games, fixed):
    """Every combination of home/away/tie for `games`, with `fixed` held: (results, projected standings)."""
    for combo in itertools.product(("home", "away", "tie"), repeat=len(games)):
        ov = {**{m["matchup_id"]: o for m, o in zip(games, combo)}, **fixed}
        yield build_playoff_race(d, base="actual", overrides=ov)["scenario"]["projected_standings"]


def test_record_bounds_are_exact_under_record_order_and_never_a_claim():
    # Periods 1-4 final, 5-6 open. Team 3 wins its period-1 game against team 2, so after period 4 the records
    # (reproducible from the stored finals) are 4-0, 2-2, 2-2, 0-4. Both period-5 games are set to home wins
    # (1 beats 3, 2 beats 4): one team is then clear, one far behind and two in between.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8, scores={(1, 3): 200.0})
    base = build_playoff_race(d, base="actual")
    assert base["record_check"] == []
    open_games = [m for m in base["matchups"]["regular"] if m["status"] == "upcoming"]
    assert len(open_games) == 4
    fixed = {m["matchup_id"]: "home" for m in open_games if m["matchup_period"] == 5}
    w = build_playoff_race(d, base="actual", overrides=fixed)["whatif"]
    bounds = w["conclusions"]["record_bounds"]
    assert bounds["fixed_results"] == 2 and bounds["open_games"] == 2 and bounds["abstain"] == []
    worlds = list(_all_outcomes(d, [m for m in open_games if m["matchup_id"] not in fixed], fixed))
    assert len(worlds) == 9
    seen = set()
    for row in bounds["teams"]:
        statuses = {standing(rows, row["team_id"])["status"] for rows in worlds}
        records = {rec(standing(rows, row["team_id"])) for rows in worlds}
        best, worst = row["best"], row["worst"]
        assert (best["wins"], best["losses"], best["ties"]) in records and (worst["wins"], worst["losses"], worst["ties"]) in records
        if row["record_order"] == "above_line":
            assert statuses == {"inside"}, row                 # sound: inside in every outcome
        if row["record_order"] == "below_line":
            assert statuses == {"outside"}, row                # sound: outside in every outcome
        assert row["claim"] is None
        seen.add(row["record_order"])
    assert seen == {"above_line", "below_line", "open"}
    # The gate: no claims, and the unmapped tiebreak and the assumed order are named as the reasons.
    c = w["conclusions"]
    assert c["status"] == "blocked" and c["claims"] == []
    assert [b["rule"] for b in c["blocked_by"]] == ["tiebreak", "standings_order"]
    assert "clinch" not in json.dumps(c["record_bounds"]["teams"]) and "eliminat" not in json.dumps(c["record_bounds"]["teams"])


def test_unmapped_tiebreak_and_other_unknown_rules_block_conclusions(monkeypatch):
    from app.analytics import playoff_race
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=6, final_week=8)
    d.rules_raw = {"scheduleSettings.playoffSeedingRule": "TOTAL_POINTS_SCORED"}
    game = next(m for m in build_playoff_race(d)["matchups"]["regular"] if m["status"] == "upcoming")
    c = build_playoff_race(d, overrides={game["matchup_id"]: "tie"})["whatif"]["conclusions"]
    by = {b["rule"]: b for b in c["blocked_by"]}
    assert by["tiebreak"]["state"] == "unmapped"
    assert by["tiebreak"]["reported"] == {"scheduleSettings.playoffSeedingRule": "TOTAL_POINTS_SCORED"}
    assert c["status"] == "blocked" and c["claims"] == [] and c["record_bounds"]["abstain"] == []
    assert {t["record_order"] for t in c["record_bounds"]["teams"]} >= {"above_line"}      # decided by record alone ...
    assert all(t["claim"] is None for t in c["record_bounds"]["teams"])                     # ... and still not claimed
    # Even a verified tiebreak mapping leaves the gate closed while the order itself is assumed.
    monkeypatch.setattr(playoff_race, "MAPPED_TIEBREAK_RULES", {"scheduleSettings.playoffSeedingRule": frozenset({"TOTAL_POINTS_SCORED"})})
    c2 = build_playoff_race(d, overrides={game["matchup_id"]: "tie"})["whatif"]["conclusions"]
    assert "tiebreak" not in {b["rule"] for b in c2["blocked_by"]} and c2["status"] == "blocked" and c2["claims"] == []
    # Unknown playoff spots: blocked, and no team gets a record order.
    d.league["playoff_teams"] = None
    c3 = build_playoff_race(d, overrides={game["matchup_id"]: "tie"})["whatif"]["conclusions"]
    assert "playoff_spots" in {b["rule"] for b in c3["blocked_by"]}
    assert {t["record_order"] for t in c3["record_bounds"]["teams"]} == {None}


def _no_record_order(c):
    return all(t["record_order"] is None and t["teams_able_to_match_worst"] is None
               and t["teams_certain_ahead_of_best"] is None for t in c["record_bounds"]["teams"])


def test_record_order_is_withheld_when_divisions_or_seeds_can_override_it():
    # Several divisions: a division auto-bid could put a worse record in, so "above the line" would read as a clinch.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=6, final_week=8, divisions={1: 1, 2: 1, 3: 2, 4: 2})
    game = next(m for m in build_playoff_race(d)["matchups"]["regular"] if m["status"] == "upcoming")
    c = build_playoff_race(d, overrides={game["matchup_id"]: "tie"})["whatif"]["conclusions"]
    assert {b["rule"]: b["state"] for b in c["blocked_by"]}["divisions"] == "not_modeled"
    assert any("2 divisions" in a for a in c["record_bounds"]["abstain"]) and _no_record_order(c)
    # The provider's seeds disagree with record order (team 4, last on record, holds seed 1).
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=6, final_week=8)
    for t in d.teams:
        t["playoff_seed"] = 5 - t["team_id"]
    c = build_playoff_race(d, overrides={game["matchup_id"]: "tie"})["whatif"]["conclusions"]
    assert "seeding" in {b["rule"] for b in c["blocked_by"]}
    assert any("seeds do not follow record order" in a for a in c["record_bounds"]["abstain"]) and _no_record_order(c)


def test_record_bounds_abstain_when_records_do_not_match_the_stored_finals_or_a_median_game_counts():
    # Review repro (#278): records carry results the stored matchups do not explain (e.g. Sleeper median games).
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8,
                records={1: [7, 1, 0], 2: [6, 2, 0], 3: [3, 5, 0], 4: [0, 8, 0]})
    res0 = build_playoff_race(d)
    assert len(res0["record_check"]) == 4
    game = next(m for m in res0["matchups"]["regular"] if m["status"] == "upcoming")
    c = build_playoff_race(d, overrides={game["matchup_id"]: "home"})["whatif"]["conclusions"]
    assert any("do not match the stored final matchups" in a for a in c["record_bounds"]["abstain"])
    assert _no_record_order(c) and all(t["best"] is not None for t in c["record_bounds"]["teams"])
    # A median-game league whose records do match: still withheld, with its own reason.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=6, final_week=8)
    d.median_game = True
    res1 = build_playoff_race(d)
    assert res1["record_check"] == []
    game = next(m for m in res1["matchups"]["regular"] if m["status"] == "upcoming")
    c = build_playoff_race(d, overrides={game["matchup_id"]: "home"})["whatif"]["conclusions"]
    assert c["record_bounds"]["abstain"] == ["The league adds a result against the league median each week, which "
                                             "these bounds do not model."]
    assert _no_record_order(c)


def test_a_median_league_says_its_records_add_head_to_head_games_only():
    from app.analytics.playoff_race import MEDIAN_NOTE
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8)
    game = next(m for m in build_playoff_race(d)["matchups"]["regular"] if m["status"] == "upcoming")
    espn = build_playoff_race(d, overrides={game["matchup_id"]: "home"})
    assert MEDIAN_NOTE not in espn["warnings"] and MEDIAN_NOTE not in espn["whatif"]["assumptions"]
    d.median_game = True
    assert MEDIAN_NOTE in build_playoff_race(d)["warnings"]                       # scenarios off too
    med = build_playoff_race(d, overrides={game["matchup_id"]: "home"})
    assert MEDIAN_NOTE in med["warnings"] and MEDIAN_NOTE in med["whatif"]["assumptions"]


def test_undecided_matchup_without_a_period_makes_the_whatif_partial():
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=6, final_week=8,
                overrides_matchups=[{"matchup_id": 77, "matchup_period": None, "home_team_id": 1, "away_team_id": 2}])
    res = build_playoff_race(d, base="actual")
    open_ids = [m["matchup_id"] for m in res["matchups"]["regular"] if m["status"] == "upcoming"]
    w = build_playoff_race(d, base="actual", overrides={mid: "home" for mid in open_ids})["whatif"]
    assert w["completeness"] == {"status": "partial", "unresolved_games": 1, "by_reason": {"no_period": 1}}
    assert any("no matchup period" in a for a in w["conclusions"]["record_bounds"]["abstain"])


def test_record_bounds_abstain_when_the_schedule_may_hide_games():
    pairs = sr.round_robin([1, 2, 3, 4], 6)
    short = {p: g for p, g in pairs.items() if p != 6}                  # period 6 missing from the stored schedule
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8, schedule=short)
    game = next(m for m in build_playoff_race(d)["matchups"]["regular"] if m["status"] == "upcoming")
    b = build_playoff_race(d, overrides={game["matchup_id"]: "home"})["whatif"]["conclusions"]["record_bounds"]
    assert b["abstain"] and "missing regular-season matchups" in b["abstain"][0]
    assert all(t["record_order"] is None and t["teams_able_to_match_worst"] is None for t in b["teams"])
    assert all(t["best"] is not None for t in b["teams"])               # the records themselves are still shown


# ------------------------------------------------------- rooting guide (issue #217) --

def _viewer_bounds(d, tid, overrides):
    """The viewer's record-bounds row from the what-if with `overrides` set (the deterministic evaluator)."""
    b = build_playoff_race(d, team_id=tid, overrides=overrides)["whatif"]["conclusions"]["record_bounds"]
    row = standing(b["teams"], tid)
    return {k: row[k] for k in ("teams_able_to_match_worst", "teams_certain_ahead_of_best", "record_order")}


_ORDER = {"inside": 2, "cutline_tie": 1, "outside": 0, None: -1}


def _places(rows, tid):
    """The places a team's rank group spans, straight from the standings (no tiebreak: rank .. rank + group - 1)."""
    me = standing(rows, tid)
    if me["rank"] is None:
        return None
    return [me["rank"], me["rank"] + sum(1 for x in rows if x["rank"] == me["rank"]) - 1]


def _no_worse(a, b):
    """a's scenario place is no worse than b's: a better status, or the same status and both ends of the span."""
    if _ORDER[a["status"]] != _ORDER[b["status"]]:
        return _ORDER[a["status"]] > _ORDER[b["status"]]
    return a["places"] is None or b["places"] is None or (a["places"][0] <= b["places"][0] and a["places"][1] <= b["places"][1])


def test_every_rooting_outcome_reproduces_the_deterministic_simulation():
    for d, tid, ov in ((sr.race(), 1, {}),
                       (sr.race(n_teams=6, reg_periods=8, spots=3, start_week=5, final_week=10,
                                strength={1: 24.0, 2: 31.0, 3: 22.0, 4: 27.5, 5: 26.0, 6: 21.0}), 3, {}),
                       (sr.race(n_teams=8, reg_periods=10, spots=4, start_week=6, final_week=13), 4, {40: "home"})):
        res = build_playoff_race(d, team_id=tid, overrides=ov)
        g = res["scenario"]["rooting"]
        assert g["label"] == "hypothetical" and g["probabilities"] is None and g["conclusions"]["claims"] == []
        rows = g["games"] + g["no_effect"]
        assert rows and g["status"] == "ok"
        for x in rows:
            assert tid not in (x["home_team_id"], x["away_team_id"]) and x["matchup_period"] == g["period"]
            for key, outcome in (("if_home", "home"), ("if_away", "away")):
                trial = {**ov, x["matchup_id"]: outcome}
                rows_ = build_playoff_race(d, team_id=tid, overrides=trial)["scenario"]["projected_standings"]
                row = standing(rows_, tid)
                assert x[key]["scenario"] == {"rank": row["rank"], "tied": row["tied"], "status": row["status"],
                                              "record": row["record"], "places": _places(rows_, tid)}
                assert x[key]["range"] == _viewer_bounds(d, tid, trial)
            if x["prefer"]:
                win, lose = x["if_" + x["prefer"]], x["if_" + ("away" if x["prefer"] == "home" else "home")]
                assert _no_worse(win["scenario"], lose["scenario"])
                assert x["basis"] and not x["conflict"]
            else:
                assert x["conflict"] or (x["if_home"] == x["if_away"] or not x["basis"])
        assert all(x["prefer"] is None and not x["conflict"] for x in g["no_effect"])
        # Ranked: preferences first, then status swings, then record-order swings, then rank swings.
        keys = [(x["prefer"] is None, not x["swing"]["status_changes"], not x["swing"]["range_changes"])
                for x in g["games"]]
        assert keys == sorted(keys)


def test_rooting_covers_only_the_next_period_and_leaves_inputs_untouched():
    d = sr.race()
    snapshot = copy.deepcopy(d)
    res = build_playoff_race(d)
    g = res["scenario"]["rooting"]
    open_games = [m for m in res["matchups"]["regular"] if m["status"] == "upcoming"]
    first = min(m["matchup_period"] for m in open_games)
    assert g["period"] == first and g["weeks"] == [first]
    own = next(m for m in open_games if m["matchup_period"] == first and 1 in (m["home_team_id"], m["away_team_id"]))
    assert g["own_game"] == own["matchup_id"]
    listed = {x["matchup_id"] for x in g["games"] + g["no_effect"]}
    assert listed == {m["matchup_id"] for m in open_games if m["matchup_period"] == first} - {own["matchup_id"]}
    assert g["later_open_games"] == sum(1 for m in open_games if m["matchup_period"] != first)
    assert d.matchups == snapshot.matchups and d.teams == snapshot.teams and build_playoff_race(d) == res
    # A set result elsewhere is held while each game is evaluated; the game's own set result is not.
    x = (g["games"] + g["no_effect"])[0]
    res2 = build_playoff_race(d, overrides={x["matchup_id"]: "home"})
    y = next(z for z in res2["scenario"]["rooting"]["games"] + res2["scenario"]["rooting"]["no_effect"]
             if z["matchup_id"] == x["matchup_id"])
    assert y["scenario_result"] == "home" and (y["if_home"], y["if_away"]) == (x["if_home"], x["if_away"])


def test_rooting_with_ties_at_the_cutline_three_way_ties_and_a_tied_game():
    # Two teams tied at the cutline (tie_league with 3 spots); a real tied final in period 3 counts half.
    res = build_playoff_race(tie_league(spots=3), team_id=3, base="actual")
    g = res["scenario"]["rooting"]
    assert standing(res["scenario"]["projected_standings"], 3)["status"] == "cutline_tie"
    assert g["status"] == "ok" and g["period"] == 4
    # Team 3's rival for the last spot is team 4: rooting for team 4's opponent is preferred, and it reproduces.
    rival = next(x for x in g["games"] + g["no_effect"] if 4 in (x["home_team_id"], x["away_team_id"]))
    against_4 = "away" if rival["home_team_id"] == 4 else "home"
    assert rival["prefer"] == against_4
    assert rival["if_" + against_4]["scenario"]["status"] == "inside"
    # Every team level at 0-0-1 (all period-1 games tied, a four-way tie across the cutline). Whoever wins the other
    # game, team 1 stays tied with the other idle team for the last spot: no preference, listed as no effect.
    every = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=2, final_week=8,
                    scores={(1, t): 100.0 for t in range(1, 5)})
    assert all(x["status"] == "cutline_tie" for x in build_playoff_race(every, base="actual")["scenario"]["current_standings"])
    g = build_playoff_race(every, base="actual")["scenario"]["rooting"]
    assert g["games"] == [] and len(g["no_effect"]) == 1
    x = g["no_effect"][0]
    assert x["if_home"]["scenario"]["status"] == x["if_away"]["scenario"]["status"] == "cutline_tie"
    assert x["prefer"] is None and x["basis"] == [] and not x["swing"]["status_changes"]


def test_a_shared_rank_is_compared_as_the_span_of_places_it_covers():
    from app.analytics.playoff_race import _root_better
    place = lambda status, places: {"status": status, "places": places}                       # noqa: E731
    # Reviewer's repro: #3 in a five-way tie (3rd to 7th) vs #4 in a three-way tie (4th to 6th), both at the line.
    assert not _root_better(place("cutline_tie", [3, 7]), place("cutline_tie", [4, 6]))
    assert not _root_better(place("cutline_tie", [4, 6]), place("cutline_tie", [3, 7]))
    # #2 shared by three (2nd to 4th) vs #3 alone, both inside: neither dominates.
    assert not _root_better(place("inside", [2, 4]), place("inside", [3, 3]))
    assert _root_better(place("inside", [2, 3]), place("inside", [3, 3]))                    # no worse, better top
    assert _root_better(place("inside", [4, 4]), place("cutline_tie", [2, 5]))              # status first
    assert not _root_better(place(None, None), place(None, None))
    # On real leagues: whenever a side is named on the scenario with the same status both ways, its span dominates.
    import random
    rnd = random.Random(11)
    checked = 0
    for _ in range(40):
        n, reg = rnd.choice([8, 10]), rnd.choice([8, 10])
        scores = {(p, t): float(rnd.choice([90, 100, 110])) for p in range(1, reg + 1) for t in range(1, n + 1)}
        d = sr.race(n_teams=n, reg_periods=reg, spots=rnd.choice([3, 4, 5]), start_week=rnd.randint(3, reg),
                    final_week=reg + 3, scores=scores, strength={t: rnd.uniform(15, 35) for t in range(1, n + 1)})
        g = build_playoff_race(d, team_id=rnd.randint(1, n), base=rnd.choice(["projected", "actual"]))["scenario"]["rooting"]
        for x in g["games"]:
            if x["prefer"] and "scenario" in x["basis"]:
                win, lose = x["if_" + x["prefer"]]["scenario"], x["if_" + ("away" if x["prefer"] == "home" else "home")]["scenario"]
                assert _no_worse(win, lose) and win != lose
                checked += 1
    assert checked > 0


def test_rooting_abstains_from_the_possible_range_and_never_claims_when_rules_are_unknown():
    # Divisions: the range is withheld with its reason, the scenario comparison still shows, and nothing is claimed.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8, divisions={1: 1, 2: 1, 3: 2, 4: 2})
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert any("2 divisions" in a for a in g["range_abstain"])
    assert g["conclusions"] == {"status": "blocked", "claims": []}
    for x in g["games"] + g["no_effect"]:
        assert x["if_home"]["range"] is None and x["if_away"]["range"] is None and "possible_range" not in x["basis"]
    # Unknown playoff spots: no status anywhere, so preferences come from rank alone.
    g = build_playoff_race(sr.race(spots=None))["scenario"]["rooting"]
    for x in g["games"] + g["no_effect"]:
        assert x["if_home"]["scenario"]["status"] is None and x["if_home"]["range"]["record_order"] is None
    # The provider's records do not match the stored finals: range withheld.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8,
                records={1: [7, 1, 0], 2: [6, 2, 0], 3: [3, 5, 0], 4: [0, 8, 0]})
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert g["range_abstain"] and all(x["if_home"]["range"] is None for x in g["games"] + g["no_effect"])


def test_rooting_with_a_bye_an_in_progress_week_no_other_games_and_none_remaining():
    # The viewer has a bye in the next period (5 teams, one sits out): every game that period is someone else's.
    pairs = {p: g for p, g in sr.round_robin([1, 2, 3, 4], 6).items()}
    pairs[5] = [(2, 3)]                                               # team 1 and team 4 idle in period 5
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=5, final_week=8, schedule=pairs)
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert g["period"] == 5 and g["own_game"] is None
    assert {x["matchup_id"] for x in g["games"] + g["no_effect"]} == {
        m["matchup_id"] for m in d.matchups if m["matchup_period"] == 5}
    assert any("missing regular-season matchups" in a for a in g["range_abstain"])   # the stored schedule is short
    # The current period is under way: its unresolved games are this week's, still never projected.
    d = sr.race(n_teams=8, reg_periods=10, spots=4, start_week=6, final_week=13, in_progress={5})
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert g["period"] == 5 and all(x["status"] == "in_progress" and x["scenario_result"] is None
                                    for x in g["games"] + g["no_effect"])
    # Two teams: the only open game each period is the viewer's own.
    d = sr.race(n_teams=2, reg_periods=4, spots=1, start_week=3, final_week=6)
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert g["status"] == "no_other_games" and g["games"] == g["no_effect"] == [] and g["own_game"] is not None
    # Every regular-season game is final: nothing left to root for.
    d = sr.race(n_teams=4, reg_periods=6, spots=2, start_week=None, final_week=8)
    g = build_playoff_race(d)["scenario"]["rooting"]
    assert g["status"] == "no_games_remaining" and g["period"] is None and g["games"] == []
    # No spotlight team: no guide.
    d = sr.race(mine=99)
    assert build_playoff_race(d)["scenario"]["rooting"] is None


# ------------------------------------------------------- database, API, isolation --

def _db_league(tmp_path):
    db_path = tmp_path / "race.duckdb"
    con = synced(db_path)
    # Round robin for the fixture's 4 teams over 14 regular-season periods; period 1 is the fixture's final.
    pairs = sr.round_robin([1, 2, 3, 4], 14)
    con.execute("DELETE FROM matchups")
    mid = 0
    for p in range(1, 15):
        for h, a in pairs[p]:
            mid += 1
            final = p <= 2
            con.execute("INSERT INTO matchups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NONE')",
                        [1, SEASON, mid, p, h, a, 110.0 if final else None, 90.0 if final else None,
                         "HOME" if final else "UNDECIDED"])
    con.execute("UPDATE fantasy_teams SET wins = 0, losses = 0, ties = 0")
    for p in (1, 2):
        for h, a in pairs[p]:
            con.execute("UPDATE fantasy_teams SET wins = wins + 1 WHERE team_id = ?", [h])
            con.execute("UPDATE fantasy_teams SET losses = losses + 1 WHERE team_id = ?", [a])
    return db_path, con


def test_load_race_data_from_the_database(tmp_path):
    _, con = _db_league(tmp_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    con.execute("UPDATE matchups SET league_id = ?", [lid])
    data = load_race_data(con)
    assert len(data.teams) == 4 and len(data.matchups) == 28 and data.league["playoff_teams"] == 2
    res = build_playoff_race(data)
    assert res["focus_team_id"] == 2 and team(res, 2)["is_mine"]
    assert res["record_check"] == []
    assert {m["status"] for m in res["matchups"]["regular"] if m["matchup_period"] <= 2} == {"final"}
    assert res["window"]["first_actionable_week"] == data.state.start_week
    con.close()


def test_load_race_data_reads_the_median_game_setting(tmp_path):
    _, con = _db_league(tmp_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    con.execute("UPDATE matchups SET league_id = ?", [lid])
    assert load_race_data(con).median_game is False                                # the ESPN fixture: no median game
    raw = fetch_dicts(con, "SELECT settings_raw FROM leagues WHERE league_id = ?", [lid])[0]["settings_raw"]
    settings = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    con.execute("UPDATE leagues SET settings_raw = ? WHERE league_id = ?",
                [json.dumps({**settings, "league_average_match": 1}), lid])         # Sleeper's league setting
    assert load_race_data(con).median_game is True
    con.close()


def test_api_is_read_only_and_leaves_trade_and_projection_engines_unchanged(tmp_path, monkeypatch):
    db_path, con = _db_league(tmp_path)
    lid = fetch_dicts(con, "SELECT league_id FROM leagues")[0]["league_id"]
    con.execute("UPDATE matchups SET league_id = ?", [lid])
    raw = _raw_snapshot(con)
    tables = {t: fetch_dicts(con, f"SELECT * FROM {t} ORDER BY ALL") for t in
              ("player_scores", "player_ros_projections", "team_week_lineup_projections", "team_ros_lineup_projections",
               "waiver_recommendations", "manager_classifications", "fantasy_teams", "matchups")}
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    from test_schedule_intelligence import stable
    api = TestClient(app)
    trade = {"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304], "team_b_sends": [301]}
    before = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json(),
              api.get("/api/lineup-projections").json())
    mtime = db_path.stat().st_mtime_ns

    res = api.get("/api/playoff-race")
    assert res.status_code == 200
    body = res.json()
    assert body["focus_team_id"] == 2 and len(body["teams"]) == 4
    open_game = next(m for m in body["matchups"]["regular"] if m["status"] in ("upcoming", "in_progress"))
    over = api.get("/api/playoff-race", params=[("override", f"{open_game['matchup_id']}:home"), ("base", "actual")]).json()
    assert over["scenario"]["overrides"] == [{"matchup_id": open_game["matchup_id"], "outcome": "home"}]
    assert "whatif" not in body and over["whatif"]["inputs"]["base"] == "actual"           # issue #192
    assert over["whatif"]["inputs"]["set_results"][0]["matchup_id"] == open_game["matchup_id"]
    assert api.get("/api/playoff-race", params={"team_id": 3}).json()["focus_team_id"] == 3
    assert api.get("/api/playoff-race", params={"team_id": 99}).status_code == 404
    assert api.get("/api/playoff-race", params={"base": "odds"}).status_code == 422
    assert api.get("/api/playoff-race", params={"override": "1:home"}).status_code == 422      # a final game
    assert api.get("/api/playoff-race", params={"override": "nonsense"}).status_code == 422

    after = (api.post("/api/trades/evaluate", json=trade).json(), api.get("/api/trades/discover").json(),
             api.get("/api/lineup-projections").json())
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
    assert TestClient(app).get("/api/playoff-race").status_code == 503
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "analytics"
    for f in ("player_scores.py", "waivers.py", "manager_metrics.py", "manager_scouting.py", "projections.py",
              "trades.py", "team_lineups.py", "trade_data.py", "trade_discovery.py", "pipeline.py", "context.py",
              "schedule_intelligence.py", "lineup.py"):
        assert "playoff_race" not in (root / f).read_text(), f

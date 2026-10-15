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

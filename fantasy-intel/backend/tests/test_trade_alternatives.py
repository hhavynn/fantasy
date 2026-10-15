"""V3.1 trade vs. free agency (analytics/trade_alternatives): the canonical trade delta next to one
hypothetical acquisition of an available player. Synthetic inputs only; nothing here says anything
about real league outcomes."""

import copy
from dataclasses import replace
from itertools import combinations

import pytest

from app.analytics.team_lineups import known_total, project_team
from app.analytics.trade_alternatives import (FRONTIER_LABEL, FreeAgencyBoard, analyze, consistent_with,
                                              for_evaluation)
from app.analytics.trade_discovery import TradeSearch
from app.analytics.trade_practical import discover
from app.analytics.trades import evaluate_trade, legal_roster

from synthetic_league import SMALL_ROSTER, add_free_agents, synthetic_league
from synthetic_v3 import filler, league, market_league

# filler(team, base): base+1 QB 15 | +2..+4 RB 9, 8, 4 | +5..+7 WR 9, 8, 4 | +8 TE 6 | +9 K 7 | +10 D/ST 7.
# Lineup QB RB RB WR WR TE K D/ST FLEX: RB 4 (lower id) takes FLEX, WR 4 (+7) sits. Weeks 10-13, 13 = playoffs.


def team(base, drop=(), **kw):
    return [r for r in filler(0, base, **kw) if r[1] not in drop]


def statuses(state, **by_id):
    state.free_agent_status = {p.espn_player_id: "FREEAGENT" for p in state.free_agents} | \
        {int(k[1:]): v for k, v in by_id.items()}
    return state


def side(res, key="team_a"):
    return res[key]


def gain(s, k="ros_delta"):
    return s["comparison"]["free_agent_alternative_gain"][k]


def canon(s, k="ros_delta"):
    return s["comparison"]["canonical_gain"][k]


def adv(s, k="ros_delta"):
    return s["comparison"]["trade_advantage"][k]


def dst_league(t1_dst=3.0, fa=((901, "D/ST", 8.5),), capacity=10, t1_drop=(), limits=None, extra2=()):
    """Team 1 may trade its bench WR 4 (107) for team 2's spare D/ST 9 (211)."""
    t1 = team(100, drop=t1_drop, dst=t1_dst)
    t2 = team(200) + [(211, "D/ST", 9.0)] + list(extra2)
    return statuses(league({1: t1, 2: t2}, list(fa), capacity=capacity, limits=limits))


# ------------------------------------------------------------------- D/ST scenarios --

def test_empty_dst_slot_large_canonical_gain_mostly_available_from_free_agency():
    st = dst_league(t1_drop=("D/ST",), fa=((901, "D/ST", 8.0),))
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    me = side(res)
    assert canon(me) == 36.0                       # an empty D/ST slot filled with 9 x 4 weeks
    assert gain(me) == 32.0 and adv(me) == 4.0     # the FA D/ST fills it with 8 x 4 weeks
    assert me["comparison"]["share_available_without_trade"] == pytest.approx(0.889)
    alt = me["free_agent_alternative"]
    assert alt["player"]["espn_player_id"] == 901 and alt["acquisition_type"] == "free_agent"
    assert not alt["requires_drop"]                # 9 active of 10: an open spot
    dst = next(s for s in me["slot_breakdown"] if s["slot"] == "D/ST")
    assert dst["before_empty_weeks"] == 4 and dst["trade_empty_weeks"] == 0 and dst["alternative_empty_weeks"] == 0
    assert me["statements"][0].startswith("Most of this projected gain is available without trading")
    assert any("open active roster spot" in s for s in me["statements"])
    assert any(s.startswith("An available D/ST could provide a similar lineup improvement") for s in me["statements"])


def test_weak_dst_with_comparable_free_agent_needs_a_legal_drop():
    st = dst_league()                              # 10 active of 10: full
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    me = side(res)
    assert canon(me) == 24.0 and gain(me) == 22.0 and adv(me) == 2.0
    alt = me["free_agent_alternative"]
    assert alt["requires_drop"] and alt["drop_lineup_cost"] == 0.0
    # Equal-cost drops (the benched D/ST 3 and WR 4): the one with fewer projected points is shown.
    assert [d["espn_player_id"] for d in alt["drops"]] == [110]
    roster = st.rosters[1]
    after = [p for p in roster if p.espn_player_id != 110] + [p for p in st.free_agents if p.espn_player_id == 901]
    w = st.window()
    independent = known_total(project_team(after, st.slots, w, st.projections)) - \
        known_total(project_team(roster, st.slots, w, st.projections))
    assert gain(me, "known_ros_delta") == round(independent, 2)
    assert any("require dropping D/ST 110" in s for s in me["statements"])


def test_premium_dst_without_a_comparable_replacement_keeps_its_full_advantage():
    st = dst_league(fa=((901, "D/ST", 3.0),), extra2=())
    st.projections[211] = {w: 12.0 for w in (10, 11, 12, 13)}
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    assert canon(me) == 36.0 and gain(me) == 0.0 and adv(me) == 36.0
    assert me["free_agent_alternative"]["player"] is None
    assert me["free_agent_alternative"]["reason"] == "no_improving_player"
    assert me["statements"][0].startswith("No available free agent improves your lineup: the trade's +36.0")


def test_no_projected_free_agent_defenses_are_unknown_not_zero():
    st = dst_league(fa=((901, "D/ST", None), (902, "WR", 5.0)))
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    assert res["pool"]["excluded_unprojected"] == {"D/ST": 1}
    me = side(res)
    assert me["free_agent_alternative"]["player"]["espn_player_id"] == 902       # WR 5 over FLEX 4
    pos = next(p for p in me["position_alternatives"] if p["position"] == "D/ST")
    assert pos["player"] is None and pos["search"]["excluded_unprojected"] == 1


def test_dst_plus_elite_wr_package_keeps_the_wr_gain_beyond_free_agency():
    st = dst_league(extra2=[(212, "WR", 20.0)])
    me = side(analyze(st, 1, 2, [106], [211, 212], labels={1: "You"}))
    assert canon(me) == 72.0                       # D/ST +6 and WR 20 for WR 8, four weeks each
    assert gain(me) == 22.0 and adv(me) == 50.0
    assert me["statements"][0].startswith("The trade offers an improvement beyond available replacements: +50.0 of "
                                          "its +72.0 remains after the best free-agent move (D/ST 901")
    assert any(s.startswith("An available D/ST could provide a similar lineup improvement: at D/ST the trade adds "
                            "+24.0; adding D/ST 901 adds +22.0") for s in me["statements"])


# ------------------------------------------------------------- K, QB and TE scenarios --

def test_kicker_streaming_frontier_is_a_separate_optimistic_illustration():
    swing_a = {10: 10.0, 11: 2.0, 12: 10.0, 13: 2.0}
    swing_b = {10: 2.0, 11: 10.0, 12: 2.0, 13: 10.0}
    st = statuses(league({1: team(100, k=6), 2: team(200) + [(211, "K", 9.0)]},
                         [(901, "K", swing_a), (902, "K", swing_b)], capacity=11))
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    me = side(res)
    assert canon(me) == 12.0 and gain(me) == 8.0                 # one kicker kept all four weeks
    fr = next(f for f in me["streaming_frontier"] if f["position"] == "K")
    assert fr["no_trade"]["label"] == FRONTIER_LABEL and "not a plan" in FRONTIER_LABEL
    assert fr["no_trade"]["total_gain"] == 16.0                  # the best kicker every week, added for free
    assert [w["best_available"][0]["espn_player_id"] for w in fr["no_trade"]["weeks"]] == [901, 902, 901, 902]
    # The frontier never enters a metric: the headline alternative is the single best kicker.
    assert me["comparison"]["free_agent_alternative_gain"]["ros_delta"] == 8.0
    # Trade + that kicker keeps two kickers and starts the better one each week (+6 beyond the pickup);
    # trading instead of picking him up is +4.
    assert me["comparison"]["trade_advantage"]["ros_delta"] == 6.0
    assert me["comparison"]["trade_instead_of_free_agent"]["ros_delta"] == 4.0


@pytest.mark.parametrize("pos,mine,theirs,fa,expect", [
    ("TE", {"te": (4,), "rb": (9, 8, 8)}, 10.0, 9.0, (24.0, 20.0, 8.0)),   # TE 9 also beats the FLEX 8 by 1
    ("QB", {"qb": 15}, 20.0, 18.0, (20.0, 12.0, 8.0)),
])
def test_qb_and_te_replacement_comparison(pos, mine, theirs, fa, expect):
    st = statuses(league({1: team(100, **mine), 2: team(200) + [(211, pos, theirs)]}, [(901, pos, fa)], capacity=11))
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    assert (canon(me), gain(me), adv(me)) == expect
    assert me["free_agent_alternative"]["player"]["position"] == pos
    assert me["statements"][0].startswith("Most of this projected gain is available without trading")


# ----------------------------------------------------------------- roster legality --

def test_no_legal_free_agent_acquisition_is_reported():
    t1 = team(100, drop=("D/ST",)) + [(110, "D/ST", 0.0, {"on_ir": True})]
    st = statuses(league({1: t1, 2: team(200) + [(211, "D/ST", 9.0)]}, [(901, "D/ST", 8.0)], capacity=9,
                         limits={"D/ST": 1}))
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    alt = me["free_agent_alternative"]
    assert alt["player"] is None and alt["reason"] == "no_legal_acquisition"
    assert alt["search"]["only_by_cutting_candidate"] == 1
    assert any(s.startswith("No legal free-agent acquisition improves your lineup") for s in me["statements"])


def test_fantasy_ir_player_frees_no_spot_and_is_never_the_drop():
    t1 = team(100) + [(111, "RB", 0.0, {"on_ir": True})]
    st = statuses(league({1: t1, 2: team(200) + [(211, "D/ST", 9.0)]}, [(901, "RB", 12.0)], capacity=10))
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    alt = me["free_agent_alternative"]
    assert alt["requires_drop"] and 111 not in [d["espn_player_id"] for d in alt["drops"]]
    assert gain(me) == 32.0                        # RB 12 over the FLEX RB 4, four weeks


def test_nfl_ineligible_free_agent_counts_only_from_his_eligible_weeks():
    st = statuses(league({1: team(100), 2: team(200) + [(211, "D/ST", 9.0)]}, [(901, "WR", {12: 20.0, 13: 20.0})],
                         capacity=11))
    st.free_agents = [replace(p, unavailable_weeks=frozenset({10, 11})) for p in st.free_agents]
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    alt = me["free_agent_alternative"]
    assert alt["player"]["espn_player_id"] == 901 and alt["weeks_started"] == [12, 13]
    assert gain(me) == 32.0                        # WR 20 for the FLEX 4, weeks 12-13 only


# ----------------------------------------------------------------- projection data --

def test_missing_weekly_projections_make_the_comparison_unavailable():
    st = dst_league(fa=((901, "D/ST", 8.5), (902, "WR", {10: 30.0, 11: 30.0})))
    st.projections.pop(103)                        # a rostered RB with an NFL team and no projection
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    me = side(res)
    assert canon(me) is None and gain(me) is None and adv(me) is None
    # Known-only: with RB 103 unprojected, WR 107 is the FLEX starter, so trading him costs 4 a week.
    assert me["comparison"]["canonical_gain"]["known_ros_delta"] == 8.0
    assert me["statements"][0].startswith("Replacement comparison unavailable: incomplete ESPN projections")
    assert res["pool"]["excluded_unprojected"] == {"WR": 1}       # partially projected FA never scored
    assert me["free_agent_alternative"]["player"]["espn_player_id"] == 901
    assert 103 not in [d["espn_player_id"] for d in me["free_agent_alternative"]["drops"]]


def test_dropping_an_unprojected_player_has_an_unknown_cost():
    st = dst_league()
    st.projections[107] = {10: 4.0, 11: 4.0}      # the bench WR lacks weeks 12-13
    st.projections[110] = {w: 3.0 for w in (10, 11, 12, 13)}
    st.rosters[1] = [p for p in st.rosters[1] if p.espn_player_id != 110]   # only 107 can go
    st.rosters[1] = st.rosters[1] + [replace(st.rosters[2][0], espn_player_id=120, position="QB")]
    st.projections[120] = {w: 1.0 for w in (10, 11, 12, 13)}
    board = FreeAgencyBoard(st, st.window())
    alt = board.best_acquisition(1, st.rosters[1])
    assert alt["player"]["espn_player_id"] == 901
    assert [d["espn_player_id"] for d in alt["drops"]] == [120]    # the known 1-point QB, not the unknown WR


def test_known_zero_projection_is_a_real_value():
    st = dst_league(fa=((901, "D/ST", {10: 9.0, 11: 0.0, 12: 9.0, 13: 9.0}),))
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    alt = me["free_agent_alternative"]
    assert alt["player"]["espn_player_id"] == 901
    assert gain(me) == 18.0                        # +6 in three weeks; D/ST 3 still starts the 0 week
    # RB 104 / WR 107 tie on lineup cost and own projection (the other one takes FLEX): lowest id shown.
    assert [d["espn_player_id"] for d in alt["drops"]] == [104]


# ----------------------------------------------------- managers, phases, independence --

def one_sided_league():
    t2 = team(200, rb=(9, 8, 4)) + [(211, "D/ST", 9.0)]
    return statuses(league({1: team(100, dst=3.0), 2: t2}, [(901, "RB", 9.0), (902, "D/ST", 4.0)], capacity=11))


def test_both_managers_get_independent_opportunity_costs_and_one_sided_outcomes_are_explained():
    st = one_sided_league()
    res = analyze(st, 1, 2, [107], [211], labels={1: "You"})
    me, them = side(res), side(res, "team_b")
    assert canon(me) == 24.0 and canon(them) == -8.0
    assert gain(them) == 20.0                      # RB 9 over their FLEX RB 4
    assert them["statements"][0].startswith("The trade does not improve Team 2's lineup (-8.0); adding RB 901 (RB) "
                                            "alone would (+20.0)")
    assert adv(them) == -8.0                       # their RB pickup is independent of the trade
    assert them["comparison"]["trade_instead_of_free_agent"]["ros_delta"] == -28.0
    # Both managers' best alternative is the same free agent: flagged, never simultaneously available.
    assert gain(me) == 20.0 and me["free_agent_alternative"]["player"]["espn_player_id"] == 901
    assert res["contested"]["player"]["espn_player_id"] == 901
    assert "only one of them could add" in res["contested"]["note"]
    st2 = one_sided_league()
    st2.free_agents = [p for p in st2.free_agents if p.espn_player_id == 902]
    assert analyze(st2, 1, 2, [107], [211])["contested"] is None


def test_multiweek_phases_add_up():
    st = dst_league(fa=((901, "D/ST", {10: 8.0, 11: 4.0, 12: 8.0, 13: 10.0}),))
    me = side(analyze(st, 1, 2, [107], [211]))
    for key in ("canonical_gain", "free_agent_alternative_gain", "trade_advantage", "trade_instead_of_free_agent",
                "trade_advantage_if_both_add", "available_without_trade"):
        c = me["comparison"][key]
        assert c["regular_season_delta"] + c["playoff_delta"] == pytest.approx(c["ros_delta"], abs=0.011)
    assert me["comparison"]["trade_instead_of_free_agent"]["playoff_delta"] == -1.0   # FA D/ST 10 beats 9 in week 13
    assert me["comparison"]["trade_advantage"]["playoff_delta"] == 0.0     # with both, the 10 starts week 13


def test_no_trade_baseline_equals_the_canonical_before_and_deltas_match_the_evaluator():
    st = market_league()
    st = statuses(st)
    for a, b in (([106], [204]), ([105, 106], [202]), ([112], [450, 451])):
        res = analyze(st, 1, 2 if 204 in b or 202 in b else 4, a, b)
        ev = evaluate_trade(st, 1, 2 if 204 in b or 202 in b else 4, a, b, scenarios=False)
        assert consistent_with(res, ev)
        board = FreeAgencyBoard(st, st.window())
        before = board.team(st.rosters[1])
        assert before["summary"]["ros_projected_starting_points"] == ev["team_a"]["before"]["ros_projected_starting_points"]


def test_changing_the_free_agent_pool_never_changes_the_canonical_delta():
    st = dst_league()
    ev1 = evaluate_trade(st, 1, 2, [107], [211], scenarios=False)
    r1 = analyze(st, 1, 2, [107], [211])
    st2 = copy.deepcopy(st)
    st2.free_agents = st2.free_agents + [replace(st.free_agents[0], espn_player_id=990, position="D/ST")]
    st2.projections[990] = {w: 11.0 for w in (10, 11, 12, 13)}
    st2.free_agent_status[990] = "FREEAGENT"
    ev2 = evaluate_trade(st2, 1, 2, [107], [211], scenarios=False)
    r2 = analyze(st2, 1, 2, [107], [211])
    assert ev1["team_a"]["deltas"] == ev2["team_a"]["deltas"] and ev1["team_b"]["deltas"] == ev2["team_b"]["deltas"]
    assert canon(side(r1)) == canon(side(r2)) == 24.0
    assert gain(side(r1)) == 22.0 and gain(side(r2)) == 32.0
    assert adv(side(r2)) == 0.0 and side(r2)["comparison"]["trade_instead_of_free_agent"]["ros_delta"] == -8.0


def test_analysis_never_mutates_state_or_the_canonical_result():
    st = dst_league(extra2=[(212, "WR", 20.0)])
    snap = copy.deepcopy(st)
    ev = evaluate_trade(st, 1, 2, [106], [211, 212], scenarios=False)
    analyze(st, 1, 2, [106], [211, 212])
    assert st == snap
    assert evaluate_trade(st, 1, 2, [106], [211, 212], scenarios=False) == ev


def test_waiver_claims_are_a_separate_non_guaranteed_alternative():
    st = dst_league(fa=((901, "D/ST", 6.0), (902, "D/ST", 8.5)))
    st.free_agent_status[902] = "WAIVERS"
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    assert me["free_agent_alternative"]["player"]["espn_player_id"] == 901       # headline: no waiver assumed
    assert gain(me) == 12.0
    w = me["waiver_claim_alternative"]
    assert w["player"]["espn_player_id"] == 902 and w["acquisition_type"] == "waiver_claim"
    assert w["gain"]["ros_delta"] == 22.0
    assert any("waiver claim on D/ST 902" in s and "not guaranteed" in s for s in me["statements"])


def test_unknown_acquisition_type_is_labelled():
    st = dst_league()
    st.free_agent_status = {}
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    # Headline alternative must not claim an unconfirmed player is an immediate free agent
    assert me["free_agent_alternative"]["player"] is None
    # Unknown availability is reported separately
    unk = me["unknown_availability_alternative"]
    assert unk is not None and unk["player"]["espn_player_id"] == 901
    assert unk["acquisition_type"] == "unknown"
    assert any("unconfirmed availability status" in s for s in me["statements"])


def test_large_canonical_gain_with_little_advantage_over_free_agency():
    st = dst_league(t1_drop=("D/ST",), fa=((901, "D/ST", 8.5),))
    me = side(analyze(st, 1, 2, [107], [211], labels={1: "You"}))
    assert canon(me) == 36.0 and adv(me) == 2.0
    assert adv(me) <= 0.1 * canon(me)


def test_trade_plus_free_agent_is_a_separate_diagnostic():
    st = dst_league(fa=((901, "D/ST", 8.5), (902, "WR", 6.0)))
    me = side(analyze(st, 1, 2, [107], [211]))
    d = me["with_trade_alternative"]
    assert d["player"]["espn_player_id"] == 902            # after the trade, WR 6 over the FLEX 4
    c = me["comparison"]
    assert c["trade_plus_best_free_agent_gain"]["ros_delta"] == canon(me) + d["gain"]["ros_delta"]
    assert c["trade_advantage_if_both_add"]["ros_delta"] == c["trade_plus_best_free_agent_gain"]["ros_delta"] - gain(me)


# --------------------------------------------------------------------- exactness --

def brute_alternative(st, team_id, kinds=("FREEAGENT",)):
    """Reference: every scorable free agent, the forced-drop rule by full enumeration (fewest drops;
    among sets keeping the candidate, the largest remaining total), the best gain (0 = no acquisition)."""
    w = st.window()
    roster = st.rosters[team_id]

    def tot(ps):
        return known_total(project_team(ps, st.slots, w, st.projections))
    base, best = tot(roster), 0.0
    for c in st.free_agents:
        if st.free_agent_status.get(c.espn_player_id) not in kinds or any(
                st.projections.get(c.espn_player_id, {}).get(x) is None for x in w.weeks if x not in c.unavailable_weeks):
            continue
        after = roster + [c]
        if legal_roster(after, st, roster):
            best = max(best, tot(after) - base)
            continue
        for k in range(1, len(after) + 1):
            legal = [s for s in combinations(after, k) if legal_roster([p for p in after if p not in s], st, roster)]
            if legal:
                keep = [s for s in legal if c not in s]
                if keep:
                    best = max(best, max(tot([p for p in after if p not in s]) for s in keep) - base)
                break
    return round(best, 2)


@pytest.mark.parametrize("seed", [3, 5, 8])
@pytest.mark.parametrize("negative", [False, True])
def test_best_acquisition_equals_brute_force(seed, negative):
    st = synthetic_league(n_teams=2, weeks=range(10, 14), last_regular=12, seed=seed, roster=SMALL_ROSTER)
    add_free_agents(st, n=24, seed=seed, start_id=60000, scale=0.9)
    st.position_limits = {"RB": 4, "D/ST": 1}
    if negative:
        dst = next(p for p in st.free_agents if p.position == "D/ST")
        st.projections[dst.espn_player_id][11] = -2.0
    statuses(st)
    board = FreeAgencyBoard(st, st.window())
    for t in (1, 2):
        alt = board.best_acquisition(t, st.rosters[t])
        assert (alt["gain"]["known_ros_delta"] or 0.0) == brute_alternative(st, t)
        assert alt["search"]["method"].startswith("exact (every candidate" if negative else "exact (branch and bound")


# ----------------------------------------------------------------- discovery rows --

def test_discovery_rows_get_a_consistent_trade_vs_free_agency_panel():
    st = statuses(market_league())
    ts = TradeSearch(st, 1)
    out = discover(ts, "practical", limit=5)
    board = FreeAgencyBoard(st, ts.window)
    shown = [x for items in out["sections"].values() for x in items]
    assert shown
    for row in shown:
        tvf = for_evaluation(st, {"team_a": row["mine_detail"], "team_b": row["theirs_detail"]}, 1,
                             row["opponent_team_id"], row["sends"], row["receives"], board=board, labels={1: "You"})
        assert tvf["available"] and tvf["consistent_with_canonical"]
        assert tvf["team_a"]["comparison"]["canonical_gain"]["ros_delta"] == row["mine"]["ros_delta"]
        assert tvf["team_a"]["statements"]


def _row(statement, g, av):
    tvf = {"available": True, "team_a": {"statements": [statement], "comparison": {
        "canonical_gain": {"ros_delta": g}, "available_without_trade": {"ros_delta": av}}}}
    return {"trade_vs_free_agency": tvf, "rationale": {"points": ["p"], "concerns": ["c"]}}


@pytest.mark.parametrize("g,av,concern", [(30.0, 0.0, False), (30.0, 15.0, False), (30.0, 15.1, True),
                                          (-2.0, 0.0, True), (None, None, True)])
def test_discovery_evidence_uses_the_majority_rule_and_never_touches_tiers(g, av, concern):
    from app.api.trade_discovery import _free_agency_evidence
    row = _row("lead", g, av)
    _free_agency_evidence(row)
    assert ("lead" in row["rationale"]["concerns"]) == concern and row["rationale"]["points"] == ["p"]
    assert "tier" not in row

"""Waiver recommendation logic."""

import pytest

from app.analytics.waivers import (PlayerValue, evaluate_candidate, interest_label, starters_by_position,
                                   suggest_drop, team_needs)

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 7, "IR": 1}
REQ = starters_by_position(SLOTS)


def pv(pid, pos, value, opp=None, trend=None):
    return PlayerValue(pid, pos, value, opp if opp is not None else value, value, trend, "HOLD", "usage_model")


def p(pid, pos, slot=20, starter=False, injury="ACTIVE", name=None):
    return {"espn_player_id": pid, "position": pos, "lineup_slot_id": slot, "is_starter": starter,
            "injury_status": injury, "full_name": name or f"P{pid}"}


def test_starter_slots_give_flex_to_rb_wr_not_te():
    from app.analytics.waivers import starter_slots
    assert [starter_slots(REQ[p]) for p in ("QB", "RB", "WR", "TE")] == [1, 3, 3, 1]


def test_starters_by_position_splits_flex():
    assert REQ["RB"] == pytest.approx(2.45) and REQ["WR"] == pytest.approx(2.45) and REQ["TE"] == pytest.approx(1.1)
    assert REQ["QB"] == 1 and REQ["D/ST"] == 1


def my_roster():
    return [p(1, "QB", 0, True), p(2, "RB", 2, True), p(3, "RB", 2, True), p(4, "RB", 23, True, injury="OUT"),
            p(5, "WR", 4, True), p(6, "WR", 4, True), p(7, "WR", 20), p(8, "TE", 6, True), p(9, "TE", 20),
            p(10, "D/ST", 16, True), p(11, "K", 17, True), p(12, "RB", 21, injury="INJURY_RESERVE"),
            p(13, "WR", 20, name="Weak WR")]


def values():
    v = {1: 8, 2: 6, 3: 5, 4: 7, 5: 8, 6: 7, 7: 4, 8: 5, 9: 2, 10: 5, 11: 5, 12: 6, 13: 1.0}
    pos = {r["espn_player_id"]: r["position"] for r in my_roster()}
    return {k: pv(k, pos[k], x) for k, x in v.items()}


def test_injuries_create_positional_need():
    needs = team_needs(my_roster(), values(), REQ, {pos: 6.0 for pos in ("QB", "RB", "WR", "TE", "D/ST", "K")})
    assert needs["RB"]["unavailable"] == 2 and needs["RB"]["healthy"] == 2
    assert needs["RB"]["need_score"] > needs["QB"]["need_score"]
    assert needs["RB"]["need_score"] > 0.5


def test_suggest_drop_picks_weakest_bench_but_keeps_positions_startable():
    d = suggest_drop(my_roster(), values(), "RB", REQ)
    assert d["espn_player_id"] == 13   # Weak WR, never the IR player or a starter
    # Only one healthy K and it's a starter -> never suggested; a lone bench TE backup can go
    roster = [p(1, "QB", 0, True), p(2, "K", 20)]
    assert suggest_drop(roster, {1: pv(1, "QB", 9), 2: pv(2, "K", 1)}, "RB", {"QB": 1, "K": 1}) is None
    assert suggest_drop(roster, {1: pv(1, "QB", 9), 2: pv(2, "K", 1)}, "K", {"QB": 1, "K": 1})["espn_player_id"] == 2


def _eval(cand_value, cand_opp, trend=None, injury="ACTIVE", profiles=None, status="WAIVERS", pos="RB"):
    vals = values()
    cand = {"espn_player_id": 99, "position": pos, "availability_status": status, "injury_status": injury,
            "percent_change": 8.0, "percent_owned": 20.0}
    cv = pv(99, pos, cand_value, cand_opp, trend)
    lsa = {pos: 6.0 for pos in ("QB", "RB", "WR", "TE", "D/ST", "K")}
    rival_roster = [p(50, "RB", 2, True, injury="OUT"), p(51, "QB", 0, True)]
    needs = {2: team_needs(my_roster(), vals, REQ, lsa), 7: team_needs(rival_roster, vals, REQ, lsa),
             8: team_needs(my_roster(), vals, REQ, lsa)}
    profiles = profiles or {7: {"add_share": {"RB": 0.6}, "adds_total": 10, "aggression_pct": 0.9, "resource": 0.8},
                            8: {"add_share": {"RB": 0.1}, "adds_total": 10, "aggression_pct": 0.1, "resource": 0.2}}
    return evaluate_candidate(cand, cv, my_team_id=2, needs=needs, my_roster=my_roster(), values=vals, required=REQ,
                              profiles=profiles, league_pos_share={"RB": 0.3}, team_names={7: "Rival", 8: "Sleepy"},
                              scarcity={"RB": 4, "WR": 10})


def test_strong_rising_back_is_a_claim_and_rival_is_top_competitor():
    r = _eval(8.5, 8.0, trend="UP")
    assert r["recommendation"] == "CLAIM"
    assert r["opportunity_score"] == 9.0
    assert r["suggested_drop_id"] == 13
    assert r["competitors"][0]["name"] == "Rival"
    assert r["league_interest"] in ("MEDIUM", "HIGH")
    assert r["components"]["roster"]["value_margin_vs_drop"] == pytest.approx(7.5)


def test_fringe_player_is_ignored():
    assert _eval(1.5, 1.5)["recommendation"] == "IGNORE"


def test_same_player_profile_depends_on_my_roster():
    # Two of my RBs are out -> a median-role RB fills a real hole.
    assert _eval(5.0, 5.5)["recommendation"] == "CLAIM"
    # My WR room is healthy and better -> a similar WR is only worth watching.
    r = _eval(4.5, 6.5, pos="WR")
    assert r["recommendation"] == "WAIT"
    assert r["roster_fit_score"] < 4


def test_injured_player_is_never_a_claim():
    r = _eval(8.5, 8.0, injury="OUT")
    assert r["recommendation"] in ("WAIT", "IGNORE")
    assert r["components"]["player"]["injury_adj"] == -3.0


def test_backup_te_is_not_a_claim_when_my_starter_is_better():
    # Huge role, but my TE1 (value 5) is better and a TE2 rarely plays -> not a CLAIM.
    r = _eval(4.8, 9.0, pos="TE")
    assert r["recommendation"] == "WAIT"
    assert r["components"]["roster"]["depth_utility"] == 0.3
    assert r["roster_fit_score"] < 3.5


def test_interest_labels_are_coarse_buckets():
    assert interest_label(0.5) == "HIGH" and interest_label(0.3) == "MEDIUM" and interest_label(0.1) == "LOW"


def test_no_competition_when_nobody_needs_or_adds_position():
    profiles = {7: {"add_share": {"RB": 0.0}, "adds_total": 10, "aggression_pct": 0.0, "resource": 0.0},
                8: {"add_share": {"RB": 0.0}, "adds_total": 10, "aggression_pct": 0.0, "resource": 0.0}}
    r = _eval(3.0, 3.0, profiles=profiles)
    assert r["league_interest"] == "LOW"

"""Waiver recommendation logic on the real-lineup model."""

import pytest

from app.analytics.availability import availability
from app.analytics.waivers import (PlayerValue, evaluate_candidate, interest_label, league_starter_averages,
                                   suggest_drop, team_needs, team_state)

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 7, "IR": 1}


def pv(pid, pos, value, opp=None, trend=None, basis="xfp"):
    return PlayerValue(pid, pos, value, opp if opp is not None else value, value, trend, "HOLD", basis)


def p(pid, pos, slot=20, injury="ACTIVE", name=None, report=None):
    return {"espn_player_id": pid, "position": pos, "lineup_slot_id": slot, "injury_status": injury,
            "full_name": name or f"P{pid}", "nfl_report_status": report}


def my_roster(over=None):
    r = [p(1, "QB", 0), p(2, "RB", 2), p(3, "RB", 2), p(4, "RB", 23, injury="OUT"),
         p(5, "WR", 4), p(6, "WR", 4), p(7, "WR", 20), p(8, "TE", 6), p(9, "TE", 20),
         p(10, "D/ST", 16), p(11, "K", 17), p(12, "RB", 21, injury="INJURY_RESERVE"),
         p(13, "WR", 20, name="Weak WR")]
    return [{**x, **(over or {}).get(x["espn_player_id"], {})} for x in r]


VALS = {1: 8, 2: 6, 3: 5, 4: 7, 5: 8, 6: 7, 7: 4, 8: 5, 9: 2, 10: 5, 11: 5, 12: 6, 13: 1.0}


def values(extra=None):
    pos = {r["espn_player_id"]: r["position"] for r in my_roster()}
    out = {k: pv(k, pos[k], x) for k, x in VALS.items()}
    out.update(extra or {})
    return out


LSA = {pos: 6.0 for pos in ("QB", "RB", "WR", "TE", "D/ST", "K")}


def test_needs_count_flex_once():
    st = team_state(my_roster(), values(), SLOTS)
    needs = team_needs(st, SLOTS, LSA)
    assert needs["RB"]["starters_required"] + needs["WR"]["starters_required"] + needs["TE"]["starters_required"] == 6
    assert sum(1 for a in st["lineup"] if a["position"] in ("RB", "WR", "TE")) == 6   # 2+2+1+FLEX, never 7


def test_out_and_ir_players_create_rb_need():
    needs = team_needs(team_state(my_roster(), values(), SLOTS), SLOTS, LSA)
    assert needs["RB"]["unavailable"] == 2 and needs["RB"]["healthy"] == 2
    assert needs["RB"]["need_score"] > needs["QB"]["need_score"]
    # 2 healthy RBs for 2 fixed RB slots and no depth: a real (not maximal) need. The old
    # fractional-FLEX model invented a 3rd RB starter here and overstated it.
    assert needs["RB"]["need_score"] > 0.35 and needs["RB"]["need_score"] > needs["WR"]["need_score"]
    assert needs["RB"]["details"]["depth_target"] == 3 and needs["RB"]["starters_required"] == 2


def test_doubtful_player_is_effectively_unavailable_for_need():
    healthy = team_needs(team_state(my_roster(), values(), SLOTS), SLOTS, LSA)["TE"]
    doubtful = team_needs(team_state(my_roster({8: {"injury_status": "DOUBTFUL"}}), values(), SLOTS), SLOTS, LSA)["TE"]
    out = team_needs(team_state(my_roster({8: {"injury_status": "OUT"}}), values(), SLOTS), SLOTS, LSA)["TE"]
    assert healthy["effective_available"] == 2.0 and doubtful["effective_available"] == pytest.approx(1.15)
    assert doubtful["need_score"] > healthy["need_score"]
    assert doubtful["need_score"] == pytest.approx(out["need_score"])   # backup TE starts either way
    st = next(a for a in doubtful["details"]["availability"] if a["espn_player_id"] == 8)
    assert st["status"] == "DOUBTFUL" and st["weight"] == 0.15           # adjustment is stored


def test_doubtful_backup_does_not_change_need_when_starter_is_healthy():
    healthy = team_needs(team_state(my_roster(), values(), SLOTS), SLOTS, LSA)["TE"]
    backup = team_needs(team_state(my_roster({9: {"injury_status": "DOUBTFUL"}}), values(), SLOTS), SLOTS, LSA)["TE"]
    assert backup["need_score"] == healthy["need_score"]


def test_questionable_is_risk_adjusted_not_out():
    q = team_needs(team_state(my_roster({8: {"injury_status": "QUESTIONABLE"}}), values(), SLOTS), SLOTS, LSA)["TE"]
    out = team_needs(team_state(my_roster({8: {"injury_status": "OUT"}}), values(), SLOTS), SLOTS, LSA)["TE"]
    healthy = team_needs(team_state(my_roster(), values(), SLOTS), SLOTS, LSA)["TE"]
    assert healthy["need_score"] <= q["need_score"] < out["need_score"]
    assert q["unavailable"] == 0 and out["unavailable"] == 1


def test_nfl_report_for_current_week_can_downgrade_espn_status():
    assert availability("ACTIVE", None, "Doubtful")["status"] == "DOUBTFUL"
    assert availability("OUT", None, "Questionable")["status"] == "OUT"     # most severe wins
    assert availability(None, 21, None)["status"] == "IR"


def test_league_starter_average_uses_lineup_starters_by_player_position():
    st = team_state(my_roster(), values(), SLOTS)
    lsa = league_starter_averages({1: st}, SLOTS)
    assert lsa["RB"] == pytest.approx(5.5)    # RB starters 6 and 5 (OUT/IR excluded); FLEX went to a WR
    assert lsa["WR"] == pytest.approx(round((8 + 7 + 4) / 3, 2))   # WR1, WR2 + the WR in FLEX


def test_suggest_drop_weakest_bench_never_starter_or_ir():
    st = team_state(my_roster(), values(), SLOTS)
    d = suggest_drop(st)
    assert d["espn_player_id"] == 13
    starters = {a["player_id"] for a in st["lineup"]}
    assert d["espn_player_id"] not in starters and d["espn_player_id"] != 12


def test_suggest_drop_keeps_fixed_slots_fillable():
    roster = [p(1, "QB", 0), p(2, "K", 20), p(3, "K", 17)]
    vals = {1: pv(1, "QB", 9), 2: pv(2, "K", 1), 3: pv(3, "K", 2)}
    st = team_state(roster, vals, {"QB": 1, "K": 1})
    assert suggest_drop(st)["espn_player_id"] == 2               # backup K is droppable
    st = team_state([p(1, "QB", 0), p(3, "K", 17)], vals, {"QB": 1, "K": 1})
    assert suggest_drop(st) is None                              # only starters left


def _eval(cand_value, cand_opp, trend=None, injury="ACTIVE", pos="RB", basis="xfp", status="WAIVERS",
          profiles=None, context=None):
    vals = values()
    cand = {"espn_player_id": 99, "position": pos, "availability_status": status, "injury_status": injury,
            "percent_change": 8.0, "percent_owned": 20.0, "full_name": "Cand"}
    cv = pv(99, pos, cand_value, cand_opp, trend, basis)
    rival = team_state([p(50, "RB", 2, injury="OUT"), p(51, "QB", 0)], vals, SLOTS)
    mine = team_state(my_roster(), vals, SLOTS)
    needs = {2: team_needs(mine, SLOTS, LSA), 7: team_needs(rival, SLOTS, LSA), 8: team_needs(mine, SLOTS, LSA)}
    profiles = profiles or {7: {"add_share": {"RB": 0.6}, "adds_total": 10, "aggression_pct": 0.9, "resource": 0.8},
                            8: {"add_share": {"RB": 0.1}, "adds_total": 10, "aggression_pct": 0.1, "resource": 0.2}}
    return evaluate_candidate(cand, cv, my_team_id=2, needs=needs, my_state=mine, profiles=profiles,
                              league_pos_share={"RB": 0.3}, team_names={7: "Rival", 8: "Sleepy"},
                              scarcity={"RB": 4, "WR": 10, "TE": 10}, context_reasons=context)


def test_strong_rising_back_is_a_claim_and_rival_is_top_competitor():
    r = _eval(8.5, 8.0, trend="UP")
    assert r["recommendation"] == "CLAIM"
    assert r["opportunity_score"] == 9.0
    # Twelve active players for sixteen slots: preserve the bench and use open capacity.
    assert r["suggested_drop_id"] is None
    assert r["components"]["decision"]["roster_move"]["status"] == "ADD_WITHOUT_DROP"
    assert r["components"]["roster"]["enters_lineup"] is True
    assert r["competitors"][0]["name"] == "Rival"
    assert r["league_interest"] in ("MEDIUM", "HIGH")


def test_fringe_player_is_ignored():
    assert _eval(1.5, 1.5)["recommendation"] == "IGNORE"


def test_same_profile_depends_on_my_roster():
    r = _eval(6.0, 6.5)                                           # two of my RBs are out
    assert r["recommendation"] == "CLAIM" and r["components"]["roster"]["lineup_slot"] in ("RB", "FLEX")
    r = _eval(4.5, 6.5, pos="WR")                                 # my WR room is healthy and better
    assert r["recommendation"] == "WAIT" and r["roster_fit_score"] < 4
    assert r["components"]["roster"]["lineup_gain"] == pytest.approx(0.5)   # only a marginal FLEX upgrade


def test_backup_te_is_not_a_claim_when_my_starter_is_better():
    r = _eval(4.8, 9.0, pos="TE")
    assert r["recommendation"] == "WAIT"
    assert r["components"]["roster"]["depth_utility"] == 0.3
    assert r["roster_fit_score"] < 3.5


def test_injured_player_is_never_a_claim():
    r = _eval(8.5, 8.0, injury="OUT")
    assert r["recommendation"] in ("WAIT", "IGNORE")
    assert r["components"]["player"]["injury_adj"] == -3.0
    assert _eval(8.5, 8.0, injury="DOUBTFUL")["components"]["player"]["injury_adj"] == -2.0


@pytest.mark.parametrize("injury", [None, "UNKNOWN", "NOT_REPORTED"])
def test_unknown_injury_is_not_an_affirmative_claim(injury):
    r = _eval(8.5, 8.0, injury=injury)
    assert r["recommendation"] == "WAIT"
    assert r["components"]["decision"]["action"] == "MONITOR"


@pytest.mark.parametrize("basis,phrase", [
    ("xfp", "expected-fantasy-points (xFP) percentile"),
    ("targets+carries", "targets+carries percentile (xFP unavailable)"),
    ("espn_points", "ESPN league-points percentile"),
])
def test_opportunity_basis_survives_into_explanations(basis, phrase):
    r = _eval(6.0, 6.0, basis=basis)
    assert r["components"]["player"]["basis"] == basis
    assert phrase in r["reasons"][0]
    if basis != "xfp":
        assert "expected-fantasy-points" not in " ".join(r["reasons"])


def test_unknown_basis_is_labelled_unavailable():
    r = _eval(None, None, basis="unavailable")
    assert r["components"]["player"]["basis"] == "unavailable" and r["opportunity_score"] is None
    assert "no usage or scoring data" in r["reasons"][0]


def test_context_headlines_flow_into_reasons():
    r = _eval(8.5, 8.0, context=["82% backfield rush share over last 2 games"])
    assert "82% backfield rush share over last 2 games" in r["reasons"]


def test_interest_labels_are_coarse_buckets():
    assert interest_label(0.5) == "HIGH" and interest_label(0.3) == "MEDIUM" and interest_label(0.1) == "LOW"


def test_no_competition_when_nobody_needs_or_adds_position():
    profiles = {7: {"add_share": {"RB": 0.0}, "adds_total": 10, "aggression_pct": 0.0, "resource": 0.0},
                8: {"add_share": {"RB": 0.0}, "adds_total": 10, "aggression_pct": 0.0, "resource": 0.0}}
    assert _eval(3.0, 3.0, profiles=profiles)["league_interest"] == "LOW"


# ---- positional starter quality includes flex-assigned starters --------------------------

FLEX_SLOTS = {"RB": 2, "WR": 2, "TE": 1, "FLEX": 1}


def _flex_state(players, slots=FLEX_SLOTS):
    roster = [p(pid, pos) for pid, pos, _ in players]
    vals = {pid: pv(pid, pos, v) for pid, pos, v in players}
    return team_state(roster, vals, slots)


def test_wr_in_flex_contributes_to_wr_quality():
    # WR 9, WR 7, FLEX -> WR 4 (beats RB3 = 3 and TE2 = 2)
    st = _flex_state([(1, "WR", 9), (2, "WR", 7), (3, "WR", 4), (4, "RB", 8), (5, "RB", 6), (6, "RB", 3),
                      (7, "TE", 5), (8, "TE", 2)])
    flex = next(a for a in st["lineup"] if a["slot"] == "FLEX")
    assert flex["player_id"] == 3
    wr = team_needs(st, FLEX_SLOTS, {})["WR"]
    assert wr["starters_required"] == 3
    assert wr["starter_value"] == pytest.approx(round((9 + 7 + 4) / 3, 2))   # not mean(9, 7) = 8
    assert wr["details"]["weakest_starter_value"] == 4                         # not 7
    assert sorted(wr["details"]["starter_values"]) == [4, 7, 9]


def test_rb_in_flex_contributes_to_rb_quality():
    st = _flex_state([(1, "WR", 9), (2, "WR", 7), (3, "WR", 2), (4, "RB", 8), (5, "RB", 6), (6, "RB", 5),
                      (7, "TE", 5)])
    rb = team_needs(st, FLEX_SLOTS, {})["RB"]
    assert rb["starters_required"] == 3 and rb["details"]["weakest_starter_value"] == 5
    assert rb["starter_value"] == pytest.approx(round((8 + 6 + 5) / 3, 2))
    wr = team_needs(st, FLEX_SLOTS, {})["WR"]
    assert wr["starters_required"] == 2 and wr["details"]["weakest_starter_value"] == 7   # WR3 is benched


def test_te_in_flex_contributes_to_te_quality():
    st = _flex_state([(1, "WR", 9), (2, "WR", 7), (3, "WR", 2), (4, "RB", 8), (5, "RB", 6), (6, "RB", 1),
                      (7, "TE", 6), (8, "TE", 5)])
    te = team_needs(st, FLEX_SLOTS, {})["TE"]
    assert te["starters_required"] == 2 and te["details"]["flex_slots_filled"] == 1
    assert te["starter_value"] == pytest.approx(5.5) and te["details"]["weakest_starter_value"] == 5


def test_league_starter_average_includes_flex_assigned_players():
    a = _flex_state([(1, "WR", 9), (2, "WR", 7), (3, "WR", 4), (4, "RB", 8), (5, "RB", 6), (7, "TE", 5)])
    b = _flex_state([(11, "WR", 8), (12, "WR", 6), (14, "RB", 8), (15, "RB", 6), (16, "RB", 5), (17, "TE", 5)])
    lsa = league_starter_averages({1: a, 2: b}, FLEX_SLOTS)
    assert lsa["WR"] == pytest.approx(round(((9 + 7 + 4) / 3 + (8 + 6) / 2) / 2, 2))   # team A's flex WR counts
    assert lsa["RB"] == pytest.approx(round(((8 + 6) / 2 + (8 + 6 + 5) / 3) / 2, 2))   # team B's flex RB counts


def test_flex_quality_change_keeps_exactly_five_rb_wr_te_starters():
    st = _flex_state([(1, "WR", 9), (2, "WR", 7), (3, "WR", 4), (4, "RB", 8), (5, "RB", 6), (6, "RB", 3)],
                     {"RB": 2, "WR": 2, "FLEX": 1})
    needs = team_needs(st, {"RB": 2, "WR": 2, "FLEX": 1}, {})
    total = sum(needs[x]["starters_required"] for x in ("RB", "WR", "TE"))
    counted = sum(len(needs[x]["details"]["starter_values"]) for x in ("RB", "WR", "TE"))
    assert total == 5 and counted == 5
    assert sum(1 for a in st["lineup"] if a["player_id"] is not None) == 5


def test_weak_flex_starter_raises_position_need_vs_league():
    strong = _flex_state([(1, "WR", 9), (2, "WR", 8), (3, "WR", 8), (4, "RB", 8), (5, "RB", 6), (7, "TE", 5)])
    weak = _flex_state([(1, "WR", 9), (2, "WR", 8), (3, "WR", 1), (4, "RB", 8), (5, "RB", 0.5), (7, "TE", 5)])
    lsa = league_starter_averages({1: strong, 2: weak}, FLEX_SLOTS)
    assert team_needs(weak, FLEX_SLOTS, lsa)["WR"]["need_score"] > team_needs(strong, FLEX_SLOTS, lsa)["WR"]["need_score"]


def test_experimental_ftn_evidence_never_reaches_waiver_reasons_or_score():
    from app.analytics.waivers import context_reasons
    ctx = {"labels": {}, "evidence": {"headlines": ["14% target share"],
                                      "experimental": {"ftn_read1_target_share": 0.9}}}
    reasons = context_reasons(ctx)
    assert reasons == ["14% target share"]
    with_exp = _eval(6.0, 6.0, pos="WR", context=reasons)
    without = _eval(6.0, 6.0, pos="WR", context=["14% target share"])
    assert with_exp["waiver_score"] == without["waiver_score"]
    assert with_exp["recommendation"] == without["recommendation"]

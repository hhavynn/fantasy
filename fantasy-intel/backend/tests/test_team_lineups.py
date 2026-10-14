"""V2 weekly projected lineups, missing-vs-zero, roster marginal value and the replacement market.
Pure functions, synthetic data."""

from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import (RosterPlayer, Window, best_fa_addition, project_team, project_week,
                                        replacement_context, replacement_market, roster_marginal_value, starters)

CFG = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BE": 5, "IR": 1}
SLOTS = lineup_slots(CFG)
W = Window((10, 11, 12), last_regular=11)


def P(pid, pos, team="KC", ir=False):
    return RosterPlayer(pid, pos, team, f"P{pid}", ir)


def flat(v, weeks=W.weeks):
    return {w: v for w in weeks}


def base_roster():
    players = [P(1, "QB"), P(2, "RB"), P(3, "RB"), P(4, "RB"), P(5, "WR"), P(6, "WR"), P(7, "WR"), P(8, "TE"), P(9, "TE")]
    proj = {1: flat(20), 2: flat(15), 3: flat(12), 4: flat(9), 5: flat(16), 6: flat(11), 7: flat(8), 8: flat(10), 9: flat(5)}
    return players, proj


def slot_of(week, pid):
    return next((a["slot"] for a in week["assignment"] if a["player_id"] == pid), None)


# ----------------------------------------------------------------------------- lineups --

def test_fixed_slots_take_the_best_eligible_player():
    players, proj = base_roster()
    w = project_week(players, SLOTS, 10, proj)
    assert slot_of(w, 1) == "QB" and slot_of(w, 8) == "TE"
    assert {slot_of(w, 2), slot_of(w, 3)} == {"RB"} and {slot_of(w, 5), slot_of(w, 6)} == {"WR"}
    assert slot_of(w, 9) is None                                  # TE2 benched


def test_flex_takes_best_remaining_rb_wr_te_and_counts_once():
    players, proj = base_roster()
    w = project_week(players, SLOTS, 10, proj)
    assert slot_of(w, 4) == "FLEX"                                # RB3 (9) beats WR3 (8) and TE2 (5)
    assert w["known_lineup_points"] == 20 + 15 + 12 + 16 + 11 + 10 + 9
    proj[9] = flat(9.5)                                           # TE2 now beats RB3 for FLEX
    assert slot_of(project_week(players, SLOTS, 10, proj), 9) == "FLEX"


def test_rb_wr_and_wr_te_flex_slots_are_exact():
    slots = lineup_slots({"RB": 1, "WR": 1, "RB/WR": 1, "WR/TE": 1})
    players = [P(1, "RB"), P(2, "RB"), P(3, "WR"), P(4, "WR"), P(5, "TE")]
    proj = {1: flat(10), 2: flat(9), 3: flat(8), 4: flat(3), 5: flat(7)}
    w = project_week(players, slots, 10, proj)
    assert slot_of(w, 2) == "RB/WR" and slot_of(w, 5) == "WR/TE"  # RB2 can't play WR/TE; TE can't play RB/WR
    assert w["known_lineup_points"] == 10 + 8 + 9 + 7


def test_op_slot_takes_a_second_qb_when_best():
    slots = lineup_slots({"QB": 1, "WR": 1, "OP": 1})
    players = [P(1, "QB"), P(2, "QB"), P(3, "WR"), P(4, "WR")]
    w = project_week(players, slots, 10, {1: flat(22), 2: flat(18), 3: flat(12), 4: flat(10)})
    assert slot_of(w, 2) == "OP" and w["known_lineup_points"] == 52


def test_exactly_one_player_per_slot():
    players, proj = base_roster()
    for wk in project_team(players, SLOTS, W, proj)["weeks"]:
        ids = [a["player_id"] for a in wk["assignment"] if a["player_id"] is not None]
        assert len(ids) == len(set(ids)) == len(SLOTS) == len(wk["assignment"])
        assert not set(ids) & {b["player_id"] for b in wk["bench"]}


def test_weekly_projections_change_who_starts():
    players, proj = base_roster()
    proj[7] = {10: 8, 11: 14, 12: 8}                              # WR3 has a big week 11
    t = project_team(players, SLOTS, W, proj)["weeks"]
    assert slot_of(t[0], 7) is None and slot_of(t[1], 7) is not None and slot_of(t[2], 7) is None


def test_bye_week_zero_benches_the_player_and_stays_complete():
    players, proj = base_roster()
    proj[2] = {10: 15, 11: 0.0, 12: 15}                           # bye in week 11
    t = project_team(players, SLOTS, W, proj)
    w11 = t["weeks"][1]
    assert 2 not in starters(w11) and w11["projection_complete"]
    assert any(b["player_id"] == 2 and b["points"] == 0.0 for b in w11["bench"])
    assert t["summary"]["ros_projected_starting_points"] is not None


def test_real_non_bye_zero_projection_counts_as_zero_not_missing():
    players = [P(1, "QB")]
    w = project_week(players, lineup_slots({"QB": 1}), 10, {1: {10: 0.0}})
    assert w["projection_complete"] and w["projected_lineup_points"] == 0.0
    assert w["assignment"][0]["player_id"] == 1 and w["assignment"][0]["points"] == 0.0


def test_missing_projection_is_not_zero_and_marks_week_incomplete():
    players, proj = base_roster()
    del proj[5][11]                                               # WR1 has no week-11 projection
    t = project_team(players, SLOTS, W, proj)
    w11 = t["weeks"][1]
    assert w11["projection_complete"] is False and w11["projected_lineup_points"] is None
    assert w11["missing_projection_ids"] == [5] and 5 not in starters(w11)
    assert w11["known_lineup_points"] == 20 + 15 + 12 + 11 + 8 + 10 + 9   # known-only, labelled as such
    s = t["summary"]
    assert s["ros_projected_starting_points"] is None and s["regular_season_projected_starting_points"] is None
    assert s["playoff_projected_starting_points"] is not None     # week 12 is complete
    assert s["missing_weeks"] == [11] and s["missing_players"] == [5] and s["ros_weeks_complete"] == 2
    # a projected 0 in the same spot is a different, complete answer
    proj[5][11] = 0.0
    assert project_team(players, SLOTS, W, proj)["weeks"][1]["projection_complete"]


def test_teamless_missing_player_does_not_poison_the_week():
    players, proj = base_roster()
    players.append(P(50, "WR", team=None))                        # e.g. released player stashed on IR
    w = project_week(players, SLOTS, 10, proj)
    assert w["projection_complete"] and w["teamless_unprojected_ids"] == [50] and w["missing_projection_ids"] == []


def test_missing_player_at_a_position_with_no_slot_is_irrelevant():
    players, proj = base_roster()
    players.append(P(60, "K"))                                    # league without a K slot
    assert project_week(players, SLOTS, 10, proj)["projection_complete"]


# ----------------------------------------------------------------- marginal value --

def test_roster_marginal_value_counts_only_lineup_points():
    players, proj = base_roster()
    rb3 = roster_marginal_value(players, 4, SLOTS, W, proj)       # FLEX RB3 (9) -> WR3 (8) replaces him
    assert rb3["roster_marginal_lineup_points"] == 3.0 and rb3["weeks_started"] == [10, 11, 12]
    te2 = roster_marginal_value(players, 9, SLOTS, W, proj)       # never starts -> 0, not his 15 bench points
    assert te2["roster_marginal_lineup_points"] == 0.0 and te2["weeks_started"] == []
    qb = roster_marginal_value(players, 1, SLOTS, W, proj)        # no backup QB: the whole 20/week
    assert qb["roster_marginal_lineup_points"] == 60.0
    assert qb["regular_season_marginal_lineup_points"] == 40.0 and qb["playoff_marginal_lineup_points"] == 20.0


def test_bench_player_gains_value_only_in_the_week_he_covers_a_bye():
    players, proj = base_roster()
    proj[2] = {10: 15, 11: 0.0, 12: 15}
    wr3 = roster_marginal_value(players, 7, SLOTS, W, proj)       # WR3 enters FLEX in week 11 only
    assert wr3["weeks_started"] == [11]
    assert [w["marginal"] for w in wr3["weeks"]] == [0.0, 3.0, 0.0]


# ------------------------------------------------------------- replacement market --

def fa_pool():
    fas = [P(101, "QB"), P(102, "QB"), P(201, "RB"), P(202, "RB"), P(301, "WR"), P(401, "TE"), P(402, "TE")]
    proj = {101: flat(14), 102: {10: 9, 11: 17, 12: 9}, 201: flat(7), 202: {10: 3, 11: 11, 12: 3},
            301: flat(8), 401: flat(6)}                           # 402 TE: no projection at all
    return fas, proj


def test_market_best_available_per_position_and_coverage():
    fas, proj = fa_pool()
    m = replacement_market(fas, SLOTS, W, proj)
    assert [g["group"] for g in m["groups"]] == ["QB", "RB", "WR", "TE", "FLEX"]
    w10 = m["weeks"][0]["positions"]
    assert w10["QB"]["best_available"]["espn_player_id"] == 101 and w10["QB"]["best_available_projection"] == 14
    assert w10["RB"]["best_available"]["espn_player_id"] == 201
    assert w10["WR"]["best_available"]["espn_player_id"] == 301
    assert w10["TE"]["available_count"] == 2 and w10["TE"]["projected_count"] == 1 and w10["TE"]["unprojected_count"] == 1
    assert [p["espn_player_id"] for p in w10["TE"]["top_available"]] == [401]   # no manufactured projection


def test_market_flex_accepts_rb_wr_te_but_not_qb():
    fas, proj = fa_pool()
    flex = replacement_market(fas, SLOTS, W, proj)["weeks"][0]["positions"]["FLEX"]
    assert {p["position"] for p in flex["top_available"]} <= {"RB", "WR", "TE"}
    assert flex["available_count"] == 5 and flex["best_available"]["espn_player_id"] == 301


def test_market_best_available_changes_by_week():
    fas, proj = fa_pool()
    weeks = replacement_market(fas, SLOTS, W, proj)["weeks"]
    assert [w["positions"]["QB"]["best_available"]["espn_player_id"] for w in weeks] == [101, 102, 101]
    assert [w["positions"]["FLEX"]["best_available"]["espn_player_id"] for w in weeks] == [301, 202, 301]


def test_best_fa_addition_is_lineup_specific_and_skips_unprojected():
    players, proj = base_roster()
    fas, fproj = fa_pool()
    proj.update(fproj)
    best = best_fa_addition(players, fas, SLOTS, W, proj)
    assert best["label"] == "hypothetical FA replacement"
    # no QB helps (QB1 is 20); WR 301 (8) / RB 201 (7) don't beat the FLEX RB3 (9); RB 202's week-11
    # 11 does: +2 in week 11 only
    assert best["player"]["espn_player_id"] == 202 and best["lineup_points_added"] == 2.0
    assert best["weeks_started"] == [11] and best["excluded_unprojected"] == 1


def test_replacement_context_recovered_plus_residual_equals_loss():
    players, proj = base_roster()
    fas, fproj = fa_pool()
    proj.update(fproj)
    rc = replacement_context(players, 1, fas, SLOTS, W, proj)    # lose the QB; best FA QB fills in
    assert rc["replacement"]["espn_player_id"] in (101, 102) and rc["replacement_position"] == "QB"
    assert rc["lineup_points_lost_without_player"] == 60.0
    assert rc["lineup_points_recovered"] + rc["residual_loss_vs_original"] == 60.0
    assert rc["lineup_points_recovered"] == 42.0                 # 101: 14 x 3 (102: 9+17+9 = 35)


def test_replacement_context_only_considers_players_who_can_fill_his_slots():
    players, proj = base_roster()
    fas = [P(701, "K"), P(702, "QB"), P(703, "WR")]
    proj.update({701: flat(30), 702: flat(10), 703: flat(9)})
    slots = SLOTS + ["K"]                                         # the team has an empty K slot
    rc = replacement_context(players, 1, fas, slots, W, proj)     # lose the QB
    assert rc["eligible_positions"] == ["QB"] and rc["replacement"]["espn_player_id"] == 702
    assert best_fa_addition(players, fas, slots, W, proj)["player"]["espn_player_id"] == 701   # hole fill, unrestricted
    rb = replacement_context(players, 4, fas, slots, W, proj)     # lose the FLEX RB: WR/TE can replace via FLEX
    assert rb["eligible_positions"] == ["RB", "TE", "WR"] and rb["replacement"]["espn_player_id"] == 703

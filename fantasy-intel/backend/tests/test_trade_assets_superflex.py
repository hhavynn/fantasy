"""Flex-aware league starter reference for the asset board (issue #229): league starters at a position, the
league-structure replacement level, starter-caliber, package labels and surplus all read one reference built from
every team's exact optimal lineup. Each flexible slot (FLEX, RB/WR, WR/TE, OP) is counted once, for the position
that fills it. Cross-checked against an independent brute-force lineup solver and lineup.assign_lineup. Synthetic
inputs only (invented players and projections)."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.analytics.lineup import FIXED_SLOTS, FLEX_ELIGIBILITY, assign_lineup, lineup_slots
from app.analytics.team_lineups import project_team, projection
from app.analytics.trade_assets import FLEX_NOTE, NOTE, AssetBoard, league_needs, league_reference, team_needs

from synthetic_v3 import league

ONE_QB = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1})
SUPERFLEX = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "OP": 1})
TWO_OP = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "OP": 2})
THREE_FLEX = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 3})
MIXED_FLEX = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "RB/WR": 1, "WR/TE": 1, "FLEX": 1})
FIXTURES = {"1qb": ONE_QB, "superflex": SUPERFLEX, "two_op": TWO_OP, "three_flex": THREE_FLEX, "mixed": MIXED_FLEX}
WEEKS = (10, 11, 12)
ROSTER = ("QB", "QB", "QB", "RB", "RB", "RB", "RB", "WR", "WR", "WR", "WR", "TE", "TE")


def random_league(slots, n_teams=3, seed=0, fas=()):
    """Distinct full-precision projections per player-week, so every team-week has a unique optimal starter set."""
    rng = random.Random(seed)
    mean = {"QB": (8, 26), "RB": (3, 20), "WR": (3, 20), "TE": (2, 13)}
    teams = {}
    for t in range(1, n_teams + 1):
        rows = []
        for i, pos in enumerate(ROSTER):
            lo, hi = mean[pos]
            rows.append((100 * t + i + 1, pos, {w: rng.uniform(lo, hi) for w in WEEKS}))
        teams[t] = rows
    return league(teams, fas, weeks=WEEKS, last_regular=11, slots=slots)


# ---------------------------------------------------------------- independent brute-force solver --

def brute_force_starters(players: list[tuple[int, str, float]], slots: list[str]) -> tuple[float, Counter]:
    """Exhaustive search over every injective assignment of players to slots (a slot stays empty only when no unused
    player fits it: projections are non-negative, so emptying a fillable slot never gains). Returns the best total and
    the number of starters per position. Written without lineup.py: its own eligibility table, no pruning."""
    elig = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "RB/WR": {"RB", "WR"}, "WR/TE": {"WR", "TE"},
            "FLEX": {"RB", "WR", "TE"}, "OP": {"QB", "RB", "WR", "TE"}}
    best = (-1.0, Counter())

    def go(i, used, total, pos_count):
        nonlocal best
        if i == len(slots):
            if total > best[0] + 1e-12:
                best = (total, Counter(pos_count))
            return
        fits = [(pid, pos, v) for pid, pos, v in players if pid not in used and pos in elig[slots[i]]]
        if not fits:
            go(i + 1, used, total, pos_count)                      # nobody left for this slot
        for pid, pos, v in fits:
            pos_count[pos] += 1
            go(i + 1, used | {pid}, total + v, pos_count)
            pos_count[pos] -= 1

    go(0, frozenset(), 0.0, Counter())
    return best


def week_players(st, roster, w):
    return [(p.espn_player_id, p.position, projection(st.projections, p.espn_player_id, w)) for p in roster
            if projection(st.projections, p.espn_player_id, w) is not None and not p.on_ir]


@pytest.mark.parametrize("name", list(FIXTURES))
@pytest.mark.parametrize("seed", [1, 2])
def test_reference_matches_an_independent_brute_force_solver(name, seed):
    slots = FIXTURES[name]
    st = random_league(slots, seed=seed)
    board = AssetBoard(st, st.window())
    fixed = Counter(s for s in slots if s in FIXED_SLOTS)
    for w in WEEKS:
        expect = Counter()
        for t in sorted(st.rosters):
            total, starters = brute_force_starters(week_players(st, st.rosters[t], w), slots)
            # every fixed slot is filled in these fixtures, so a position's flex starters are its starters beyond them
            assert all(starters[pos] >= n for pos, n in fixed.items())
            expect.update({pos: starters[pos] - fixed[pos] for pos in starters if starters[pos] > fixed[pos]})
            canon = next(x for x in board.lineups[t]["weeks"] if x["week"] == w)
            assert canon["_total"] == pytest.approx(total, abs=1e-9)       # the reference lineup is the true optimum
        teams = len(st.rosters)
        got = {pos: k - teams * fixed.get(pos, 0) for pos, k in board.reference["weeks"][w].items()}
        assert {p: k for p, k in got.items() if k} == dict(expect), (name, w)


@pytest.mark.parametrize("name", list(FIXTURES))
def test_reference_is_lineup_assign_lineup_over_each_teams_roster(name):
    slots = FIXTURES[name]
    st = random_league(slots, n_teams=4, seed=5)
    board = AssetBoard(st, st.window())
    for w in WEEKS:
        flex = Counter()
        for t in sorted(st.rosters):
            pool = [{"espn_player_id": pid, "position": pos, "value": v}
                    for pid, pos, v in week_players(st, st.rosters[t], w)]
            rows = assign_lineup(pool, slots)
            flex.update(a["position"] for a in rows if a["is_flex"] and a["player_id"] is not None)
            canon = next(x for x in board.lineups[t]["weeks"] if x["week"] == w)
            pairs = [(a["slot"], a["player_id"]) for a in canon["assignment"]]
            assert [(a["slot"], a["player_id"]) for a in rows] == pairs
        fixed = Counter(s for s in slots if s in FIXED_SLOTS)
        assert board.reference["weeks"][w] == {pos: 4 * fixed.get(pos, 0) + flex.get(pos, 0)
                                               for pos in sorted(set(fixed) | set(flex))}


@pytest.mark.parametrize("name", list(FIXTURES))
def test_each_flex_slot_is_counted_once_for_one_position(name):
    slots = FIXTURES[name]
    st = random_league(slots, n_teams=4, seed=9)
    board = AssetBoard(st, st.window())
    ref = board.reference
    fixed = Counter(s for s in slots if s in FIXED_SLOTS)
    n_flex = sum(1 for s in slots if s in FLEX_ELIGIBILITY)
    assert ref["flex_slots"] == 4 * n_flex
    for w in WEEKS:
        flex_total = sum(k - 4 * fixed.get(pos, 0) for pos, k in ref["weeks"][w].items())
        filled = sum(1 for t in st.rosters for x in board.lineups[t]["weeks"] if x["week"] == w
                     for a in x["assignment"] if a["is_flex"] and a["player_id"] is not None)
        assert flex_total == filled == ref["flex_slots"]           # these rosters fill every flex slot
        assert sum(ref["weeks"][w].values()) == 4 * len(slots)     # league starters = league starting slots
    assert sum(ref["flex_starts"].values()) == ref["flex_slots"] * len(WEEKS)
    assert sum(ref["flex_seats"].values()) == ref["flex_slots"]     # season seats never exceed the slots filled
    assert sum(board.starters.values()) == 4 * len(slots)
    for pos in ("QB",) if name not in ("superflex", "two_op") else ():
        assert board.starters[pos] == 4                            # QB only ever fills an OP slot


def test_an_empty_flex_slot_counts_for_nobody():
    teams = {1: [(101, "QB", 20), (102, "RB", 10), (103, "RB", 9), (104, "WR", 8), (105, "WR", 7), (106, "TE", 5)],
             2: [(201, "QB", 18), (202, "RB", 11), (203, "RB", 6), (204, "WR", 9), (205, "WR", 4), (206, "TE", 6),
                 (207, "QB", 15)]}
    st = league(teams, weeks=WEEKS, last_regular=11, slots=SUPERFLEX)
    board = AssetBoard(st, st.window())
    # Team 1 has nobody left for FLEX or OP; team 2 puts QB 207 in OP and leaves FLEX empty.
    assert board.reference["weeks"][10] == {"QB": 3, "RB": 4, "TE": 2, "WR": 4}
    assert board.starters == {"QB": 3, "RB": 4, "TE": 2, "WR": 4}


# -------------------------------------------------------------------------- 1QB is unchanged --

def test_one_qb_league_without_flex_keeps_teams_times_fixed_slots():
    st = random_league(ONE_QB, n_teams=4, seed=3)
    board = AssetBoard(st, st.window())
    assert board.starters == {"QB": 4, "RB": 8, "TE": 4, "WR": 8}
    assert all(board.reference["weeks"][w] == board.starters for w in WEEKS)
    assert board.reference["note"] is None and board.package([101], [201])["note"] == NOTE
    # league-structure replacement (no free agents): the (teams x fixed + 1)-th best rostered projection that week
    for pos, k in board.starters.items():
        for w in WEEKS:
            vals = sorted((v for t in st.rosters for p in st.rosters[t] if p.position == pos
                           and (v := projection(st.projections, p.espn_player_id, w)) is not None), reverse=True)
            assert board.base[pos][w]["level"] == vals[k]
    assert board.base["QB"][10]["source"] == "league-structure replacement (no free agent projected)"


# ------------------------------------------------------------------------ superflex scarcity --

def superflex_league(qb2: float, n_teams: int = 4, fas=()):
    """Every team: QB1 20+, a QB2 at `qb2`, RBs 14/12/9, WRs 13/11/8, TE 6. Best remaining FLEX/OP candidate after
    the fixed slots and FLEX is the RB 9 / WR 8, so the OP takes the QB2 exactly when qb2 > 9 (the RB 9 goes to
    FLEX first: FLEX and OP are filled jointly by the exact search)."""
    teams = {}
    for t in range(1, n_teams + 1):
        b = 100 * t
        teams[t] = [(b + 1, "QB", 20 + t), (b + 2, "QB", qb2 + t / 10), (b + 3, "RB", 14), (b + 4, "RB", 12),
                    (b + 5, "RB", 9), (b + 6, "WR", 13), (b + 7, "WR", 11), (b + 8, "WR", 8), (b + 9, "TE", 6)]
    return league(teams, fas, weeks=WEEKS, last_regular=11, slots=SUPERFLEX)


def test_superflex_qb_scarcity_responds_when_qbs_fill_the_op_slots():
    strong = AssetBoard(st := superflex_league(qb2=16), st.window())
    weak = AssetBoard(sw := superflex_league(qb2=5), sw.window())
    # strong QB2s: each OP takes a QB, so the league starts 8 QBs; weak QB2s: OP takes the WR 8, 4 QBs start
    assert strong.starters["QB"] == 8 and strong.reference["flex_starts"]["QB"] == 4 * len(WEEKS)
    assert weak.starters["QB"] == 4 and "QB" not in weak.reference["flex_starts"]
    assert weak.starters["WR"] == 12 and weak.starters["RB"] == 12                 # FLEX: RB 9, OP: WR 8
    assert strong.starters["RB"] == 12 and strong.starters["WR"] == 8
    # the 5th-8th QBs (each team's QB2) are starter-caliber only when they really fill the OP slots
    qb2 = [102, 202, 302, 402]
    assert all(strong.profiles[p]["starter_caliber"] for p in qb2)
    assert all(strong.profiles[p]["league_starters_at_position"] == 8 for p in qb2)
    assert not any(weak.profiles[p]["starter_caliber"] for p in qb2)
    # no free agents: the league-structure QB level is the first QB beyond the league's QB starters that week
    assert weak.base["QB"][10]["level"] == pytest.approx(5.4)       # 5th QB: the best QB2
    assert strong.base["QB"][10]["level"] == 0.0                   # only 8 QBs rostered, all starting
    assert strong.profiles[102]["par"] > 0 and weak.profiles[102]["par"] == 0.0


def test_superflex_package_labels_and_surplus_follow_the_reference():
    st = superflex_league(qb2=16)
    st.rosters[1].append(next(p for p in st.rosters[1] if p.espn_player_id == 101).__class__(199, "QB", "KC", "QB3"))
    st.projections[199] = {w: 15.5 for w in WEEKS}                 # a third QB who never starts for team 1
    board = AssetBoard(st, st.window())
    assert board.starters["QB"] == 8
    # QB3 (15.5) is below every QB2 (16.1-16.4): the 9th QB, beyond the 8 league QB starters.
    assert board.profiles[199]["position_rank"] == 9 and not board.profiles[199]["starter_caliber"]
    pkg = board.package([102, 104], [201])                          # QB2 (OP starter) + RB2 for a QB1
    assert pkg["you_send_starter_caliber"] == 2
    assert pkg["note"] == NOTE + " " + FLEX_NOTE and "dynamic" in pkg["note"]
    needs = league_needs(st, st.window(), board)
    assert needs[1]["QB"]["surplus"] == []                          # QB2 starts every week in OP: not surplus
    # team 1's QB3 overtakes its QB2 (16.1), who drops to the bench; team 4's QB2 falls to 10 but still beats its
    # WR 8 for the OP. Team 1's benched QB2 is the 8th-best QB of 8 league QB starters: starter-caliber and surplus.
    st.projections[199] = {w: 17.0 for w in WEEKS}
    st.projections[402] = {w: 10.0 for w in WEEKS}
    board = AssetBoard(st, st.window())
    assert board.starters["QB"] == 8 and board.profiles[102]["position_rank"] == 8
    assert board.profiles[102]["starter_caliber"] and board.profiles[199]["starter_caliber"]
    assert team_needs(st, st.window(), 1, board)["QB"]["surplus"] == [102]


def test_a_flex_only_position_uses_its_weekly_starters_even_when_its_season_share_rounds_to_zero():
    no_te = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "FLEX": 1})
    teams = {t: [(100 * t + 1, "QB", 20), (100 * t + 2, "RB", 14), (100 * t + 3, "RB", 12), (100 * t + 4, "RB", 5),
                 (100 * t + 5, "WR", 13), (100 * t + 6, "WR", 11), (100 * t + 7, "WR", 4), (100 * t + 8, "TE", 3)]
             for t in range(1, 5)}
    teams[1][-1] = (108, "TE", {10: 9, 11: 3, 12: 3})                # team 1's TE takes FLEX in week 10 only
    st = league(teams, weeks=WEEKS, last_regular=11, slots=no_te)
    board = AssetBoard(st, st.window())
    assert board.reference["flex_starts"] == {"RB": 11, "TE": 1} and "TE" not in board.starters
    assert board.reference["weeks"][10]["TE"] == 1
    assert board.base["TE"][10]["level"] == 3.0                    # the 2nd-best TE, not the (teams + 1)-th
    assert board.base["TE"][11]["level"] == 3.0                    # no TE starts: the best TE is the level


def test_incomplete_team_weeks_make_the_flex_counts_a_caveat_not_exact_demand():
    st = superflex_league(qb2=16)
    st.rosters[4].append(next(iter(st.rosters[4])).__class__(499, "WR", "KC", "WR unprojected"))
    board = AssetBoard(st, st.window())
    assert board.reference["incomplete_team_weeks"] == len(WEEKS)
    pkg = board.package([102], [201])
    assert pkg["label"] != "insufficient_data"
    assert any("known projections only for 3 team-week(s)" in c for c in pkg["caveats"])
    complete = AssetBoard(sc := superflex_league(qb2=16), sc.window())
    assert complete.reference["incomplete_team_weeks"] == 0
    assert not any("known projections" in c for c in complete.package([102], [201])["caveats"])


def test_two_op_league_counts_both_op_slots_by_their_actual_occupants():
    teams = {}
    for t in (1, 2, 3):
        b = 100 * t
        teams[t] = [(b + 1, "QB", 22), (b + 2, "QB", 17), (b + 3, "QB", 9 + t), (b + 4, "RB", 14), (b + 5, "RB", 12),
                    (b + 6, "RB", 11.5), (b + 7, "WR", 13), (b + 8, "WR", 11), (b + 9, "TE", 6)]
    st = league(teams, weeks=WEEKS, last_regular=11, slots=TWO_OP)
    board = AssetBoard(st, st.window())
    # QB2 (17) always takes an OP; the second OP goes to QB3 (12) on team 3 but to the RB 11.5 on teams 1 and 2
    assert board.reference["weeks"][10] == {"QB": 7, "RB": 8, "TE": 3, "WR": 6}
    assert board.starters["QB"] == 7 and board.starters["RB"] == 8
    assert board.profiles[303]["starter_caliber"] and not board.profiles[203]["starter_caliber"]


def test_three_flex_and_mixed_flex_counts():
    teams = {}
    for t in (1, 2):
        b = 100 * t
        teams[t] = [(b + 1, "QB", 20), (b + 2, "RB", 15), (b + 3, "RB", 13), (b + 4, "RB", 12), (b + 5, "RB", 7),
                    (b + 6, "WR", 14), (b + 7, "WR", 11), (b + 8, "WR", 10), (b + 9, "WR", 6), (b + 10, "TE", 9),
                    (b + 11, "TE", 8)]
    three = league(teams, weeks=WEEKS, last_regular=11, slots=THREE_FLEX)
    b3 = AssetBoard(three, three.window())
    assert b3.reference["weeks"][10] == {"QB": 2, "RB": 6, "TE": 4, "WR": 6}     # FLEX x3: RB 12, WR 10, TE 8
    mixed = league(teams, weeks=WEEKS, last_regular=11, slots=MIXED_FLEX)
    bm = AssetBoard(mixed, mixed.window())
    # RB/WR: RB 12; WR/TE: WR 10; FLEX: TE 8 -> one each, nothing counted twice. Without a TE 8 the WR/TE would take
    # WR 10, RB/WR the RB 12 and FLEX the RB 7: two RBs, one WR, no TE.
    assert bm.reference["weeks"][10] == {"QB": 2, "RB": 6, "TE": 4, "WR": 6}
    assert sum(bm.reference["weeks"][10].values()) == 2 * len(MIXED_FLEX)
    for t in (1, 2):
        mixed.projections.pop(100 * t + 11)
    bm = AssetBoard(mixed, mixed.window())
    assert bm.reference["weeks"][10] == {"QB": 2, "RB": 8, "TE": 2, "WR": 6}


def test_flex_share_is_averaged_over_the_window_and_apportioned_without_double_counting():
    # Team 1's QB2 beats the RB 9 for the OP in weeks 10 and 11 only (2 of 3 weeks -> 0.67 per week -> 1).
    teams = {1: [(101, "QB", 22), (102, "QB", {10: 12, 11: 12, 12: 2}), (103, "RB", 14), (104, "RB", 12),
                 (105, "RB", 9), (106, "WR", 13), (107, "WR", 11), (108, "WR", 8), (109, "TE", 6)],
             2: [(201, "QB", 21), (202, "RB", 14), (203, "RB", 12), (204, "RB", 9), (205, "WR", 13), (206, "WR", 11),
                 (207, "WR", 8), (208, "TE", 6)]}
    st = league(teams, weeks=WEEKS, last_regular=11, slots=SUPERFLEX)
    board = AssetBoard(st, st.window())
    assert [board.reference["weeks"][w]["QB"] for w in WEEKS] == [3, 3, 2]
    assert board.reference["flex_starts"]["QB"] == 2 and board.starters["QB"] == 3
    st.projections[102] = {10: 12, 11: 2, 12: 2}                   # 1 of 3 weeks -> 0.33 -> 0
    assert AssetBoard(st, st.window()).starters["QB"] == 2
    ref = league_reference({1: {"weeks": [{"week": 1, "assignment": [
        {"slot": "OP", "player_id": 1, "position": "QB", "is_flex": True}]}, {"week": 2, "assignment": [
        {"slot": "OP", "player_id": 2, "position": "RB", "is_flex": True}]}]}}, ["OP"], [1, 2])
    # one OP: a QB in week 1, an RB in week 2. One flex starter per week, so one season seat (not one each): the
    # tie at exactly half goes to the first position by name.
    assert ref["flex_seats"] == {"QB": 1, "RB": 0} and ref["starters"] == {"QB": 1}


# ------------------------------------------------------------------- determinism and consistency --

def test_reference_is_deterministic_under_roster_and_team_reordering():
    st = random_league(MIXED_FLEX, n_teams=4, seed=21)
    a = AssetBoard(st, st.window())
    shuffled = random_league(MIXED_FLEX, n_teams=4, seed=21)
    rng = random.Random(4)
    order = list(shuffled.rosters)
    rng.shuffle(order)
    shuffled.rosters = {t: rng.sample(shuffled.rosters[t], len(shuffled.rosters[t])) for t in order}
    b = AssetBoard(shuffled, shuffled.window())
    assert a.reference == b.reference and a.starters == b.starters
    assert a.profiles == b.profiles


def test_board_lineups_are_the_canonical_lineups_and_needs_reuse_them():
    st = random_league(SUPERFLEX, n_teams=3, seed=8)
    board = AssetBoard(st, st.window())
    for t in st.rosters:
        assert board.lineups[t] == project_team(st.rosters[t], st.slots, st.window(), st.projections)
        fresh = team_needs(st, st.window(), t, board, team=project_team(st.rosters[t], st.slots, st.window(),
                                                                         st.projections))
        assert team_needs(st, st.window(), t, board) == fresh


def test_kdst_streamers_fill_fixed_slots_only_and_never_count_as_flex():
    from app.analytics.trades import with_streaming
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "OP": 1, "K": 1, "D/ST": 1})
    teams = {t: [(100 * t + 1, "QB", 20), (100 * t + 2, "RB", 12), (100 * t + 3, "RB", 10), (100 * t + 4, "WR", 11),
                 (100 * t + 5, "WR", 9), (100 * t + 6, "TE", 6), (100 * t + 7, "RB", 8), (100 * t + 8, "WR", 7)]
             for t in (1, 2)}
    fas = [(900 + i, pos, 5 + i) for i, pos in enumerate(("K", "K", "K", "D/ST", "D/ST", "D/ST"))]
    st = with_streaming(league(teams, fas, weeks=WEEKS, last_regular=11, slots=slots))
    board = AssetBoard(st, st.window())
    assert any(a.get("streamer") for x in board.lineups[1]["weeks"] for a in x["assignment"])
    assert board.reference["weeks"][10] == {"D/ST": 2, "K": 2, "QB": 2, "RB": 6, "TE": 2, "WR": 6}
    assert board.reference["flex_seats"] == {"RB": 2, "WR": 2}

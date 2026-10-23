"""V2 trade discovery: candidate generation, provable screening, exact best-first ranking, categories,
consistency with the manual evaluator, safeguards and the read-only API. Synthetic leagues only
(in memory or temporary databases); nothing touches a persistent database."""

import copy
import os
import random
import time
from dataclasses import replace
from itertools import combinations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import IR_HOLD_NOTE, RosterPlayer
from app.analytics.trade_discovery import (CATEGORIES, SHAPES, SORTS, TradeSearch, _key, _rank_key, _same_effects,
                                          detail)
from app.analytics.trades import LeagueState, NoRemainingWeeksError, TradeError, evaluate_trade, side_lineups
from app.db.connection import connect, fetch_dicts

from synthetic_league import SMALL_ROSTER, synthetic_league
from test_trade_data import _raw_snapshot, synced

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WEEKS = (10, 11, 12)                 # 10-11 regular season, 12 playoffs
KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta", "average_weekly_delta",
        "weeks_improved", "weeks_worsened", "weeks_unchanged", "weeks_incomplete", "projection_complete")


def P(pid, pos, team="KC", ir=False):
    return RosterPlayer(pid, pos, team, f"P{pid}", on_ir=ir)


def flat(v):
    return {w: v for w in WEEKS}


def league(rosters: dict[int, list[tuple]], capacity=20, limits=None, start_week=10) -> LeagueState:
    """rosters: team -> [(pid, pos, projection (number = flat, dict = by week, None = missing), ir?)]."""
    players, proj = {}, {}
    for team, items in rosters.items():
        players[team] = []
        for item in items:
            pid, pos, v, *rest = item
            players[team].append(P(pid, pos, ir=bool(rest and rest[0])))
            if v is not None:
                proj[pid] = flat(v) if isinstance(v, (int, float)) else dict(v)
    return LeagueState(season=2026, slots=SLOTS, final_week=12, last_regular=11, start_week=start_week,
                       rosters=players, free_agents=[], projections=proj, active_capacity=capacity,
                       position_limits=limits or {}, team_names={t: f"T{t}" for t in rosters},
                       first_projection_week=10, players_missing={t: 0 for t in players})


def two_teams(extra_a=(), extra_b=(), **kw):
    """A (1): RB surplus (RB 12 benched), weak WR2 (2). B (2): WR surplus (WR 13 benched), weak RB2 (1)."""
    a = [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14), (104, "RB", 13), (105, "RB", 12),
         (106, "WR", 8), (107, "WR", 2), (108, "TE", 6), *extra_a]
    b = [(201, "QB", 18), (202, "RB", 6), (203, "RB", 1), (204, "WR", 16), (205, "WR", 15), (206, "WR", 14),
         (207, "WR", 13), (208, "TE", 7), *extra_b]
    return league({1: a, 2: b}, **kw)


def TS(*args, **kw):
    """The V2 exact engine over every position (the V3 discovery default leaves out K and D/ST)."""
    kw.setdefault("positions", None)
    return TradeSearch(*args, **kw)


def find(rows, sends, receives):
    return next((r for r in rows if r["sends"] == list(sends) and r["receives"] == list(receives)), None)


def all_rows(ts: TradeSearch, sort="my_gain", category=None):
    """Every qualifying candidate, exactly evaluated (no top-K cut), in ranking order."""
    rows = [row for c in ts.cands if (row := ts.evaluate(c)) and (category is None or category in row["categories"])]
    return sorted(rows, key=_rank_key(sort))


def every_candidate(state: LeagueState, anchor: int, shapes=tuple(SHAPES)):
    for opp in sorted(t for t in state.rosters if t != anchor):
        mine = sorted(p.espn_player_id for p in state.rosters[anchor])
        theirs = sorted(p.espn_player_id for p in state.rosters[opp])
        if "1-for-1" in shapes:
            yield from (("1-for-1", opp, (s,), (t,)) for s in mine for t in theirs)
        if "2-for-1" in shapes:
            yield from (("2-for-1", opp, ss, (t,)) for ss in combinations(mine, 2) for t in theirs)
        if "1-for-2" in shapes:
            yield from (("1-for-2", opp, (s,), tt) for s in mine for tt in combinations(theirs, 2))


# --------------------------------------------------------------------- core semantics --

def test_genuinely_mutual_one_for_one():
    st = two_teams()
    ts = TS(st, 1)
    rows = all_rows(ts)
    r = find(rows, [105], [207])               # A's benched RB 12 for B's benched WR 13
    assert r["mine"]["ros_delta"] == 33.0 and r["theirs"]["ros_delta"] == 33.0   # +11/week each
    assert "MUTUAL_PROJECTED_IMPROVEMENT" in r["categories"] and r["min_side_ros_delta"] == 33.0
    res = evaluate_trade(st, 1, 2, [105], [207])
    assert res["team_a"]["deltas"]["ros_delta"] == 33.0 and res["result"]["label"] == "MUTUAL_PROJECTED_GAIN"
    top = ts.top("min_side", "MUTUAL_PROJECTED_IMPROVEMENT", limit=1)["results"][0]
    assert top["min_side_ros_delta"] >= 33.0


def test_trade_that_helps_me_and_costs_them():
    st = two_teams()
    r = find(all_rows(TS(st, 1)), [107], [204])     # my weak WR 2 for their WR1 16
    assert r["mine"]["ros_delta"] > 0 > r["theirs"]["ros_delta"]
    assert r["categories"][0] == "IMPROVES_MINE_COSTS_THEIRS"
    assert r["combined_ros_delta"] == r["mine"]["ros_delta"] + r["theirs"]["ros_delta"]


def test_consolidation_where_second_incoming_never_starts_and_forced_drop_cost():
    st = two_teams(capacity=8)                               # A is full: 1-for-2 forces a drop
    ts = TS(st, 1)
    r = find(all_rows(ts), [107], [203, 206])                # get WR 14 + RB 1 (RB 1 never starts for A)
    assert r is not None and r["forced_drops"]["mine"]
    d = detail(st, r)
    assert d["consistent_with_manual_evaluator"]
    usage = {u["espn_player_id"]: u["weeks_started"] for u in d["mine_detail"]["incoming_usage"]}
    assert usage.get(203, []) == []                          # never starts (or is the forced drop)
    assert d["mine_detail"]["forced_drops"][0]["espn_player_id"] in (203, 105)
    assert d["mine_detail"]["forced_drop_lineup_cost"] == 0.0
    cons = find(all_rows(ts, category="CONSOLIDATION"), [107], [203, 206])
    assert cons is None or "CONSOLIDATION" in cons["categories"]
    two_for_one = [x for x in all_rows(ts) if x["shape"] == "2-for-1" and x["mine"]["ros_delta"] > 0]
    assert all("CONSOLIDATION" in x["categories"] for x in two_for_one)   # I consolidate and my lineup gains


def test_flex_and_position_scarcity_drive_value():
    # A has no RB/WR/TE left for FLEX (FLEX empty) and a weak WR2 (2). A TE 8 starts at TE and pushes
    # TE 6 into FLEX (+8/week); a WR 7 replaces nothing at TE and only lifts WR2/FLEX (+7/week).
    st = league({1: [(101, "QB", 20), (102, "RB", 10), (103, "RB", 9), (104, "WR", 8), (105, "WR", 2),
                     (106, "TE", 6), (107, "QB", 15)],
                 2: [(201, "QB", 18), (202, "TE", 9), (203, "TE", 8), (204, "WR", 7), (205, "RB", 5),
                     (206, "RB", 4), (207, "WR", 3)]})
    rows = all_rows(TS(st, 1))
    te, wr = find(rows, [107], [203]), find(rows, [107], [204])
    assert te["mine"]["ros_delta"] == 24.0 and wr["mine"]["ros_delta"] == 21.0
    assert te["theirs"]["ros_delta"] == -24.0              # B loses its FLEX TE and has no one to replace him
    for r in rows:
        res = evaluate_trade(st, 1, r["opponent_team_id"], r["sends"], r["receives"])
        assert res["team_a"]["deltas"]["ros_delta"] == r["mine"]["ros_delta"]
        assert res["team_b"]["deltas"]["ros_delta"] == r["theirs"]["ros_delta"]


def test_bye_week_zero_is_real_and_counted():
    bye = {10: 14.0, 11: 0.0, 12: 14.0}                     # incoming WR on bye in week 11
    st = two_teams(extra_b=[(209, "WR", bye)])
    r = find(all_rows(TS(st, 1)), [105], [209])
    assert r["mine"]["projection_complete"] and r["mine"]["weeks_incomplete"] == []
    assert r["mine"]["ros_delta"] == 24.0                   # +12, 0 (bye: WR 2 starts as before), +12
    assert (r["mine"]["weeks_improved"], r["mine"]["weeks_unchanged"]) == (2, 1)


def test_playoff_versus_regular_season_split_and_categories():
    late = {10: 0.0, 11: 0.0, 12: 30.0}                     # projected only for the playoff week
    early = {10: 30.0, 11: 30.0, 12: 0.0}
    st = two_teams(extra_b=[(209, "WR", late), (210, "WR", early)])
    rows = all_rows(TS(st, 1))
    po = find(rows, [105], [209])
    assert po["mine"]["regular_season_delta"] == 0.0 and po["mine"]["playoff_delta"] == 28.0
    assert "PLAYOFF_FOCUSED" in po["categories"]
    reg = find(rows, [105], [210])
    assert reg["mine"]["regular_season_delta"] == 56.0 and reg["mine"]["playoff_delta"] == 0.0
    assert "REGULAR_SEASON_FOCUSED" in reg["categories"]
    top_po = TS(st, 1).top("playoff_gain", limit=1)["results"][0]
    assert top_po["mine"]["playoff_delta"] >= 28.0


def test_ir_players_follow_evaluator_rules_and_sensitivity_stays_separate():
    st = two_teams(extra_a=[(109, "WR", 30, True)], extra_b=[(209, "RB", 25, True)])
    ts = TS(st, 1)
    rows = all_rows(ts)
    # My IR WR 30 never counts for me; sending him costs my canonical lineup nothing.
    send_ir = find(rows, [109], [207])
    assert send_ir["mine"]["ros_delta"] == 33.0
    # Their IR RB 25 arrives ACTIVE for me (evaluator rule) and starts.
    get_ir = find(rows, [105], [209])
    assert get_ir["mine"]["ros_delta"] == evaluate_trade(st, 1, 2, [105], [209])["team_a"]["deltas"]["ros_delta"]
    d = detail(st, get_ir)
    assert d["consistent_with_manual_evaluator"] and d["ir_assumption"] == IR_HOLD_NOTE
    assert [p["espn_player_id"] for p in d["mine_detail"]["ir_activation_sensitivity"]["players"]] == [109]
    assert d["mine"]["ros_delta"] == get_ir["mine"]["ros_delta"]     # upside is never in the delta


def test_missing_projection_is_never_zero_and_is_counted():
    # My rostered WR with an NFL team has no week-11 projection: my "before" week 11 is incomplete, so
    # no trade has a canonical delta for me (missing is never 0). Nothing is shown, the reason is counted.
    st = two_teams(extra_a=[(109, "WR", {10: 5.0, 12: 5.0})])
    out = TS(st, 1).top("my_gain", limit=200)
    assert out["results"] == [] and out["search"]["excluded_missing_data"] > 0
    assert evaluate_trade(st, 1, 2, [109], [207])["team_a"]["deltas"]["ros_delta"] is None
    # Their missing projection leaves my side canonical: the trade qualifies, theirs is incomplete.
    st = two_teams(extra_b=[(209, "WR", {10: 5.0, 12: 5.0})])
    rows = TS(st, 1).top("their_gain", limit=200)["results"]
    r = find(rows, [105], [207])
    assert r["mine"]["ros_delta"] == 33.0 and r["theirs"]["ros_delta"] is None
    assert r["theirs"]["weeks_incomplete"] == [11] and "MUTUAL_PROJECTED_IMPROVEMENT" not in r["categories"]
    complete = [x for x in rows if x["theirs"]["ros_delta"] is not None]
    assert rows[:len(complete)] == complete                  # missing values sort last, never as 0
    assert find(rows, [105], [209]) is None                  # receiving him would make MY side incomplete
    assert evaluate_trade(st, 1, 2, [105], [209])["team_a"]["deltas"]["ros_delta"] is None


def test_no_remaining_scoring_weeks():
    st = replace(two_teams(), start_week=None, start_week_reason={"reason": "season over"})
    with pytest.raises(NoRemainingWeeksError, match="season over"):
        TS(st, 1)


def test_empty_candidate_results_are_not_manufactured():
    st = league({1: [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14), (104, "WR", 12), (105, "WR", 11),
                     (106, "TE", 9), (107, "RB", 10)],
                 2: [(201, "QB", 5), (202, "RB", 2), (203, "RB", 1), (204, "WR", 3), (205, "WR", 2),
                     (206, "TE", 1)]})
    out = TS(st, 1).top("my_gain")
    assert out["results"] == [] and out["search"]["exhaustive"] is True
    assert out["search"]["screened_out_by_upper_bound"] == out["search"]["candidates_considered"]


def test_multiple_opposing_teams_and_opponent_filter():
    st = synthetic_league(n_teams=4, weeks=range(12, 18), roster=SMALL_ROSTER, seed=3)
    ts = TS(st, 1)
    assert ts.opponents == [2, 3, 4]
    for opp in (2, 3, 4):
        rows = ts.top("my_gain", opponent=opp, limit=10)["results"]
        assert rows and all(r["opponent_team_id"] == opp and r["anchor_team_id"] == 1 for r in rows)
        roster = {p.espn_player_id for p in st.rosters[opp]}
        mine = {p.espn_player_id for p in st.rosters[1]}
        assert all(set(r["receives"]) <= roster and set(r["sends"]) <= mine for r in rows)
    both = TS(st, 1, opponents=[3, 4])
    assert both.opponents == [3, 4]


def test_rankings_are_deterministic_and_order_independent():
    st = synthetic_league(n_teams=4, weeks=range(12, 18), roster=SMALL_ROSTER, seed=5)
    shuffled = copy.deepcopy(st)
    rng = random.Random(1)
    for t in shuffled.rosters:
        rng.shuffle(shuffled.rosters[t])
    for sort in SORTS:
        a = [r["key"] for r in TS(st, 1).top(sort, limit=30)["results"]]
        b = [r["key"] for r in TS(shuffled, 1).top(sort, limit=30)["results"]]
        assert a == b and len(a) == 30
        rows = TS(st, 1).top(sort, limit=30)["results"]
        assert rows == sorted(rows, key=_rank_key(sort))


# ------------------------------------------------------- screening proof + exactness --

@pytest.fixture(scope="module")
def small():
    st = synthetic_league(n_teams=3, weeks=range(12, 18), roster=SMALL_ROSTER, seed=11)
    ts = TS(st, 1)
    exact, invalid = {}, 0
    for c in every_candidate(st, 1):
        try:
            exact[_key(*c)] = (c, ts._side(1, c[2], c[3]), ts._side(c[1], c[3], c[2]))
        except TradeError:
            invalid += 1
    assert len(exact) > 2000 and invalid == 0
    return st, ts, exact


def test_screening_never_drops_a_trade_that_helps_me(small):
    st, ts, exact = small
    survivors = {_key(*c[:4]): c for c in ts.cands}
    assert len(exact) == len(survivors) + ts.screened_out
    for key, (c, mine, theirs) in exact.items():
        if key not in survivors:
            assert mine["known_ros_delta"] <= 0 and (mine["playoff_delta"] is None or mine["playoff_delta"] <= 0), key
            continue
        ub_me, ub_them = survivors[key][4], survivors[key][5]
        assert mine["known_ros_delta"] <= round(ub_me[0], 2) + 0.01 and theirs["known_ros_delta"] <= round(ub_them[0], 2) + 0.01
        for side, ub in ((mine, ub_me), (theirs, ub_them)):
            if side["regular_season_delta"] is not None:
                assert side["regular_season_delta"] <= ub[1] + 0.01 and side["playoff_delta"] <= ub[2] + 0.01


def test_best_first_top_k_equals_brute_force(small):
    st, ts, exact = small
    rows = [ts._row(c[0], c[1], c[2], c[3], dict(m), dict(t)) for c, m, t in exact.values()
            if m["ros_delta"] is not None and (m["ros_delta"] > 0 or (m["playoff_delta"] or 0) > 0)]
    for sort in SORTS:
        for category in (None, *CATEGORIES):
            brute = sorted((r for r in rows if category is None or category in r["categories"]), key=_rank_key(sort))[:15]
            got = ts.top(sort, category, limit=15, include_redundant=True)
            assert [r["key"] for r in got["results"]] == [r["key"] for r in brute], (sort, category)
            assert got["search"]["exhaustive"]


def test_discovery_deltas_equal_the_manual_evaluator(small):
    st, ts, exact = small
    for i, (key, (c, mine, theirs)) in enumerate(sorted(exact.items())):
        if i % 9:
            continue                                         # a deterministic ninth of every candidate
        try:
            m = side_lineups(st, ts.window, 1, list(c[2]), [ts.players[x] for x in c[3]])
            o = side_lineups(st, ts.window, c[1], list(c[3]), [ts.players[x] for x in c[2]])
        except TradeError:
            continue
        assert {k: m["compare"][k] for k in KEYS} == {k: mine[k] for k in KEYS}, key
        assert {k: o["compare"][k] for k in KEYS} == {k: theirs[k] for k in KEYS}, key
        assert [d["espn_player_id"] for d in m["resolution"]["forced_drops"]] == mine["forced_drops"]
        assert [d["espn_player_id"] for d in o["resolution"]["forced_drops"]] == theirs["forced_drops"]
    for r in ts.top("min_side", limit=10)["results"]:
        assert detail(st, r)["consistent_with_manual_evaluator"]


def test_mixed_flex_types_and_position_limits_use_the_exact_rules():
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "RB/WR": 1, "WR/TE": 1, "K": 1, "D/ST": 1})
    st = synthetic_league(n_teams=2, weeks=range(14, 18), roster=SMALL_ROSTER, slots=slots, seed=2)
    st = replace(st, position_limits={"RB": 4, "QB": 1})
    ts = TS(st, 1)
    assert ts.T.general
    for c in every_candidate(st, 1):
        try:
            m = side_lineups(st, ts.window, 1, list(c[2]), [ts.players[x] for x in c[3]])
        except TradeError:
            with pytest.raises(TradeError):
                ts._side(1, c[2], c[3])
            continue
        mine = ts._side(1, c[2], c[3])
        assert {k: m["compare"][k] for k in KEYS} == {k: mine[k] for k in KEYS}, c
        assert [d["espn_player_id"] for d in m["resolution"]["forced_drops"]] == mine["forced_drops"]


def test_negative_projection_disables_screening_and_early_stopping():
    st = two_teams(extra_b=[(209, "WR", -1.0)])
    ts = TS(st, 1)
    assert ts.T.negative and ts.screened_out == 0
    for sort in SORTS:
        out = ts.top(sort, limit=3, include_redundant=True)
        s = out["search"]
        assert s["bounds_valid"] is False and s["exhaustive"] is True
        assert s["fully_evaluated"] == s["matching_filters"] and s["not_evaluated"] == 0   # bounds never end it
        assert [r["key"] for r in out["results"]] == [r["key"] for r in all_rows(ts, sort)[:3]]
    capped = TS(st, 1, max_exact=4).top("my_gain")["search"]
    assert capped["exhaustive"] is False and capped["termination"] == "evaluation_cap"
    assert capped["largest_unevaluated_upper_bound"] is None          # an invalid bound proves nothing
    assert "not a proven top list" in capped["not_evaluated_reason"]


def test_negative_projection_where_the_best_trade_has_an_invalid_lower_bound():
    """K -5 and D/ST -4 are forced into their slots, so dumping them raises my total (lineup totals are not
    monotone with negative values). The best trade (K + D/ST for WR 25: +31/week) has a stage-A "bound"
    of +26/week, below the exact +27/week of K-for-WR. Ending the search on that bound returned K-for-WR."""
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1})
    a = [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14), (104, "WR", 10), (105, "WR", 9), (106, "TE", 6),
         (107, "K", -5), (108, "D/ST", -4), (109, "WR", 3)]
    b = [(201, "QB", 18), (202, "RB", 12), (203, "RB", 11), (204, "WR", 25), (205, "WR", 8), (206, "TE", 7),
         (207, "K", 8), (208, "D/ST", 7), (209, "WR", 2)]
    st = replace(league({1: a, 2: b}), slots=slots)
    ts = TS(st, 1, shapes=("1-for-1", "2-for-1"))
    assert ts.T.negative and ts.screened_out == 0
    best = next(c for c in ts.cands if c[:4] == ("2-for-1", 2, (107, 108), (204,)))
    k_for_wr = next(c for c in ts.cands if c[:4] == ("1-for-1", 2, (107,), (204,)))
    exact_best = ts.evaluate(best)["mine"]["ros_delta"]
    exact_k = ts.evaluate(k_for_wr)["mine"]["ros_delta"]
    assert exact_best == 93.0 and exact_k == 81.0
    assert best[4][0] == 78.0 < exact_k < exact_best             # its bound is invalid (below its exact value)
    assert k_for_wr[4][0] > best[4][0]                           # and below another candidate's bound
    fresh = TS(st, 1, shapes=("1-for-1", "2-for-1"))
    top = fresh.top("my_gain", limit=1)
    assert [r["key"] for r in top["results"]] == ["2-for-1:2:107-108:204"]
    assert top["search"]["termination"] == "all_candidates_evaluated" and top["search"]["exhaustive"]
    assert top["search"]["fully_evaluated"] == top["search"]["matching_filters"] == len(fresh.cands)
    for sort in SORTS:
        for category in (None, *CATEGORIES):
            got = fresh.top(sort, category, limit=3, include_redundant=True)["results"]
            assert [r["key"] for r in got] == [r["key"] for r in all_rows(fresh, sort, category)[:3]], (sort, category)


def test_equivalents_only_hidden_when_the_simpler_trade_is_eligible():
    st = two_teams(extra_a=[(109, "TE", 0.5)])               # TE 0.5 never starts: a pure throw-in
    filler, simple = "2-for-1:2:105-109:207", "1-for-1:2:105:207"

    def keys(ts, **kw):
        return [r["key"] for r in ts.top(kw.pop("sort", "my_gain"), limit=200, **kw)["results"]]

    # Both eligible: the throw-in version is hidden by default, shown (and linked) with the toggle.
    ts = TS(st, 1)
    out = ts.top("my_gain", limit=200)
    assert simple in keys(ts) and filler not in keys(ts) and out["search"]["simpler_equivalents_hidden"] > 0
    shown = ts.top("my_gain", limit=200, include_redundant=True)["results"]
    assert next(r for r in shown if r["key"] == filler)["simpler_equivalent"] == simple
    # Same opponent filter and a category both trades belong to: still hidden.
    assert filler not in keys(ts, opponent=2)
    assert filler not in keys(ts, category="MUTUAL_PROJECTED_IMPROVEMENT", sort="min_side")
    assert simple in keys(ts, category="MUTUAL_PROJECTED_IMPROVEMENT", sort="min_side")
    # 1-for-1 not searched: nothing simpler is eligible, so the 2-for-1 is shown.
    only = TS(st, 1, shapes=("2-for-1",))
    o = only.top("my_gain", limit=200)
    assert filler in [r["key"] for r in o["results"]] and o["search"]["simpler_equivalents_hidden"] == 0
    assert next(r for r in o["results"] if r["key"] == filler)["simpler_equivalent"] is None
    # CONSOLIDATION: a 1-for-1 can never be in it, so the qualifying 2-for-1 is shown.
    cons = ts.top("my_gain", "CONSOLIDATION", limit=200)
    assert filler in [r["key"] for r in cons["results"]] and cons["search"]["simpler_equivalents_hidden"] == 0
    assert all("CONSOLIDATION" in r["categories"] for r in cons["results"])


def test_hidden_equivalent_is_replaced_by_its_simpler_trade_even_under_the_cap():
    # Raising a candidate's upper bound keeps it a valid bound; it makes the throw-in trade come first,
    # and a cap of 1 then stops before the simpler trade's own turn. Hiding the throw-in must still
    # surface the simpler trade instead of losing both.
    st = two_teams(extra_a=[(109, "TE", 0.5)])
    ts = TS(st, 1, max_exact=1)
    ts.cands = [c[:4] + ((999.0, 999.0, 999.0), c[5]) if _key(*c[:4]) == "2-for-1:2:105-109:207" else c
                for c in ts.cands]
    out = ts.top("my_gain", limit=5)
    assert [r["key"] for r in out["results"]] == ["1-for-1:2:105:207"]
    assert out["search"]["fully_evaluated"] == 1 and out["search"]["simpler_equivalents_hidden"] == 1
    assert out["search"]["termination"] == "evaluation_cap" and out["search"]["exhaustive"] is False
    every = ts.top("my_gain", limit=5, include_redundant=True)["results"]
    assert [r["key"] for r in every] == ["2-for-1:2:105-109:207"]


def test_cutoff_accounts_for_rounding_of_the_kth_value():
    """ESPN projections carry more than two decimals. WR2-for-302 is +11.994 exact (shown +11.99) and has the
    higher bound, so it is evaluated first; WR2-for-301 is +11.98599 exact, also shown +11.99, and wins the
    tie-break (lower player id). Its bound 11.98599 is below the rounded K-th value 11.99, so comparing the
    bound with the rounded value ended the search one candidate too early and returned the wrong trade."""
    st = league({1: [(101, "QB", 20), (102, "RB", 10), (103, "RB", 9), (104, "WR", 8), (105, "WR", 2.0),
                     (106, "TE", 5)],
                 2: [(201, "QB", 18), (301, "WR", 5.99533), (302, "WR", 5.998)]})
    ts = TS(st, 1, shapes=("1-for-1",))
    out = ts.top("my_gain", limit=1)
    assert [r["key"] for r in out["results"]] == ["1-for-1:2:105:301"]
    assert [r["key"] for r in out["results"]] == [r["key"] for r in all_rows(ts, "my_gain")[:1]]
    a, b = (ts.evaluate(next(c for c in ts.cands if c[3] == (pid,) and c[2] == (105,))) for pid in (301, 302))
    assert a["mine"]["ros_delta"] == b["mine"]["ros_delta"] == 11.99 and a["combined_ros_delta"] == b["combined_ros_delta"]
    for sort in SORTS:
        for k in (1, 2, 3):
            got = TS(st, 1, shapes=("1-for-1",)).top(sort, limit=k, include_redundant=True)
            assert [r["key"] for r in got["results"]] == [r["key"] for r in all_rows(ts, sort)[:k]], (sort, k)


def test_equivalence_requires_identical_weekly_and_forced_drop_effects():
    # Forced drop: B is full, so the throw-in version makes B drop RB 0.2 (zero lineup cost, same deltas
    # everywhere). A different roster move is a different trade: it is shown, not hidden.
    st = two_teams(capacity=9, extra_a=[(109, "TE", 0.5)], extra_b=[(209, "RB", 0.2)])
    ts = TS(st, 1)
    keys = [r["key"] for r in ts.top("my_gain", limit=200)["results"]]
    r_ = ts.evaluate(next(c for c in ts.cands if c[:4] == ("2-for-1", 2, (105, 109), (207,))))
    s_ = ts.evaluate(next(c for c in ts.cands if c[:4] == ("1-for-1", 2, (105,), (207,))))
    assert r_["forced_drops"]["theirs"] in ([203], [209]) and s_["forced_drops"]["theirs"] == []
    assert all(r_[side][k] == s_[side][k] for side in ("mine", "theirs")
               for k in ("ros_delta", "regular_season_delta", "playoff_delta", "weekly_known_deltas"))
    assert r_["key"] in keys and s_["key"] in keys and ts.simpler_equivalent(r_) is None
    # Weekly pattern: the same aggregates with different weeks (e.g. +4 / -4 within the regular season)
    # are not equivalent either.
    same = copy.deepcopy(s_)
    moved = copy.deepcopy(s_)
    moved["theirs"]["weekly_known_deltas"] = [x + d for x, d in zip(s_["theirs"]["weekly_known_deltas"], (4.0, -4.0, 0.0))]
    assert _same_effects(s_, same) and not _same_effects(s_, moved)


def test_simpler_equivalents_are_hidden_but_counted():
    st = two_teams(extra_a=[(109, "TE", 0.5)])               # a TE who never starts: pure filler
    ts = TS(st, 1)
    shown = ts.top("my_gain", limit=200)
    every = ts.top("my_gain", limit=200, include_redundant=True)
    assert shown["search"]["simpler_equivalents_hidden"] > 0
    filler = find(every["results"], [105, 109], [207])
    assert filler and filler["simpler_equivalent"] == "1-for-1:2:105:207"
    assert find(shown["results"], [105, 109], [207]) is None


def test_bounded_search_reports_the_cap_and_is_fast_on_a_full_league():
    st = synthetic_league(n_teams=12)
    t = time.perf_counter()
    ts = TS(st, 1)
    capped = TS(st, 1, max_exact=5).top("combined")
    assert capped["search"]["exhaustive"] is False and capped["search"]["fully_evaluated"] == 5
    assert capped["search"]["termination"] == "evaluation_cap"
    assert "not a proven top list" in capped["search"]["not_evaluated_reason"]
    assert capped["search"]["largest_unevaluated_upper_bound"] is not None
    outs = {s: ts.top(s, limit=25) for s in SORTS}
    elapsed = time.perf_counter() - t
    s = outs["my_gain"]["search"]
    assert s["candidates_considered"] == 11 * (17 * 17 + 136 * 17 + 17 * 136)   # 16 active + 1 IR per team
    assert all(o["search"]["exhaustive"] and len(o["results"]) == 25 for o in outs.values())
    assert elapsed < 60, elapsed                               # ~3s locally; generous for slow CI


def test_discovery_never_mutates_the_league_state():
    st = synthetic_league(n_teams=3, weeks=range(14, 18), roster=SMALL_ROSTER)
    snap = copy.deepcopy((st.rosters, st.projections, st.free_agents, st.start_week))
    ts = TS(st, 1)
    for r in ts.top("min_side", limit=5)["results"]:
        detail(st, r)
    assert (st.rosters, st.projections, st.free_agents, st.start_week) == snap


def test_validation_errors():
    st = two_teams()
    with pytest.raises(TradeError, match="unknown team"):
        TS(st, 9)
    with pytest.raises(TradeError, match="shapes"):
        TS(st, 1, shapes=("4-for-4",))
    with pytest.raises(TradeError, match="sort"):
        TS(st, 1).top("acceptance")
    only = TS(st, 1, shapes=("1-for-1",)).top("my_gain", limit=200)["results"]
    assert {r["shape"] for r in only} == {"1-for-1"}


# ------------------------------------------------------------------------------ API --

def test_discovery_api_is_read_only_and_defaults_to_my_team(tmp_path, monkeypatch):
    db_path = tmp_path / "disc.duckdb"
    con = synced(db_path)
    raw = _raw_snapshot(con)
    derived = fetch_dicts(con, "SELECT * FROM team_ros_lineup_projections ORDER BY team_id")
    con.close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    default = api.get("/api/trades/discover").json()
    assert default["anchor"]["is_mine"] is True and default["anchor"]["team_id"] == 2   # from is_mine, not hardcoded
    assert default["query"]["mode"] == "practical" and default["query"]["include_kdst"] is False
    assert set(default["sections"]) == {"practical", "concerns", "speculative"}
    body = api.get("/api/trades/discover", params={"mode": "upside", "include_kdst": True}).json()
    assert body["results"] and body["search"]["consistency_failures"] == 0
    assert all(r["consistent_with_manual_evaluator"] for r in body["results"])
    r = body["results"][0]
    assert r["mine"]["ros_delta"] > 0 and r["manager_context"] is not None
    assert {p["espn_player_id"] for p in r["players"]} == set(r["sends"]) | set(r["receives"])
    assert r["anchor_team_id"] == 2 and r["opponent_team_id"] != 2
    assert len(body["opponents"]) == 3 and "acceptance" not in str(body["categories"]).lower()
    assert all("asset_balance" in x and "rationale" in x for x in body["results"])
    manual = api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": r["opponent_team_id"],
                                                    "team_a_sends": r["sends"], "team_b_sends": r["receives"]}).json()
    assert manual["team_a"]["deltas"]["ros_delta"] == r["mine"]["ros_delta"]
    assert manual["team_b"]["deltas"]["ros_delta"] == r["theirs"]["ros_delta"]

    other = api.get("/api/trades/discover", params={"team_id": 1, "shapes": "1-for-1", "mode": "all",
                                                    "sort": "min_side", "limit": 5}).json()
    assert other["anchor"]["team_id"] == 1 and {x["shape"] for x in other["results"]} <= {"1-for-1"}
    assert api.get("/api/trades/discover", params={"shapes": "4-for-4"}).status_code == 422
    assert api.get("/api/trades/discover", params={"mode": "bogus"}).status_code == 422
    assert api.get("/api/trades/discover", params={"sort": "fairness"}).status_code == 422
    assert api.get("/api/trades/discover", params={"team_id": 99}).status_code == 404
    assert api.get("/api/trades/discover", params={"category": "MUTUAL_PROJECTED_IMPROVEMENT"}).status_code == 200

    assert db_path.stat().st_mtime_ns == mtime                 # nothing written
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    assert fetch_dicts(con, "SELECT * FROM team_ros_lineup_projections ORDER BY team_id") == derived
    con.close()


def _untimed(v):
    """A payload without the search's run instrumentation (wall-clock `seconds`, lineup-solve counters), which
    differs between runs of the same search."""
    if isinstance(v, dict):
        return {k: _untimed(x) for k, x in v.items() if k != "seconds" and not k.startswith("lineup_solves")}
    return [_untimed(x) for x in v] if isinstance(v, list) else v


def test_discovery_split_endpoints_match_the_full_payload(tmp_path, monkeypatch):
    """context + summary + evidence are /discover cut into pieces: same numbers, same caveats, nothing new."""
    db_path = tmp_path / "split.duckdb"
    synced(db_path).close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    from app.api.trade_discovery import EVIDENCE_FIELDS, SUMMARY_FIELDS
    api = TestClient(app)
    for params in ({}, {"mode": "upside", "include_kdst": True}, {"mode": "all", "sort": "min_side", "limit": 5},
                   {"team_id": 1, "shapes": "1-for-1,2-for-1", "opponent": 3}):
        full = api.get("/api/trades/discover", params=params).json()
        ctx = api.get("/api/trades/discover/context", params=params).json()
        summary = api.get("/api/trades/discover/summary", params=params).json()
        assert summary == api.get("/api/trades/discover/summary", params=params).json()     # memoized, stable
        for k in ("league", "anchor", "teams", "evaluation", "query", "mode_text", "modes", "phases",
                  "strictness_options", "categories", "sorts", "exact_shapes", "heuristic_shapes"):
            assert ctx[k] == full[k], k
        assert "sections" not in ctx and "results" not in ctx
        assert "results" not in summary
        for k, v in full.items():
            if k not in ("sections", "results", "names"):
                assert _untimed(summary[k]) == _untimed(v), k
        assert {k: [x["key"] for x in v] for k, v in summary["sections"].items()} == \
            {k: [x["key"] for x in v] for k, v in full["sections"].items()}
        shown = {pid for v in summary["sections"].values() for x in v for pid in x["sends"] + x["receives"]}
        assert shown <= {int(pid) for pid in summary["names"]}
        assert all(summary["names"][pid] == full["names"][pid] for pid in summary["names"])
        rows = {x["key"]: x for x in full["results"]}
        for x in (x for v in summary["sections"].values() for x in v):
            r = rows[x["key"]]
            assert {k: x[k] for k in SUMMARY_FIELDS} == {k: r[k] for k in SUMMARY_FIELDS}
            assert x["rationale"] == {"trade_type": r["rationale"]["trade_type"], "concerns": r["rationale"]["concerns"]}
            assert all(x["asset_balance"][k] == r["asset_balance"][k] for k in x["asset_balance"])
            if r["manager_context"] is None:
                assert x["manager_context"] is None
            else:
                assert x["manager_context"]["trade_history"]["completed_trades"] == r["manager_context"]["trade_history"]["completed_trades"]
            ev = api.get("/api/trades/discover/evidence", params={**params, "key": x["key"]}).json()
            assert {k: ev[k] for k in EVIDENCE_FIELDS} == {k: r[k] for k in EVIDENCE_FIELDS}
            assert ev["rationale"] == {"points": r["rationale"]["points"], "concerns": r["rationale"]["concerns"]}
            assert all(ev["asset_balance"][k] == r["asset_balance"][k] for k in ("you_send", "they_send", "caveats"))
            for side in ("mine_detail", "theirs_detail"):
                assert all(ev[side][k] == r[side][k] for k in ("team_id", "team_name", "fit_notes"))
    assert api.get("/api/trades/discover/evidence", params={"key": "nope"}).status_code == 404
    for path in ("context", "summary", "evidence"):
        extra = {"key": "x"} if path == "evidence" else {}
        assert api.get(f"/api/trades/discover/{path}", params={"shapes": "4-for-4", **extra}).status_code == 422
        assert api.get(f"/api/trades/discover/{path}", params={"mode": "bogus", **extra}).status_code == 422
        assert api.get(f"/api/trades/discover/{path}", params={"team_id": 99, **extra}).status_code == 404
    assert db_path.stat().st_mtime_ns == mtime                 # nothing written


def test_discovery_split_endpoints_with_no_remaining_weeks(tmp_path, monkeypatch):
    from test_trade_data import _final_week_state
    db_path = tmp_path / "over-split.duckdb"
    con = synced(db_path)
    _final_week_state(con, [(20, 10), (27, 24)])
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    full = api.get("/api/trades/discover")
    for path in ("context", "summary"):
        res = api.get(f"/api/trades/discover/{path}")
        assert res.status_code == 409 and res.json()["detail"] == full.json()["detail"]


def test_discovery_api_with_no_remaining_weeks(tmp_path, monkeypatch):
    from test_trade_data import _final_week_state
    db_path = tmp_path / "over.duckdb"
    con = synced(db_path)
    _final_week_state(con, [(20, 10), (27, 24)])
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    res = TestClient(app).get("/api/trades/discover")
    assert res.status_code == 409 and "No remaining fantasy scoring periods" in res.json()["detail"]


def test_scoring_modules_never_import_discovery():
    root = Path(__file__).resolve().parents[1] / "app" / "analytics"
    for f in ("player_scores.py", "waivers.py", "manager_metrics.py", "manager_scouting.py", "projections.py",
              "trades.py", "team_lineups.py", "trade_data.py", "pipeline.py"):
        assert "trade_discovery" not in (root / f).read_text(), f


# ------------------------------------------------------- real league smoke (read-only) --

REAL_DB = os.getenv("FANTASY_REAL_DB_PATH") or str(Path(__file__).resolve().parents[2] / "data" / "fantasy.duckdb")
# Opened read-only (DuckDB refuses writes on a read-only connection); synthetic players are never written to it.


@pytest.mark.skipif(not Path(REAL_DB).exists(), reason="no local ESPN database")
def test_real_league_smoke_read_only():
    from app.analytics.trade_data import load_league_state
    con = connect(Path(REAL_DB), read_only=True)
    try:
        state = load_league_state(con)
        mine = fetch_dicts(con, "SELECT team_id FROM fantasy_teams WHERE is_mine ORDER BY season DESC LIMIT 1")
        if state is None or not mine or state.start_week is None:
            pytest.skip("league window unavailable or season over")
        ts = TS(state, mine[0]["team_id"])
        out = ts.top("min_side", limit=10)
        for r in out["results"]:
            assert detail(state, r)["consistent_with_manual_evaluator"]
    finally:
        con.close()

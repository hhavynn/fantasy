"""Trade Discovery 2.0 lenses (mutual_upgrades, fair, consolidation, playoff): each lens draws a labelled pool from
the exact search, assesses it with the dual-value contract and ranks it by verdict, Pareto front and its own
measure, without touching the exact search's guarantees or the legacy modes. Synthetic leagues only (in memory or
temporary databases); nothing here says anything about real outcomes."""

import json

import pytest
from fastapi.testclient import TestClient

from app.analytics import trade_practical as practical
from app.analytics.trade_discovery import SHAPES, TradeSearch, _categories, _rank_key
from app.analytics.trade_dual_value import CRITERIA, RANKING_POLICY, VERDICT_ORDER, VERDICTS
from app.analytics.trade_practical import LENSES, MODES, PRACTICAL_POOL, discover
from app.analytics.trades import evaluate_trade

from synthetic_league import add_free_agents, synthetic_league
from synthetic_v3 import league, market_league, filler
from test_trade_data import synced
from test_trade_discovery import SORTS, CATEGORIES, TS, small, every_candidate   # noqa: F401  (small is a fixture)

HEURISTIC = ("2-for-2", "3-for-1", "1-for-3")
NEW_MODES = tuple(LENSES)                      # mutual_upgrades, fair, consolidation, playoff
LEGACY_MODES = ("practical", "mutual", "upside", "all")
ALL_SHAPES = tuple(SHAPES) + HEURISTIC


@pytest.fixture(scope="module")
def market():
    return TradeSearch(market_league(), 1, ALL_SHAPES)


@pytest.fixture(scope="module")
def outs(market):
    """Every lens (and every legacy mode) on one TradeSearch, so the later tests see a shared, warmed cache."""
    return {m: discover(market, m, limit=8) for m in (*NEW_MODES, *LEGACY_MODES)}


def shown(out):
    return [x for items in out["sections"].values() for x in items]


def measure(mode):
    if LENSES[mode]["measure"] == "asset_ratio":
        return lambda x: x["dual_value"]["asset"]["ratio"]
    return lambda x: x["dual_value"]["rank_axes"]["lineup_min_side"]


def order_key(mode, phase_sort):
    """verdict tier, then Pareto front (none last), then the lens's measure (none last), then the exact search order."""
    m, rk = measure(mode), _rank_key(phase_sort)

    def key(x):
        dv = x["dual_value"]
        f, p = dv["rank"]["front"], m(x)
        return (VERDICT_ORDER[dv["plausibility"]["verdict"]], f is None, f or 0, p is None, -(p or 0.0), rk(x))
    return key


# ------------------------------------------------------------------------ the lenses --

@pytest.mark.parametrize("mode", NEW_MODES)
@pytest.mark.parametrize("phase", ["ros", "regular", "playoff"])
def test_lens_rows_belong_to_their_sections_in_rank_order(market, mode, phase):
    out = discover(market, mode, phase=phase, limit=8)
    lens = LENSES[mode]
    eff = "playoff" if mode == "playoff" else phase
    assert list(out["sections"]) == list(lens["sections"]) and list(out["tiers"]) == list(lens["sections"])
    assert out["phase"] == eff and out["mode"] == mode
    rows = shown(out)
    assert rows
    for verdict, items in out["sections"].items():
        assert all(x["dual_value"]["plausibility"]["verdict"] == verdict == x["tier"] for x in items)
        caps = 8 if verdict in ("sensible", "sensible_with_concerns") else max(3, 8 // 2)
        assert len(items) <= caps
    sort = practical.PHASES[eff][0]
    key = order_key(mode, sort)
    assert rows == sorted(rows, key=key)                   # sections are in verdict order, rows in rank order
    for x in rows:
        assert x["dual_value"]["phase"] == eff
        assert x["dual_value"]["rank"]["front"] is None or x["dual_value"]["rank"]["front"] >= 0
    assert set(out["excluded"]) == {*lens["excluded"], "similar_variants"}


def test_fronts_are_per_verdict_and_missing_axes_have_no_front(market):
    """A row with an unknown axis has no front and sorts last within its verdict (missing is never 0)."""
    st = market_league()
    st.projections[307].pop(11)                  # a team-3 starter: their week-11 lineup total is unknown
    ts = TradeSearch(st, 1, tuple(SHAPES), opponents=[3])
    out = discover(ts, "mutual_upgrades", phase="regular", opponent=3, limit=50)
    inc = out["sections"]["incomplete"]
    assert inc and all(x["dual_value"]["plausibility"]["verdict"] == "incomplete" for x in inc)
    assert all(x["dual_value"]["rank"]["front"] is None and x["dual_value"]["rank_axes"]["lineup_min_side"] is None
               for x in inc)
    assert all(x["opponent_team_id"] == 3 and x["theirs"]["regular_season_delta"] is None for x in inc)
    for items in out["sections"].values():
        fronts = [x["dual_value"]["rank"]["front"] for x in items]
        assert fronts == sorted(fronts, key=lambda f: (f is None, f or 0))
        if items and items[0]["dual_value"]["plausibility"]["verdict"] != "incomplete":
            assert min(f for f in fronts if f is not None) == 0           # fronts restart inside every verdict


def test_mutual_upgrades_keeps_both_phase_deltas_positive_or_unknown(market):
    for phase, key in (("ros", "ros_delta"), ("regular", "regular_season_delta"), ("playoff", "playoff_delta")):
        out = discover(market, "mutual_upgrades", phase=phase, limit=50)
        for x in shown(out):
            assert (x["mine"][key] is None or x["mine"][key] > 0) and (x["theirs"][key] is None or x["theirs"][key] > 0)
    out = discover(market, "mutual_upgrades", phase="regular", limit=50)
    assert out["excluded"]["one_sided"] > 0                                  # known losses are counted, not shown


def test_fair_contains_no_significantly_imbalanced_package(market):
    out = discover(market, "fair", limit=50)
    rows = shown(out)
    assert rows and out["excluded"]["not_fair"] > 0
    board = practical.AssetBoard(market.state, market.window)
    significant = {"significant_they_give_more", "significant_you_give_more", "they_surrender_multiple_strong",
                   "you_surrender_multiple_strong"}
    for x in rows:
        assert x["dual_value"]["asset"]["imbalance"] in ("none", "moderate")
        assert board.package(x["sends"], x["receives"])["label"] not in significant        # independent recomputation
        assert x["asset_balance"]["label"] not in significant
    ratios = [x["dual_value"]["asset"]["ratio"] for x in out["sections"]["fair_no_lineup_gain"]]
    assert ratios and all(r is not None for r in ratios)
    # fair_no_lineup_gain is an asset-fair exchange whose starting lineups do not both improve
    for x in out["sections"]["fair_no_lineup_gain"]:
        assert x["dual_value"]["plausibility"]["checks"][0]["status"] == "fail"
        assert x["dual_value"]["plausibility"]["checks"][1]["status"] == "pass"


def test_playoff_rows_all_have_a_positive_playoff_delta(market):
    out = discover(market, "playoff", phase="ros", limit=50)            # the lens is the playoff phase whatever is asked
    rows = shown(out)
    assert rows and out["phase"] == "playoff" and out["lens_note"] is None
    assert all(x["mine"]["playoff_delta"] > 0 and x["dual_value"]["phase"] == "playoff" for x in rows)
    # smaller-side playoff gain is the within-front measure
    for x in rows:
        a, b = x["mine"]["playoff_delta"], x["theirs"]["playoff_delta"]
        assert x["dual_value"]["rank_axes"]["lineup_min_side"] == (None if b is None else min(a, b))
    assert "playoff" in out["lens"]["pool_rule"]


def test_consolidation_rows_are_uneven_shapes_whose_consolidating_side_gains(market):
    out = discover(market, "consolidation", limit=50)
    rows = shown(out)
    assert rows
    for x in rows:
        sent, got = len(x["sends"]), len(x["receives"])
        assert sent != got
        side = x["mine"] if got < sent else x["theirs"]                   # the side receiving fewer players
        assert side["ros_delta"] > 0 and "CONSOLIDATION" in x["categories"]
    assert {x["shape"] for x in rows} & {"2-for-1", "1-for-2"}


def test_consolidation_category_covers_uneven_heuristic_shapes():
    gain = {"ros_delta": 5.0, "regular_season_delta": 3.0, "playoff_delta": 2.0}
    loss = {"ros_delta": -1.0, "regular_season_delta": -1.0, "playoff_delta": 0.0}
    # (shape, consolidating side) for every uneven shape; even shapes never consolidate
    for shape, who in (("2-for-1", "mine"), ("3-for-1", "mine"), ("3-for-2", "mine"),
                       ("1-for-2", "theirs"), ("1-for-3", "theirs"), ("2-for-3", "theirs")):
        cats = _categories(shape, gain if who == "mine" else loss, gain if who == "theirs" else loss)
        assert "CONSOLIDATION" in cats, shape
        cats = _categories(shape, loss if who == "mine" else gain, loss if who == "theirs" else gain)
        assert "CONSOLIDATION" not in cats, shape                          # the consolidating side must gain
    for shape in ("1-for-1", "2-for-2", "3-for-3"):
        assert "CONSOLIDATION" not in _categories(shape, gain, gain), shape
    st = synthetic_league(n_teams=6, weeks=range(12, 18))
    add_free_agents(st, 40)
    ts = TradeSearch(st, 1, ("1-for-1", "3-for-1", "1-for-3", "3-for-2", "2-for-3", "2-for-2"))
    heur = ts.heuristic_top("my_gain", "CONSOLIDATION", None, 200)["results"]
    assert heur and {r["shape"] for r in heur} <= {"3-for-1", "1-for-3", "3-for-2", "2-for-3"}
    for r in heur:
        sent, got = len(r["sends"]), len(r["receives"])
        assert (r["mine"] if got < sent else r["theirs"])["ros_delta"] > 0
    # the exact shapes and their pruning are untouched: 2-for-1 / 1-for-2 only, as before
    exact = TradeSearch(st, 1, tuple(SHAPES)).top("my_gain", "CONSOLIDATION", None, 50)["results"]
    assert exact and {r["shape"] for r in exact} <= {"2-for-1", "1-for-2"}


def test_a_lopsided_mutual_deal_is_unlikely_mutual_not_sensible():
    ts = TradeSearch(market_league(), 1, tuple(SHAPES), opponents=[2])
    out = discover(ts, "mutual_upgrades", opponent=2, limit=200)        # the section cap is max(3, limit // 2)
    key = "1-for-2:2:105:204-209"                                       # "Diggs" for "Jeanty" + "Bowers"
    sec = {k: [x["key"] for x in v] for k, v in out["sections"].items()}
    assert key in sec["unlikely_mutual"]
    assert key not in sec["sensible"] + sec["sensible_with_concerns"]
    row = next(x for x in out["sections"]["unlikely_mutual"] if x["key"] == key)
    assert row["mine"]["ros_delta"] > 0 and row["theirs"]["ros_delta"] > 0                  # both lineups gain
    assert row["dual_value"]["asset"]["label"] == "they_surrender_multiple_strong"
    assert row["dual_value"]["plausibility"]["verdict_text"] == VERDICTS["unlikely_mutual"]
    assert row["dual_value"]["plausibility"]["checks"][1]["status"] == "fail"
    # the fair lens leaves it out and says so; the practical mode still excludes it
    fair = discover(ts, "fair", opponent=2, limit=200)
    assert key not in [x["key"] for x in shown(fair)] and fair["excluded"]["not_fair"] >= 1


def test_diversity_rule_counts_what_it_removes(market):
    out = discover(market, "mutual_upgrades", limit=50)
    assert out["excluded"]["similar_variants"] > 0 and "similar variants are counted" in out["diversity_rule"]
    for verdict in ("sensible", "sensible_with_concerns"):
        items = out["sections"][verdict]
        ideas, managers = {}, {}
        for x in items:
            ideas[x["opponent_team_id"], tuple(x["receives"])] = ideas.get((x["opponent_team_id"], tuple(x["receives"])), 0) + 1
            managers[x["opponent_team_id"]] = managers.get(x["opponent_team_id"], 0) + 1
        assert max(ideas.values(), default=0) <= practical.PER_IDEA and max(managers.values(), default=0) <= practical.PER_MANAGER


def test_the_playoff_lens_without_a_playoff_split_is_empty_with_a_note():
    t1 = filler(1, 100, rb=(12, 6, 5), wr=(13, 12, 11, 10))
    t2 = filler(2, 200, rb=(13, 12, 11, 10), wr=(7, 6, 3))
    st = league({1: t1, 2: t2}, capacity=12, last_regular=None)
    ts = TradeSearch(st, 1, tuple(SHAPES))
    assert ts.split is False
    out = discover(ts, "playoff", limit=10)
    assert all(items == [] for items in out["sections"].values()) and list(out["sections"]) == list(LENSES["playoff"]["sections"])
    assert "no fantasy-playoff split" in out["lens_note"] and out["lens"]["mode"] == "playoff"
    assert out["coverage"]["exact"] is None and out["pool_size"] == 0 and out["consistency_checked"] == 0
    assert discover(ts, "mutual_upgrades", limit=10)["lens_note"] is None
    # a split league whose window ends before the playoffs says so as well
    short = league({1: t1, 2: t2}, capacity=12, weeks=(10, 11), last_regular=12)
    out = discover(TradeSearch(short, 1, tuple(SHAPES)), "playoff", limit=10)
    assert shown(out) == [] and "No fantasy-playoff week remains" in out["lens_note"]


def test_every_lens_output_carries_the_policy_fields(outs):
    for mode, out in outs.items():
        assert out["lens"]["mode"] == mode and out["lens"]["label"] and out["lens"]["pool_rule"] == out["pool_rule"]
        assert out["ranking_policy"] == RANKING_POLICY and out["verdicts"] == VERDICTS and out["criteria"] == CRITERIA
        ps = out["projection_source"]
        assert ps["used"] == "espn" and ps["experimental_comparison"] is None
        assert "ifp-2" in ps["note"] and "never" in ps["note"]
    assert {m: outs[m]["lens"]["label"] for m in NEW_MODES} == {
        "mutual_upgrades": "Best mutual upgrades", "fair": "Fairer asset exchanges",
        "consolidation": "Consolidation deals", "playoff": "Playoff-focused trades"}
    assert all(m in MODES for m in NEW_MODES) and list(MODES)[:4] == list(LEGACY_MODES)[:2] + ["upside", "all"]


# --------------------------------------------------------- exact search is never touched --

def _instrumentation_free(v):
    """A search payload without wall-clock seconds, solver counters and the cache size (which grows as more
    candidates are evaluated for other views, never changing what top() returns)."""
    if isinstance(v, dict):
        return {k: _instrumentation_free(x) for k, x in v.items()
                if k not in ("seconds", "exact_evaluations_cached") and not k.startswith("lineup_solves")}
    return [_instrumentation_free(x) for x in v] if isinstance(v, list) else v


def test_lenses_leave_the_exact_search_unchanged_and_report_its_coverage():
    ts = TradeSearch(market_league(), 1, ALL_SHAPES)
    calls = [("min_side", "MUTUAL_PROJECTED_IMPROVEMENT"), ("min_side", None), ("min_side_playoff", None),
             ("min_side", "CONSOLIDATION"), ("my_gain", None), ("min_side_regular", None)]
    before = [_instrumentation_free(ts.top(s, c, None, PRACTICAL_POOL)) for s, c in calls]
    heur_before = _instrumentation_free(ts.heuristic_top("min_side", None, None, PRACTICAL_POOL))
    outs = {m: discover(ts, m, limit=10) for m in (*NEW_MODES, *LEGACY_MODES)}
    after = [_instrumentation_free(ts.top(s, c, None, PRACTICAL_POOL)) for s, c in calls]
    assert after == before
    assert _instrumentation_free(ts.heuristic_top("min_side", None, None, PRACTICAL_POOL)) == heur_before
    pools = {"mutual_upgrades": ("min_side", "MUTUAL_PROJECTED_IMPROVEMENT"), "fair": ("min_side", None),
             "consolidation": ("min_side", "CONSOLIDATION"), "playoff": ("min_side_playoff", None)}
    for mode, (sort, cat) in pools.items():
        cov = outs[mode]["coverage"]
        exact = ts.top(sort, cat, None, PRACTICAL_POOL)["search"]
        assert cov["exact"]["exhaustive"] is True and cov["exact"]["termination"] == exact["termination"]
        assert _instrumentation_free(cov["exact"]) == _instrumentation_free(exact)
        assert cov["heuristic"]["exhaustive"] is False and cov["exact_shapes"] == list(SHAPES)
        assert cov["heuristic_shapes"] == list(HEURISTIC)
    for mode in (*NEW_MODES, *LEGACY_MODES):
        assert {"exhaustive", "termination"} <= set(outs[mode]["coverage"]["exact"]), mode


def test_the_best_first_top_k_still_equals_brute_force_after_every_lens(small):
    st, ts, exact = small
    for mode in (*NEW_MODES, *LEGACY_MODES):
        for phase in ("ros", "playoff"):
            discover(ts, mode, phase=phase, limit=6)
    rows = [ts._row(c[0], c[1], c[2], c[3], dict(m), dict(t)) for c, m, t in exact.values()
            if m["ros_delta"] is not None and (m["ros_delta"] > 0 or (m["playoff_delta"] or 0) > 0)]
    for sort in SORTS:
        for category in (None, *CATEGORIES):
            brute = sorted((r for r in rows if category is None or category in r["categories"]), key=_rank_key(sort))[:15]
            got = ts.top(sort, category, limit=15, include_redundant=True)
            assert [r["key"] for r in got["results"]] == [r["key"] for r in brute], (sort, category)
            assert got["search"]["exhaustive"]


def test_detail_results_are_cached_on_the_search_and_reused(monkeypatch):
    ts = TradeSearch(market_league(), 1, ALL_SHAPES)
    calls = []
    real = practical.detail
    monkeypatch.setattr(practical, "detail", lambda state, row: calls.append(row["key"]) or real(state, row))
    first = discover(ts, "mutual_upgrades", limit=6)
    n_first = len(calls)
    assert n_first >= first["pool_size"] > 0 and len(set(calls)) == n_first            # once per pool row
    discover(ts, "mutual_upgrades", limit=6)
    discover(ts, "practical", limit=6)                                                 # same pool rows, legacy mode
    discover(ts, "fair", limit=6)                                                      # overlapping rows
    assert len(calls) < n_first + 80 and set(calls[n_first:]) .isdisjoint(set(calls[:n_first]))
    # a view is read-only: asking again changes nothing in the rows already handed out
    again = discover(ts, "mutual_upgrades", limit=6)
    assert [x["key"] for x in shown(again)] == [x["key"] for x in shown(first)]
    assert again["sections"]["sensible"][0]["dual_value"] == first["sections"]["sensible"][0]["dual_value"]


# ------------------------------------------------------------------- legacy is unchanged --

# Snapshots taken from the code path BEFORE the lenses were added (same leagues, same arguments).
LEGACY = {'market': {'all_min_side': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                             'pool_size': 12,
                             'sections': {'results': ['2-for-1:3:108-109:302', '2-for-2:3:106-107:302-305', '2-for-2:3:106-108:303-305',
                                                      '2-for-2:3:107-108:304-305', '2-for-1:3:107-109:302', '2-for-1:3:108-109:303']}},
            'mutual': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                       'pool_size': 16,
                       'sections': {'results': ['2-for-1:3:108-109:302', '2-for-2:3:106-107:302-305', '2-for-2:3:106-108:303-305',
                                                '2-for-2:3:107-108:304-305', '2-for-1:3:107-109:302', '2-for-1:3:108-109:303',
                                                '2-for-2:3:106-107:302-304', '2-for-2:3:106-108:302-305']}},
            'mutual_playoff': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                               'pool_size': 16,
                               'sections': {'results': ['2-for-1:3:108-109:302', '2-for-2:3:106-107:302-305', '2-for-2:3:106-108:303-305',
                                                        '2-for-2:3:107-108:304-305', '2-for-1:3:107-109:302', '2-for-1:3:108-109:303',
                                                        '2-for-2:3:106-107:302-304', '2-for-2:3:106-108:302-305']}},
            'practical': {'excluded': {'lopsided': 3, 'one_sided': 0, 'similar_variants': 59},
                          'pool_size': 124,
                          'sections': {'concerns': ['2-for-1:3:108-109:302', '2-for-1:3:107-109:302', '2-for-1:3:108-109:303'],
                                       'practical': ['2-for-2:3:106-107:302-305', '2-for-2:3:106-108:303-305', '2-for-2:3:107-108:304-305',
                                                     '2-for-2:3:106-107:302-304', '2-for-2:2:105-106:202-208', '1-for-1:2:105:203'],
                                       'speculative': []}},
            'practical_regular': {'excluded': {'lopsided': 3, 'one_sided': 23, 'similar_variants': 59},
                                  'pool_size': 147,
                                  'sections': {'concerns': ['2-for-1:3:108-109:302', '2-for-1:3:107-109:302', '2-for-1:3:108-109:303'],
                                               'practical': ['2-for-2:3:106-107:302-305', '2-for-2:3:106-108:303-305',
                                                             '2-for-2:3:107-108:304-305', '2-for-2:3:106-107:302-304',
                                                             '2-for-2:2:105-106:202-208', '1-for-1:2:105:203'],
                                               'speculative': []}},
            'upside': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                       'pool_size': 8,
                       'sections': {'results': ['1-for-2:2:102:202-203', '1-for-2:2:103:202-203', '1-for-2:2:108:202-203',
                                                '1-for-2:2:109:202-203']}}},
 'market_exact_consolidation': {'all_cons': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                                             'pool_size': 6,
                                             'sections': {'results': ['1-for-2:2:105:203-209', '1-for-2:2:105:204-208',
                                                                      '1-for-2:2:105:204-209', '2-for-1:2:108-109:202',
                                                                      '2-for-1:2:103-108:202', '2-for-1:2:104-108:202']}}},
 'synth6': {'mutual': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                       'pool_size': 12,
                       'sections': {'results': ['1-for-2:4:1001:1062-1068', '1-for-2:4:1001:1055-1062', '1-for-2:4:1001:1054-1062',
                                                '1-for-2:4:1001:1060-1068', '1-for-2:4:1001:1055-1060', '1-for-2:4:1001:1054-1060']}},
            'practical': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 22},
                          'pool_size': 123,
                          'sections': {'concerns': ['1-for-2:4:1001:1055-1062', '1-for-2:4:1001:1054-1062', '1-for-2:4:1001:1055-1060'],
                                       'practical': ['1-for-2:4:1001:1052-1062', '1-for-2:4:1001:1053-1062', '1-for-2:4:1001:1052-1063',
                                                     '1-for-2:4:1001:1063-1064', '2-for-2:2:1002-1006:1018-1028',
                                                     '2-for-2:2:1001-1006:1018-1028'],
                                       'speculative': ['1-for-2:4:1001:1062-1068', '1-for-2:4:1001:1060-1068',
                                                       '2-for-1:4:1001-1017:1062']}},
            'upside': {'excluded': {'lopsided': 0, 'one_sided': 0, 'similar_variants': 0},
                       'pool_size': 8,
                       'sections': {'results': ['1-for-2:3:1005:1041-1045', '1-for-2:3:1012:1041-1045', '1-for-2:3:1004:1041-1045',
                                                '1-for-2:3:1008:1041-1045']}}}}


def _legacy_run(ts, calls):
    res = {}
    for name, mode, kw in calls:
        out = discover(ts, mode, **kw)
        assert all(x["dual_value"]["plausibility"]["verdict"] in VERDICTS for x in shown(out))      # evidence only
        res[name] = {"sections": {k: [x["key"] for x in v] for k, v in out["sections"].items()},
                     "excluded": out["excluded"], "pool_size": out["pool_size"]}
    return res


def test_legacy_modes_keep_their_rows_sections_and_order():
    market_calls = [("practical", "practical", dict(limit=6)), ("practical_regular", "practical", dict(limit=6, phase="regular")),
                    ("mutual", "mutual", dict(limit=8)), ("mutual_playoff", "mutual", dict(limit=8, phase="playoff")),
                    ("upside", "upside", dict(limit=4)), ("all_min_side", "all", dict(limit=6, sort="min_side"))]
    assert _legacy_run(TradeSearch(market_league(), 1, ALL_SHAPES), market_calls) == LEGACY["market"]
    assert _legacy_run(TradeSearch(market_league(), 1, tuple(SHAPES)),
                       [("all_cons", "all", dict(limit=6, sort="my_gain", category="CONSOLIDATION"))]) \
        == LEGACY["market_exact_consolidation"]
    st = synthetic_league(n_teams=6, weeks=range(12, 18))
    add_free_agents(st, 40)
    assert _legacy_run(TradeSearch(st, 1, ALL_SHAPES), [("practical", "practical", dict(limit=6)),
                                                        ("mutual", "mutual", dict(limit=6)),
                                                        ("upside", "upside", dict(limit=4))]) == LEGACY["synth6"]


def test_legacy_modes_equal_the_exact_search_order_they_describe():
    """Independent of the snapshot: upside is the proven top by my_gain, mutual the proven top by min_side among
    mutual-improvement trades (exact shapes only, so there is no heuristic merge)."""
    ts = TradeSearch(market_league(), 1, tuple(SHAPES))
    assert [x["key"] for x in discover(ts, "upside", limit=5)["sections"]["results"]] == \
        [r["key"] for r in ts.top("my_gain", None, None, 5)["results"]]
    assert [x["key"] for x in discover(ts, "mutual", limit=7)["sections"]["results"]] == \
        [r["key"] for r in ts.top("min_side", "MUTUAL_PROJECTED_IMPROVEMENT", None, 7)["results"]]
    assert [x["key"] for x in discover(ts, "all", sort="combined", limit=7)["sections"]["results"]] == \
        [r["key"] for r in ts.top("combined", None, None, 7)["results"]]


# ------------------------------------------------------------ evidence on every shown row --

@pytest.mark.parametrize("mode", [*NEW_MODES, *LEGACY_MODES])
def test_every_shown_row_matches_the_manual_evaluator_and_its_dual_value(market, outs, mode):
    out = outs[mode]
    st = market.state
    rows = shown(out)
    assert rows and out["consistency_failures"] == 0 and out["consistency_checked"] == len(rows)
    for x in rows:
        assert x["consistent_with_manual_evaluator"] is True
        res = evaluate_trade(st, x["anchor_team_id"], x["opponent_team_id"], x["sends"], x["receives"])
        lu = x["dual_value"]["lineup"]
        for side, key in (("you", "team_a"), ("them", "team_b")):
            for k, dk in (("ros", "ros_delta"), ("regular", "regular_season_delta"), ("playoff", "playoff_delta"),
                          ("known_ros", "known_ros_delta")):
                assert lu[side]["delta"][k] == res[key]["deltas"][dk], (x["key"], side, k)
            assert lu[side]["average_weekly_delta"] == res[key]["deltas"]["average_weekly_delta"]
        assert lu["you"]["team_id"] == x["anchor_team_id"] and lu["them"]["team_id"] == x["opponent_team_id"]
        assert x["mine"]["ros_delta"] == lu["you"]["delta"]["ros"] and x["theirs"]["ros_delta"] == lu["them"]["delta"]["ros"]
        assert x["dual_value"]["projection_source"] == "espn"


def test_dual_value_is_evidence_only_the_legacy_tier_and_rationale_are_unchanged(market, outs):
    for mode in LEGACY_MODES:
        for x in shown(outs[mode]):
            assert x["tier"] in practical.TIERS or mode != "practical"      # the legacy tier is the legacy classifier's
            assert "rank" not in x["dual_value"]                          # legacy rows are not re-ranked
            assert {"asset_balance", "injury_flags", "tier_reasons", "rationale", "dual_value"} <= set(x)


# ----------------------------------------------------------------- no acceptance probability --

BANNED = ("probab", "likelihood", "chance", "acceptance_")


def banned_hits(o, path=""):
    """Keys are checked strictly; string values too (a disclaimer such as 'not an acceptance estimate' contains none
    of the banned stems)."""
    hits = []
    if isinstance(o, dict):
        for k, v in o.items():
            if any(b in str(k).lower() for b in BANNED):
                hits.append(("key", f"{path}/{k}"))
            hits += banned_hits(v, f"{path}/{k}")
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            hits += banned_hits(v, f"{path}[{i}]")
    elif isinstance(o, str) and any(b in o.lower() for b in BANNED):
        hits.append(("value", f"{path}: {o[:80]}"))
    return hits


def test_the_banned_word_scan_is_not_vacuous():
    assert banned_hits({"acceptance_probability": 0.4, "ok": ["a", {"x": "a 40% chance"}]}) == [
        ("key", "/acceptance_probability"), ("value", "/ok[1]/x: a 40% chance")]
    assert banned_hits({"note": "not an acceptance estimate"}) == []


@pytest.mark.parametrize("mode", [*NEW_MODES, *LEGACY_MODES])
def test_no_lens_output_carries_an_acceptance_probability(outs, mode):
    out = json.loads(json.dumps(outs[mode], default=str))
    assert banned_hits(out) == []
    assert "acceptance" not in json.dumps([x["dual_value"]["plausibility"] for x in shown(outs[mode])]).lower().replace(
        "acceptance estimate", "")


# ---------------------------------------------------------------------- team selection --

def test_a_different_anchor_team_anchors_every_lens_on_that_team():
    st = market_league()
    for anchor in (2, 3):
        ts = TradeSearch(st, anchor, ALL_SHAPES)
        for mode in NEW_MODES:
            out = discover(ts, mode, limit=6)
            rows = shown(out)
            assert rows, (anchor, mode)
            assert all(x["anchor_team_id"] == anchor and x["opponent_team_id"] != anchor for x in rows)
            assert all(x["dual_value"]["lineup"]["you"]["team_id"] == anchor for x in rows)
            assert all(x["consistent_with_manual_evaluator"] for x in rows)
            assert {o["opponent_team_id"] for o in out["opponents"]} == set(st.rosters) - {anchor}


# ----------------------------------------------------------------------------------- API --

@pytest.fixture(scope="module")
def api(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("lenses") / "lenses.duckdb"
    synced(db_path).close()
    mp = pytest.MonkeyPatch()
    mp.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    yield TestClient(app)
    mp.undo()


SUMMARY_KEYS = {"verdict", "verdict_text", "main_concern", "front", "you_change", "them_change", "asset", "lineup", "checks"}


def test_api_default_stays_practical_and_new_modes_validate(api):
    body = api.get("/api/trades/discover/summary").json()
    assert body["query"]["mode"] == "practical" and set(body["sections"]) == {"practical", "concerns", "speculative"}
    assert set(NEW_MODES) <= set(body["modes"])
    for mode in NEW_MODES:
        assert api.get("/api/trades/discover/summary", params={"mode": mode}).status_code == 200
        assert api.get("/api/trades/discover/context", params={"mode": mode}).status_code == 200
    assert api.get("/api/trades/discover/summary", params={"mode": "no_such_lens"}).status_code == 422


@pytest.mark.parametrize("mode", [*NEW_MODES, "practical", "mutual", "upside", "all"])
def test_api_summary_rows_carry_the_documented_dual_value_summary(api, mode):
    body = api.get("/api/trades/discover/summary", params={"mode": mode}).json()
    assert body["lens"]["mode"] == mode and body["lens"]["label"] and body["lens"]["pool_rule"]
    assert body["ranking_policy"] == RANKING_POLICY and body["verdicts"] == VERDICTS and body["criteria"] == CRITERIA
    assert body["projection_source"]["used"] == "espn" and body["projection_source"]["experimental_comparison"] is None
    assert any("labelled candidate pool" in x for x in body["limitations"])
    rows = [x for v in body["sections"].values() for x in v]
    assert rows
    for x in rows:
        s = x["dual_value_summary"]
        assert set(s) == SUMMARY_KEYS and s["verdict"] in VERDICTS and s["verdict_text"]
        assert set(s["asset"]) == {"label", "text", "ratio", "imbalance", "favours", "you_send_par", "they_send_par"}
        for side in ("you", "them"):
            lu = s["lineup"][side]
            assert set(lu) == {"before", "after", "delta", "average_weekly_delta"}
            assert set(lu["before"]) == set(lu["after"]) == {"ros", "regular", "playoff"}
            assert set(lu["delta"]) == {"ros", "regular", "playoff", "known_ros"}
        assert [c["criterion"] for c in s["checks"]] == list(CRITERIA)
        assert all(set(c) == {"criterion", "status"} for c in s["checks"])
        assert s["you_change"] and s["them_change"]
        assert s["lineup"]["you"]["delta"]["ros"] == x["mine"]["ros_delta"]
        if mode in NEW_MODES:
            assert s["front"] is None or s["front"] >= 0
    if mode in NEW_MODES:
        verdicts = {k: {x["dual_value_summary"]["verdict"] for x in v} for k, v in body["sections"].items()}
        assert all(v <= {k} for k, v in verdicts.items())
    assert banned_hits(json.loads(json.dumps(body))) == []


def test_api_full_payload_passes_the_lens_fields_through(api):
    params = {"mode": "mutual_upgrades", "limit": 5}
    full = api.get("/api/trades/discover", params=params).json()
    summary = api.get("/api/trades/discover/summary", params=params).json()
    for k in ("lens", "lens_note", "ranking_policy", "verdicts", "criteria", "projection_source", "excluded", "coverage"):
        assert k in full and k in summary
    assert summary["lens"] == full["lens"] and summary["ranking_policy"] == full["ranking_policy"]
    assert all("dual_value" in x for x in full["results"])
    assert summary["coverage"]["exact"]["exhaustive"] is True and "termination" in summary["coverage"]["exact"]
    assert [x["key"] for v in summary["sections"].values() for x in v] == [x["key"] for x in full["results"]]


def test_api_evidence_has_dual_value_ratings_and_names(api):
    for mode in ("mutual_upgrades", "consolidation", "practical"):
        params = {"mode": mode, "include_kdst": True}
        body = api.get("/api/trades/discover/summary", params=params).json()
        row = next(x for v in body["sections"].values() for x in v)
        ev = api.get("/api/trades/discover/evidence", params={**params, "key": row["key"]}).json()
        dv = ev["dual_value"]
        assert dv["plausibility"]["verdict"] == row["dual_value_summary"]["verdict"]
        assert set(dv) >= {"asset", "lineup", "plausibility", "rank_axes", "phase"}
        assert ev["ratings"]["available"] is False and ev["ratings"]["reason"] and ev["ratings"]["version"] is None
        assert set(ev["ratings"]["players"]) == {str(p) for p in row["sends"] + row["receives"]}
        assert all(v is None for v in ev["ratings"]["players"].values())            # missing is never 0
        ids = {p for side in ("you", "them") for w in dv["lineup"][side]["weekly"] for p in (*w["entered"], *w["exited"])}
        assert ids and ids <= {int(k) for k in ev["names"]}
        assert all(set(v) == {"name", "position"} and v["name"] for v in ev["names"].values())
        assert banned_hits(json.loads(json.dumps(ev))) == []
    assert api.get("/api/trades/discover/evidence", params={"mode": "fair", "key": "nope"}).status_code == 404


def test_api_different_team_selection_anchors_on_that_team_in_each_new_mode(api):
    default = api.get("/api/trades/discover/summary").json()["anchor"]["team_id"]
    other = 1
    assert default != other
    for mode in NEW_MODES:
        body = api.get("/api/trades/discover/summary", params={"mode": mode, "team_id": other}).json()
        assert body["anchor"]["team_id"] == other and body["query"]["team_id"] == other
        rows = [x for v in body["sections"].values() for x in v]
        assert rows and all(x["anchor_team_id"] == other and x["opponent_team_id"] != other for x in rows)
        assert {o["opponent_team_id"] for o in body["opponents"]} == {t["team_id"] for t in body["teams"]} - {other}
        key = rows[0]["key"]
        ev = api.get("/api/trades/discover/evidence", params={"mode": mode, "team_id": other, "key": key}).json()
        assert ev["anchor_team_id"] == other and ev["dual_value"]["lineup"]["you"]["team_id"] == other
        mine = api.get("/api/trades/discover/summary", params={"mode": mode}).json()
        assert mine["anchor"]["team_id"] == default
        assert {x["key"] for v in mine["sections"].values() for x in v} != {x["key"] for x in rows}


def test_api_playoff_lens_without_a_split_returns_empty_sections_and_a_note(tmp_path, monkeypatch):
    db_path = tmp_path / "nosplit.duckdb"
    con = synced(db_path)
    con.execute("UPDATE leagues SET reg_season_matchups = NULL")          # the regular-season length is unknown
    con.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    for path in ("discover", "discover/summary"):
        body = api.get(f"/api/trades/{path}", params={"mode": "playoff"}).json()
        assert body["lens"]["mode"] == "playoff" and "no fantasy-playoff split" in body["lens_note"]
        assert list(body["sections"]) == list(LENSES["playoff"]["sections"])
        assert all(v == [] for v in body["sections"].values())
        assert body["coverage"]["exact"] is None
    other = api.get("/api/trades/discover/summary", params={"mode": "mutual_upgrades"}).json()
    assert other["lens_note"] is None


# ------------------------------------------------------------------- performance --

def test_lenses_on_a_ten_team_league_are_fast_enough_with_shared_details():
    import time
    st = synthetic_league(n_teams=10)
    add_free_agents(st, 60)
    ts = TradeSearch(st, 1, ("1-for-1", "2-for-1", "1-for-2", "2-for-2", "3-for-1", "1-for-3", "3-for-2", "2-for-3"))
    t0 = time.perf_counter()
    first = discover(ts, "mutual_upgrades", limit=10)
    cold = time.perf_counter() - t0
    assert first["pool_size"] > PRACTICAL_POOL and first["consistency_failures"] == 0
    t0 = time.perf_counter()
    for mode in ("fair", "consolidation"):
        assert discover(ts, mode, limit=10)["consistency_failures"] == 0
    warm = time.perf_counter() - t0
    assert cold < 60 and warm < 60                 # generous: the real numbers are in the report (a few seconds)

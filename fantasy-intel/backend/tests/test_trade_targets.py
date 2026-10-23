"""Targeted trade discovery (issue #182): a named player, a position, a roster need or a horizon narrows the exact
search without re-weighting it. Brute-force equivalence of the filtered best-first top K and of the focus-week bound,
the targeted mode's ordering and trade-off statements, reproducible analyzer links, query bounds, empty candidates,
cache isolation per target and the legacy modes left unchanged. Synthetic leagues only."""

import json
from itertools import combinations

import pytest
from fastapi.testclient import TestClient

from app.analytics import trade_practical as practical
from app.analytics.trade_discovery import SHAPES, SORTS, TradeSearch, TradeTarget, _key, _rank_key, detail
from app.analytics.trade_dual_value import VERDICT_ORDER
from app.analytics.trade_practical import LENSES, discover
from app.analytics.trades import TradeError, evaluate_trade

from synthetic_league import SMALL_ROSTER, synthetic_league
from synthetic_v3 import market_league
from test_trade_data import synced
from test_trade_lenses import banned_hits

HEURISTIC = ("2-for-2", "3-for-1", "1-for-3")
FORBIDDEN = ("guarantee", "will accept", "would accept", "accepted")


def shown(out):
    return [x for items in out["sections"].values() for x in items]


def positions_of(ts, ids):
    return [ts.position_of(x) for x in ids]


# ------------------------------------------------------------------ exact search with a target or focus --

@pytest.fixture(scope="module")
def small_focus():
    """Three teams, weeks 12-17 with real byes; the focus is weeks 13-14. Every candidate evaluated exactly."""
    st = synthetic_league(n_teams=3, weeks=range(12, 18), roster=SMALL_ROSTER, seed=11)
    ts = TradeSearch(st, 1, positions=None, focus_weeks=(13, 14))
    exact = {}
    for opp in (2, 3):
        mine = sorted(p.espn_player_id for p in st.rosters[1])
        theirs = sorted(p.espn_player_id for p in st.rosters[opp])
        cands = [("1-for-1", opp, (s,), (t,)) for s in mine for t in theirs]
        cands += [("2-for-1", opp, ss, (t,)) for ss in combinations(mine, 2) for t in theirs]
        cands += [("1-for-2", opp, (s,), tt) for s in mine for tt in combinations(theirs, 2)]
        for c in cands:
            try:
                exact[_key(*c)] = (c, ts._side(1, c[2], c[3]), ts._side(c[1], c[3], c[2]))
            except TradeError:
                pass
    return st, ts, exact


def _qualifying_rows(ts, exact):
    return [ts._row(c[0], c[1], c[2], c[3], dict(m), dict(t)) for c, m, t in exact.values()
            if m["ros_delta"] is not None and (m["ros_delta"] > 0 or (m["playoff_delta"] or 0) > 0
                                               or (m["focus_delta"] or 0) > 0)]


def test_focus_screening_never_drops_a_trade_that_helps_me_in_the_focus_weeks(small_focus):
    st, ts, exact = small_focus
    survivors = {_key(*c[:4]): c for c in ts.cands}
    assert len(exact) == len(survivors) + ts.screened_out
    for key, (c, mine, _) in exact.items():
        assert "focus_delta" in mine
        if key not in survivors:
            assert mine["known_ros_delta"] <= 0 and (mine["playoff_delta"] or 0) <= 0, key
            assert mine["focus_delta"] is None or mine["focus_delta"] <= 0, key
            continue
        bound = survivors[key][4][3]
        if mine["focus_delta"] is not None:
            assert mine["focus_delta"] <= round(bound, 2) + 0.01, key      # the focus bound is an upper bound


@pytest.mark.parametrize("target", [None, TradeTarget(position="RB"), TradeTarget(position="WR"),
                                    TradeTarget(position="TE")])
@pytest.mark.parametrize("sort", ["focus_gain", "my_gain", "regular_gain", "min_side"])
def test_filtered_best_first_top_k_equals_brute_force(small_focus, target, sort):
    st, ts, exact = small_focus
    rows = [r for r in _qualifying_rows(ts, exact)
            if target is None or target.matches(r["receives"], ts.position_of)]
    brute = sorted(rows, key=_rank_key(sort))[:12]
    got = ts.top(sort, limit=12, include_redundant=True, target=target)
    assert [r["key"] for r in got["results"]] == [r["key"] for r in brute], (sort, target)
    assert got["search"]["exhaustive"]
    if target is not None:
        assert all(target.matches(r["receives"], ts.position_of) for r in got["results"])


def test_a_named_player_target_returns_only_trades_that_bring_him_in(small_focus):
    st, ts, exact = small_focus
    pid = next(p.espn_player_id for p in st.rosters[2] if p.position == "WR")
    t = TradeTarget(player=pid)
    got = ts.top("my_gain", limit=50, target=t)["results"]
    brute = [r for r in _qualifying_rows(ts, exact) if pid in r["receives"]]
    assert {r["key"] for r in got} <= {r["key"] for r in brute}
    assert all(pid in r["receives"] and r["opponent_team_id"] == 2 for r in got)


def test_a_simpler_equivalent_is_shown_only_when_it_still_brings_in_the_target(small_focus):
    st, ts, exact = small_focus
    for pos in ("RB", "WR", "TE", "QB"):
        t = TradeTarget(position=pos)
        for r in ts.top("my_gain", limit=40, target=t)["results"]:
            assert t.matches(r["receives"], ts.position_of), (pos, r["key"])


def test_focus_gain_needs_focus_weeks_and_focus_weeks_must_be_in_the_window():
    st = market_league()
    ts = TradeSearch(st, 1, tuple(SHAPES))
    with pytest.raises(TradeError):
        ts.top("focus_gain")
    with pytest.raises(TradeError):
        TradeSearch(st, 1, tuple(SHAPES), focus_weeks=(99,))
    with pytest.raises(TradeError):
        ts.top("no_such_sort")


def test_targets_change_the_suggestions_meaningfully():
    """The acceptance criterion: a target or position changes what is suggested (and only narrows it)."""
    ts = TradeSearch(market_league(), 1, tuple(SHAPES) + HEURISTIC)
    base = shown(discover(ts, "targeted", limit=10))
    by_pos = {pos: shown(discover(ts, "targeted", limit=10, target=TradeTarget(position=pos))) for pos in ("WR", "TE")}
    assert base and by_pos["WR"] and by_pos["TE"]
    for pos, rows in by_pos.items():
        assert all(pos in positions_of(ts, x["receives"]) for x in rows), pos
    assert {x["key"] for x in by_pos["WR"]} != {x["key"] for x in by_pos["TE"]}
    assert {x["key"] for x in base} != {x["key"] for x in by_pos["TE"]}
    jeanty = shown(discover(ts, "targeted", limit=10, target=TradeTarget(player=204)))
    assert jeanty and all(204 in x["receives"] and x["opponent_team_id"] == 2 for x in jeanty)
    assert all(x["target_fit"]["matched"] == [204] for x in jeanty)


def test_beam_rows_with_a_target_all_bring_in_the_target():
    ts = TradeSearch(market_league(), 1, HEURISTIC)
    for t in (TradeTarget(position="RB"), TradeTarget(player=204), TradeTarget(position="TE")):
        b = ts.beam(t)
        assert all(t.matches(r["receives"], ts.position_of) for r in b["rows"]), t
        if t.player is not None:
            assert all(r["opponent_team_id"] == 2 for r in b["rows"])
    assert ts.beam() is ts.beam(None) and ts.beam(TradeTarget(position="RB")) is ts.beam(TradeTarget(position="RB"))


# ------------------------------------------------------------------------------- the targeted mode --

@pytest.fixture(scope="module")
def market_focus():
    st = market_league()
    return TradeSearch(st, 1, tuple(SHAPES) + HEURISTIC, focus_weeks=(st.start_week,))


def _order_key(phase, focus, sort):
    rk = _rank_key(sort)

    def key(x):
        dv = x["dual_value"]
        f = dv["rank"]["front"]
        h = practical.horizon_delta(x["mine"], phase, focus)
        return (VERDICT_ORDER[dv["plausibility"]["verdict"]], f is None, f or 0, h is None, -(h or 0.0), rk(x))
    return key


@pytest.mark.parametrize("phase", ["ros", "regular", "playoff"])
def test_targeted_rows_are_sorted_by_verdict_front_then_my_horizon_gain(phase):
    ts = TradeSearch(market_league(), 1, tuple(SHAPES) + HEURISTIC)
    out = discover(ts, "targeted", phase=phase, limit=8, target=TradeTarget(position="RB"))
    lens = LENSES["targeted"]
    assert list(out["sections"]) == list(lens["sections"]) and out["mode"] == "targeted"
    rows = shown(out)
    assert rows
    eff = out["phase"]
    assert rows == sorted(rows, key=_order_key(eff, False, practical.TARGET_PHASE_SORT[eff]))
    for verdict, items in out["sections"].items():
        assert all(x["tier"] == verdict == x["dual_value"]["plausibility"]["verdict"] for x in items)
    for x in rows:
        h = practical.horizon_delta(x["mine"], eff, False)
        assert h is None or h > 0
        assert "RB" in positions_of(ts, x["receives"])
    assert set(out["excluded"]) == {"no_gain_in_horizon", "similar_variants"}


def test_a_bye_or_short_horizon_ranks_by_the_focus_weeks_and_states_the_trade_off(market_focus):
    ts = market_focus
    out = discover(ts, "targeted", limit=8)
    rows = shown(out)
    assert rows and out["horizon"] == {"kind": "weeks", "phase": None, "weeks": list(ts.focus_weeks),
                                       "label": f"week {ts.focus_weeks[0]}", "phrase": f"week {ts.focus_weeks[0]}"}
    assert rows == sorted(rows, key=_order_key("ros", True, "focus_gain"))
    for x in rows:
        assert x["mine"]["focus_delta"] is None or x["mine"]["focus_delta"] > 0
        fit = x["target_fit"]
        assert fit["horizon"]["mine_delta"] == x["mine"]["focus_delta"]
        kinds = [t["kind"] for t in fit["tradeoffs"]]
        assert kinds[0] == "horizon" and "partner" in kinds
        assert f"week {ts.focus_weeks[0]}" in fit["tradeoffs"][0]["text"]


def test_short_term_gain_with_a_rest_of_season_loss_is_called_short_term():
    """A one-week rental: the incoming RB plays big in week 10 only, the outgoing one is steady."""
    st = market_league()
    st.projections[302] = {10: 40.0, 11: 0.0, 12: 0.0, 13: 0.0}
    ts = TradeSearch(st, 1, ("1-for-1",), opponents=[3], focus_weeks=(10,))
    out = discover(ts, "targeted", limit=20, target=TradeTarget(player=302))
    rows = shown(out)
    assert rows
    short = [x for x in rows if x["mine"]["ros_delta"] < 0 < x["mine"]["focus_delta"]]
    assert short
    assert all("short-term" in x["target_fit"]["tradeoffs"][0]["text"] for x in short)


def test_depth_and_roster_fit_trade_offs_are_stated_from_the_evaluator():
    ts = TradeSearch(market_league(), 1, tuple(SHAPES))
    rows = shown(discover(ts, "targeted", limit=20, target=TradeTarget(position="TE")))
    uneven = [x for x in rows if len(x["sends"]) > len(x["receives"])]
    assert uneven
    for x in uneven:
        assert any(t["kind"] == "depth" and "fewer rostered player" in t["text"] for t in x["target_fit"]["tradeoffs"])
    full = TradeSearch(synthetic_league(n_teams=4), 1, tuple(SHAPES))       # full rosters: incoming players force drops
    dropped = [x for x in shown(discover(full, "targeted", limit=10)) if x["mine_detail"]["forced_drops"]]
    assert dropped
    for x in dropped:
        names = [d["name"] for d in x["mine_detail"]["forced_drops"]]
        fit = [t["text"] for t in x["target_fit"]["tradeoffs"] if t["kind"] == "roster_fit"]
        assert fit and all(n in fit[0] for n in names)


def test_every_targeted_row_deep_links_to_a_reproducible_analyzer_scenario(market_focus):
    st = market_focus.state
    for t in (None, TradeTarget(position="RB"), TradeTarget(player=204)):
        for x in shown(discover(market_focus, "targeted", limit=6, target=t)):
            link = x["manual_evaluator_link"]
            assert link == {"ta": 1, "tb": x["opponent_team_id"], "a": x["sends"], "b": x["receives"]}
            res = evaluate_trade(st, link["ta"], link["tb"], link["a"], link["b"])
            assert res["team_a"]["deltas"]["ros_delta"] == x["mine"]["ros_delta"]
            assert res["team_b"]["deltas"]["ros_delta"] == x["theirs"]["ros_delta"]
            assert x["consistent_with_manual_evaluator"]


def test_no_targeted_text_promises_or_predicts_a_deal(market_focus):
    outs = [discover(market_focus, "targeted", limit=8, target=t)
            for t in (None, TradeTarget(position="RB"), TradeTarget(player=204))]
    for out in outs:
        blob = json.loads(json.dumps(out, default=str))
        assert banned_hits(blob) == []
        text = json.dumps([x["target_fit"] for x in shown(out)] + [out["pool_rule"], out["mode_text"]]).lower()
        assert not any(w in text for w in FORBIDDEN), [w for w in FORBIDDEN if w in text]


def test_legacy_modes_are_unchanged_without_a_target():
    ts = TradeSearch(market_league(), 1, tuple(SHAPES) + HEURISTIC)
    for mode in ("practical", "mutual", "upside", "all", "mutual_upgrades", "fair", "consolidation", "playoff"):
        a = discover(ts, mode, limit=6)
        b = discover(ts, mode, limit=6, target=None)
        assert [x["key"] for x in shown(a)] == [x["key"] for x in shown(b)], mode
        assert all("target_fit" not in x for x in shown(a)), mode


def test_a_target_nobody_can_trade_for_profitably_is_an_empty_view_not_filler():
    """Team 3's QB (18) would only back up your QB (20): no 1-for-1 for him raises your lineup."""
    ts = TradeSearch(market_league(), 1, ("1-for-1",))
    out = discover(ts, "targeted", limit=10, target=TradeTarget(player=301))
    assert shown(out) == [] and out["pool_size"] == 0
    assert all(o["best"] is None for o in out["opponents"])
    assert out["coverage"]["exact"]["qualified_found"] == 0


def test_a_league_without_a_playoff_split_falls_back_to_the_rest_of_season_with_a_note():
    st = market_league()
    st.last_regular = None
    ts = TradeSearch(st, 1, tuple(SHAPES))
    out = discover(ts, "targeted", phase="playoff", limit=5)
    assert out["phase"] == "ros" and "rest-of-season horizon" in out["lens_note"]


# ------------------------------------------------------------------------------------------- API --

@pytest.fixture(scope="module")
def api(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("targets") / "targets.duckdb"
    synced(db_path).close()
    mp = pytest.MonkeyPatch()
    mp.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    yield TestClient(app)
    mp.undo()


def test_api_targets_lists_other_rosters_positions_and_need_resolution(api):
    body = api.get("/api/trades/discover/targets").json()
    anchor = body["anchor"]["team_id"]
    assert body["players"] and all(p["team_id"] != anchor for p in body["players"])
    assert set(body["positions"]) <= set(body["target_positions"])
    weakest, bye = body["need_resolution"]["weakest"], body["need_resolution"]["bye"]
    assert weakest["kind"] == "weakest" and (weakest["position"] is not None) == weakest["resolved"]
    assert bye["kind"] == "bye" and (bye["week"] is not None) == bye["resolved"] and bye["reason"]
    assert body["provenance"]["contract"] == "signal-provenance.v1"
    other = api.get("/api/trades/discover/targets", params={"team_id": body["players"][0]["team_id"]}).json()
    assert all(p["team_id"] != other["anchor"]["team_id"] for p in other["players"])


def test_api_target_position_filters_rows_and_is_echoed(api):
    base = api.get("/api/trades/discover/summary", params={"mode": "targeted"}).json()
    assert base["target"] == {"active": False, "player": None, "position": None, "need": None, "weeks": None}
    for pos in ("RB", "WR"):
        body = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_position": pos}).json()
        assert body["query"]["target_position"] == pos and body["target"]["position"] == pos
        rows = shown(body)
        assert rows
        assert all(any(body["names"][str(i)]["position"] == pos for i in x["receives"]) for x in rows)
        assert all(x["target_fit"]["tradeoffs"] for x in rows)


def test_api_named_player_implies_his_team_and_rejects_other_leagues_players(api):
    tg = api.get("/api/trades/discover/targets").json()
    p = next(x for x in tg["players"] if x["position"] in ("RB", "WR"))
    body = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_player": p["espn_player_id"]}).json()
    assert body["target"]["player"]["team_id"] == p["team_id"]
    assert all(p["espn_player_id"] in x["receives"] and x["opponent_team_id"] == p["team_id"] for x in shown(body))
    mine = next(t for t in tg["teams"] if t["team_id"] == tg["anchor"]["team_id"])
    own = api.get("/api/trades/discover/targets", params={"team_id": p["team_id"]}).json()["players"][0]
    r = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_player": own["espn_player_id"],
                                                         "team_id": own["team_id"]})
    assert r.status_code == 422 and "not on another team's roster" in r.json()["detail"]
    assert api.get("/api/trades/discover/summary", params={"target_player": 987654321}).status_code == 422
    r = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_player": p["espn_player_id"],
                                                         "opponent": next(t["team_id"] for t in tg["teams"]
                                                                          if t["team_id"] not in (p["team_id"], mine["team_id"]))})
    assert r.status_code == 422


@pytest.mark.parametrize("params", [
    {"target_position": "XX"}, {"need": "nope"}, {"weeks": "3"}, {"need": "bye"},
    {"mode": "targeted", "weeks": "99"}, {"mode": "targeted", "weeks": "7-3"}, {"mode": "targeted", "weeks": "a"},
    {"mode": "targeted", "weeks": "12345678"}, {"mode": "targeted", "need": "bye", "weeks": "3"},
    {"need": "weakest", "target_position": "RB"}, {"target_player": 0}, {"target_player": -4},
    {"target_position": "R" * 40}, {"need": "x" * 40},
])
def test_api_query_bounds_are_enforced(api, params):
    for path in ("summary", "context", "targets"):
        assert api.get(f"/api/trades/discover/{path}", params=params).status_code == 422, (path, params)


def test_api_weeks_and_needs_resolve_to_a_horizon(api):
    ctx = api.get("/api/trades/discover/context", params={"mode": "targeted"}).json()
    start = ctx["evaluation"]["start_week"]
    body = api.get("/api/trades/discover/summary", params={"mode": "targeted", "weeks": f"{start}-{start + 1}"}).json()
    assert body["target"]["weeks"] == [start, start + 1] and body["horizon"]["weeks"] == [start, start + 1]
    assert all(x["mine"]["focus_delta"] is None or x["mine"]["focus_delta"] > 0 for x in shown(body))
    weak = api.get("/api/trades/discover/summary", params={"mode": "targeted", "need": "weakest"}).json()
    need = weak["target"]["need"]
    assert need["kind"] == "weakest" and need["reason"]
    if need["resolved"]:
        assert weak["target"]["position"] == need["position"]
        assert all(any(weak["names"][str(i)]["position"] == need["position"] for i in x["receives"]) for x in shown(weak))
    bye = api.get("/api/trades/discover/summary", params={"mode": "targeted", "need": "bye"}).json()
    assert bye["target"]["need"]["kind"] == "bye"
    assert bye["target"]["weeks"] == ([bye["target"]["need"]["week"]] if bye["target"]["need"]["resolved"] else None)


def test_api_views_and_evidence_are_cached_per_target(api):
    rb = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_position": "RB"}).json()
    wr = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_position": "WR"}).json()
    rb_keys, wr_keys = {x["key"] for x in shown(rb)}, {x["key"] for x in shown(wr)}
    assert rb_keys and wr_keys and rb_keys != wr_keys
    only_rb = next(iter(rb_keys - wr_keys))
    ok = api.get("/api/trades/discover/evidence", params={"mode": "targeted", "target_position": "RB", "key": only_rb})
    assert ok.status_code == 200 and ok.json()["key"] == only_rb
    miss = api.get("/api/trades/discover/evidence", params={"mode": "targeted", "target_position": "WR", "key": only_rb})
    assert miss.status_code == 404


def test_api_saved_links_without_target_params_answer_as_before(api):
    """Original modes and saved links stay compatible: no target, same rows; the echo adds only null fields."""
    for mode in ("practical", "mutual_upgrades", "fair", "playoff", "all"):
        body = api.get("/api/trades/discover/summary", params={"mode": mode}).json()
        assert body["query"]["target_player"] is None and body["query"]["need"] is None and body["query"]["weeks"] is None
        assert body["target"]["active"] is False
        assert all("target_fit" not in x for x in shown(body)), mode


def test_targets_route_is_mounted_under_the_league_scope():
    from app.api.league_scope import league_scope
    from app.api.routing import iter_routes
    from app.main import app
    v2 = [x for x in iter_routes(app) if x.path == "/v2/leagues/{league_id}/api/trades/discover/targets"]
    assert len(v2) == 1 and league_scope in v2[0].dependency_calls()


def test_targeted_results_for_another_anchor_never_use_the_default_team(api):
    default = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_position": "WR"}).json()
    other_team = next(t["team_id"] for t in default["teams"] if t["team_id"] != default["anchor"]["team_id"])
    other = api.get("/api/trades/discover/summary", params={"mode": "targeted", "target_position": "WR",
                                                             "team_id": other_team}).json()
    assert other["anchor"]["team_id"] == other_team
    assert all(x["anchor_team_id"] == other_team and x["opponent_team_id"] != other_team for x in shown(other))


def test_detail_of_a_focus_row_matches_the_manual_evaluator(market_focus):
    for r in market_focus.top("focus_gain", limit=5)["results"]:
        assert detail(market_focus.state, r)["consistent_with_manual_evaluator"]


def test_sorts_offered_by_the_api_are_unchanged():
    assert SORTS == ("my_gain", "their_gain", "combined", "min_side", "playoff_gain", "min_side_regular",
                     "min_side_playoff")


def test_a_slot_already_empty_before_the_trade_is_not_reported_as_emptied():
    """trade_discovery._empty_slots compares per-label counts: a WR slot that was empty before stays the roster's,
    and only a slot the trade empties is named (the roster-fit statement and the rationale read this list)."""
    from app.analytics.trade_discovery import _empty_slots

    def week(w, filled):
        return {"week": w, "assignment": [{"slot": s, "player_id": (1 if f else None), "points": (5.0 if f else None)}
                                          for s, f in filled]}
    before = [week(3, [("WR", True), ("WR", False), ("RB", True)])]
    same = [week(3, [("WR", True), ("WR", False), ("RB", True)])]          # a new WR fills the slot the old one left
    lost = [week(3, [("WR", False), ("WR", False), ("RB", True)])]
    assert _empty_slots({"lineups": {"before": before, "after": same}}) == []
    assert _empty_slots({"lineups": {"before": before, "after": lost}}) == [{"week": 3, "slot": "WR"}]

"""Counteroffers (#214, app/analytics/trade_counteroffers.py) on synthetic leagues: bounded edits of one offer, refused
before evaluation when illegal, every result the exact two-team evaluation re-checked by the manual evaluator, a
deterministic order and budget, and a zero-result answer that says why."""
import random

import pytest
from fastapi.testclient import TestClient

import compat_fixtures as cf
from app.analytics import trade_counteroffers as co
from app.analytics.lineup import lineup_slots
from app.analytics.trade_counteroffers import CounterofferConstraints, counteroffers
from app.analytics.trades import TradeError, evaluate_trade
from app.db.connection import connect

from test_trade_data import _raw_snapshot, synced
from test_trade_discovery import league, two_teams

C = CounterofferConstraints


def _untimed(res: dict) -> dict:
    return {**res, "search": {k: v for k, v in res["search"].items() if k != "seconds"}}


def _manual(st, res, x):
    return evaluate_trade(st, res["team_a_id"], res["team_b_id"], x["team_a_sends"], x["team_b_sends"],
                          scenarios=False)


def test_a_lopsided_offer_gets_exact_counteroffers_that_lift_the_worse_side():
    st = two_teams()
    res = counteroffers(st, 1, 2, [105], [204])         # A's benched RB for B's best WR: B gains less
    o = res["original"]
    assert res["status"] == "found" and o["min_side_ros_delta"] == min(o["team_a"]["ros_delta"], o["team_b"]["ros_delta"])
    assert 1 <= len(res["results"]) <= co.MAX_SHOWN
    mins = [x["min_side_ros_delta"] for x in res["results"]]
    assert mins == sorted(mins, reverse=True) and all(m > o["min_side_ros_delta"] for m in mins)
    for x in res["results"]:
        manual = _manual(st, res, x)
        assert manual["team_a"]["deltas"]["ros_delta"] == x["team_a"]["ros_delta"]
        assert manual["team_b"]["deltas"]["ros_delta"] == x["team_b"]["ros_delta"]
        assert x["consistent_with_manual_evaluator"] is True and len(x["edits"]) == 1
        assert x["team_a"]["change_vs_original"] == pytest.approx(x["team_a"]["ros_delta"] - o["team_a"]["ros_delta"])
        assert x["analyzer_link"] == {"ta": 1, "tb": 2, "a": x["team_a_sends"], "b": x["team_b_sends"]}
    assert res["search"]["exhaustive"] and res["search"]["inconsistent_dropped"] == 0
    assert "accept" in res["note"] and _untimed(res) == _untimed(counteroffers(st, 1, 2, [105], [204]))


def test_locked_players_stay_and_excluded_players_are_never_added():
    st = two_teams()
    locked = counteroffers(st, 1, 2, [105], [204], C(locked=frozenset({105, 204})))
    assert all(105 in x["team_a_sends"] and 204 in x["team_b_sends"] for x in locked["results"])
    assert all(e["kind"] == "add" for x in locked["results"] for e in x["edits"])
    free = counteroffers(st, 1, 2, [105], [204])
    used = {e["in"] for x in free["results"] for e in x["edits"] if e["in"]}
    assert used
    kept_out = counteroffers(st, 1, 2, [105], [204], C(excluded=frozenset(used)))
    assert not {e["in"] for x in kept_out["results"] for e in x["edits"]} & used


def test_added_players_respect_the_positions_and_the_per_side_caps():
    st = two_teams()
    res = counteroffers(st, 1, 2, [105], [204], C(positions=("TE",), max_added_per_side=2))
    pos = {p.espn_player_id: p.position for t in st.rosters.values() for p in t}
    assert all(pos[e["in"]] == "TE" for x in res["results"] for e in x["edits"] if e["in"])
    two = counteroffers(st, 1, 2, [105], [204], C(max_added_per_side=2))
    for x in two["results"]:
        assert len(set(x["team_a_sends"]) - {105}) <= 2 and len(set(x["team_b_sends"]) - {204}) <= 2
    assert two["search"]["packages_evaluated"] > counteroffers(st, 1, 2, [105], [204])["search"]["packages_evaluated"]
    one_each = counteroffers(st, 1, 2, [105], [204], max_per_side=1)
    assert all(len(x["team_a_sends"]) == 1 == len(x["team_b_sends"]) for x in one_each["results"])


def test_no_legal_edit_and_no_better_edit_are_explained():
    st = two_teams()
    stuck = counteroffers(st, 1, 2, [105], [204], C(locked=frozenset({105, 204}), max_added_per_side=0))
    assert stuck["status"] == "no_legal_edit" and stuck["results"] == [] and "locked" in stuck["reason"]
    # Only kickers may be added, and this league starts none: every package has the original's effects.
    kicked = league({1: [*[(i, p, v) for i, p, v in [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14),
                                                     (106, "WR", 8), (107, "WR", 2), (108, "TE", 6)]], (109, "K", 9)],
                     2: [(201, "QB", 18), (202, "RB", 6), (203, "RB", 1), (204, "WR", 16), (205, "WR", 15),
                         (208, "TE", 7), (209, "K", 9)]})
    same = counteroffers(kicked, 1, 2, [102], [204], C(locked=frozenset({102, 204}), positions=("K",)))
    assert same["status"] == "none_better" and same["results"] == []
    assert same["search"]["statuses"]["no_effect"] == same["search"]["packages_evaluated"] > 0


def test_illegal_requests_are_refused_before_any_evaluation():
    st = two_teams()
    for args, kw in ((([204], [105]), {}),                                   # crossed owners
                     (([105, 105], [204]), {}),                              # duplicate asset
                     (([105], [204]), {"locked": frozenset({102})}),         # stale lock: not in the offer
                     (([105], [204]), {"excluded": frozenset({999})}),       # not on either roster
                     (([105], [204]), {"locked": frozenset({105}), "excluded": frozenset({105})}),
                     (([105], [204]), {"max_added_per_side": 3}), (([105], [204]), {"limit": 4})):
        with pytest.raises(TradeError):
            counteroffers(st, 1, 2, *args, C(**kw))


def test_a_missing_projection_is_unknown_never_a_counteroffer():
    # An unprojected active player leaves his team's total unknown, so the offer has nothing exact to improve on.
    unknown = counteroffers(two_teams(extra_b=[(209, "WR", None)]), 1, 2, [105], [204])
    assert unknown["status"] == "original_unknown" and unknown["results"] == [] and "missing" in unknown["reason"]
    # One in an IR slot starts nothing until traded: the packages that bring him in are unknown and never listed.
    st = two_teams(extra_b=[(209, "WR", None, True)])
    res = counteroffers(st, 1, 2, [105], [204])
    assert res["status"] == "found" and res["search"]["statuses"]["excluded_missing_data"] >= 1
    assert all(209 not in x["team_b_sends"] for x in res["results"])


def test_an_unsupported_starting_slot_makes_the_offer_unknown():
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "P": 1})
    st = two_teams(extra_a=[(109, "P", 5)])
    st = type(st)(**{**st.__dict__, "slots": slots})
    assert counteroffers(st, 1, 2, [109], [204])["status"] == "original_unknown"


def test_negative_projections_still_match_the_manual_evaluator():
    st = two_teams(extra_a=[(109, "WR", -3)], extra_b=[(209, "RB", -2)])
    res = counteroffers(st, 1, 2, [105, 109], [204], C(max_added_per_side=2))
    for x in res["results"]:
        manual = _manual(st, res, x)
        assert (manual["team_a"]["deltas"]["ros_delta"], manual["team_b"]["deltas"]["ros_delta"]) == \
            (x["team_a"]["ros_delta"], x["team_b"]["ros_delta"])


def test_equivalent_counteroffers_show_only_the_simplest():
    st = two_teams()
    res = counteroffers(st, 1, 2, [105], [204], C(max_added_per_side=2))
    effects = [(x["team_a"], x["team_b"]) for x in res["results"]]
    assert len(effects) == len(set(map(repr, effects)))
    assert res["search"]["equivalent_hidden"] >= 1


def test_a_spent_budget_stops_the_search_deterministically_and_says_so():
    st = two_teams()
    tight = C(work_budget=60)
    a, b = counteroffers(st, 1, 2, [105], [204], tight), counteroffers(st, 1, 2, [105], [204], tight)
    s = a["search"]
    assert s["termination"] == "work_budget" and not s["exhaustive"] and s["packages_not_evaluated"] > 0
    assert "work budget" in s["stop_reason"] and _untimed(a) == _untimed(b)
    if not a["results"]:
        assert a["status"] == "none_within_budget" and "may qualify" in a["reason"]


def test_the_time_cap_is_reported(monkeypatch):
    st = two_teams()
    res = counteroffers(st, 1, 2, [105], [204], C(time_cap_seconds=0.0))
    assert res["search"]["termination"] == "time_cap" and res["search"]["packages_evaluated"] == 0
    assert res["status"] == "none_within_budget"


def test_a_three_flex_superflex_league_stays_bounded_and_exact():
    rng = random.Random(214)
    slots = lineup_slots(cf.FX17)
    positions = ["QB"] * 3 + ["RB"] * 6 + ["WR"] * 8 + ["TE"] * 3 + ["K", "D/ST"]
    rosters = {t: [(t * 100 + i, pos, rng.choice([3.0, 6.5, 9.0, 12.5, 15.0, 18.0, 21.0]))
                   for i, pos in enumerate(positions)] for t in (1, 2)}
    st = league(rosters, capacity=len(positions))
    st = type(st)(**{**st.__dict__, "slots": slots})
    res = counteroffers(st, 1, 2, [101], [205, 206], C(max_added_per_side=2))
    assert res["search"]["work_spent"] <= co.WORK_BUDGET + 2_000           # one package past the budget at most
    assert res["search"]["inconsistent_dropped"] == 0
    for x in res["results"]:
        manual = _manual(st, res, x)
        assert manual["team_b"]["deltas"]["ros_delta"] == x["team_b"]["ros_delta"]


def test_the_counteroffer_api_is_read_only_bounded_and_snapshot_checked(tmp_path, monkeypatch):
    db_path = tmp_path / "co.duckdb"
    con = synced(db_path)
    raw = _raw_snapshot(con)
    con.close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    opts = api.get("/api/trades/options").json()
    teams = {t["team_id"]: [p["espn_player_id"] for p in t["roster"]] for t in opts["teams"]}
    ta, tb = sorted(teams)[:2]
    body = {"team_a_id": ta, "team_b_id": tb, "team_a_sends": teams[ta][:1], "team_b_sends": teams[tb][:1]}
    res = api.post("/api/trades/counteroffers", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["contract"] == "trade-counteroffers/v1" and data["status"] in (
        "found", "none_better", "no_legal_edit", "original_unknown", "none_within_budget")
    assert set(data["original"]["team_a_sends"]) <= set(map(int, data["names"])) | set(data["original"]["team_a_sends"])
    for x in data["results"]:
        manual = api.post("/api/trades/evaluate", json={**{k: body[k] for k in ("team_a_id", "team_b_id")},
                                                        "team_a_sends": x["team_a_sends"],
                                                        "team_b_sends": x["team_b_sends"]}).json()
        assert manual["team_a"]["deltas"]["ros_delta"] == x["team_a"]["ros_delta"]
    again = api.post("/api/trades/counteroffers", json={**body, "data_version": data["data_version"]})
    assert again.status_code == 200
    assert api.post("/api/trades/counteroffers", json={**body, "data_version": "old"}).status_code == 409
    for bad in ({"max_added_per_side": 3}, {"limit": 9}, {"positions": ["XX"]}, {"team_a_sends": list(range(11))},
                {"team_b_sends": teams[ta][:1]}, {"locked": [123456789]}):
        assert api.post("/api/trades/counteroffers", json={**body, **bad}).status_code == 422, bad
    assert db_path.stat().st_mtime_ns == mtime                    # nothing written
    con = connect(db_path, read_only=True)
    assert _raw_snapshot(con) == raw
    con.close()


def test_the_original_offer_is_echoed_with_its_exact_deltas():
    st = two_teams()
    res = counteroffers(st, 1, 2, [105], [204])
    manual = evaluate_trade(st, 1, 2, [105], [204], scenarios=False)
    assert res["original"]["team_a"]["ros_delta"] == manual["team_a"]["deltas"]["ros_delta"]
    assert res["original"]["team_b"]["ros_delta"] == manual["team_b"]["deltas"]["ros_delta"]
    assert res["evaluation"]["weeks"] == [10, 11, 12]

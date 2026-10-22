"""Trade Discovery 2.0 dual value (docs/trade_discovery_dual_value.md): the guarantee table, one focused test
per guarantee, plus the six checks, the verdict rule, Pareto fronts and the ranking policy. Synthetic leagues
only (in memory or temporary databases); every player is invented."""

import copy
import json
import re
from dataclasses import replace
from itertools import combinations

import pytest
from fastapi.testclient import TestClient

from app.analytics import trade_dual_value as tdv
from app.analytics.team_lineups import known_total, project_team
from app.analytics.trade_alternatives import FreeAgencyBoard
from app.analytics.trade_assets import BALANCED_RATIO, AssetBoard
from app.analytics.trade_discovery import (ALL_SHAPES, SHAPES, TradeSearch, _key, _rank_key, _side_detail, detail)
from app.analytics.trades import evaluate_trade, with_streaming
from app.db.connection import connect

from synthetic_league import SMALL_ROSTER, add_free_agents, synthetic_league
from synthetic_v3 import P, WEEKS, filler, league, market_league
from test_trade_data import _raw_snapshot, synced

DELTA_KEYS = ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta")
BANNED = ("probab", "accept", "likelihood", "chance")
NEGATION = re.compile(r"\b(not|no|never|nothing|neither|without)\b", re.I)


def flat(v):
    return {w: float(v) for w in WEEKS}


def views(state, anchor, opp, sends, receives, start_week=None):
    """The manual evaluator's result and its two _side_detail views (what the analyzer hands to for_evaluation)."""
    res = evaluate_trade(state, anchor, opp, sends, receives, start_week)
    return res, _side_detail(res["team_a"]), _side_detail(res["team_b"])


def assess_trade(state, anchor, opp, sends, receives, phase="ros", labels=None, **kw):
    """for_evaluation built the way POST /api/trades/evaluate builds it (fresh boards, no shared context)."""
    _, mine, theirs = views(state, anchor, opp, sends, receives)
    window = state.window()
    fa = FreeAgencyBoard(state, window) if state.free_agents else None
    labels = labels or ("You", state.team_names[opp])
    return tdv.for_evaluation(state, window, anchor, sends, receives, mine, theirs, phase=phase, fa_board=fa,
                              labels=labels, **kw)


def status(dv_or_checks, criterion):
    cs = dv_or_checks["plausibility"]["checks"] if isinstance(dv_or_checks, dict) else dv_or_checks
    return next(c["status"] for c in cs if c["criterion"] == criterion)


def check_text(dv, criterion):
    return next(c["text"] for c in dv["plausibility"]["checks"] if c["criterion"] == criterion)


def _untimed(v):
    """A search payload without its run instrumentation (wall-clock seconds, lineup-solve counters)."""
    if isinstance(v, dict):
        return {k: _untimed(x) for k, x in v.items() if k != "seconds" and not k.startswith("lineup_solves")}
    return [_untimed(x) for x in v] if isinstance(v, list) else v


# ------------------------------------------------------------------------ fixture leagues --

def with_free_agents(st, *fas):
    """Confirmed free agents (ESPN FREEAGENT) given as (id, position, weekly points); only those count as an
    immediate alternative in the free-agent check."""
    for pid, pos, pts in fas:
        st.free_agents = [*st.free_agents, P(pid, pos)]
        st.projections[pid] = flat(pts)
        st.free_agent_status = {**st.free_agent_status, pid: "FREEAGENT"}
    return st


def fair_no_gain_league():
    """market_league whose team 2 is stacked at WR and RB, so Bowers (TE 13) and Team 1's bench WR 13 both sit for
    their new teams' lineups except where a TE is needed: Team 1 starts Bowers, Team 2 gains nothing."""
    st = market_league()
    st.projections[205], st.projections[206] = flat(20), flat(19)
    return st


def bench_receiver_league():
    """Team 1 has an elite TE (20), so Bowers (TE 13) cannot start for it; its WR 12 (109) is already a bench player."""
    st = market_league()
    st.projections[110] = flat(20)
    return st


def consolidation_league(rb_value=11, deep_rb=False):
    """Capacity 9. Team 2 holds exactly its nine lineup-relevant players plus RB 207 (`rb_value`), and receives two
    WRs for one: someone must go. RB 207 is the cheapest drop (zero lineup cost) but a real asset. `deep_rb` gives
    Team 3 two more RBs (7) so that its FLEX takes an RB 7 and RB 207 at 6 ranks below the league's RB starters
    (the asset board counts each FLEX for the position filling it, so RB 207, Team 2's FLEX starter before the
    trade, is otherwise the 7th of 8 league RB starters)."""
    t1 = [(101, "QB", 20), (102, "RB", 15), (103, "RB", 14), (104, "WR", 16), (105, "WR", 14), (106, "WR", 13),
          (107, "TE", 7), (108, "K", 8), (109, "D/ST", 7), (110, "RB", 3)]
    t2 = [(201, "QB", 18), (202, "RB", 13), (203, "RB", 12), (204, "WR", 19, {"name": "Ember"}), (205, "WR", 17),
          (206, "TE", 6), (207, "RB", rb_value), (208, "K", 7), (209, "D/ST", 6)]
    fas = [(901, "QB", 12), (902, "RB", 5), (903, "WR", 6), (904, "TE", 4), (905, "K", 7), (906, "D/ST", 6)]
    return league({1: t1, 2: t2, 3: filler(3, 300, rb=(9, 8, 7, 7) if deep_rb else (9, 8, 4))}, fas, capacity=9)


def playoff_league():
    """Team 1's RB2 is on a bye in the playoff week; Closer (RB, 12 in week 13) fixes that. He sits behind Rusher
    in the regular season, so Team 1's regular-season delta is exactly 0 while its playoff delta is positive."""
    bye = {10: 12, 11: 12, 12: 12, 13: 0}
    late = {10: 11, 11: 11, 12: 11, 13: 12}
    t1 = [(101, "QB", 20), (102, "RB", 15), (103, "RB", bye, {"name": "Rusher"}), (104, "WR", 16), (105, "WR", 15),
          (106, "TE", 7), (109, "WR", 13), (107, "WR", 11, {"name": "Wideout"}), (108, "K", 8), (110, "D/ST", 7)]
    t2 = [(201, "QB", 18), (202, "RB", 14), (203, "RB", 13), (204, "RB", late, {"name": "Closer"}), (205, "WR", 9),
          (206, "WR", 8), (207, "TE", 6), (208, "K", 7), (209, "D/ST", 6), (210, "RB", 12)]
    fas = [(901, "QB", 12), (902, "RB", 5), (903, "WR", 6), (904, "TE", 4), (905, "K", 7), (906, "D/ST", 6)]
    return league({1: t1, 2: t2, 3: filler(3, 300)}, fas, capacity=10)


@pytest.fixture(scope="module")
def market():
    """market_league searched for team 1 over every shape, with exact and heuristic rows."""
    st = market_league()
    ts = TradeSearch(st, 1, shapes=tuple(ALL_SHAPES), positions=None)
    exact = {r["key"]: r for r in ts.top("min_side", None, None, 6)["results"] + ts.top("my_gain", None, None, 4)["results"]}
    heur = {r["key"]: r for r in ts.heuristic_top("min_side", None, None, 6)["results"]}
    assert len({r["shape"] for r in exact.values()}) >= 2 and heur
    return st, ts, {"exact": list(exact.values()), "heuristic": list(heur.values())}


# ============================================================ manual analyzer = discovery --

@pytest.mark.parametrize("kind", ["exact", "heuristic"])
def test_manual_and_discovery_identical_deltas_and_assessment(market, kind):
    st, ts, rows = market
    board = AssetBoard(st, ts.window)
    fb = FreeAgencyBoard(st, ts.window)
    ctx = tdv.anchor_fa_context(fb, st, 1)
    assert rows[kind]
    for row in rows[kind]:
        d = detail(st, row)
        assert d["consistent_with_manual_evaluator"]
        labels = ("You", row["opponent_name"])
        via_discovery = tdv.for_evaluation(st, ts.window, 1, row["sends"], row["receives"], d["mine_detail"],
                                           d["theirs_detail"], asset_board=board, fa_board=fb, fa_context=ctx, labels=labels)
        res = evaluate_trade(st, 1, row["opponent_team_id"], row["sends"], row["receives"])
        via_manual = tdv.for_evaluation(st, st.window(), 1, row["sends"], row["receives"], _side_detail(res["team_a"]),
                                        _side_detail(res["team_b"]), phase="ros",
                                        fa_board=FreeAgencyBoard(st, st.window()), labels=labels)
        assert via_discovery == via_manual, row["key"]
        for who, side, stage_b in (("you", res["team_a"], row["mine"]), ("them", res["team_b"], row["theirs"])):
            lin, dl = via_manual["lineup"][who], side["deltas"]
            assert lin["before"] == {"ros": side["before"]["ros_projected_starting_points"],
                                     "regular": side["before"]["regular_season_projected_starting_points"],
                                     "playoff": side["before"]["playoff_projected_starting_points"]}
            assert lin["after"] == {"ros": side["after"]["ros_projected_starting_points"],
                                    "regular": side["after"]["regular_season_projected_starting_points"],
                                    "playoff": side["after"]["playoff_projected_starting_points"]}
            assert lin["delta"] == {"ros": dl["ros_delta"], "regular": dl["regular_season_delta"],
                                    "playoff": dl["playoff_delta"], "known_ros": dl["known_ros_delta"]}
            assert lin["average_weekly_delta"] == dl["average_weekly_delta"]
            assert (lin["weeks_improved"], lin["weeks_worsened"], lin["weeks_unchanged"]) == \
                (dl["weeks_improved"], dl["weeks_worsened"], dl["weeks_unchanged"])
            assert lin["delta"]["ros"] == stage_b["ros_delta"]            # and the search's own stage-B number


def test_manual_api_returns_dual_value(tmp_path, monkeypatch):
    db_path = tmp_path / "dv.duckdb"
    synced(db_path).close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    api = TestClient(app)
    row = api.get("/api/trades/discover", params={"mode": "upside", "include_kdst": True}).json()["results"][0]
    body = {"team_a_id": 2, "team_b_id": row["opponent_team_id"], "team_a_sends": row["sends"],
            "team_b_sends": row["receives"]}
    res = api.post("/api/trades/evaluate", json=body).json()
    dv = res["dual_value"]
    assert set(dv) == {"phase", "asset", "lineup", "plausibility", "rank_axes", "most_important_change",
                       "projection_source"}
    assert dv["projection_source"] == "espn" and dv["phase"] == "ros"
    assert {"label", "ratio", "imbalance", "you_send", "you_receive", "depth", "best_asset_surrender"[:0] or "bench_assets"} <= set(dv["asset"])
    assert set(dv["lineup"]) == {"you", "them", "weeks", "basis"}
    assert [c["criterion"] for c in dv["plausibility"]["checks"]] == list(tdv.CRITERIA)
    assert set(dv["rank_axes"]) == set(tdv.AXES)
    # team 2 is the user's team: "You"; the other side carries its team name
    assert dv["lineup"]["you"]["label"] == "You" and dv["lineup"]["you"]["team_id"] == 2
    assert dv["lineup"]["them"]["label"] == res["team_b"]["team_name"]
    for who, side in (("you", res["team_a"]), ("them", res["team_b"])):
        assert dv["lineup"][who]["delta"] == {"ros": side["deltas"]["ros_delta"], "regular": side["deltas"]["regular_season_delta"],
                                              "playoff": side["deltas"]["playoff_delta"], "known_ros": side["deltas"]["known_ros_delta"]}
        assert dv["lineup"][who]["before"]["ros"] == side["before"]["ros_projected_starting_points"]
    assert dv["lineup"]["you"]["delta"]["ros"] == row["mine"]["ros_delta"]      # discovery's number for the same trade
    # the same call, made directly on the same state, gives the same assessment
    con = connect(db_path, read_only=True)
    from app.analytics.trade_data import load_league_state
    state = with_streaming(load_league_state(con))
    con.close()
    direct = assess_trade(state, 2, row["opponent_team_id"], row["sends"], row["receives"],
                          labels=("You", state.team_names[row["opponent_team_id"]]))
    assert json.loads(json.dumps(direct)) == dv
    # a non-user team_a is labelled by name on both sides
    other = api.post("/api/trades/evaluate", json={"team_a_id": row["opponent_team_id"], "team_b_id": 2,
                                                   "team_a_sends": row["receives"], "team_b_sends": row["sends"]}).json()["dual_value"]
    assert other["lineup"]["you"]["label"] == res["team_b"]["team_name"] and other["lineup"]["them"]["label"] == res["team_a"]["team_name"]
    # existing fields are untouched
    assert {"team_a", "team_b", "result", "trade_vs_free_agency", "players"} <= set(res)


# ===================================================================== verdict scenarios --

def test_lopsided_mutual_is_unlikely_mutual():
    dv = assess_trade(market_league(), 1, 2, [105], [204, 209])
    lin = dv["lineup"]
    assert lin["you"]["delta"]["ros"] > 0 and lin["them"]["delta"]["ros"] > 0              # both lineups improve
    assert status(dv, "lineup_gain") == "pass" and status(dv, "asset_balance") == "fail"
    assert dv["asset"]["label"] == "they_surrender_multiple_strong" and dv["asset"]["imbalance"] == "significant"
    assert dv["asset"]["favours"] == "You"
    assert dv["asset"]["you_send_par"] < dv["asset"]["they_send_par"]
    assert dv["plausibility"]["verdict"] == "unlikely_mutual"
    assert dv["plausibility"]["verdict_text"] == tdv.VERDICTS["unlikely_mutual"]
    assert dv["plausibility"]["main_concern"] == check_text(dv, "asset_balance")


@pytest.mark.parametrize("trade,who", [(((1, 2), [108], [209]), "Team 2's"), (((1, 2), [109], [209]), "your")])
def test_fair_value_without_lineup_gain_is_flagged(trade, who):
    # (108, 209): Team 2 is stacked at WR/RB, so Team 1's bench WR 13 never starts for it; Team 1 starts Bowers.
    # (109, 209) on a league where Team 1's TE is elite: Bowers sits for Team 1, Team 2 starts the WR.
    st = fair_no_gain_league() if trade[1] == [108] else bench_receiver_league()
    dv = assess_trade(st, *trade[0], *trade[1:])
    assert dv["asset"]["imbalance"] == "none" and status(dv, "asset_balance") == "pass"
    assert status(dv, "lineup_gain") == "fail"
    assert dv["plausibility"]["verdict"] == "fair_no_lineup_gain"
    assert dv["plausibility"]["verdict_text"] == f"Asset-fair exchange, but {who} starting lineup does not improve."
    assert who.lower() in check_text(dv, "lineup_gain").lower()
    losers = [k for k in ("you", "them") if dv["lineup"][k]["delta"]["ros"] <= 0]
    assert losers == (["them"] if who == "Team 2's" else ["you"])
    winners = [k for k in ("you", "them") if k not in losers]
    assert all(dv["lineup"][k]["delta"]["ros"] > 0 for k in winners)


@pytest.mark.parametrize("rb_value,expect,starter_caliber", [(11, "fail", True), (6, "concern", False)])
def test_consolidation_forced_drop_depth_cost(rb_value, expect, starter_caliber):
    st = consolidation_league(rb_value, deep_rb=not starter_caliber)
    res, mine, theirs = views(st, 1, 2, [105, 106], [204])           # Team 1 consolidates 2-for-1; Team 2 receives two
    dv = assess_trade(st, 1, 2, [105, 106], [204])
    you, them = dv["asset"]["depth"]["you"], dv["asset"]["depth"]["them"]
    assert (you["net_players"], you["kind"]) == (-1, "consolidates")
    assert (them["net_players"], them["kind"]) == (1, "adds depth")
    assert you["forced_drops"] == []
    drop = them["forced_drops"][0]
    profile = AssetBoard(st, st.window()).profiles[207]
    assert len(them["forced_drops"]) == 1 and drop["espn_player_id"] == 207
    assert drop["par"] == profile["par"] and drop["par"] > 0 and drop["starter_caliber"] is starter_caliber
    assert them["forced_drop_par"] == drop["par"]
    # the same drop is in the lineup block, resolved by the manual evaluator, and costs no lineup points
    assert [x["espn_player_id"] for x in dv["lineup"]["them"]["forced_drops"]] == [207]
    assert [d["espn_player_id"] for d in res["team_b"]["roster"]["forced_drops"]] == [207]
    assert dv["lineup"]["them"]["forced_drop_lineup_cost"] == them["forced_drop_lineup_cost"] == 0.0
    assert dv["lineup"]["you"]["forced_drops"] == []
    assert status(dv, "depth_cost") == expect
    assert "Team 2 must drop RB 207" in check_text(dv, "depth_cost")
    assert dv["plausibility"]["verdict"] != "sensible"
    # the receiving side of the two keeps both players; the consolidating side's open spot is reported
    assert you["open_roster_spots_after"] == 0 and them["open_roster_spots_after"] == 0


def test_bench_asset_retains_value():
    plain = assess_trade(market_league(), 1, 2, [108], [209])
    bowers = plain["asset"]["you_receive"][0]
    assert bowers["espn_player_id"] == 209 and bowers["par"] > 0 and bowers["starter_caliber"] is True
    assert bowers["role_before"] == "bench" and bowers["starts_before"] == 0       # never starts for Team 2

    st = bench_receiver_league()
    res, mine, theirs = views(st, 1, 2, [109], [209])
    dv = assess_trade(st, 1, 2, [109], [209])
    bowers = dv["asset"]["you_receive"][0]
    assert bowers["starts_after"] == 0 and bowers["role_after"] == "bench" and bowers["bench_asset"] is True
    assert dv["asset"]["bench_assets"] == [209]
    # his points above replacement still count in the package, though he adds nothing to the lineup
    board = AssetBoard(st, st.window())
    assert dv["asset"]["they_send_par"] == board.profiles[209]["par"] > 0
    assert dv["asset"]["you_send_par"] == board.profiles[109]["par"]
    # bench points never enter the lineup delta: it is the starters-only evaluate_trade delta
    assert dv["lineup"]["you"]["delta"]["ros"] == res["team_a"]["deltas"]["ros_delta"] == 0.0
    window, roster = st.window(), st.rosters[1]
    bowers_p = st.rosters[2][8]
    assert bowers_p.espn_player_id == 209
    sans = [p for p in roster if p.espn_player_id != 109]
    total = lambda ps: known_total(project_team(ps, st.slots, window, st.projections))      # noqa: E731
    assert total(sans + [bowers_p]) == total(sans)                    # a benched Bowers adds no starting points
    bench_inclusive = sum(st.projections[209][w] - st.projections[109][w] for w in window.weeks)
    assert bench_inclusive != 0 and bench_inclusive != dv["lineup"]["you"]["delta"]["ros"]


def test_missing_data_is_not_zero():
    st = market_league()
    del st.projections[302][11]                                          # one regular-season week of Team 3's RB
    dv = assess_trade(st, 1, 3, [107], [302])
    assert dv["plausibility"]["verdict"] == "incomplete"
    assert dv["plausibility"]["verdict_text"] == tdv.VERDICTS["incomplete"]
    assert status(dv, "lineup_gain") == "unknown" and status(dv, "asset_balance") == "unknown"
    assert dv["rank_axes"]["lineup_min_side"] is None and dv["rank_axes"]["asset_ratio"] is None
    you = dv["lineup"]["you"]
    assert you["delta"]["ros"] is None and you["delta"]["regular"] is None          # week 11 is regular season
    assert you["delta"]["playoff"] is not None                                      # week 13 is complete
    assert isinstance(you["delta"]["known_ros"], float) and you["missing"]["weeks"] == [11]
    got = dv["asset"]["you_receive"][0]
    assert got["par"] is None and got["known_par"] is not None and got["missing_weeks"] == [11]
    assert dv["asset"]["label"] == "insufficient_data" and dv["asset"]["imbalance"] is None
    assert dv["asset"]["ratio"] is None and dv["asset"]["you_send_par"] is None
    # an unknown axis puts the row on no Pareto front, and it sorts after complete rows
    complete = assess_trade(market_league(), 1, 3, [107], [302])
    assert complete["plausibility"]["verdict"] == "sensible"
    assert tdv.pareto_fronts([dv["rank_axes"], complete["rank_axes"]]) == [None, 0]
    rows = [{"key": "incomplete", "dual_value": dv}, {"key": "complete", "dual_value": complete}]
    ranked = tdv.rank(rows, lambda x: x["dual_value"]["rank_axes"]["lineup_min_side"], lambda x: x["key"])
    assert [x["key"] for x in ranked] == ["complete", "incomplete"]
    # a projected 0 (a bye) is a real value, not missing
    zero = market_league()
    zero.projections[302][11] = 0.0
    dv0 = assess_trade(zero, 1, 3, [107], [302])
    assert dv0["plausibility"]["verdict"] != "incomplete" and dv0["asset"]["you_receive"][0]["par"] is not None
    assert dv0["lineup"]["you"]["delta"]["ros"] is not None


def test_missing_data_sorts_last_within_the_same_verdict():
    complete = {"lineup_min_side": 1.0, "asset_ratio": 0.5, "certainty": -3}
    unknown = {"lineup_min_side": None, "asset_ratio": 0.9, "certainty": 0}
    rows = [_row("u", "sensible", unknown), _row("c", "sensible", complete)]
    assert [x["key"] for x in tdv.rank(rows, lambda x: 0.0, lambda x: x["key"])] == ["c", "u"]
    assert [x["dual_value"]["rank"]["front"] for x in rows if x["key"] == "u"] == [None]


# ============================================================ no acceptance probability --

def _scan(x, path="", bad=None):
    """Keys naming a probability/acceptance and string claims of one (a sentence that mentions it without
    negating it)."""
    bad = [] if bad is None else bad
    if isinstance(x, dict):
        for k, v in x.items():
            if any(b in str(k).lower() for b in BANNED):
                bad.append(f"key {path}/{k}")
            _scan(v, f"{path}/{k}", bad)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            _scan(v, f"{path}[{i}]", bad)
    elif isinstance(x, str):
        for sentence in re.split(r"(?<=[.;])\s+", x):
            if any(b in sentence.lower() for b in BANNED) and not NEGATION.search(sentence):
                bad.append(f"claim {path}: {sentence}")
    return bad


def test_no_acceptance_probability(market):
    st, ts, rows = market
    board, fb = AssetBoard(st, ts.window), FreeAgencyBoard(st, ts.window)
    assessments = []
    for row in rows["exact"] + rows["heuristic"]:
        d = detail(st, row)
        assessments.append(tdv.for_evaluation(st, ts.window, 1, row["sends"], row["receives"], d["mine_detail"],
                                              d["theirs_detail"], asset_board=board, fa_board=fb,
                                              labels=("You", row["opponent_name"])))
    assessments += [assess_trade(market_league(), 1, 2, [105], [204, 209]),
                    assess_trade(fair_no_gain_league(), 1, 2, [108], [209]),
                    assess_trade(consolidation_league(), 1, 2, [105, 106], [204]),
                    assess_trade(playoff_league(), 1, 2, [107], [204], phase="playoff")]
    gone = market_league()
    del gone.projections[302][11]
    assessments.append(assess_trade(gone, 1, 3, [107], [302]))
    rows_ = [{"key": str(i), "dual_value": a} for i, a in enumerate(assessments)]
    tdv.rank(rows_, lambda x: None, lambda x: x["key"])
    for a in assessments:
        json.dumps(a)
        assert _scan(a) == []
    assert _scan([tdv.VERDICTS, tdv.CRITERIA, tdv.RANKING_POLICY]) == []
    # the scan itself detects a claim and a key
    assert _scan({"acceptance_probability": 0.7, "text": "Likely to be accepted."})
    assert _scan({"text": "This is not an acceptance estimate."}) == []


# ================================================================== exact search unchanged --

def _every_candidate(state, anchor):
    for opp in sorted(t for t in state.rosters if t != anchor):
        mine = sorted(p.espn_player_id for p in state.rosters[anchor])
        theirs = sorted(p.espn_player_id for p in state.rosters[opp])
        yield from (("1-for-1", opp, (s,), (t,)) for s in mine for t in theirs)
        yield from (("2-for-1", opp, ss, (t,)) for ss in combinations(mine, 2) for t in theirs)
        yield from (("1-for-2", opp, (s,), tt) for s in mine for tt in combinations(theirs, 2))


def test_exact_search_guarantees_unchanged():
    def build():
        return market_league()

    def search(st):
        return TradeSearch(st, 1, positions=None)
    fresh = _untimed(search(build()).top("min_side", None, None, 15))
    st = build()
    ts = search(st)
    res = ts.top("min_side", None, None, 15)
    snapshot = copy.deepcopy(res)
    fb = FreeAgencyBoard(st, ts.window)
    rows = []
    for r in res["results"]:
        d = detail(st, r)
        rows.append({**r, "dual_value": tdv.for_evaluation(st, ts.window, 1, r["sends"], r["receives"], d["mine_detail"],
                                                           d["theirs_detail"], fa_board=fb, labels=("You", r["opponent_name"]))})
    ranked = tdv.rank(rows, lambda x: x["dual_value"]["rank_axes"]["lineup_min_side"], _rank_key("min_side"))
    # assessing and ranking changed nothing in the rows the search returned, nor in a rerun, nor in a fresh search
    assert _untimed(res) == _untimed(snapshot)
    assert _untimed(ts.top("min_side", None, None, 15)) == _untimed(snapshot) == fresh
    assert _untimed(search(build()).top("min_side", None, None, 15)) == fresh
    assert [r["key"] for r in res["results"]] == [r["key"] for r in fresh["results"]]
    # the dual-value order is its own: the search order is not fed from it
    assert [x["key"] for x in ranked] != [r["key"] for r in res["results"]] and len(ranked) == len(rows)

    # best-first top K equals brute force on a small league, before and after assessments ran on its rows
    small = add_free_agents(synthetic_league(n_teams=3, weeks=range(12, 18), roster=SMALL_ROSTER, seed=11))
    sts = TradeSearch(small, 1, positions=None)
    exact = {}
    for c in _every_candidate(small, 1):
        exact[_key(*c)] = (c, sts._side(1, c[2], c[3]), sts._side(c[1], c[3], c[2]))
    brute_rows = [sts._row(c[0], c[1], c[2], c[3], dict(m), dict(t)) for c, m, t in exact.values()
                  if m["ros_delta"] is not None and (m["ros_delta"] > 0 or (m["playoff_delta"] or 0) > 0)]
    for sort in ("min_side", "my_gain", "min_side_playoff"):
        brute = [r["key"] for r in sorted(brute_rows, key=_rank_key(sort))[:12]]
        top = sts.top(sort, None, None, 12, include_redundant=True)
        assert [r["key"] for r in top["results"]] == brute and top["search"]["exhaustive"], sort
        assessed = []
        for r in top["results"]:
            d = detail(small, r)
            assessed.append({**r, "dual_value": tdv.for_evaluation(small, sts.window, 1, r["sends"], r["receives"],
                                                                    d["mine_detail"], d["theirs_detail"],
                                                                    labels=("You", r["opponent_name"]))})
        tdv.rank(assessed, lambda x: None, _rank_key(sort))
        again = sts.top(sort, None, None, 12, include_redundant=True)
        assert [r["key"] for r in again["results"]] == brute and _untimed(again["search"]) == _untimed(top["search"])


# ========================================================================= other anchors --

def test_different_team_selection():
    # Team 1's best free-agent move is a TE (its TE is weak); Team 2's is a WR (its WRs are weak)
    st = with_free_agents(market_league(), (907, "TE", 12), (908, "WR", 17))
    ts = TradeSearch(st, 2, shapes=tuple(SHAPES), positions=None)
    rows = ts.top("min_side", None, None, 6)["results"] + ts.top("my_gain", None, None, 4)["results"]
    assert rows and all(r["anchor_team_id"] == 2 for r in rows)
    fb = FreeAgencyBoard(st, ts.window)
    ctx1, ctx2 = tdv.anchor_fa_context(fb, st, 1), tdv.anchor_fa_context(fb, st, 2)
    assert ctx2["before"]["summary"]["ros_projected_starting_points"] == \
        project_team(st.rosters[2], st.slots, ts.window, st.projections)["summary"]["ros_projected_starting_points"]
    assert ctx2["before"]["summary"] != ctx1["before"]["summary"]
    assert ctx1["alt"]["player"]["espn_player_id"] == 907 and ctx2["alt"]["player"]["espn_player_id"] == 908
    differs = 0
    for row in rows:
        d = detail(st, row)
        labels = ("You", row["opponent_name"])
        args = (st, ts.window, 2, row["sends"], row["receives"], d["mine_detail"], d["theirs_detail"])
        dv = tdv.for_evaluation(*args, fa_board=fb, labels=labels)
        assert dv["lineup"]["you"]["label"] == "You" and dv["lineup"]["you"]["team_id"] == 2
        assert dv["lineup"]["you"]["team_name"] == "Team 2" and dv["lineup"]["them"]["team_id"] == row["opponent_team_id"]
        res = evaluate_trade(st, 2, row["opponent_team_id"], row["sends"], row["receives"])
        for who, side in (("you", res["team_a"]), ("them", res["team_b"])):
            assert dv["lineup"][who]["delta"] == {"ros": side["deltas"]["ros_delta"], "regular": side["deltas"]["regular_season_delta"],
                                                  "playoff": side["deltas"]["playoff_delta"], "known_ros": side["deltas"]["known_ros_delta"]}
        assert dv["asset"]["you_send"][0]["from_side"] == "You"
        # team 2's own free-agent context is the one used (an explicit context equals the implicit one) ...
        assert dv == tdv.for_evaluation(*args, fa_board=fb, fa_context=ctx2, labels=labels)
        # ... and team 1's context would give a different free-agent check for some rows
        wrong = tdv.for_evaluation(*args, fa_board=fb, fa_context=ctx1, labels=labels)
        differs += check_text(wrong, "free_agent_alternative") != check_text(dv, "free_agent_alternative") or \
            status(wrong, "free_agent_alternative") != status(dv, "free_agent_alternative")
    assert differs, "team 1's free-agent context never differed from team 2's on these rows"


def test_no_implicit_source_change(tmp_path, monkeypatch):
    st = market_league()
    proj, rosters, free = copy.deepcopy(st.projections), copy.deepcopy(st.rosters), copy.deepcopy(st.free_agents)
    dv = assess_trade(st, 1, 2, [105], [204, 209])
    ts = TradeSearch(st, 1, positions=None)
    row = ts.top("min_side", None, None, 1)["results"][0]
    d = detail(st, row)
    tdv.for_evaluation(st, ts.window, 1, row["sends"], row["receives"], d["mine_detail"], d["theirs_detail"],
                       fa_board=FreeAgencyBoard(st, ts.window))
    assert st.projections == proj and st.rosters == rosters and st.free_agents == free
    assert dv["projection_source"] == "espn"
    src = open(tdv.__file__).read().lower()
    assert not any(s in src for s in ("independent_projections", "forward_ours", "ifp-2", "trade_ratings"))

    db_path = tmp_path / "src.duckdb"
    con = synced(db_path)
    raw = _raw_snapshot(con)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("fantasy_teams", "rosters", "projections_espn", "team_ros_lineup_projections")
              if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [t]).fetchone()[0]}
    con.close()
    mtime = db_path.stat().st_mtime_ns
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    from app.api.deps import data_version
    ro = connect(db_path, read_only=True)
    version = data_version(ro)
    ro.close()
    res = TestClient(app).post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [304],
                                                             "team_b_sends": [301, 302]}).json()
    assert res["dual_value"]["projection_source"] == "espn" and res["projection_source"]
    assert db_path.stat().st_mtime_ns == mtime
    con = connect(db_path, read_only=True)
    assert data_version(con) == version and _raw_snapshot(con) == raw
    assert counts and all(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n for t, n in counts.items())
    con.close()


# ============================================================= Pareto fronts and ranking --

def _axes(lineup, ratio, certainty):
    return {"lineup_min_side": lineup, "asset_ratio": ratio, "certainty": certainty}


def _row(key, verdict, axes, primary=0.0):
    return {"key": key, "primary": primary,
            "dual_value": {"rank_axes": axes, "plausibility": {"verdict": verdict}}}


def test_pareto_fronts_dominance_ties_and_unknown_axes():
    axes = [_axes(10, .9, 0),          # 0: front 0
            _axes(8, .5, -1),          # 1: dominated by 0 -> front 1
            _axes(10, .9, 0),          # 2: ties with 0 -> shares front 0 (a tie is not domination)
            _axes(12, .3, 0),          # 3: better on lineup, worse on ratio -> front 0
            _axes(None, .9, 0),        # 4: unknown axis -> no front
            _axes(5, .1, -2),          # 5: dominated by 1 (and everything above) -> front 2
            {"lineup_min_side": 20, "asset_ratio": 1.0},      # 6: a missing key is unknown, not 0
            _axes(8, .5, -1)]          # 7: ties with 1 -> front 1
    assert tdv.pareto_fronts(axes) == [0, 1, 0, 0, None, 2, None, 1]
    assert tdv.pareto_fronts([]) == []
    assert tdv.pareto_fronts([_axes(None, None, 0)]) == [None]
    assert tdv.pareto_fronts([_axes(0.0, 0.0, 0)]) == [0]                  # a real 0 is a value
    assert tdv._dominates(_axes(1, 1, 0), _axes(1, 0.5, 0)) and not tdv._dominates(_axes(1, 1, 0), _axes(1, 1, 0))
    assert not tdv._dominates(_axes(2, .1, 0), _axes(1, .9, 0)) and not tdv._dominates(_axes(1, .9, 0), _axes(2, .1, 0))


def test_rank_orders_verdict_then_front_then_primary_then_tiebreak():
    big = _axes(100, 1.0, 0)
    rows = [
        _row("c-unlikely-big", "unlikely_mutual", big, primary=100),
        _row("b-sensible-dominated", "sensible", _axes(2, .5, 0), primary=9),        # front 1 inside 'sensible'
        _row("a-sensible-front0-low", "sensible", _axes(3, .6, 0), primary=1),
        _row("a-sensible-front0-high", "sensible", _axes(1, .9, 0), primary=5),
        _row("a-sensible-front0-none", "sensible", _axes(4, .4, 0), primary=None),    # unknown primary: last in front
        _row("a-sensible-tie-b", "sensible", _axes(2, .8, 0), primary=5),             # same primary as -high -> key order
        _row("z-incomplete", "incomplete", _axes(None, None, 0), primary=None),
        _row("y-one-sided", "one_sided", big, primary=100),
        _row("x-concerns", "sensible_with_concerns", _axes(0.1, .2, -4), primary=0.1),
    ]
    ranked = tdv.rank(rows, lambda x: x["primary"], lambda x: x["key"])
    assert [x["key"] for x in ranked] == [
        "a-sensible-front0-high", "a-sensible-tie-b",               # primary 5, then key order
        "a-sensible-front0-low", "a-sensible-front0-none",          # primary 1, then unknown primary
        "b-sensible-dominated",                                     # a later front, whatever its primary
        "x-concerns", "c-unlikely-big", "y-one-sided", "z-incomplete"]
    fronts = {x["key"]: x["dual_value"]["rank"]["front"] for x in ranked}
    assert fronts["b-sensible-dominated"] > fronts["a-sensible-front0-high"] and fronts["z-incomplete"] is None


def test_axes_are_never_summed():
    # (1) a dominated row stays behind its dominator whatever its view measure; fronts, not scores, decide
    dominator, dominated = _axes(5.0, 0.9, 0), _axes(5.0, 0.5, 0)
    rows = [_row("dominated", "sensible", dominated, primary=1000.0), _row("dominator", "sensible", dominator, primary=1.0)]
    assert [x["key"] for x in tdv.rank(rows, lambda x: x["primary"], lambda x: x["key"])] == ["dominator", "dominated"]
    # (2) two non-dominated rows share a front; a summed score (points + ratio + certainty) prefers X, yet the
    #     order follows the view's own measure, so it flips with the measure and not with any sum
    x, y = _axes(10.0, 0.2, 0), _axes(9.0, 1.0, 0)
    assert sum(x.values()) > sum(y.values())
    pair = lambda: [_row("X", "sensible", x), _row("Y", "sensible", y)]       # noqa: E731
    by_ratio = tdv.rank(pair(), lambda r: r["dual_value"]["rank_axes"]["asset_ratio"], lambda r: r["key"])
    by_points = tdv.rank(pair(), lambda r: r["dual_value"]["rank_axes"]["lineup_min_side"], lambda r: r["key"])
    assert [r["key"] for r in by_ratio] == ["Y", "X"] and [r["key"] for r in by_points] == ["X", "Y"]
    assert {r["dual_value"]["rank"]["front"] for r in by_ratio} == {0}
    # (3) the verdict tier outranks any amount of points
    rows = [_row("big", "unlikely_mutual", _axes(500.0, 1.0, 0), primary=500.0), _row("small", "sensible", _axes(0.5, .7, -2), primary=0.5)]
    assert [r["key"] for r in tdv.rank(rows, lambda r: r["primary"], lambda r: r["key"])] == ["small", "big"]
    # (4) nothing carries a combined score
    assert set(rows[0]["dual_value"]["rank"]) == {"front", "policy"} and tdv.AXES == ("lineup_min_side", "asset_ratio", "certainty")
    dv = assess_trade(market_league(), 1, 2, [105], [204, 209])
    assert set(dv["rank_axes"]) == set(tdv.AXES)
    assert not any(re.search(r"score|total|sum|combined|weight", k) for k in dv["rank_axes"])


def test_ranking_on_real_assessments_puts_verdict_first(market):
    st, ts, rows = market
    board, fb = AssetBoard(st, ts.window), FreeAgencyBoard(st, ts.window)
    pool = []
    for row in rows["exact"] + rows["heuristic"]:
        d = detail(st, row)
        pool.append({**row, "dual_value": tdv.for_evaluation(st, ts.window, 1, row["sends"], row["receives"],
                                                             d["mine_detail"], d["theirs_detail"], asset_board=board,
                                                             fa_board=fb, labels=("You", row["opponent_name"]))})
    ranked = tdv.rank(pool, lambda x: x["dual_value"]["rank_axes"]["lineup_min_side"], _rank_key("min_side"))
    tiers = [tdv.VERDICT_ORDER[x["dual_value"]["plausibility"]["verdict"]] for x in ranked]
    assert tiers == sorted(tiers) and len(set(tiers)) >= 2
    assert all(isinstance(x["dual_value"]["rank"]["front"], int) for x in ranked)


# ====================================================================== the six checks --

def side(delta=10.0, starts=(4,), n=4, missing=()):
    return {"weekly": [None] * n,
            "deltas": {"ros_delta": delta, "regular_season_delta": delta, "playoff_delta": delta},
            "incoming_usage": [{"weeks_started": list(range(k))} for k in starts],
            "incomplete": {"missing_players": list(missing)}}


def drop(pid=9, par=10.0, caliber=False):
    return {"espn_player_id": pid, "name": f"X{pid}", "position": "RB", "par": par, "starter_caliber": caliber}


def asset(imbalance="none", you=40.0, they=40.0, you_drops=(), they_drops=(), you_empty=(), they_empty=()):
    return {"imbalance": imbalance, "text": "Asset text", "you_send_par": you, "they_send_par": they,
            "depth": {"you": {"forced_drops": list(you_drops), "empty_slots_after": list(you_empty)},
                      "them": {"forced_drops": list(they_drops), "empty_slots_after": list(they_empty)}}}


LABELS = ("You", "Them")


def run(mine=None, theirs=None, a=None, injuries=(), phase="ros", fa=None):
    return tdv.checks(mine or side(), theirs or side(), a or asset(), list(injuries), phase, fa, LABELS)


def test_check_lineup_gain_paths():
    assert status(run(), "lineup_gain") == "pass"
    assert status(run(theirs=side(delta=-1.0)), "lineup_gain") == "fail"
    zero = run(mine=side(delta=0.0))
    assert status(zero, "lineup_gain") == "fail"                     # zero is not an improvement
    assert "your starting lineup does not improve" in next(c["text"] for c in zero if c["criterion"] == "lineup_gain").lower()
    both = next(c["text"] for c in run(mine=side(-2.0), theirs=side(0.0)) if c["criterion"] == "lineup_gain")
    assert "your" in both.lower() and "Them's" in both
    assert status(run(mine=side(delta=None)), "lineup_gain") == "unknown"
    # the phase picks the delta
    po = {"ros_delta": 5.0, "regular_season_delta": 5.0, "playoff_delta": -1.0}
    assert status(run(mine=side() | {"deltas": po}, phase="playoff"), "lineup_gain") == "fail"
    assert status(run(mine=side() | {"deltas": po}, phase="regular"), "lineup_gain") == "pass"


def test_check_asset_balance_paths():
    assert status(run(a=asset("none")), "asset_balance") == "pass"
    assert status(run(a=asset("moderate")), "asset_balance") == "concern"
    assert status(run(a=asset("significant")), "asset_balance") == "fail"
    assert status(run(a=asset(None, you=None, they=None)), "asset_balance") == "unknown"


def test_check_positional_fit_paths():
    assert status(run(), "positional_fit") == "pass"
    assert status(run(mine=side(starts=(2,))), "positional_fit") == "pass"           # exactly half the weeks
    assert status(run(theirs=side(starts=(1, 0))), "positional_fit") == "concern"     # best incoming starts 1 of 4
    assert status(run(mine=side(starts=(0,))), "positional_fit") == "fail"            # nobody ever starts
    assert status(run(mine=side(starts=(0, 0)), theirs=side(starts=(1,))), "positional_fit") == "fail"


def test_check_depth_cost_paths():
    assert status(run(), "depth_cost") == "pass"
    assert status(run(a=asset(they_drops=[drop(par=0.0)])), "depth_cost") == "pass"          # a drop with no asset value
    assert status(run(a=asset(they_drops=[drop(par=10.0)])), "depth_cost") == "concern"
    assert status(run(a=asset(you_drops=[drop(par=30.0, caliber=True)])), "depth_cost") == "fail"
    assert status(run(a=asset(you_empty=[{"week": 11, "slot": "RB"}])), "depth_cost") == "fail"
    assert status(run(a=asset(they_drops=[drop(par=None)])), "depth_cost") == "unknown"
    both = run(a=asset(they_drops=[drop(par=None)], you_drops=[drop(2, 5.0)]))
    assert status(both, "depth_cost") == "concern"                                           # a known concern wins over unknown


def test_check_free_agent_alternative_paths():
    assert status(run(fa=None), "free_agent_alternative") == "not_applicable"
    assert status(run(fa={"trade_advantage": None}), "free_agent_alternative") == "unknown"
    assert status(run(fa={"trade_advantage": 0.0, "has_concern": True}), "free_agent_alternative") == "fail"
    assert status(run(fa={"trade_advantage": -3.0, "has_concern": True, "lead": "L"}), "free_agent_alternative") == "fail"
    concern = run(fa={"trade_advantage": 2.0, "has_concern": True, "lead": "Most of it is available."})
    assert status(concern, "free_agent_alternative") == "concern"
    assert next(c["text"] for c in concern if c["criterion"] == "free_agent_alternative") == "Most of it is available."
    assert status(run(fa={"trade_advantage": 5.0, "has_concern": False}), "free_agent_alternative") == "pass"


def test_check_free_agent_alternative_end_to_end():
    st = market_league()
    # no free agents in the state: the analyzer passes no board and the check does not apply
    bare = replace(st, free_agents=[])
    assert status(assess_trade(bare, 1, 2, [108], [209]), "free_agent_alternative") == "not_applicable"
    # a free-agent TE (12) nearly matches Bowers (13): most of the gain is available without trading
    near = with_free_agents(market_league(), (907, "TE", 12))
    near.projections[205], near.projections[206] = flat(20), flat(19)
    dv = assess_trade(near, 1, 2, [108], [209])
    assert dv["lineup"]["you"]["delta"]["ros"] > 0 and status(dv, "free_agent_alternative") == "concern"
    assert "available without trading" in check_text(dv, "free_agent_alternative")
    # a free-agent TE (14) is better than Bowers: after that move the trade adds nothing
    better = with_free_agents(market_league(), (907, "TE", 14))
    better.projections[205], better.projections[206] = flat(20), flat(19)
    dv = assess_trade(better, 1, 2, [108], [209])
    assert status(dv, "free_agent_alternative") == "fail" and "available without trading" in check_text(dv, "free_agent_alternative")
    # a trade that is not covered by free agency passes
    assert status(assess_trade(market_league(), 1, 3, [107], [302]), "free_agent_alternative") == "pass"


def test_check_uncertainty_paths():
    flag = {"espn_player_id": 1, "status": "x", "warnings": ["Out for the season."]}
    assert status(run(), "uncertainty") == "pass"
    assert status(run(injuries=[flag]), "uncertainty") == "concern"
    assert next(c["text"] for c in run(injuries=[flag]) if c["criterion"] == "uncertainty") == "Out for the season."
    assert status(run(mine=side(missing=[5])), "uncertainty") == "unknown"
    assert status(run(mine=side(missing=[5]), injuries=[flag]), "uncertainty") == "concern"
    assert status(run(injuries=[{"espn_player_id": 2, "warnings": []}]), "uncertainty") == "concern"
    # end to end: an injured traded player is a concern and one more certainty flag
    st = market_league()
    st.rosters[2] = [P(p.espn_player_id, p.position, p.name, status="OUT") if p.espn_player_id == 204 else p
                     for p in st.rosters[2]]
    dv = assess_trade(st, 1, 2, [105], [204, 209])
    clean = assess_trade(market_league(), 1, 2, [105], [204, 209])
    assert dv["rank_axes"]["certainty"] <= clean["rank_axes"]["certainty"] and clean["rank_axes"]["certainty"] == 0


def _statuses(**kw):
    base = {"lineup_gain": "pass", "asset_balance": "pass", "positional_fit": "pass", "depth_cost": "pass",
            "free_agent_alternative": "pass", "uncertainty": "pass"} | kw
    return [{"criterion": k, "status": v, "text": ""} for k, v in base.items()]


@pytest.mark.parametrize("statuses,losers,expected", [
    ({}, [], "sensible"),
    ({"free_agent_alternative": "not_applicable"}, [], "sensible"),
    ({"depth_cost": "concern"}, [], "sensible_with_concerns"),
    ({"uncertainty": "unknown"}, [], "sensible_with_concerns"),
    ({"positional_fit": "fail"}, [], "sensible_with_concerns"),
    ({"asset_balance": "concern"}, [], "sensible_with_concerns"),
    ({"asset_balance": "fail"}, [], "unlikely_mutual"),
    ({"asset_balance": "fail", "depth_cost": "fail"}, [], "unlikely_mutual"),
    ({"lineup_gain": "fail"}, ["Them"], "fair_no_lineup_gain"),
    ({"lineup_gain": "fail", "asset_balance": "concern"}, ["Them"], "one_sided"),
    ({"lineup_gain": "fail", "asset_balance": "fail"}, ["You", "Them"], "one_sided"),
    ({"lineup_gain": "unknown"}, [], "incomplete"),
    ({"asset_balance": "unknown"}, [], "incomplete"),
    ({"lineup_gain": "fail", "asset_balance": "unknown"}, ["You"], "incomplete"),
])
def test_verdict_rule(statuses, losers, expected):
    v, text = tdv.verdict(_statuses(**statuses), LABELS, losers)
    assert v == expected and text == tdv.VERDICTS[expected].format(who=" and ".join(tdv.possessive(x) for x in losers) or "a")
    assert tdv.VERDICT_ORDER[v] == list(tdv.VERDICTS).index(v)


def test_possessive_and_key_change_names():
    assert tdv.possessive("You") == "your" and tdv.possessive("They") == "their" and tdv.possessive("Team 2") == "Team 2's"


# ======================================================= regressions: contract fixes --

def test_key_change_keeps_the_team_name_case():
    st = bench_receiver_league()
    dv = assess_trade(st, 1, 2, [107], [209], labels=("Nico Robin FC", "Team 2"))    # a starter out, a benched Bowers in
    assert dv["lineup"]["you"]["key_change"].startswith("Nico Robin FC's lineup loses")


def test_free_agent_check_does_not_claim_a_gain_when_there_is_none():
    dv = assess_trade(bench_receiver_league(), 1, 2, [109], [209])
    assert dv["lineup"]["you"]["delta"]["ros"] <= 0
    assert "this gain is available" not in check_text(dv, "free_agent_alternative")


def test_free_agent_check_follows_the_playoff_phase():
    # Team 1 gives up regular-season points (WR 15 for Closer) for a playoff-week gain: ros -4, playoffs +8 (Team 2: +7)
    dv = assess_trade(playoff_league(), 1, 2, [105], [204], phase="playoff")
    you = dv["lineup"]["you"]["delta"]
    assert you["playoff"] > 0 >= you["ros"] and dv["lineup"]["them"]["delta"]["playoff"] > 0
    assert status(dv, "lineup_gain") == "pass" and status(dv, "free_agent_alternative") != "fail"


def test_fronts_are_computed_within_a_verdict():
    rows = [_row("s1", "sensible", _axes(5, .5, 0), primary=10.0), _row("s2", "sensible", _axes(3, .9, 0), primary=1.0),
            _row("u", "unlikely_mutual", _axes(6, .6, 0), primary=6.0)]
    ranked = tdv.rank(rows, lambda x: x["primary"], lambda x: x["key"])
    # s1 and s2 do not dominate each other, so they share front 0 and the view's measure decides: s1 first
    assert [x["key"] for x in ranked][:2] == ["s1", "s2"]


def test_multiple_strong_assets_within_the_balanced_ratio_is_a_moderate_imbalance():
    """Two starter-caliber assets for one is significant only when the packages are also far apart."""
    st = market_league()
    board = AssetBoard(st, st.window())
    seen = set()
    for opp, sends, receives in ((2, [105], [204, 209]), (3, [106, 107], [303]), (2, [106, 108], [202])):
        res = evaluate_trade(st, 1, opp, sends, receives)
        asset = tdv.asset_block(board, _side_detail(res["team_a"]), _side_detail(res["team_b"]), sends, receives,
                                ("You", "They"))
        assert asset["label"].endswith("surrender_multiple_strong")
        assert asset["imbalance"] == ("significant" if asset["ratio"] < BALANCED_RATIO else "moderate")
        seen.add(asset["imbalance"])
    assert seen == {"significant", "moderate"}

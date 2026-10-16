"""The 0-100 ratings adapter (analytics/trade_ratings.py): reads player_scores only when the ratings-100.v1 columns
exist, keys ratings by ESPN id through player_id_map, never turns a missing rating into 0, skips unsupported
scale versions, pairs only like with like, never sums, and never reaches an assessment. In-memory duckdb with
invented players; the ratings branch itself is not needed."""

import inspect
import json

import duckdb

from app.analytics import trade_dual_value as tdv
from app.analytics import trade_ratings as tr

from synthetic_v3 import market_league
from test_trade_dual_value import assess_trade

SEASON = 2026
V1 = "ratings-100.v1"
ID_MAP = [(101, "g101"), (102, "g102"), (103, "g103"), (104, "g104"), (105, "g105"), (106, "g106")]


def db(columns=True, scores=(), id_map=ID_MAP, legacy=False):
    """player_id_map + player_scores; `columns=False` is a database from before the ratings branch landed."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE player_id_map (espn_player_id INTEGER, gsis_id VARCHAR)")
    con.executemany("INSERT INTO player_id_map VALUES (?, ?)", list(id_map))
    ratings = ("opportunity_score_100 DOUBLE, production_score_100 DOUBLE, value_score_100 DOUBLE, "
               "rating_scale_version VARCHAR, ") if columns else ""
    old = "value_score DOUBLE, " if legacy else ""
    con.execute(f"CREATE TABLE player_scores (gsis_id VARCHAR, season INTEGER, position VARCHAR, "
                f"opportunity_basis VARCHAR, games INTEGER, {old}{ratings}opportunity_score DOUBLE)")
    for s in scores:
        cols = ["gsis_id", "season", "position", "opportunity_basis", "games"]
        vals = [s["gsis_id"], s.get("season", SEASON), s["position"], s.get("basis"), s.get("games", 10)]
        if columns:
            cols += ["opportunity_score_100", "production_score_100", "value_score_100", "rating_scale_version"]
            vals += [s.get("opp"), s.get("prod"), s.get("value"), s.get("version", V1)]
        if legacy:
            cols.append("value_score")
            vals.append(s.get("legacy"))
        con.execute(f"INSERT INTO player_scores ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return con


def score(gsis, position="WR", basis="xfp", opp=70.0, prod=60.0, value=65.0, **kw):
    return {"gsis_id": gsis, "position": position, "basis": basis, "opp": opp, "prod": prod, "value": value, **kw}


def test_no_player_scores_table_is_unavailable_with_a_reason():
    con = duckdb.connect(":memory:")
    out = tr.load_ratings(con, SEASON, [101])
    assert out["available"] is False and out["reason"] == "no player_scores table"
    assert out["players"] == {} and out["version"] is None and out["note"] == tr.NOTE
    con.execute("CREATE TABLE player_scores (gsis_id VARCHAR, season INTEGER)")        # but no player_id_map
    assert tr.load_ratings(con, SEASON, [101])["available"] is False


def test_table_without_the_100_columns_is_unavailable_with_a_reason():
    con = db(columns=False, scores=[score("g101")], legacy=False)
    out = tr.load_ratings(con, SEASON, [101])
    assert out["available"] is False and "no 0-100 rating columns" in out["reason"] and out["players"] == {}
    # one missing column is enough: the version column alone is not a rating
    con2 = duckdb.connect(":memory:")
    con2.execute("CREATE TABLE player_id_map (espn_player_id INTEGER, gsis_id VARCHAR)")
    con2.execute("CREATE TABLE player_scores (gsis_id VARCHAR, season INTEGER, position VARCHAR, opportunity_basis VARCHAR, "
                 "games INTEGER, opportunity_score_100 DOUBLE, production_score_100 DOUBLE, value_score_100 DOUBLE)")
    assert tr.load_ratings(con2, SEASON, [101])["available"] is False


def test_ratings_are_keyed_by_espn_id_through_the_id_map():
    con = db(scores=[score("g101", "WR", "xfp", 70, 60, 65), score("g102", "RB", "targets+carries", 40, 55, 50),
                     score("g103", "WR", "xfp", 90, 80, 85, season=2025),          # another season
                     score("g999", "WR", "xfp", 1, 1, 1)])                         # no id-map row
    out = tr.load_ratings(con, SEASON, [101, 102, 103, 104])
    assert out["available"] is True and out["reason"] is None and out["version"] == V1
    assert set(out["players"]) == {101, 102}                                      # keyed by int ESPN id; 103 other season, 104 none
    assert out["players"][101] == {"position": "WR", "opportunity_basis": "xfp", "games": 10, "opportunity_score_100": 70.0,
                                   "production_score_100": 60.0, "value_score_100": 65.0, "rating_scale_version": V1}
    assert out["players"][102]["opportunity_basis"] == "targets+carries"
    # only the requested ids are read; no ids is an available, empty result
    assert set(tr.load_ratings(con, SEASON, [102])["players"]) == {102}
    assert set(tr.load_ratings(con, SEASON, ["101", None, 101])["players"]) == {101}
    empty = tr.load_ratings(con, SEASON, [])
    assert empty["available"] is True and empty["players"] == {}


def test_null_ratings_are_excluded_never_zero():
    con = db(scores=[score("g101", opp=None, prod=None, value=None),               # no rating at all: absent
                     score("g102", opp=55.0, prod=None, value=None),               # partial: the missing parts stay None
                     score("g103", opp=0.0, prod=0.0, value=0.0)],                 # a real 0 is a rating
              legacy=True)
    out = tr.load_ratings(con, SEASON, [101, 102, 103, 104])
    assert 101 not in out["players"] and 104 not in out["players"]
    assert out["players"][102]["production_score_100"] is None and out["players"][102]["value_score_100"] is None
    assert out["players"][102]["opportunity_score_100"] == 55.0
    assert out["players"][103]["value_score_100"] == 0.0
    ev = tr.evidence(out, [101, 102], [103, 104])
    assert ev["players"][101] is None and ev["players"][104] is None             # missing is None, never 0 or {}
    assert ev["players"][103] is not None
    # a legacy 0-10 value is never converted into a 0-100 rating
    legacy_only = db(scores=[score("g101", opp=None, prod=None, value=None, legacy=8.5)], legacy=True)
    assert 101 not in tr.load_ratings(legacy_only, SEASON, [101])["players"]


def test_unsupported_scale_version_is_reported_and_skipped():
    con = db(scores=[score("g101"), score("g102", version="ratings-100.v2"), score("g103", version=None)])
    out = tr.load_ratings(con, SEASON, [101, 102, 103])
    assert set(out["players"]) == {101}
    assert out["reason"] == "unsupported rating scale version(s): ratings-100.v2"
    assert tr.SUPPORTED_VERSIONS == (V1,)
    only_v2 = tr.load_ratings(db(scores=[score("g101", version="ratings-100.v2")]), SEASON, [101])
    assert only_v2["players"] == {} and "ratings-100.v2" in only_v2["reason"]


def test_comparable_pairs_only_the_same_position_and_opportunity_basis():
    wr_xfp = {"position": "WR", "opportunity_basis": "xfp"}
    assert tr.comparable(wr_xfp, {"position": "WR", "opportunity_basis": "xfp"}) is True
    assert tr.comparable(wr_xfp, {"position": "RB", "opportunity_basis": "xfp"}) is False
    assert tr.comparable(wr_xfp, {"position": "WR", "opportunity_basis": "targets+carries"}) is False
    assert tr.comparable(wr_xfp, None) is False and tr.comparable(None, wr_xfp) is False
    assert tr.comparable(wr_xfp, {"opportunity_basis": "xfp"}) is False              # an unknown position pairs with nothing
    assert tr.comparable({"opportunity_basis": "xfp"}, {"opportunity_basis": "xfp"}) is False


def test_evidence_pairs_like_with_like_and_never_sums():
    con = db(scores=[score("g101", "WR", "xfp", 70, 60, 65), score("g102", "WR", "xfp", 50, 40, 45),
                     score("g103", "RB", "xfp", 80, 70, 75), score("g104", "WR", "targets+carries", 30, 20, 25)])
    out = tr.load_ratings(con, SEASON, [101, 102, 103, 104, 105])
    ev = tr.evidence(out, sends=[101, 103], receives=[102, 104, 105])
    assert set(ev) == {"available", "reason", "version", "players", "comparable_pairs", "note"}
    assert ev["available"] is True and ev["version"] == V1 and ev["note"] == tr.NOTE
    assert ev["comparable_pairs"] == [{"you_send": 101, "you_receive": 102, "position": "WR"}]     # not RB-WR, not xfp-vs-targets
    assert ev["players"][105] is None
    assert ev["players"][101] == out["players"][101] and ev["players"][103] == out["players"][103]    # stored values, untouched
    # nothing in the evidence is a total, a combined rating or a difference of two players' ratings
    flat = json.dumps({k: v for k, v in ev.items() if k != "note"})
    assert not any(w in flat.lower() for w in ("total", "combined", "sum", "delta", "diff"))
    stored = {v for p in out["players"].values() for k, v in p.items() if k.endswith("_100")}
    numbers = {x for p in ev["players"].values() if p for k, x in p.items() if k.endswith("_100")}
    assert numbers <= stored
    # unavailable ratings: the evidence says so and every player is None
    off = tr.evidence(tr.unavailable("no player_scores table"), [101], [102])
    assert off["available"] is False and off["reason"] == "no player_scores table"
    assert off["players"] == {101: None, 102: None} and off["comparable_pairs"] == []


def test_ratings_never_change_an_assessment():
    for fn in (tdv.assess, tdv.for_evaluation, tdv.checks, tdv.verdict, tdv.rank, tdv.pareto_fronts):
        assert not any("rating" in p.lower() for p in inspect.signature(fn).parameters), fn.__name__
    assert "trade_ratings" not in inspect.getsource(tdv) and "rating" not in inspect.getsource(tdv.assess).lower()
    before = assess_trade(market_league(), 1, 2, [105], [204, 209])
    con = db(scores=[score("g101", opp=99.0, prod=99.0, value=99.0), score("g102", opp=1.0, prod=1.0, value=1.0)])
    out = tr.load_ratings(con, SEASON, [101, 102])
    tr.evidence(out, [101], [102])
    after = assess_trade(market_league(), 1, 2, [105], [204, 209])
    assert before == after
    assert "rating" not in json.dumps(before).lower().replace("rating_scale", "")

"""Versioned 0-100.0 player ratings (ratings-100.v1) next to the unchanged legacy 0-10 policy scale."""

import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.analytics import waivers
from app.analytics.player_scores import (RATING_SCALE_VERSION, blended_value, blended_value_100, percentile_scores,
                                         percentile_scores_100, score_players)
from app.analytics.waivers import PlayerValue, evaluate_candidate, load_values, team_needs, team_state
from app.analytics.pipeline import run_analytics
from app.db.connection import connect, open_db, table_columns
from app.espn.sync import run_espn_sync

from espn_fixture import MY_SWID, SEASON, FakeEspnClient
from test_waivers import LSA, SLOTS, my_roster, p, values


# ------------------------------------------------------------ percentile_scores_100 --

def _pool(n):
    return {f"p{i}": float(i) * 1.7 + (i % 5) * 0.01 for i in range(n)}


@pytest.mark.parametrize("n", [37, 200])
def test_raw_granularity_beats_ten_times_legacy(n):
    pool = _pool(n)
    new, old = percentile_scores_100(pool), percentile_scores(pool)
    assert any(abs(v - round(v)) > 1e-9 for v in new.values())                 # not all whole numbers
    assert any(round(new[k], 1) != round(10 * old[k], 1) for k in pool)        # not just 10x the rounded legacy
    assert len({round(v, 1) for v in new.values()}) >= len(set(old.values()))
    assert len({round(v, 1) for v in new.values()}) == n                       # every distinct rank resolves


def test_documented_example_is_not_ten_times_legacy():
    pool = {f"p{i}": float(i) for i in range(37)}
    k = "p22"                                                                  # 22 below, 1 equal (itself)
    assert percentile_scores(pool)[k] == 6.1
    assert round(percentile_scores_100(pool)[k], 1) == 60.8


def test_ties_share_the_average_rank():
    pct = percentile_scores_100({"a": 1, "b": 2, "c": 3, "d": 3})
    assert pct["c"] == pct["d"] == pytest.approx(100 * (2 + 0.5 * 2) / 4)
    assert pct["a"] == pytest.approx(12.5) and pct["b"] == pytest.approx(37.5)
    assert set(percentile_scores_100({k: 5.0 for k in "abcd"}).values()) == {50.0}


def test_empty_and_single_player_pools():
    assert percentile_scores_100({}) == {}
    assert percentile_scores_100({"solo": 4.2}) == {"solo": 50.0}


def test_top_is_never_forced_to_100_and_bottom_is_not_zero():
    n = 40
    pct = percentile_scores_100({f"p{i}": float(i) for i in range(n)})
    assert max(pct.values()) == pytest.approx(100 * (n - 0.5) / n) == pytest.approx(98.75)
    assert min(pct.values()) == pytest.approx(50 / n) == pytest.approx(1.25)
    assert round(max(pct.values()), 1) == 98.8 and round(min(pct.values()), 1) == 1.2   # exact halves round to even


def test_output_is_independent_of_input_order():
    pool = _pool(60) | {"tie1": 3.4, "tie2": 3.4}
    items = list(pool.items())
    base = percentile_scores_100(pool)
    for seed in range(5):
        random.Random(seed).shuffle(items)
        assert percentile_scores_100(dict(items)) == base


# --------------------------------------------------------------- blended_value_100 --

def test_blended_value_100_missing_inputs_follow_legacy_fallbacks():
    assert blended_value_100(None, None, 0.5) is None
    assert blended_value_100(80.0, None, 0.5) == pytest.approx(85.0)           # opp stands in for prod
    assert blended_value_100(None, 60.0, -0.2) == pytest.approx(58.0)          # prod stands in for opp
    assert blended_value_100(60.0, 40.0, None) == pytest.approx(55.0)          # no trend -> 0
    assert blended_value_100(60.0, 40.0, 0.5) == pytest.approx(60.0)


def test_blended_value_100_is_unrounded_and_clamped():
    assert blended_value_100(33.3, 66.6, 0.123) == pytest.approx(0.75 * 33.3 + 0.25 * 66.6 + 1.23)
    assert blended_value_100(99.0, 99.0, 1.0) == 100.0
    assert blended_value_100(2.0, 1.0, -1.0) == 0.0


@pytest.mark.parametrize("args", [(8.0, 6.0, 0.5), (None, 6.0, 0.3), (7.3, None, -0.4), (9.8, 9.6, 1.0), (0.5, 0.2, -1.0),
                                  (5.0, 5.0, None), (None, None, 0.5)])
def test_blended_value_100_is_the_legacy_formula_on_the_100_scale(args):
    legacy = blended_value(*args)
    new = blended_value_100(None if args[0] is None else args[0] * 10, None if args[1] is None else args[1] * 10,
                            args[2])
    if legacy is None:
        assert new is None
    else:
        assert new == pytest.approx(10 * legacy, abs=0.06)


# ------------------------------------------------------------- legacy is unchanged --

def test_legacy_percentile_scores_golden():
    v = {"a": 1, "b": 2, "c": 3, "d": 3, "e": 7, "f": 0.5, "g": 9, "h": 9, "i": 9}
    assert percentile_scores(v) == {"a": 1.7, "b": 2.8, "c": 4.4, "d": 4.4, "e": 6.1, "f": 0.6, "g": 8.3, "h": 8.3,
                                    "i": 8.3}
    assert percentile_scores({"solo": 4}) == {"solo": 5.0} and percentile_scores({}) == {}


def test_legacy_blended_value_golden():
    assert blended_value(8.0, 6.0, 0.5) == 8.0
    assert blended_value(None, 6.0, 0.3) == 6.3
    assert blended_value(7.3, None, -0.4) == 6.9
    assert blended_value(None, None, 0.5) is None
    assert blended_value(9.8, 9.6, 1.0) == 10.0
    assert blended_value(0.5, 0.2, -1.0) == 0.0
    assert blended_value(5.0, 5.0, None) == 5.0


def test_waiver_policy_constants_are_pinned():
    assert (waivers.CLAIM_SCORE, waivers.WAIT_SCORE, waivers.CLAIM_MARGIN, waivers.CLAIM_MIN_FIT) == (6.0, 4.5, 0.5, 3.0)
    assert (waivers.W_OPP, waivers.W_FIT) == (0.55, 0.45)
    assert waivers.INJURY_ADJ == {"QUESTIONABLE": -0.5, "DOUBTFUL": -2.0, "OUT": -3.0, "IR": -3.0, "SUSPENDED": -3.0}


# --------------------------------------------------------------------- score_players --

def _row(g, week, pos, **kw):
    base = {"gsis_id": g, "week": week, "position": pos, "offense_snaps": 40, "offense_pct": .6, "targets": 5,
            "target_share": .15, "carries": 0, "rush_share": 0, "opportunities": 5, "xfp": 10.0,
            "fantasy_points_ppr": 10.0}
    base.update(kw)
    return base


def _usage():
    rows = []
    for i in range(30):                                                        # WR xfp pool (3 identical = ties)
        for w in range(1, 5):
            rows.append(_row(f"wr{i}", w, "WR", xfp=3.0 + 0.7 * min(i, 27) + 0.1 * w,
                             fantasy_points_ppr=2.0 + 0.9 * i + (0.3 * w if i % 4 else 0.0), opportunities=4 + i % 7))
    for i in range(8):                                                         # WR targets+carries basis (no xFP)
        for w in range(1, 5):
            rows.append(_row(f"wt{i}", w, "WR", xfp=None, opportunities=2 + i, fantasy_points_ppr=5.0 + i))
    for w in range(1, 5):                                                      # no PPR at all: production is None
        rows.append(_row("noppr", w, "WR", fantasy_points_ppr=None))
    for i in range(12):
        for w in range(1, 5):
            rows.append(_row(f"rb{i}", w, "RB", xfp=4.0 + i, carries=8 + i, fantasy_points_ppr=3.0 + 1.3 * i,
                             opportunities=9 + i))
    rows.append(_row("onegame", 1, "TE"))                                      # below MIN_GAMES_PERCENTILE_POOL
    return rows


def test_score_players_emits_100_scale_fields_consistent_with_legacy():
    out = score_players(_usage(), 2026)
    assert out
    for r in out:
        assert r["rating_scale_version"] == RATING_SCALE_VERSION == "ratings-100.v1"
        for legacy, new in (("opportunity_score", "opportunity_score_100"), ("production_score", "production_score_100"),
                            ("value_score", "value_score_100")):
            assert (r[legacy] is None) == (r[new] is None), (r["gsis_id"], legacy)
        if r["value_score"] is not None:
            assert abs(r["value_score_100"] - 10 * r["value_score"]) <= 0.6
        for new in ("opportunity_score_100", "production_score_100", "value_score_100"):
            assert r[new] is None or (0.0 <= r[new] <= 100.0 and r[new] == round(r[new], 1))
        assert "value_formula_100" in r["components"]
    by = {r["gsis_id"]: r for r in out}
    assert by["onegame"]["opportunity_score_100"] is None and by["onegame"]["production_score_100"] is None
    assert by["noppr"]["production_score_100"] is None and by["noppr"]["opportunity_score_100"] is not None
    assert {r["opportunity_score_100"] for r in out if r["opportunity_score_100"] is not None} != \
        {round(10 * r["opportunity_score"], 1) for r in out if r["opportunity_score"] is not None}
    wr = [r for r in out if r["gsis_id"].startswith("wr")]
    assert len({r["production_score_100"] for r in wr}) >= len({r["production_score"] for r in wr})


def test_score_players_pools_are_unchanged_by_the_new_fields():
    """Same pools => the rounded 0-100 rank agrees with the legacy 0-10 rank to within its own rounding."""
    for r in score_players(_usage(), 2026):
        if r["opportunity_score"] is not None:
            assert abs(r["opportunity_score_100"] - 10 * r["opportunity_score"]) <= 0.5 + 1e-9
        if r["production_score"] is not None:
            assert abs(r["production_score_100"] - 10 * r["production_score"]) <= 0.5 + 1e-9


# ------------------------------------------------------------------ waiver decisions --

def _evaluate(cv, injury="ACTIVE", pos="RB", status="WAIVERS"):
    vals = values()
    cand = {"espn_player_id": 99, "position": pos, "availability_status": status, "injury_status": injury,
            "percent_change": 8.0, "percent_owned": 20.0, "full_name": "Cand"}
    rival = team_state([p(50, "RB", 2, injury="OUT"), p(51, "QB", 0)], vals, SLOTS)
    mine = team_state(my_roster(), vals, SLOTS)
    needs = {2: team_needs(mine, SLOTS, LSA), 7: team_needs(rival, SLOTS, LSA), 8: team_needs(mine, SLOTS, LSA)}
    profiles = {7: {"add_share": {"RB": 0.6}, "adds_total": 10, "aggression_pct": 0.9, "resource": 0.8},
                8: {"add_share": {"RB": 0.1}, "adds_total": 10, "aggression_pct": 0.1, "resource": 0.2}}
    return evaluate_candidate(cand, cv, my_team_id=2, needs=needs, my_state=mine, profiles=profiles,
                              league_pos_share={"RB": 0.3}, team_names={7: "Rival", 8: "Sleepy"},
                              scarcity={"RB": 4, "WR": 10, "TE": 10})


def _strip_100(row):
    row = {k: v for k, v in row.items() if k != "opportunity_score_100"}
    comp = {**row["components"], "player": {k: v for k, v in row["components"]["player"].items()
                                           if k not in ("value_100", "base_opportunity_100")}}
    reasons = [r for r in row["reasons"] if not r.startswith("Opportunity ")]
    return {**row, "components": comp, "reasons": reasons}


SCENARIOS = [  # value, opp, opp100, value100, trend, injury
    (8.5, 8.0, 79.6, 84.9, "UP", "ACTIVE"),
    (6.0, 5.0, 52.3, 61.2, "DOWN", "QUESTIONABLE"),
    (7.0, 7.5, 74.7, 70.1, None, "OUT"),
    (4.2, None, None, 41.8, "UP", "DOUBTFUL"),           # no opportunity: value is the base
    (1.0, 1.0, 9.6, 10.4, None, "ACTIVE"),
]


@pytest.mark.parametrize("value, opp, opp100, value100, trend, injury", SCENARIOS)
def test_waiver_decisions_do_not_read_the_100_fields(value, opp, opp100, value100, trend, injury):
    legacy_cv = PlayerValue(99, "RB", value, opp, value, trend, "HOLD", "xfp")
    rated_cv = PlayerValue(99, "RB", value, opp, value, trend, "HOLD", "xfp",
                           opportunity_100=opp100, production_100=value100, value_100=value100)
    a, b = _evaluate(legacy_cv, injury), _evaluate(rated_cv, injury)
    for field in ("recommendation", "waiver_score", "opportunity_score", "roster_fit_score", "interest_score",
                  "league_interest", "suggested_drop_id"):
        assert a[field] == b[field], field
    for field in ("action", "recommendation"):
        assert a["components"]["decision"][field] == b["components"]["decision"][field]
    assert _strip_100(a) == _strip_100(b)                                      # everything else, byte for byte
    assert a["opportunity_score_100"] is None and a["components"]["player"]["value_100"] is None
    assert a["components"]["player"]["base_opportunity_100"] is None
    assert any(r.startswith("Opportunity") and "/10 at" in r and "/100" not in r for r in a["reasons"])
    assert any(r.startswith("Opportunity") and "/100 at" in r for r in b["reasons"]) == (opp100 is not None or opp is None)


def test_waiver_opportunity_100_adjusts_the_100_base_by_ten_times_the_policy_adjustments():
    cv = PlayerValue(99, "RB", 8.5, 8.0, 8.5, "UP", "HOLD", "xfp", opportunity_100=79.6, production_100=88.0,
                     value_100=84.9)
    r = _evaluate(cv, injury="QUESTIONABLE")
    assert r["opportunity_score_100"] == round(79.6 + 10 * 1.0 + 10 * -0.5, 1) == 84.6
    assert r["components"]["player"]["base_opportunity_100"] == 79.6 and r["components"]["player"]["value_100"] == 84.9
    assert r["components"]["player"]["base_opportunity"] == 8.0
    assert any(x == "Opportunity 79.6/100 at RB: recent expected-fantasy-points (xFP) percentile" for x in r["reasons"])
    out = _evaluate(PlayerValue(99, "RB", 3.0, 2.0, 3.0, "DOWN", "HOLD", "xfp", opportunity_100=20.0, value_100=30.0),
                    injury="OUT")
    assert out["opportunity_score_100"] == 0.0 and out["opportunity_score"] == 0.0     # both clamp at the floor


def test_waiver_base_100_follows_the_legacy_none_rule():
    cv = PlayerValue(99, "RB", 4.2, None, 4.2, None, "HOLD", "xfp", opportunity_100=55.5, value_100=41.8)
    r = _evaluate(cv)
    assert r["components"]["player"]["base_opportunity_100"] == 41.8           # value_100, since legacy opportunity is None
    assert r["opportunity_score_100"] == 41.8


def test_playervalue_positional_construction_still_works():
    v = PlayerValue(1, "WR", 5.0, 5.0, 5.0, "UP", "BUY", "xfp", "00-1", 4, {"x": 1})
    assert v.extras == {"x": 1} and v.opportunity_100 is None and v.value_100 is None and v.production_100 is None


# --------------------------------------------------------------------- load_values --

def test_load_values_reads_100_fields_and_the_espn_fallback_mirrors_legacy(con):
    for pid, pos in ((1, "WR"), (2, "WR"), (3, "WR"), (4, "WR")):
        con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position) VALUES (?, ?, ?)",
                    [pid, f"P{pid}", pos])
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id) VALUES (1, 'g1')")
    con.execute("INSERT INTO player_scores (season, gsis_id, position, opportunity_basis, value_score, opportunity_score, "
                "production_score, opportunity_score_100, production_score_100, value_score_100, rating_scale_version) "
                "VALUES (2026, 'g1', 'WR', 'xfp', 7.0, 6.9, 7.1, 69.4, 71.2, 70.3, 'ratings-100.v1')")
    for pid, pts in ((2, 4.0), (3, 9.0), (4, 9.0)):                            # no usage score: ESPN-points fallback
        for week in (1, 2):
            con.execute("INSERT INTO espn_player_weekly_points (season, espn_player_id, scoring_period, actual_points, projected_points, synced_at) VALUES (2026, ?, ?, ?, NULL, NULL)", [pid, week, pts])
    vals = load_values(con, 2026, 5)
    v1 = vals[1]
    assert (v1.opportunity_100, v1.production_100, v1.value_100) == (69.4, 71.2, 70.3)
    assert (v1.value, v1.opportunity, v1.production) == (7.0, 6.9, 7.1)
    legacy = percentile_scores({2: 4.0, 3: 9.0, 4: 9.0})
    exact = percentile_scores_100({2: 4.0, 3: 9.0, 4: 9.0})
    for pid in (2, 3, 4):
        v = vals[pid]
        assert v.basis == "espn_points" and v.opportunity is None and v.opportunity_100 is None
        assert v.value == v.production == legacy[pid]
        assert v.value_100 == v.production_100 == round(exact[pid], 1)
    assert vals[2].value_100 == 16.7 and vals[3].value_100 == 66.7


def test_load_values_without_100_columns_in_an_older_table(con):
    con.execute("ALTER TABLE player_scores DROP COLUMN opportunity_score_100")
    con.execute("ALTER TABLE player_scores DROP COLUMN production_score_100")
    con.execute("ALTER TABLE player_scores DROP COLUMN value_score_100")
    con.execute("INSERT INTO fantasy_players (espn_player_id, full_name, position) VALUES (1, 'P1', 'WR')")
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id) VALUES (1, 'g1')")
    con.execute("INSERT INTO player_scores (season, gsis_id, position, opportunity_basis, value_score, opportunity_score, "
                "production_score) VALUES (2026, 'g1', 'WR', 'xfp', 7.0, 6.9, 7.1)")
    v = load_values(con, 2026, 5)[1]
    assert v.value == 7.0 and v.opportunity_100 is None and v.value_100 is None


# ------------------------------------------------------------------------------ API --

RATING_COLS = ("opportunity_score_100", "production_score_100", "value_score_100", "rating_scale_version")


def _api_db(tmp_path):
    db_path = tmp_path / "ratings.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    con = connect(db_path)
    run_analytics(con, SEASON)
    echo = con.execute("SELECT espn_player_id FROM fantasy_players WHERE full_name = 'Echo RB'").fetchone()[0]
    con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id) VALUES (?, 'g-echo') "
                "ON CONFLICT DO UPDATE SET gsis_id = 'g-echo'", [echo])
    con.execute("DELETE FROM player_scores WHERE gsis_id = 'g-echo'")
    con.execute("INSERT INTO player_scores (season, gsis_id, position, opportunity_basis, value_score, opportunity_score, "
                "production_score, opportunity_score_100, production_score_100, value_score_100, rating_scale_version) "
                "VALUES (?, 'g-echo', 'RB', 'xfp', 7.0, 6.9, 7.1, 69.4, 71.2, 70.3, 'ratings-100.v1')", [SEASON])
    return db_path, con, echo


def _client(db_path, monkeypatch):
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    from app.main import app
    return TestClient(app)


def test_players_api_returns_the_100_ratings_when_the_table_has_them(tmp_path, monkeypatch):
    db_path, con, echo = _api_db(tmp_path)
    assert set(RATING_COLS) <= set(table_columns(con, "player_scores"))
    assert "opportunity_score_100" in table_columns(con, "waiver_recommendations")
    con.close()
    client = _client(db_path, monkeypatch)
    found = client.get("/api/players", params={"q": "echo"}).json()
    row = next(r for r in found if r["espn_player_id"] == echo)
    assert (row["opportunity_score_100"], row["production_score_100"], row["value_score_100"]) == (69.4, 71.2, 70.3)
    assert row["rating_scale_version"] == "ratings-100.v1" and row["value_score"] == 7.0
    detail = client.get(f"/api/players/{echo}").json()
    assert detail["player"]["value_score_100"] == 70.3 and detail["player"]["opportunity_score"] == 6.9
    waivers_ = client.get("/api/waivers").json()["players"]
    assert waivers_ and all("opportunity_score_100" in w for w in waivers_)
    assert all("value_100" in w["components"]["player"] for w in waivers_)


def test_players_api_degrades_to_null_on_a_database_without_the_100_columns(tmp_path, monkeypatch):
    db_path, con, echo = _api_db(tmp_path)
    for c in ("opportunity_score_100", "production_score_100", "value_score_100", "rating_scale_version"):
        con.execute(f"ALTER TABLE player_scores DROP COLUMN {c}")
    con.execute("ALTER TABLE waiver_recommendations DROP COLUMN opportunity_score_100")
    con.close()
    client = _client(db_path, monkeypatch)
    found = client.get("/api/players", params={"q": "echo"})
    assert found.status_code == 200
    row = next(r for r in found.json() if r["espn_player_id"] == echo)
    assert all(row[c] is None for c in RATING_COLS)
    assert row["opportunity_score"] == 6.9 and row["value_score"] == 7.0       # legacy still served
    assert client.get(f"/api/players/{echo}").status_code == 200
    assert client.get("/api/overview").status_code == 200
    w = client.get("/api/waivers")
    assert w.status_code == 200 and w.json()["players"]

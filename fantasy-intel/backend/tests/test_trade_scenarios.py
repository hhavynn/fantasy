"""Trade scenario comparison (issue #183): every scenario is the analyzer's own canonical computation over one shared
window, stale scenarios are caught instead of re-valued, scenarios are alternatives (shared players are conflicts,
nothing is summed across scenarios), and missing projections stay unknown. Pure synthetic rosters; the API is covered
below and by its golden (tests/golden/e2e/api_trades_scenarios.json).

League: QB, RB x2, WR x2, TE, FLEX(RB/WR/TE). Weeks 10-11 regular season, week 12 playoffs."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.analytics import trade_scenarios as ts
from app.analytics.lineup import lineup_slots
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trade_scenarios import (
    ALTERNATIVES_NOTE,
    MAX_SCENARIOS,
    ScenarioError,
    ScenarioRequest,
    compare_scenarios,
    parse_scenario,
    parse_scenarios,
)
from app.analytics.trades import (
    EvaluationUnavailable,
    LeagueState,
    NoRemainingWeeksError,
    TradeError,
    evaluate_trade,
)

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WEEKS = (10, 11, 12)


def P(pid, pos, team="KC"):
    return RosterPlayer(pid, pos, team, f"P{pid}")


def flat(v):
    return {w: v for w in WEEKS}


def league(proj_over=None, start_week=10, rosters=None):
    a = [P(101, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"), P(107, "WR"),
         P(108, "TE"), P(109, "TE")]
    b = [P(201, "QB"), P(202, "RB"), P(203, "RB"), P(204, "WR"), P(205, "WR"), P(206, "TE"), P(207, "RB"),
         P(208, "WR"), P(209, "TE")]
    c = [P(301, "QB"), P(302, "RB"), P(303, "WR"), P(304, "TE")]
    proj = {101: flat(20), 102: flat(15), 103: flat(12), 104: flat(9), 105: flat(16), 106: flat(11), 107: flat(8),
            108: flat(10), 109: flat(5), 201: flat(18), 202: flat(14), 203: flat(11), 204: flat(13), 205: flat(10),
            206: flat(8), 207: flat(6), 208: flat(4), 209: flat(3), 301: flat(17), 302: flat(13), 303: flat(12),
            304: flat(7)}
    for pid, v in (proj_over or {}).items():
        if v is None:
            proj.pop(pid, None)
        else:
            proj[pid] = v
    rosters = rosters or {1: a, 2: b, 3: c}
    return LeagueState(season=2026, slots=SLOTS, final_week=12, last_regular=11, start_week=start_week,
                       rosters=rosters, free_agents=[], projections=proj, active_capacity=20,
                       team_names={1: "A", 2: "B", 3: "C"}, players_missing={t: 0 for t in rosters})


def S(ta, tb, a, b):
    return ScenarioRequest(ta, tb, tuple(a), tuple(b))


# ------------------------------------------------------------------------------ parsing --

def test_parses_scenarios_in_saved_order_with_team_defense_ids():
    out = parse_scenarios("2:1:207.204:105,1:3:-16002:302")
    assert out == [S(2, 1, [207, 204], [105]), S(1, 3, [-16002], [302])]


@pytest.mark.parametrize("text", ["", "1:2:105", "1:2::204", "1:2:105:204:9", "a:2:105:204", "1:2:105,,204",
                                  "1:2:0:204", "1:2:105:9999999999999", "1234567:2:105:204", "1:2:1.2.3.4.5.6.7.8.9.10.11.12.13:5"])
def test_refuses_a_malformed_scenario(text):
    with pytest.raises(ScenarioError):
        parse_scenarios(text)


def test_compares_at_most_five_scenarios():
    assert len(parse_scenarios(",".join(["1:2:105:204"] * MAX_SCENARIOS))) == MAX_SCENARIOS
    with pytest.raises(ScenarioError):
        parse_scenarios(",".join(["1:2:105:204"] * (MAX_SCENARIOS + 1)))


def test_the_same_trade_with_sides_swapped_has_one_key():
    assert parse_scenario("1:2:106.105:204").key() == parse_scenario("2:1:204:105.106").key()
    assert parse_scenario("1:2:105:204").key() != parse_scenario("1:2:105:205").key()


# ---------------------------------------------------------------------------- deltas --

def test_every_scenario_matches_the_analyzer_for_both_teams():
    st = league()
    reqs = [S(1, 2, [105], [204]), S(1, 3, [106, 107], [302])]
    out = compare_scenarios(st, reqs)
    for row, req in zip(out["scenarios"], reqs):
        ref = evaluate_trade(st, req.team_a, req.team_b, list(req.a_sends), list(req.b_sends))
        assert row["status"] == "evaluated"
        for key in ("team_a", "team_b"):
            mine, theirs = row[key], ref[key]
            for d in ("ros_delta", "regular_season_delta", "playoff_delta", "known_ros_delta", "weeks_incomplete"):
                assert mine[d] == theirs["deltas"][d], (key, d)
            assert mine["short_term"] == {"week": 10, "delta": theirs["weekly"][0]["delta"],
                                          "known_delta": theirs["weekly"][0]["known_delta"],
                                          "projection_complete": theirs["weekly"][0]["projection_complete"]}
            assert mine["forced_drops"] == [{k: d[k] for k in ("espn_player_id", "name", "position", "is_incoming")}
                                            for d in theirs["roster"]["forced_drops"]]
        assert row["result_label"] == ref["result"]["label"]
    a = out["scenarios"][0]["team_a"]
    assert (a["short_term"]["delta"], a["regular_season_delta"], a["playoff_delta"], a["ros_delta"]) == (-3.0, -6.0, -3.0, -9.0)


def test_all_scenarios_share_one_window():
    out = compare_scenarios(league(), [S(1, 2, [105], [204]), S(1, 3, [105], [303])], start_week=11)
    assert out["evaluation"]["weeks"] == [11, 12] and out["evaluation"]["short_term_week"] == 11
    assert all(row["team_a"]["short_term"]["week"] == 11 for row in out["scenarios"])
    assert out["scenarios"][0]["team_a"]["ros_delta"] == -6.0


def test_a_bad_horizon_fails_the_whole_comparison():
    with pytest.raises(TradeError):
        compare_scenarios(league(), [S(1, 2, [105], [204])], start_week=9)
    with pytest.raises(NoRemainingWeeksError):
        compare_scenarios(league(start_week=None), [S(1, 2, [105], [204])])


def test_a_missing_projection_leaves_the_delta_unknown_not_zero():
    out = compare_scenarios(league(proj_over={204: {10: 13, 11: 13}}), [S(1, 2, [105], [204])])
    a = out["scenarios"][0]["team_a"]
    assert a["ros_delta"] is None and a["playoff_delta"] is None
    assert isinstance(a["known_ros_delta"], float)   # the known part, reported beside the unknown total
    assert a["weeks_incomplete"] == [12] and a["projection_complete"] is False
    assert a["short_term"]["delta"] == -3.0 and a["short_term"]["projection_complete"] is True
    assert out["summary"]["incomplete"] == 1


def test_no_projections_names_no_source():
    st = league()
    out = compare_scenarios(replace(st, projections={}), [S(1, 2, [105], [204])])
    assert out["projection_source"] is None
    assert compare_scenarios(st, [S(1, 2, [105], [204])])["projection_source"] == "ESPN"


# ----------------------------------------------------------------- stale and invalid --

def test_a_player_who_moved_is_caught_and_never_revalued(monkeypatch):
    st = league()
    rosters = {t: list(ps) for t, ps in st.rosters.items()}
    moved = next(p for p in rosters[2] if p.espn_player_id == 204)
    rosters[2].remove(moved)
    rosters[3].append(moved)                       # traded to C since the scenario was saved
    dropped = next(p for p in rosters[1] if p.espn_player_id == 106)
    rosters[1].remove(dropped)                     # released to free agency
    calls = []
    real = ts.side_lineups
    monkeypatch.setattr(ts, "side_lineups", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    out = compare_scenarios(replace(st, rosters=rosters), [S(1, 2, [105, 106], [204]), S(1, 2, [107], [205])])
    stale, ok = out["scenarios"]
    assert stale["status"] == "stale" and "team_a" not in stale and "result_label" not in stale
    assert stale["moved"] == [
        {"espn_player_id": 106, "expected_team_id": 1, "current_team_id": None, "current_team_name": None},
        {"espn_player_id": 204, "expected_team_id": 2, "current_team_id": 3, "current_team_name": "C"}]
    assert ok["status"] == "evaluated"
    assert calls == [1, 2]                         # only the current scenario was simulated
    assert out["summary"]["stale"] == 1 and out["summary"]["evaluated"] == 1


def test_an_illegal_scenario_says_why_without_deltas():
    out = compare_scenarios(league(), [S(1, 1, [105], [106]), S(1, 9, [105], [1]), S(1, 2, [105, 105], [204])],
                            max_per_side=6)
    assert [r["status"] for r in out["scenarios"]] == ["invalid", "invalid", "invalid"]
    assert out["scenarios"][0]["reason"] == "teams must be different"
    assert out["scenarios"][1]["reason"] == "Team 9 is not in this league."
    assert "more than once" in out["scenarios"][2]["reason"]
    over = compare_scenarios(league(), [S(1, 2, [105, 106], [204])], max_per_side=1)["scenarios"][0]
    assert over["status"] == "invalid" and "between 1 and 1" in over["reason"]


# ------------------------------------------------------------- alternatives, not sums --

def test_scenarios_that_share_a_player_are_conflicts_never_summed():
    out = compare_scenarios(league(), [S(1, 2, [105], [204]), S(1, 3, [105], [303]), S(1, 2, [107], [205])])
    assert out["conflicts"] == [{"espn_player_id": 105, "scenarios": [0, 1]}]
    assert [r["conflicts_with"] for r in out["scenarios"]] == [[1], [0], []]
    assert ALTERNATIVES_NOTE in out["notes"]
    # Nothing in the answer adds scenarios together: only per-scenario, per-team numbers exist.
    assert not any("total" in k or "combined" in k for k in out) and not any("total" in k for k in out["summary"])


def test_a_duplicate_is_flagged_reoriented_and_not_simulated_twice(monkeypatch):
    calls = []
    real = ts.side_lineups
    monkeypatch.setattr(ts, "side_lineups", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    out = compare_scenarios(league(), [S(1, 2, [105], [204]), S(2, 1, [204], [105])])
    first, dup = out["scenarios"]
    assert calls == [1, 2]
    assert dup["duplicate_of"] == 0 and dup["conflicts_with"] == [] and out["conflicts"] == []
    assert dup["team_a"]["team_id"] == 2 and dup["team_a"] == first["team_b"] and dup["team_b"] == first["team_a"]
    assert dup["result_label"] == "TEAM_A_GAIN_TEAM_B_LOSS" and first["result_label"] == "TEAM_A_LOSS_TEAM_B_GAIN"

    # A duplicate carries its original's conflicts; a stale or invalid row is no alternative to anything.
    st = league()
    rosters = {t: list(ps) for t, ps in st.rosters.items()}
    gone = next(p for p in rosters[3] if p.espn_player_id == 303)
    rosters[3].remove(gone)                                          # 303 has left team C
    out = compare_scenarios(replace(st, rosters=rosters), [
        S(1, 2, [105], [204]),       # 0: evaluated
        S(1, 3, [105], [302]),       # 1: evaluated, shares 105 with 0
        S(2, 1, [204], [105]),       # 2: duplicate of 0
        S(1, 3, [105], [303]),       # 3: stale (303 moved), names 105 too
        S(1, 1, [105], [106]),       # 4: invalid, names 105 too
    ])
    assert [r["status"] for r in out["scenarios"]] == ["evaluated", "evaluated", "evaluated", "stale", "invalid"]
    assert out["conflicts"] == [{"espn_player_id": 105, "scenarios": [0, 1]}]
    assert [r["conflicts_with"] for r in out["scenarios"]] == [[1], [0], [1], [], []]


def test_an_unavailable_evaluation_is_counted_apart_from_an_invalid_trade(monkeypatch):
    def unavailable(*a, **k):
        raise EvaluationUnavailable("exact forced-drop resolution exceeds its budget")
    monkeypatch.setattr(ts, "side_lineups", unavailable)
    out = compare_scenarios(league(), [S(1, 2, [105], [204]), S(1, 1, [105], [106])])
    assert [r["status"] for r in out["scenarios"]] == ["unavailable", "invalid"]
    assert out["scenarios"][0]["reason"] == "exact forced-drop resolution exceeds its budget"
    assert "team_a" not in out["scenarios"][0]
    assert out["summary"] == {"scenarios": 2, "evaluated": 0, "incomplete": 0, "stale": 0, "invalid": 1, "unavailable": 1}


def test_nothing_is_submitted():
    out = compare_scenarios(league(), [S(1, 2, [105], [204])])
    assert out["contract"] == "trade-scenario-compare/v1" and out["submitted"] is False


# ------------------------------------------------------------------------------- API --

@pytest.fixture
def fixture_api(tmp_path, monkeypatch):
    from espn_fixture import MY_SWID, FakeEspnClient
    from fastapi.testclient import TestClient

    from app.db.connection import open_db
    from app.espn.sync import run_espn_sync

    db_path = tmp_path / "scenarios.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_api_compares_scenarios_from_the_synced_league(fixture_api):
    # Fixture: team 2 rosters Kilo RB (207), team 1 Foxtrot RB (202) and Oscar WR (303); Lima RB (208) is on team 3.
    res = fixture_api.get("/api/trades/scenarios?scenarios=2:1:207:202,2:1:207:303,2:1:208:202")
    assert res.status_code == 200
    body = res.json()
    assert body["contract"] == "trade-scenario-compare/v1" and body["submitted"] is False
    assert [r["status"] for r in body["scenarios"]] == ["evaluated", "evaluated", "stale"]
    assert body["scenarios"][2]["moved"][0]["current_team_id"] == 3
    assert body["names"]["207"]["name"] == "Kilo RB"
    assert body["data_version"] and body["provenance"]["contract"] == "signal-provenance.v1"
    ref = fixture_api.post("/api/trades/evaluate", json={"team_a_id": 2, "team_b_id": 1, "team_a_sends": [207],
                                                         "team_b_sends": [202]}).json()
    assert body["scenarios"][0]["team_a"]["ros_delta"] == ref["team_a"]["deltas"]["ros_delta"]
    assert body["scenarios"][0]["team_b"]["playoff_delta"] == ref["team_b"]["deltas"]["playoff_delta"]


def test_the_api_keeps_the_saved_order(fixture_api):
    a = fixture_api.get("/api/trades/scenarios?scenarios=2:1:207:202,2:1:207:303").json()
    b = fixture_api.get("/api/trades/scenarios?scenarios=2:1:207:303,2:1:207:202").json()
    assert [r["request"]["team_b_sends"] for r in a["scenarios"]] == [[202], [303]]
    assert [r["request"]["team_b_sends"] for r in b["scenarios"]] == [[303], [202]]


@pytest.mark.parametrize("query", ["", "?scenarios=", "?scenarios=2:1:207", "?scenarios=2:1:207:202&start_week=0",
                                   "?scenarios=" + ",".join(["2:1:207:202"] * 6)])
def test_the_api_refuses_a_malformed_request(fixture_api, query):
    assert fixture_api.get("/api/trades/scenarios" + query).status_code == 422


def test_the_api_refuses_a_horizon_before_the_earliest_week(fixture_api):
    assert fixture_api.get("/api/trades/scenarios?scenarios=2:1:207:202&start_week=1").status_code == 422


def test_the_route_is_read_only_and_league_scoped():
    from app.api.routing import iter_routes
    from app.main import app
    mine = [r for r in iter_routes(app) if r.path.endswith("/api/trades/scenarios")]
    assert mine and all(r.methods == {"GET"} for r in mine)
    assert {r.path for r in mine} == {"/api/trades/scenarios", "/v2/leagues/{league_id}/api/trades/scenarios"}

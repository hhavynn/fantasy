"""League analyzer (issue #191): replacement-adjusted, league-relative position strength from the existing engines;
missing projections keep a position unknown (never ranked last); #253 slot caveats; compare-two-teams mode; the route's
validation and, under /v2, authorization before validation. Synthetic leagues only (invented teams, players and
projections): they say nothing about any real league."""

from dataclasses import replace

import pytest
import synthetic_value as sv
from fastapi.testclient import TestClient

from app.analytics import league_analyzer as la
from app.analytics.lineup import lineup_slots
from app.analytics.team_value import NotFound

TEAM_SIZE = 17            # synthetic_league: 16 players + 1 IR RB per team, ids from 1001 in team order
K_INDEX, RB_INDEX = 14, 2


def pid(team: int, index: int) -> int:
    return 1000 + (team - 1) * TEAM_SIZE + index + 1


@pytest.fixture(scope="module")
def race():
    return sv.value_race(n_teams=8)


def drop_projection(race, player: int):
    proj = {k: dict(v) for k, v in race.state.projections.items()}
    proj.pop(player, None)
    return replace(race, state=replace(race.state, projections=proj))


def team(res, tid):
    return next(t for t in res["teams"] if t["team_id"] == tid)


def group(res, slot):
    return next(g for g in res["groups"] if g["slot"] == slot)


# ------------------------------------------------------------------ normalization --

def test_cells_are_points_above_replacement_per_slot_with_a_league_relative_score(race):
    res = la.build_league_analyzer(race, {}, horizon="ros")
    assert res["contract"] == "league-analyzer/v1" and not res["empty"]
    assert res["projection_source"] == "ESPN"
    assert [g["slot"] for g in res["groups"]] == ["QB", "RB", "WR", "TE", "K", "D/ST", "FLEX"]
    rb = group(res, "RB")
    assert rb["count"] == 2 and rb["teams_ranked"] == 8 and rb["replacement_per_slot"] is not None
    cells = [team(res, t)["positions"]["RB"] for t in range(1, 9)]
    for c in cells:
        assert c["status"] == "ranked" and c["of"] == 8
        # per slot = group points / 2 slots; above replacement subtracts the best free agent's level per slot
        assert c["per_slot"] == pytest.approx(c["points"] / 2, abs=0.011)
        assert c["above_replacement"] == pytest.approx(c["per_slot"] - rb["replacement_per_slot"], abs=0.011)
    scores = sorted(c["score"] for c in cells)
    assert scores[0] == 0.0 and scores[-1] == 100.0
    best = max(cells, key=lambda c: c["per_slot"])
    assert best["rank"] == 1 and best["score"] == 100.0
    # the rank order is the same as raw points (replacement is one level for every team), the unit is not
    assert sorted(cells, key=lambda c: c["rank"]) == sorted(cells, key=lambda c: -c["per_slot"])
    assert "above the best available free agent" in res["units"]["above_replacement"]


def test_strongest_and_weakest_leave_out_streamable_k_and_dst(race):
    res = la.build_league_analyzer(race, {})
    for t in res["teams"]:
        assert not {"K", "D/ST"} & set(t["strongest"] + t["weakest"])
        assert len(t["strongest"]) == 2 and len(t["weakest"]) == 2
        # ordered by the league-relative score, so a rank among fewer ranked teams never reads as stronger
        scores = {g: t["positions"][g]["score"] for g in t["strongest"] + t["weakest"]}
        assert min(scores[g] for g in t["strongest"]) >= max(scores[g] for g in t["weakest"])


def test_power_context_is_the_power_rankings_composite_unchanged(race):
    from app.analytics.power_rankings import build_power_rankings
    res = la.build_league_analyzer(race, {})
    pr = {x["team_id"]: x for x in build_power_rankings(race, None, {})["teams"]}
    for t in res["teams"]:
        assert t["power"]["rank"] == pr[t["team_id"]]["rank"]
        assert t["power"]["score"] == pr[t["team_id"]]["power_score"]
    assert [t["power"]["rank"] for t in res["teams"]] == sorted(t["power"]["rank"] for t in res["teams"])


def test_horizon_week_uses_only_the_first_remaining_week(race):
    res = la.build_league_analyzer(race, {}, horizon="week")
    assert res["horizon"]["weeks"] == [race.state.start_week] and res["horizon"]["key"] == "week"
    with pytest.raises(ValueError):
        la.build_league_analyzer(race, {}, horizon="season")


# ------------------------------------------------------------------ unknown, never worst --

def test_a_missing_kicker_projection_keeps_only_that_teams_k_unknown(race):
    k = pid(2, K_INDEX)
    res = la.build_league_analyzer(drop_projection(race, k), {})
    cell = team(res, 2)["positions"]["K"]
    assert cell["status"] == "unknown" and cell["rank"] is None and cell["score"] is None
    assert "has no ESPN projection" in cell["reason"]
    assert group(res, "K")["unknown_teams"] == [2] and group(res, "K")["teams_ranked"] == 7
    assert all(team(res, t)["positions"]["K"]["of"] == 7 for t in range(1, 9))
    # the other groups of team 2 stay exact and ranked against all eight teams
    for g in ("QB", "RB", "WR", "TE", "D/ST", "FLEX"):
        assert team(res, 2)["positions"][g]["status"] == "ranked" and team(res, 2)["positions"][g]["of"] == 8
    assert any(c["code"] == "unknown_cells" for c in res["caveats"])
    assert any(c["code"] == "unknown_positions" for c in team(res, 2)["caveats"])


def test_a_missing_rb_projection_reaches_every_group_a_flex_slot_ties_to(race):
    res = la.build_league_analyzer(drop_projection(race, pid(3, RB_INDEX)), {})
    pos = team(res, 3)["positions"]
    assert {g for g, c in pos.items() if c["status"] == "unknown"} == {"RB", "WR", "TE", "FLEX"}
    assert pos["QB"]["status"] == "ranked" and pos["K"]["status"] == "ranked"
    # unknown is never ranked last: the worst ranked RB is another team
    worst = max((team(res, t)["positions"]["RB"] for t in range(1, 9) if t != 3), key=lambda c: c["rank"])
    assert worst["rank"] == 7


def test_reach_follows_shared_flex_slots():
    reach = la.reach(lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1}))
    assert reach["QB"] == {"QB"} and reach["K"] == {"K"}
    assert reach["TE"] == reach["FLEX"] == {"RB", "WR", "TE"}
    reach = la.reach(lineup_slots({"QB": 1, "RB": 1, "OP": 1}))
    assert reach["QB"] == {"QB", "RB", "WR", "TE"}


def test_a_league_with_no_projections_is_unknown_everywhere_and_never_named_ours(race):
    st = replace(race.state, projections={}, projection_source="independent")
    res = la.build_league_analyzer(replace(race, state=st), {}, kdst_streaming=False)
    assert res["projection_source"] == "Unavailable"
    assert all(c["status"] == "unknown" and c["rank"] is None for t in res["teams"] for c in t["positions"].values())
    assert [c["code"] for c in res["caveats"]] == ["no_projections"]
    assert all(t["strongest"] == [] and t["weakest"] == [] for t in res["teams"])


def test_ours_projections_are_labelled_ours(race):
    st = replace(race.state, projection_source="independent")
    res = la.build_league_analyzer(replace(race, state=st), {})
    assert res["projection_source"] == "Ours"
    res = la.build_league_analyzer(drop_projection(replace(race, state=st), pid(2, K_INDEX)), {})
    assert "has no Ours projection" in team(res, 2)["positions"]["K"]["reason"]


def test_a_team_without_a_roster_is_unknown_not_zero(race):
    rosters = dict(race.state.rosters)
    rosters[4] = []
    res = la.build_league_analyzer(replace(race, state=replace(race.state, rosters=rosters)), {})
    t4 = team(res, 4)
    assert all(c["status"] == "unknown" and c["points"] is None for c in t4["positions"].values())
    assert t4["caveats"][0]["code"] == "no_roster" and t4["power"]["rank"] is None
    assert t4["lineup"] == {"projected_per_week": None, "known_per_week": None, "complete": False}


# ------------------------------------------------------------------ #253 compatibility --

def test_separable_unsupported_slots_keep_the_filled_groups_with_a_partial_caveat(race):
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1, "LB": 1})
    res = la.build_league_analyzer(replace(race, state=replace(race.state, slots=slots)), {})
    cav = next(c for c in res["caveats"] if c["code"] == "unsupported_slots")
    assert cav["status"] == "partial" and "LB" in cav["text"]
    assert team(res, 1)["positions"]["QB"]["status"] == "ranked"
    # lineup totals are partial, so never presented as complete
    assert team(res, 1)["lineup"]["complete"] is False and team(res, 1)["lineup"]["projected_per_week"] is None


def test_unrecognised_slots_leave_every_group_unverified(race):
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "D/ST": 1, "SUPERX": 1})
    res = la.build_league_analyzer(replace(race, state=replace(race.state, slots=slots)), {})
    assert all(c["status"] == "unverified" for t in res["teams"] for c in t["positions"].values())
    assert [c["code"] for c in res["caveats"]] == ["unsupported_slots"]
    assert res["caveats"][0]["status"] == "unverified" and "SUPERX" in res["caveats"][0]["text"]


def test_the_compatibility_entry_is_the_power_rankings_tool(race):
    dims = {k: {"status": "handled", "reason": "ok", "missing_input": None, "action": None, "evidence": []}
            for k in ("import", "roster", "scoring", "feature")}
    dims["scoring"] = {**dims["scoring"], "status": "partial", "reason": "2 rostered K have no projection."}
    compat = {"contract": "league-compatibility/v1", "tools": [{"tool": "power_rankings", "label": "Power rankings",
                                                                **dims}]}
    res = la.build_league_analyzer(race, {}, compatibility=compat)
    assert res["compatibility"]["status"] == "partial"
    assert res["compatibility"]["dimensions"]["scoring"]["reason"] == "2 rostered K have no projection."
    assert la.build_league_analyzer(race, {})["compatibility"] is None


def test_unverified_roster_counts_are_a_team_caveat(race):
    st = replace(race.state, players_missing={**race.state.players_missing, 5: 2})
    res = la.build_league_analyzer(replace(race, state=st), {})
    assert any(c["code"] == "roster_unverified" for c in team(res, 5)["caveats"])
    assert not any(c["code"] == "roster_unverified" for c in team(res, 6)["caveats"])


# ------------------------------------------------------------------ compare --

def _side():
    plan = {"players": [{"espn_player_id": 1, "name": "Bye QB"}, {"espn_player_id": 2, "name": "Bye RB"}],
            "schedule": {"available": True, "weeks_missing": []},
            "weeks": [{"week": 7, "conflict": True, "status": "short", "status_text": "Short", "short_slots": ["QB"],
                       "unconfirmed_slots": [], "byes": [1, 2], "starters_on_bye": [1]},
                      {"week": 8, "conflict": False, "status": "covered", "status_text": "Covered", "short_slots": [],
                       "unconfirmed_slots": [], "byes": [], "starters_on_bye": []}]}
    scouting = {"current_roster": {"scoring_period": 6, "positions": [
        {"position": "QB", "count": 2, "league_average": 2.25, "league_comparison": "BELOW"},
        {"position": "IR", "count": 1, "league_average": 1.0, "league_comparison": "AT"}]},
        "current_needs": [{"position": "QB", "need_score": 0.7123, "starters_required": 1, "healthy": 1}]}
    return {1: {"byes": plan, "scouting": scouting}, 2: {"byes": {"error": "League not synced yet."}, "scouting": None}}


def test_compare_two_teams_by_position_with_bench_byes_and_needs(race):
    res = la.build_league_analyzer(race, {}, team_id=1, vs=2, side=_side())
    c = res["compare"]
    assert (c["a"]["team_id"], c["b"]["team_id"]) == (1, 2) and res["compare_team_id"] == 2
    rb = next(x for x in c["positions"] if x["slot"] == "RB")
    assert rb["difference_per_slot"] == pytest.approx(rb["a"]["per_slot"] - rb["b"]["per_slot"], abs=0.011)
    assert rb["leader"] == ("a" if rb["difference_per_slot"] > 0 else "b")
    assert rb["a"]["starters"] and sum(s["weeks"] for s in rb["a"]["starters"]) == 2 * len(res["horizon"]["weeks"])
    assert rb["a"]["bench"] is not None and rb["a"]["depth"] == 5            # five active RBs; the IR RB is not depth
    assert next(x for x in c["positions"] if x["slot"] == "FLEX")["a"]["bench"] is None
    assert sum(c["leads"].values()) == 5                                       # K and D/ST are not counted as leads
    assert c["a"]["byes"]["conflict_weeks"] == [7]
    assert c["a"]["byes"]["weeks"][0]["starters_on_bye"] == ["Bye QB"]
    assert c["b"]["byes"]["available"] is False
    assert any(x["code"] == "byes_unavailable" for x in c["caveats"])
    assert c["a"]["needs"]["positions"] == [{"position": "QB", "rostered": 2, "league_average": 2.25,
                                             "comparison": "BELOW", "need_score": 0.71, "starters_required": 1,
                                             "healthy": 1}]
    assert c["b"]["needs"] == {"available": False, "positions": []}


def test_compare_leaves_a_position_uncompared_when_either_side_is_unknown(race):
    res = la.build_league_analyzer(drop_projection(race, pid(2, K_INDEX)), {}, team_id=1, vs=2, side={})
    k = next(x for x in res["compare"]["positions"] if x["slot"] == "K")
    assert k["leader"] is None and k["difference_per_slot"] is None
    assert any("Not compared at K" in x["text"] for x in res["compare"]["caveats"])


def test_team_ids_must_be_this_leagues_and_distinct(race):
    with pytest.raises(NotFound):
        la.build_league_analyzer(race, {}, team_id=99)
    with pytest.raises(NotFound):
        la.build_league_analyzer(race, {}, vs=99)
    with pytest.raises(la.SameTeam):
        la.build_league_analyzer(race, {}, team_id=3, vs=3)
    with pytest.raises(la.SameTeam):
        la.build_league_analyzer(race, {}, vs=1)               # the default focus is team 1 (is_mine)
    nobody = replace(race, teams=[{**t, "is_mine": False} for t in race.teams])
    assert la.build_league_analyzer(nobody, {})["focus_team_id"] is None
    # the page's needsTeam() contract: a visitor without a team is asked to pick one, never told the team is unknown
    with pytest.raises(NotFound, match="No team is marked as yours"):
        la.build_league_analyzer(nobody, {}, vs=2)


def test_no_remaining_weeks_is_an_empty_answer(race):
    res = la.build_league_analyzer(replace(race, state=replace(race.state, start_week=None)), {})
    assert res["empty"] and res["teams"] == [] and "every week has kicked off" in res["message"]


# ------------------------------------------------------------------ API --

def _client(monkeypatch, data):
    from app.api import league_analyzer as api
    from app.main import app

    class _Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(api, "load_team_value_inputs", lambda con: (data, {}))
    monkeypatch.setattr(api, "load_side_inputs", lambda con, ids: {})
    monkeypatch.setattr(api, "load_compatibility", lambda con: None)
    monkeypatch.setattr(api, "db", lambda: _Ctx())
    return TestClient(app)


def test_api_route_contract_and_validation(monkeypatch, race):
    client = _client(monkeypatch, race)
    res = client.get("/api/league-analyzer?team_id=2&vs=3&horizon=week&kdst_streaming=false")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["contract"] == "league-analyzer/v1" and body["focus_team_id"] == 2 and body["compare_team_id"] == 3
    assert body["horizon"]["key"] == "week" and body["kdst_streaming"] is False
    assert client.get("/api/league-analyzer?team_id=99").status_code == 404
    assert client.get("/api/league-analyzer?vs=99").status_code == 404
    assert client.get("/api/league-analyzer?team_id=2&vs=2").status_code == 422
    assert client.get("/api/league-analyzer?horizon=season").status_code == 422


def test_week_lists_read_as_ranges():
    assert la._weeks_text([7]) == "week 7"
    assert la._weeks_text([3, 4, 5, 9, 11, 12]) == "weeks 3–5, 9, 11–12"

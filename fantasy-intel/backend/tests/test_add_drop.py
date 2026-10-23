"""Add/drop scenario simulator (issue #178): parity with the lineup optimizer and the trade analyzer, and every
explicit case (add-only, drop-only, full roster, IR, waivers, rostered or unavailable players, kickoff locks).
Pure synthetic rosters; the API is covered by its golden (tests/golden/e2e/api_waivers_add_drop*.json) and below."""

import copy
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.analytics.add_drop import (ADD_NOT_AVAILABLE, ADD_ON_ROSTER, ADD_ROSTERED, ADD_OPTIONS_LIMIT, DROP_NOT_ON_ROSTER,
                                    DROP_REQUIRED, DROP_RESERVED, GAME_LOCKED, IR_SLOT_INELIGIBLE, NFL_INELIGIBLE,
                                    NO_REMAINING_WEEKS, NO_TEAM, WAIVER_CLAIM, AddDropContext, AddDropError,
                                    evaluate_add_drop)
from app.analytics.lineup import assign_lineup, lineup_slots
from app.analytics.start_sit import LOCK_LOCKED, PlayerLock
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trades import LeagueState, evaluate_trade, resolve_roster

# QB, RB x2, WR x2, TE, FLEX(RB/WR/TE) and 3 bench spots: active capacity 10. Weeks 10-11 regular, 12 playoff.
SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WEEKS = (10, 11, 12)


def P(pid, pos, team="KC", **kw):
    return RosterPlayer(pid, pos, team, f"P{pid}", **kw)


def flat(v):
    return {w: v for w in WEEKS}


def roster():
    """QB 20 | RB 15, 12 | WR 16, 11 | TE 10 | FLEX RB 9 || bench WR 8, TE 5: nine active players."""
    return [P(101, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"), P(107, "WR"),
            P(108, "TE"), P(109, "TE")]


PROJ = {101: flat(20), 102: flat(15), 103: flat(12), 104: flat(9), 105: flat(16), 106: flat(11), 107: flat(8),
        108: flat(10), 109: flat(5),
        # team 2
        201: flat(18), 202: flat(14),
        # free agents
        301: flat(13), 302: {10: 4, 11: 30, 12: 4}, 303: flat(7), 304: flat(12.5), 305: flat(6)}


def free_agents():
    return [P(301, "WR", "BUF"), P(302, "RB", "MIA"), P(303, "TE", "NYJ"), P(304, "RB", "SF"), P(305, "WR", "LV")]


def state(mine=None, capacity=10, limits=None, proj_over=None, fas=None, start_week=10, status=None):
    proj = copy.deepcopy(PROJ)
    for pid, v in (proj_over or {}).items():
        if v is None:
            proj.pop(pid, None)
        else:
            proj[pid] = v
    fas = free_agents() if fas is None else fas
    st = {p.espn_player_id: "FREEAGENT" for p in fas}
    st.update(status or {})
    return LeagueState(season=2026, slots=SLOTS, final_week=12, last_regular=11, start_week=start_week,
                       rosters={1: mine or roster(), 2: [P(201, "QB"), P(202, "RB")]}, free_agents=fas,
                       projections=proj, active_capacity=capacity, position_limits=limits or {},
                       team_names={1: "Mine", 2: "Rival"}, free_agent_status=st)


def best_total(players, week, proj):
    """The optimal starting total straight from lineup.assign_lineup (players without a projection never start)."""
    pool = [{"espn_player_id": p.espn_player_id, "position": p.position, "value": proj[p.espn_player_id][week]}
            for p in players if not p.reserved and week in proj.get(p.espn_player_id, {})]
    return sum(a["value"] for a in assign_lineup(pool, SLOTS) if a["player_id"] is not None)


def codes(items):
    return [x["code"] for x in items]


# ---- parity with the lineup optimizer -------------------------------------------------------------------------------

@pytest.mark.parametrize("add,drop", [(301, 107), (302, 104), (301, None), (None, 105), (304, 109), (303, 108)])
def test_every_week_matches_the_lineup_optimizer_exactly(add, drop):
    st = state()
    out = evaluate_add_drop(st, 1, add, drop)
    assert out["status"] == "evaluated", out["problems"]
    before = roster()
    after = [p for p in before if p.espn_player_id != drop] + [p for p in st.free_agents if p.espn_player_id == add]
    total = 0.0
    for row in out["impact"]["weeks"]:
        b, a = best_total(before, row["week"], st.projections), best_total(after, row["week"], st.projections)
        assert row["before"] == pytest.approx(b) and row["after"] == pytest.approx(a)
        assert row["delta"] == pytest.approx(a - b)
        total += a - b
    assert out["impact"]["rest_of_season"]["ros_delta"] == pytest.approx(total)
    assert out["impact"]["first_week"]["week"] == 10


def test_the_impact_is_the_trade_analyzers_own_side_computation():
    """A 1-for-1 trade with another team and an add/drop of the same incoming player value our side identically."""
    st = state()
    rival = [P(201, "QB"), P(202, "RB"), P(301, "WR", "BUF")]
    st_trade = replace(st, rosters={1: roster(), 2: rival}, free_agents=[p for p in st.free_agents if p.espn_player_id != 301])
    trade = evaluate_trade(st_trade, 1, 2, [107], [301], scenarios=False)
    sim = evaluate_add_drop(st, 1, 301, 107)
    assert sim["impact"]["rest_of_season"]["ros_delta"] == trade["team_a"]["deltas"]["ros_delta"]
    assert [w["delta"] for w in sim["impact"]["weeks"]] == [w["delta"] for w in trade["team_a"]["weekly"]]
    assert sim["impact"]["after"] == trade["team_a"]["after"]


def test_flex_displacement_names_who_enters_and_leaves():
    out = evaluate_add_drop(state(), 1, 301, 107)          # a 13-point WR: WR2 11 slides to FLEX, RB 9 sits
    first = out["impact"]["first_week"]
    assert [x["espn_player_id"] for x in first["entered"]] == [301]
    assert [x["espn_player_id"] for x in first["exited"]] == [104]
    assert first["delta"] == pytest.approx(4.0)
    slots = {a["player_id"]: a["slot"] for a in out["impact"]["lineup"]["after"]["assignment"]}
    assert slots[301] == "WR" and slots[106] == "FLEX"


def test_a_spike_week_is_reported_week_by_week_not_averaged_away():
    out = evaluate_add_drop(state(), 1, 302, 109)
    deltas = {w["week"]: w["delta"] for w in out["impact"]["weeks"]}
    assert deltas == {10: 0.0, 11: pytest.approx(21.0), 12: 0.0}
    ros = out["impact"]["rest_of_season"]
    assert ros["regular_season_delta"] == pytest.approx(21.0) and ros["playoff_delta"] == 0.0
    assert ros["weeks_improved"] == 1 and ros["weeks_unchanged"] == 2


# ---- missing is unknown, never zero ---------------------------------------------------------------------------------

def test_a_missing_projection_makes_the_delta_known_only():
    out = evaluate_add_drop(state(proj_over={301: {10: 13, 12: 13}}), 1, 301, 107)
    weeks = {w["week"]: w for w in out["impact"]["weeks"]}
    assert weeks[11]["delta"] is None and weeks[11]["known_delta"] is not None
    assert weeks[11]["projection_complete"] is False and weeks[11]["missing_projection_ids"] == [301]
    ros = out["impact"]["rest_of_season"]
    assert ros["ros_delta"] is None and ros["known_ros_delta"] is not None and ros["weeks_incomplete"] == [11]
    assert out["players"]["add"]["weekly_projection"][11] is None       # never filled with a 0


# ---- roster capacity ------------------------------------------------------------------------------------------------

def test_add_only_fits_an_open_spot():
    out = evaluate_add_drop(state(), 1, 301, None)
    r = out["roster"]
    assert (r["active_before"], r["active_after"], r["legal"], r["drop_required"]) == (9, 10, True, False)
    assert out["impact"]["first_week"]["delta"] == pytest.approx(4.0)


def test_add_only_on_a_full_roster_requires_a_drop_and_suggests_the_trade_engines_forced_drop():
    st = state(capacity=9)
    out = evaluate_add_drop(st, 1, 301, None)
    assert out["status"] == "blocked" and codes(out["problems"]) == [DROP_REQUIRED]
    assert out["impact"] is None and out["roster"]["drop_required"] is True
    assert "9 of 9 active spots" in out["problems"][0]["message"]
    expected = resolve_roster(st, st.window(), roster(), roster() + [st.free_agents[0]], {301})["forced_drops"]
    assert out["roster"]["suggested_drop"]["espn_player_id"] == expected[0]["espn_player_id"] == 104
    assert out["roster"]["suggested_drop"]["lineup_cost"] == 0.0


def test_a_position_limit_requires_a_drop_at_that_position():
    out = evaluate_add_drop(state(limits={"RB": 3}), 1, 302, None)
    assert codes(out["problems"]) == [DROP_REQUIRED] and "at most 3 RB" in out["problems"][0]["message"]
    assert out["roster"]["suggested_drop"]["position"] == "RB"
    assert evaluate_add_drop(state(limits={"RB": 3}), 1, 302, 104)["status"] == "evaluated"


def test_drop_only_is_always_legal_and_can_cost_points():
    out = evaluate_add_drop(state(), 1, None, 105)
    assert out["status"] == "evaluated" and out["acquisition"] is None
    assert out["roster"]["open_spots_after"] == 2
    assert out["impact"]["first_week"]["delta"] == pytest.approx(-(16 - 8))   # WR 16 out, bench WR 8 in


# ---- IR and taxi ----------------------------------------------------------------------------------------------------

def test_dropping_an_ir_player_frees_no_active_spot():
    mine = roster() + [P(110, "RB", on_ir=True, injury_status="OUT")]
    out = evaluate_add_drop(state(mine=mine, capacity=9, proj_over={110: flat(30)}), 1, 301, 110)
    assert codes(out["problems"]) == [DROP_REQUIRED] and "frees no active spot" in out["problems"][0]["message"]
    assert DROP_RESERVED in codes(out["notes"])
    roomy = evaluate_add_drop(state(mine=mine, proj_over={110: flat(30)}), 1, None, 110)
    assert roomy["status"] == "evaluated" and roomy["impact"]["rest_of_season"]["ros_delta"] == 0.0   # never started


def test_an_ir_occupant_without_an_ir_status_is_flagged_unless_he_is_the_drop():
    mine = roster() + [P(110, "RB", on_ir=True, injury_status="ACTIVE")]
    out = evaluate_add_drop(state(mine=mine), 1, 301, None)
    assert IR_SLOT_INELIGIBLE in codes(out["notes"]) and out["roster"]["ir_slot_blockers"] == [110]
    assert IR_SLOT_INELIGIBLE not in codes(evaluate_add_drop(state(mine=mine), 1, 301, 110)["notes"])


def test_a_taxi_player_never_starts_and_dropping_him_frees_no_spot():
    mine = roster() + [P(111, "WR", on_taxi=True)]
    out = evaluate_add_drop(state(mine=mine, capacity=9, proj_over={111: flat(40)}), 1, 301, 111)
    assert codes(out["problems"]) == [DROP_REQUIRED] and "taxi" in out["problems"][0]["message"]


def test_nfl_ineligible_weeks_of_the_added_player_are_noted_and_never_started():
    fas = [P(301, "WR", "BUF", unavailable_weeks=frozenset({10}))] + free_agents()[1:]
    out = evaluate_add_drop(state(fas=fas), 1, 301, 107)
    assert NFL_INELIGIBLE in codes(out["notes"])
    assert out["impact"]["first_week"]["entered"] == []


# ---- availability: rostered and unavailable players are never eligible claims ---------------------------------------

def test_rostered_players_are_never_offered_and_are_blocked_with_reasons():
    st = state()
    offered = {o["espn_player_id"] for o in evaluate_add_drop(st, 1, None, None)["options"]["add"]}
    rostered = {p.espn_player_id for ps in st.rosters.values() for p in ps}
    assert offered == {301, 302, 303, 304, 305} and not offered & rostered
    own = evaluate_add_drop(st, 1, 102, None)
    assert own["status"] == "blocked" and codes(own["problems"]) == [ADD_ON_ROSTER] and own["impact"] is None
    other = evaluate_add_drop(st, 1, 202, 107)
    assert codes(other["problems"]) == [ADD_ROSTERED] and "Rival" in other["problems"][0]["message"]
    assert other["players"]["add"]["roster_status"] == "other_roster"


def test_a_player_not_on_the_free_agent_or_waiver_list_cannot_be_added():
    ctx = AddDropContext(directory={999: {"espn_player_id": 999, "full_name": "Gone Guy", "availability_status": "ONTEAM"}})
    out = evaluate_add_drop(state(), 1, 999, None, ctx)
    assert codes(out["problems"]) == [ADD_NOT_AVAILABLE] and "Gone Guy" in out["problems"][0]["message"]
    assert out["players"]["add"]["roster_status"] == "not_available"


def test_a_drop_must_be_on_the_evaluated_teams_roster():
    out = evaluate_add_drop(state(), 1, 301, 201)
    assert codes(out["problems"]) == [DROP_NOT_ON_ROSTER] and out["impact"] is None
    assert codes(evaluate_add_drop(state(), 1, 301, 305)["problems"]) == [DROP_NOT_ON_ROSTER]   # a free agent


def test_a_player_on_waivers_is_a_claim_with_its_rules():
    ctx = AddDropContext(rules={"is_faab": True, "faab_budget": 100}, team_row={"faab_spent": 30},
                         waivers={"process_days": {"value": ["Wednesday"], "display": "Claims process on Wednesday"}})
    out = evaluate_add_drop(state(status={301: "WAIVERS"}), 1, 301, 107, ctx)
    assert out["status"] == "evaluated" and WAIVER_CLAIM in codes(out["notes"])
    assert "claims process on wednesday" in next(n for n in out["notes"] if n["code"] == WAIVER_CLAIM)["message"]
    acq = out["acquisition"]
    assert acq["claim_required"] is True and acq["kind"] == "FAAB" and acq["remaining"] == 70
    assert out["players"]["add"]["roster_status"] == "waivers"
    assert evaluate_add_drop(state(), 1, 301, 107)["acquisition"]["kind"] == "FREE_AGENT"


def test_the_add_picker_is_bounded_and_best_known_first():
    fas = [P(4000 + i, "WR", "LV") for i in range(ADD_OPTIONS_LIMIT + 15)]
    over = {p.espn_player_id: flat(1 + i / 10) for i, p in enumerate(fas)}
    out = evaluate_add_drop(state(fas=fas, proj_over=over), 1, fas[0].espn_player_id, None)
    opts = out["options"]["add"]
    assert out["options"]["add_total"] == ADD_OPTIONS_LIMIT + 15
    assert opts[0]["espn_player_id"] == fas[-1].espn_player_id
    assert len(opts) == ADD_OPTIONS_LIMIT + 1 and opts[-1]["espn_player_id"] == fas[0].espn_player_id  # the asked one


# ---- time window, locks, byes and depth -----------------------------------------------------------------------------

def test_a_kicked_off_game_is_noted_and_the_window_is_the_trade_engines():
    lock = PlayerLock(team="BUF", status=LOCK_LOCKED, kickoff_utc=datetime(2026, 11, 12, 1, tzinfo=timezone.utc),
                      reason="Game kicked off")
    out = evaluate_add_drop(state(start_week=11), 1, 301, 107, AddDropContext(locks={"BUF": lock}, lock_week=10))
    assert GAME_LOCKED in codes(out["notes"])
    assert out["evaluation"]["weeks"] == [11, 12] and out["impact"]["first_week"]["week"] == 11
    assert out["players"]["add"]["kickoff_lock"]["status"] == LOCK_LOCKED


def test_no_remaining_weeks_blocks_with_a_reason():
    out = evaluate_add_drop(state(start_week=None), 1, 301, None)
    assert codes(out["problems"]) == [NO_REMAINING_WEEKS] and out["impact"] is None


def test_byes_and_open_slots_week_by_week():
    games = {(t, w): ("OPP", True) for t in ("KC", "BUF", "MIA", "NYJ", "SF", "LV") for w in WEEKS}
    del games[("BUF", 11)]
    ctx = AddDropContext(games=games, covered=set(WEEKS))
    out = evaluate_add_drop(state(proj_over={301: {10: 13, 11: 0, 12: 13}}), 1, 301, 107, ctx)
    assert out["players"]["add"]["bye_weeks"] == [11]
    weeks = {w["week"]: w for w in out["impact"]["weeks"]}
    assert weeks[11]["on_bye_after"] == [301] and weeks[11]["on_bye_before"] == []
    assert weeks[11]["delta"] == 0.0 and weeks[10]["delta"] == pytest.approx(4.0)   # a projected 0 on his bye is real
    assert all(w["open_slots_after"] == 0 for w in weeks.values())
    unknown = evaluate_add_drop(state(), 1, 301, 107)
    assert unknown["players"]["add"]["bye_weeks"] is None and unknown["impact"]["weeks"][0]["on_bye_after"] is None


def test_depth_before_and_after():
    out = evaluate_add_drop(state(), 1, 302, 107)
    by = {d["position"]: d for d in out["depth"]}
    assert (by["RB"]["before"]["active"], by["RB"]["after"]["active"], by["RB"]["active_change"]) == (3, 4, 1)
    assert by["WR"]["active_change"] == -1 and by["RB"]["starting_slots"] == 2
    assert by["RB"]["flex_slots"] == [{"slot": "FLEX", "count": 1}]
    blocked = evaluate_add_drop(state(), 1, 202, None)
    assert all(d["after"] is None for d in blocked["depth"])


# ---- request shape --------------------------------------------------------------------------------------------------

def test_one_pair_only_and_known_teams():
    with pytest.raises(AddDropError):
        evaluate_add_drop(state(), 1, 301, 301)
    with pytest.raises(LookupError):
        evaluate_add_drop(state(), 9, 301, None)
    none = evaluate_add_drop(state(), None, 301, None)
    assert codes(none["problems"])[0] == NO_TEAM and none["options"]["drop"] == []
    assert evaluate_add_drop(state(), 1, None, None)["status"] == "options"


def test_the_state_is_never_mutated():
    st = state(capacity=9)
    snap = copy.deepcopy(st)
    for add, drop in ((301, 107), (301, None), (None, 105), (202, None)):
        evaluate_add_drop(st, 1, add, drop)
    assert st == snap


def test_a_large_flex_league_evaluates_quickly():
    """Superflex-style league (OP, three FLEX, RB/WR, WR/TE), 17 weeks, distinct projection values as ESPN publishes."""
    slots = lineup_slots({"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 3, "OP": 1, "RB/WR": 1, "WR/TE": 1, "K": 1,
                          "D/ST": 1})
    weeks = range(1, 18)
    pos = ["QB"] * 3 + ["RB"] * 7 + ["WR"] * 8 + ["TE"] * 3 + ["K", "D/ST"]
    mine = [P(1000 + i, p) for i, p in enumerate(pos)]
    fas = [P(5000 + i, p, "BUF") for i, p in enumerate(["RB", "WR", "TE", "QB"] * 20)]
    proj = {p.espn_player_id: {w: round(2 + ((p.espn_player_id * 37 + w * 11) % 230) / 10, 2) for w in weeks}
            for p in mine + fas}
    st = LeagueState(season=2026, slots=slots, final_week=17, last_regular=14, start_week=1, rosters={1: mine},
                     free_agents=fas, projections=proj, active_capacity=len(slots) + 10, team_names={1: "A"})
    t = time.perf_counter()
    out = evaluate_add_drop(st, 1, 5000, 1010)
    full = evaluate_add_drop(replace(st, active_capacity=len(mine)), 1, 5001, None)   # forced-drop suggestion path
    elapsed = time.perf_counter() - t
    assert out["status"] == "evaluated" and len(out["impact"]["weeks"]) == 17
    assert full["roster"]["suggested_drop"]["available"] is True
    assert elapsed < 5.0, f"{elapsed:.2f}s"


# ---- API ------------------------------------------------------------------------------------------------------------

def test_the_route_is_get_only():
    from app.api.routing import iter_routes
    from app.main import app
    mine = [r for r in iter_routes(app) if "add-drop" in r.path]
    assert mine and all(r.methods == {"GET"} for r in mine)
    assert {r.path for r in mine} == {"/api/waivers/add-drop", "/v2/leagues/{league_id}/api/waivers/add-drop"}

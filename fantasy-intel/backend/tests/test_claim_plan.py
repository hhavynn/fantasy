"""Waiver claim planner (issue #180): each claim is the add/drop simulator's own scenario, claims that depend on the
same drop or open spot are flagged on the later claim, bids are checked only where the provider supplies the rules, and
nothing incomplete is confirmed as legal. Pure synthetic rosters; the API is covered below and by its golden
(tests/golden/e2e/api_waivers_claim_plan*.json)."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.analytics.add_drop import AddDropContext, evaluate_add_drop
from app.analytics.claim_plan import (BID_FREE_AGENT, BID_MISSING, BID_NOT_USED, BID_OVER_BUDGET_LEFT,
                                      BID_OVER_REMAINING, BID_RULES_UNKNOWN, BUDGET_EXHAUSTED, BUDGET_UNKNOWN,
                                      DUPLICATE_ADD, FREE_AGENT, INVALID, LEGAL, MAX_CLAIMS, OPEN_SPOTS,
                                      RULES_UNKNOWN, SAME_PLAYER, SHARED_DROP, UNKNOWN_PLAYER, UNVERIFIED, Claim,
                                      ClaimPlanError, evaluate_claim_plan, parse_claim)
from app.analytics.lineup import lineup_slots
from app.analytics.start_sit import LOCK_LOCKED, LOCK_UNKNOWN, PlayerLock
from app.analytics.team_lineups import RosterPlayer
from app.analytics.trades import LeagueState

SLOTS = lineup_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
WEEKS = (10, 11, 12)
KNOWN_FACTS = {"type": {"value": "faab", "display": "FAAB bidding", "reason": None},
               "period": {"value": 48, "display": "48 hours", "reason": None},
               "process_days": {"value": ["Wednesday"], "display": "Claims process on Wednesday", "reason": None},
               "faab_budget": {"value": 100, "display": "$100", "reason": None}}


def P(pid, pos, team="KC", **kw):
    return RosterPlayer(pid, pos, team, f"P{pid}", **kw)


def flat(v):
    return {w: v for w in WEEKS}


def roster():
    """QB 20 | RB 15, 12 | WR 16, 11 | TE 10 | FLEX RB 9 || bench WR 8, TE 5: nine of ten active spots."""
    return [P(101, "QB"), P(102, "RB"), P(103, "RB"), P(104, "RB"), P(105, "WR"), P(106, "WR"), P(107, "WR"),
            P(108, "TE"), P(109, "TE")]


PROJ = {101: flat(20), 102: flat(15), 103: flat(12), 104: flat(9), 105: flat(16), 106: flat(11), 107: flat(8),
        108: flat(10), 109: flat(5), 201: flat(18), 202: flat(14),
        301: flat(13), 302: {10: 4, 11: 30, 12: 4}, 303: flat(7), 304: flat(12.5), 305: flat(6)}


def state(mine=None, capacity=10, waivers=(301, 302, 303)):
    fas = [P(301, "WR", "BUF"), P(302, "RB", "MIA"), P(303, "TE", "NYJ"), P(304, "RB", "SF"), P(305, "WR", "LV")]
    status = {p.espn_player_id: ("WAIVERS" if p.espn_player_id in waivers else "FREEAGENT") for p in fas}
    return LeagueState(season=2026, slots=SLOTS, final_week=12, last_regular=11, start_week=10,
                       rosters={1: mine or roster(), 2: [P(201, "QB"), P(202, "RB")]}, free_agents=fas,
                       projections=copy.deepcopy(PROJ), active_capacity=capacity, team_names={1: "Mine", 2: "Rival"},
                       free_agent_status=status)


def ctx(st, *, is_faab=True, budget=100, spent=20, rank=3, facts=None, locks=None):
    """A FAAB league with every rule synced unless told otherwise; the directory holds every player in `st`."""
    everyone = [p for ps in st.rosters.values() for p in ps] + st.free_agents
    directory = {p.espn_player_id: {"full_name": p.name, "position": p.position, "pro_team": p.pro_team,
                                    "injury_status": p.injury_status,
                                    "availability_status": st.free_agent_status.get(p.espn_player_id)}
                 for p in everyone}
    return AddDropContext(provider="ESPN", team_row={"waiver_rank": rank, "faab_spent": spent},
                          rules={"is_faab": is_faab, "faab_budget": budget}, waivers=facts or KNOWN_FACTS,
                          directory=directory, locks=locks or {}, lock_week=10)


def plan(claims, st=None, **kw):
    st = st or state()
    return evaluate_claim_plan(st, 1, [Claim(*c) for c in claims], ctx(st, **kw))


def codes(c):
    return [r["code"] for r in c["reasons"]]


# ---- the simulator is the only lineup computation -------------------------------------------------------------------

def test_each_claim_is_the_add_drop_simulators_own_scenario():
    st = state()
    out = plan([(301, 107, 10), (302, 104, 5), (304, None)], st)
    for c in out["claims"]:
        sim = evaluate_add_drop(st, 1, c["request"]["add"], c["request"]["drop"], ctx(st))
        assert c["simulation_status"] == sim["status"] == "evaluated"
        assert c["impact"]["rest_of_season"]["ros_delta"] == sim["impact"]["rest_of_season"]["ros_delta"]
        assert c["impact"]["first_week"]["delta"] == sim["impact"]["first_week"]["delta"]
        assert c["roster"]["legal"] is True
    assert [c["validation"] for c in out["claims"]] == [LEGAL, LEGAL, LEGAL]
    assert out["submitted"] is False and out["contract"] == "waiver-claim-plan/v1"
    assert out["roster"]["open_spots_before"] == 1
    assert {o["espn_player_id"] for o in out["roster"]["drop_options"]} == {p.espn_player_id for p in roster()}


def test_an_empty_plan_still_answers_the_rules_and_the_drop_options():
    out = plan([])
    assert out["status"] == "empty" and out["claims"] == []
    assert out["rules"]["acquisition"] == "FAAB" and out["rules"]["faab_remaining"] == 80
    assert len(out["roster"]["drop_options"]) == 9


def test_the_state_is_never_mutated():
    st = state(capacity=9)
    snap = copy.deepcopy(st)
    plan([(301, 107, 1), (302, None, 1), (201, None), (999, None)], st)
    assert st == snap


# ---- dependencies between claims -----------------------------------------------------------------------------------

def test_a_shared_drop_is_flagged_on_the_later_claim_only():
    out = plan([(301, 107, 10), (302, 107, 5)])
    first, second = out["claims"]
    assert first["validation"] == LEGAL and SHARED_DROP not in codes(first)
    assert second["validation"] == UNVERIFIED and SHARED_DROP in codes(second)
    assert out["summary"]["conflicts"] == [{"kind": "shared_drop", "player_id": 107, "ranks": [1, 2]}]
    # order decides which claim is the later one
    swapped = plan([(302, 107, 5), (301, 107, 10)])
    assert [c["validation"] for c in swapped["claims"]] == [LEGAL, UNVERIFIED]


def test_more_add_only_claims_than_open_spots_flags_the_extra_ones():
    out = plan([(301, None, 10), (302, None, 5), (304, None)])
    assert [c["validation"] for c in out["claims"]] == [LEGAL, UNVERIFIED, UNVERIFIED]
    assert OPEN_SPOTS in codes(out["claims"][1]) and OPEN_SPOTS in codes(out["claims"][2])
    assert "claim 1 also adds a player" in next(r["message"] for r in out["claims"][1]["reasons"] if r["code"] == OPEN_SPOTS)
    assert out["summary"]["conflicts"] == [{"kind": "open_spots", "player_id": None, "ranks": [1, 2, 3], "open_spots": 1}]
    # a drop on the second claim resolves it
    fixed = plan([(301, None, 10), (302, 104, 5)])
    assert [c["validation"] for c in fixed["claims"]] == [LEGAL, LEGAL]


def test_a_full_roster_needs_a_drop_and_names_the_simulators_suggestion():
    st = state(capacity=9)
    out = plan([(301, None, 10)], st)
    c = out["claims"][0]
    assert c["validation"] == INVALID and "DROP_REQUIRED" in codes(c)
    assert c["suggested_drop"]["available"] is True
    assert c["suggested_drop"] == evaluate_add_drop(st, 1, 301, None, ctx(st))["roster"]["suggested_drop"]
    assert c["impact"] is None


# ---- bad or incomplete data is never a confirmed legal claim -------------------------------------------------------

def test_unavailable_unknown_duplicate_and_self_drops_are_never_legal():
    out = plan([(201, None), (999, None), (301, 107, 3), (301, 106, 3), (302, 302, 3), (303, 888, 3), (304, 201)])
    rostered, unknown, ok, dup, same, unknown_drop, foreign_drop = out["claims"]
    assert rostered["validation"] == INVALID and "ADD_ROSTERED" in codes(rostered)
    assert unknown["validation"] == UNVERIFIED and codes(unknown) == [UNKNOWN_PLAYER]
    assert unknown["add"]["roster_status"] == "unknown" and unknown["impact"] is None
    assert ok["validation"] == LEGAL
    assert dup["validation"] == INVALID and codes(dup) == [DUPLICATE_ADD]
    assert same["validation"] == INVALID and codes(same) == [SAME_PLAYER]
    assert unknown_drop["validation"] == UNVERIFIED and codes(unknown_drop) == [UNKNOWN_PLAYER]
    assert foreign_drop["validation"] == INVALID and "DROP_NOT_ON_ROSTER" in codes(foreign_drop)
    assert out["summary"] | {"conflicts": None} == {"claims": 7, LEGAL: 1, UNVERIFIED: 2, INVALID: 4, "total_bid": 3,
                                                     "remaining_after_bids": 77, "conflicts": None}


def test_unknown_acquisition_rules_leave_a_waiver_claim_unconfirmed():
    out = plan([(301, 107, 4), (304, 106)], is_faab=None)
    claim, free = out["claims"]
    assert claim["validation"] == UNVERIFIED and {RULES_UNKNOWN, BID_RULES_UNKNOWN} <= set(codes(claim))
    assert free["validation"] == LEGAL and FREE_AGENT in codes(free)      # a free agent needs no claim
    assert out["rules"]["acquisition"] == "UNKNOWN" and out["rules"]["faab_budget"] is None
    assert {u["rule"] for u in out["rules"]["unknown"]} == {"acquisition"}


def test_a_kickoff_lock_we_cannot_see_or_an_ir_slot_block_is_not_legal():
    st = state()
    unknown = plan([(301, 107, 4)], st, locks={"BUF": PlayerLock(team="BUF", status=LOCK_UNKNOWN)})
    assert unknown["claims"][0]["validation"] == UNVERIFIED and "LOCK_UNKNOWN" in codes(unknown["claims"][0])
    mine = roster() + [P(110, "RB", on_ir=True, injury_status="ACTIVE")]
    blocked = plan([(301, 107, 4)], state(mine=mine))
    assert blocked["claims"][0]["validation"] == INVALID and "IR_SLOT_INELIGIBLE" in codes(blocked["claims"][0])
    locked = plan([(301, 107, 4)], st, locks={"BUF": PlayerLock(team="BUF", status=LOCK_LOCKED)})
    assert locked["claims"][0]["validation"] == UNVERIFIED and "GAME_LOCKED" in codes(locked["claims"][0])


# ---- bids: only where the provider supplies the rules; never suggested ---------------------------------------------

def test_faab_bids_are_checked_against_the_synced_budget():
    out = plan([(301, 107, 90), (302, 104, None), (303, 109, 30), (304, 106, 5)])
    over, missing, ok, free = out["claims"]
    assert over["validation"] == INVALID and BID_OVER_REMAINING in codes(over)
    assert missing["validation"] == UNVERIFIED and BID_MISSING in codes(missing)
    assert ok["validation"] == LEGAL
    assert free["validation"] == LEGAL and BID_FREE_AGENT in codes(free)
    assert out["summary"]["total_bid"] == 120 and out["summary"]["remaining_after_bids"] == -40
    assert all(r["code"] != "BID_SUGGESTED" for c in out["claims"] for r in c["reasons"])


def test_bids_that_cannot_all_win_flag_the_later_claim():
    out = plan([(301, 107, 50), (302, 104, 40)])
    assert [c["validation"] for c in out["claims"]] == [LEGAL, UNVERIFIED]
    assert BID_OVER_BUDGET_LEFT in codes(out["claims"][1])


def test_a_bid_in_a_priority_league_is_not_confirmed_and_priority_is_reported():
    out = plan([(301, 107, 5), (302, 104)], is_faab=False)
    assert out["claims"][0]["validation"] == UNVERIFIED and BID_NOT_USED in codes(out["claims"][0])
    assert out["claims"][1]["validation"] == LEGAL
    assert out["rules"] | {"waiver_type": None, "waiver_period": None, "process_days": None} == {
        "acquisition": "PRIORITY", "is_faab": False, "faab_budget": None, "faab_spent": None, "faab_remaining": None,
        "waiver_priority": 3, "waiver_type": None, "waiver_period": None, "process_days": None, "unknown": []}
    assert out["summary"]["total_bid"] is None


def test_unknown_budget_or_spend_and_an_exhausted_budget_are_not_confirmed():
    spend = plan([(301, 107, 5)], spent=None)
    assert spend["claims"][0]["validation"] == UNVERIFIED and BUDGET_UNKNOWN in codes(spend["claims"][0])
    assert {u["rule"] for u in spend["rules"]["unknown"]} == {"faab_spent"}
    budget = plan([(301, 107, 5)], budget=None)
    assert {u["rule"] for u in budget["rules"]["unknown"]} == {"faab_budget"}
    spent = plan([(301, 107, 0)], spent=100)
    assert spent["claims"][0]["validation"] == UNVERIFIED and BUDGET_EXHAUSTED in codes(spent["claims"][0])


def test_unknown_processing_rules_are_listed_with_their_reasons():
    facts = {"type": {"value": "faab", "display": "FAAB bidding", "reason": None},
             "period": {"value": None, "display": None, "reason": "The synced settings do not include the waiver period."},
             "process_days": {"value": None, "display": None, "reason": "Not interpreted here."}}
    out = plan([(301, 107, 5)], facts=facts)
    assert out["rules"]["unknown"] == [
        {"rule": "period", "reason": "The synced settings do not include the waiver period."},
        {"rule": "process_days", "reason": "Not interpreted here."}]


# ---- reading the plan ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,claim", [("301", Claim(301)), ("301:107", Claim(301, 107)), ("301::12", Claim(301, None, 12)),
                                        ("-16002:107:0", Claim(-16002, 107, 0)), ("301:", Claim(301))])
def test_claims_parse(text, claim):
    assert parse_claim(text) == claim


@pytest.mark.parametrize("text", ["", ":107", "abc", "301:x", "301:107:-1", "301:107:1.5", "301:107:100001",
                                  "301:107:5:9", str(10**13)])
def test_malformed_claims_are_refused(text):
    with pytest.raises(ClaimPlanError):
        parse_claim(text)


def test_a_plan_is_bounded_and_the_team_must_be_in_the_league():
    st = state()
    with pytest.raises(ClaimPlanError):
        evaluate_claim_plan(st, 1, [Claim(300 + i) for i in range(MAX_CLAIMS + 1)], ctx(st))
    with pytest.raises(LookupError):
        evaluate_claim_plan(st, 9, [Claim(301)], ctx(st))


# ---- API ------------------------------------------------------------------------------------------------------------

def test_the_route_is_get_only():
    from app.api.routing import iter_routes
    from app.main import app
    mine = [r for r in iter_routes(app) if "claim-plan" in r.path]
    assert mine and all(r.methods == {"GET"} for r in mine)
    assert {r.path for r in mine} == {"/api/waivers/claim-plan", "/v2/leagues/{league_id}/api/waivers/claim-plan"}


@pytest.fixture
def fixture_api(tmp_path, monkeypatch):
    from espn_fixture import MY_SWID, FakeEspnClient
    from fastapi.testclient import TestClient

    from app.db.connection import open_db
    from app.espn.sync import run_espn_sync

    db_path = tmp_path / "plan.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_the_api_checks_a_plan_from_the_synced_league(fixture_api):
    res = fixture_api.get("/api/waivers/claim-plan?claim=309:206:15&claim=209&claim=99999")
    assert res.status_code == 200, res.text[:300]
    d = res.json()
    assert d["contract"] == "waiver-claim-plan/v1" and d["submitted"] is False
    assert d["rules"]["acquisition"] == "FAAB" and d["rules"]["faab_budget"] == 100
    waiver, free, unknown = d["claims"]
    assert waiver["claim_kind"] == "waiver_claim" and waiver["request"] == {"add": 309, "drop": 206, "bid": 15}
    assert free["claim_kind"] == "free_agent"
    assert unknown["validation"] == UNVERIFIED and codes(unknown) == [UNKNOWN_PLAYER]
    assert d["provenance"]["contract"] == "signal-provenance.v1"
    assert "ESPN" in d["assumptions"][0]


@pytest.mark.parametrize("query", ["?claim=abc", "?claim=301:107:-5", "?team_id=-1", "?claim=209:206:5" + "0" * 60,
                                   "?claim=209:206:5" + "%20" * 60,
                                   "?" + "&".join(f"claim={300 + i}" for i in range(MAX_CLAIMS + 1))])
def test_the_api_refuses_a_malformed_plan(fixture_api, query):
    assert fixture_api.get("/api/waivers/claim-plan" + query).status_code == 422


def test_the_api_answers_404_for_a_team_outside_the_league(fixture_api):
    assert fixture_api.get("/api/waivers/claim-plan?team_id=77&claim=209").status_code == 404


def test_more_flex_slots_than_supported_is_the_app_wide_422(fixture_api, monkeypatch):
    from app.analytics.lineup import MAX_FLEX_SLOTS, UNSUPPORTED_LINEUP
    from app.api import add_drop as route
    real = route.load_league_state
    monkeypatch.setattr(route, "load_league_state", lambda con, **kw: replace(
        real(con, **kw), slots=lineup_slots({"QB": 1, "FLEX": MAX_FLEX_SLOTS + 1})))
    res = fixture_api.get("/api/waivers/claim-plan?claim=209:206")
    assert res.status_code == 422 and res.json() == {"detail": UNSUPPORTED_LINEUP}

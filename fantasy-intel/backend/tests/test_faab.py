"""FAAB bid guidance (issue #179): the capability audit gates everything, an unknown budget or bid history stays
unknown, every range is a labelled heuristic within the known remaining budget, the plan's bids are evaluated together,
and observed league bids are shown beside the heuristic, never blended into it or read from private claims. Pure
synthetic rosters (the claim planner's test league); the API is covered below and by its golden
(tests/golden/e2e/api_waivers_faab*.json)."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.analytics.claim_plan import Claim, evaluate_claim_plan
from app.analytics.faab import (BLOCKED, BUDGET_EXHAUSTED, CONTRACT, NO_BID_NEEDED, NO_GAIN, NOT_EVALUATED, RANGE,
                                UNAVAILABLE, VALUE_UNKNOWN, bid_range, combined, evaluate_faab, observed_bids)
from app.analytics.league_rules import HANDLED, PARTIAL, UNSUPPORTED
from test_claim_plan import P, ctx, flat, state

TEAMS = [{"team_id": 1, "faab_spent": 20}, {"team_id": 2, "faab_spent": 35}]


def bid(pid, amount, pos="WR", week=5, tx=None):
    return {"transaction_id": tx or f"t{pid}-{week}", "scoring_period": week, "bid_amount": amount,
            "espn_player_id": pid, "full_name": f"P{pid}", "position": pos}


def faab(claims, st=None, *, share=25, settings=None, teams=TEAMS, observed=(), provider="espn", label="ESPN",
         status_known=True, **kw):
    st = st or state()
    cx = ctx(st, provider=label, status_known=status_known, **kw)
    plan = evaluate_claim_plan(st, 1, [Claim(*c) for c in claims], cx)
    return evaluate_faab(plan, provider=provider, provider_label=label, settings=settings or {}, team_row=cx.team_row,
                         teams=teams, observed_rows=None if observed is None else list(observed), share=share,
                         waiver_status_known=status_known)


def with_proj(pid, value):
    st = state()
    st.projections[pid] = flat(value) if not isinstance(value, dict) else value
    return st


def field(out, name):
    return next(f for f in out["capability"]["fields"] if f["field"] == name)


# ---- capability audit ----------------------------------------------------------------------------------------------

def test_a_confirmed_faab_league_with_a_known_budget_gets_guidance():
    out = faab([(301, 107)], observed=[bid(1, 7), bid(2, 0)])
    cap = out["capability"]
    assert cap["guidance"] == "available" and cap["observed_bids"] == "available" and cap["flag"] == HANDLED
    assert out["contract"] == CONTRACT and out["submitted"] is False
    assert out["budget"] | {"other_teams": None} == {"acquisition": "FAAB", "budget": 100, "spent": 20, "remaining": 80,
                                                      "minimum_bid": None, "weeks_left": 3, "even_split_per_week": 26.67,
                                                      "other_teams": None}
    assert field(out, "faab_remaining")["status"] == "supplied"
    assert field(out, "minimum_bid")["status"] == "missing" and field(out, "minimum_bid")["source"] is None
    assert out["claims"][0]["status"] == RANGE


def test_a_priority_league_gets_no_bid_guidance():
    out = faab([(301, 107)], is_faab=False, observed=[bid(1, 7)])
    cap = out["capability"]
    assert cap["guidance"] == "not_applicable" and cap["observed_bids"] == "not_applicable" and cap["flag"] == UNSUPPORTED
    assert out["claims"][0]["status"] == UNAVAILABLE and out["claims"][0]["range"] is None
    assert out["observed"] is None and out["combined"] is None
    assert out["budget"]["budget"] is None and out["budget"]["remaining"] is None
    assert {f["field"] for f in cap["fields"] if f["status"] == "not_applicable"} >= {"faab_budget", "observed_bids"}


def test_unknown_acquisition_rules_get_no_guidance():
    out = faab([(301, 107)], is_faab=None)
    assert out["capability"]["guidance"] == "unavailable" and out["claims"][0]["status"] == UNAVAILABLE
    assert field(out, "acquisition_type")["status"] == "missing"


@pytest.mark.parametrize("kw", [{"budget": None}, {"spent": None}])
def test_an_unknown_budget_or_spend_is_never_taken_as_zero(kw):
    out = faab([(301, 107), (302, 104)], **kw)
    assert out["capability"]["guidance"] == "unavailable"
    assert "not assumed to be 0" in out["capability"]["reasons"][0]
    assert out["budget"]["remaining"] is None and out["budget"]["even_split_per_week"] is None
    assert all(c["status"] == UNAVAILABLE and c["range"] is None for c in out["claims"])
    assert out["combined"] is None


def test_bid_history_unavailability_is_reported_truthfully():
    # no transactions synced at all
    none = faab([(301, 107)], observed=None)
    assert none["capability"]["observed_bids"] == "unavailable" and none["observed"] is None
    # claims processed but the provider stored no amounts
    blank = faab([(301, 107)], observed=[bid(1, None), bid(2, None)])
    assert blank["capability"]["observed_bids"] == "unavailable" and blank["observed"] is None
    assert field(blank, "observed_bids")["status"] == "not_supplied" and field(blank, "observed_bids")["source"] is None
    assert "carry no bid amounts" in " ".join(blank["capability"]["reasons"])
    # nothing processed yet: not the same as "the provider has no bids"
    fresh = faab([(301, 107)], observed=[])
    assert fresh["capability"]["observed_bids"] == "none_yet" and "No waiver claim has been processed" in \
        " ".join(fresh["capability"]["reasons"])
    for out in (none, blank, fresh):
        assert out["capability"]["flag"] == PARTIAL and out["claims"][0]["status"] == RANGE


def test_sleeper_fields_and_waiver_status_are_stated_per_provider():
    out = faab([(301, 107)], provider="sleeper", label="Sleeper", status_known=False,
               settings={"waiver_bid_min": 1})
    assert field(out, "waiver_status")["status"] == "not_supplied"
    assert field(out, "minimum_bid") | {"reason": None} == {
        "field": "minimum_bid", "label": "Minimum bid", "status": "supplied", "value": 1, "display": "$1",
        "source": "settings.waiver_bid_min", "reason": None}
    assert field(out, "faab_spent")["source"] == "rosters[].settings.waiver_budget_used"
    c = out["claims"][0]
    assert c["status"] == RANGE and [n["code"] for n in c["notes"]] == ["AVAILABILITY_UNKNOWN"]
    assert "Sleeper does not report whether he is on waivers" in c["notes"][0]["message"]
    assert "ESPN" not in json.dumps([out["capability"], out["claims"], out["assumptions"]])


def test_espn_minimum_bid_is_read_from_acquisition_settings():
    out = faab([(301, 107)], settings={"acquisitionSettings": {"minimumBid": 0}})
    assert out["budget"]["minimum_bid"] == 0
    assert field(out, "minimum_bid")["source"] == "acquisitionSettings.minimumBid"


# ---- the heuristic --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, tier, low, high", [(13, "high", 13, 26), (11, "medium", 5, 13), (9.5, "low", 0, 5)])
def test_urgency_comes_from_the_simulated_lineup_gain_per_remaining_week(value, tier, low, high):
    """Three weeks left and $80: the even split (1/3) is above the 25% share, so the cap is $26.67."""
    c = faab([(301, 107)], st=with_proj(301, value))["claims"][0]
    assert c["status"] == RANGE and c["urgency"] == tier
    assert (c["range"]["low"], c["range"]["high"]) == (low, high)
    assert c["range"]["cap_share_pct"] == 33.3 and c["basis"]["weeks"] == 3


def test_no_lineup_gain_gets_no_bid():
    c = faab([(303, 109)])["claims"][0]           # a TE who would sit behind the starter
    assert c["status"] == NO_GAIN and c["urgency"] == "none" and c["range"] is None


def test_a_missing_projection_gives_no_range_and_is_not_zero():
    st = with_proj(301, {10: 13, 11: None, 12: 13})
    del st.projections[301][11]
    out = faab([(301, 107)], st=st)
    c = out["claims"][0]
    assert c["status"] == VALUE_UNKNOWN and c["range"] is None and c["urgency"] is None
    assert c["basis"]["ros_gain"] is None and "not 0" in c["reason"]
    assert out["combined"]["complete"] is False and out["combined"]["unranged_ranks"] == [1]


def test_the_share_is_the_managers_input_and_raises_the_cap():
    st = with_proj(301, 13)
    lo = faab([(301, 107)], st=copy.deepcopy(st), share=10)["claims"][0]["range"]
    hi = faab([(301, 107)], st=copy.deepcopy(st), share=100)["claims"][0]["range"]
    assert lo["cap_share_pct"] == 33.3                 # the even split is the floor
    assert (hi["low"], hi["high"]) == (40, 80) and hi["cap_share_pct"] == 100


@pytest.mark.parametrize("remaining", [0.5, 1, 3, 7.5, 80, 999])
@pytest.mark.parametrize("weeks", [1, 2, 9, 17])
@pytest.mark.parametrize("share", [1, 25, 100])
@pytest.mark.parametrize("tier", ["low", "medium", "high"])
@pytest.mark.parametrize("min_bid", [None, 0, 1, 5])
def test_a_range_never_exceeds_the_remaining_budget(remaining, weeks, share, tier, min_bid):
    r = bid_range(remaining, weeks, share, tier, min_bid)
    if min_bid is not None and min_bid > remaining:
        assert r is None
        return
    assert 0 <= r["low"] <= r["high"] <= remaining
    if min_bid is not None:
        assert r["low"] >= min_bid


@pytest.mark.parametrize("min_bid, rng, phrase", [(0, {"low": 0, "high": 0}, "only a $0 bid"),
                                                  (1, None, "no bid is possible"),
                                                  (None, None, "accepts a $0 bid is not in the synced settings")])
def test_a_spent_budget(min_bid, rng, phrase):
    settings = {"acquisitionSettings": {"minimumBid": min_bid}} if min_bid is not None else {}
    c = faab([(301, 107)], spent=100, settings=settings)["claims"][0]
    assert c["status"] == BUDGET_EXHAUSTED and phrase in c["reason"]
    assert (None if c["range"] is None else {k: c["range"][k] for k in ("low", "high")}) == rng


def test_a_zero_dollar_low_end_says_when_zero_bids_are_unknown():
    c = faab([(301, 107)], st=with_proj(301, 9.5))["claims"][0]
    assert c["range"]["low"] == 0 and "ZERO_BID_UNKNOWN" in [n["code"] for n in c["notes"]]
    known = faab([(301, 107)], st=with_proj(301, 9.5), settings={"acquisitionSettings": {"minimumBid": 1}})["claims"][0]
    assert known["range"]["low"] == 1 and not known["notes"]


def test_free_agents_invalid_claims_and_unknown_players_get_no_bid():
    out = faab([(304, 104), (201, 107), (999, None)])
    fa, rostered, unknown = out["claims"]
    assert fa["status"] == NO_BID_NEEDED and fa["range"] is None
    assert rostered["status"] == BLOCKED and "No bid is suggested" in rostered["reason"]
    assert unknown["status"] == NOT_EVALUATED and unknown["range"] is None


def test_no_weeks_left_gets_no_range():
    st = replace(state(), start_week=13)
    out = faab([(301, 107)], st=st)
    assert out["budget"]["weeks_left"] == 0 and out["claims"][0]["range"] is None


# ---- observed league bids ----------------------------------------------------------------------------------------

def test_observed_bids_keep_a_measured_zero_and_count_missing_amounts_apart():
    obs = observed_bids([bid(1, 0), bid(2, 10), bid(3, 4, pos="RB"), bid(4, None)], 100)
    assert obs["claims"] == 4 and obs["with_bid"] == 3 and obs["without_bid"] == 1
    assert obs["overall"] == {"count": 3, "median": 4, "min": 0, "max": 10, "median_share_pct": 4.0}
    assert obs["by_position"]["WR"]["median"] == 5 and obs["by_position"]["RB"]["count"] == 1
    assert all("team_id" not in r and "team" not in r for r in obs["recent"])


def test_observed_bids_are_compared_but_never_change_the_range():
    st = with_proj(301, 13)
    plain = faab([(301, 107)], st=copy.deepcopy(st), observed=[])["claims"][0]
    many = faab([(301, 107)], st=copy.deepcopy(st), observed=[bid(i, 30 + i) for i in range(1, 5)])["claims"][0]
    few = faab([(301, 107)], st=copy.deepcopy(st), observed=[bid(1, 2), bid(2, 3)])["claims"][0]
    assert plain["range"] == many["range"] == few["range"]
    assert many["observed"]["count"] == 4 and many["observed"]["relation"] == "below"     # 13-26 vs a $32.5 median
    assert few["observed"]["count"] == 2 and few["observed"]["relation"] is None          # too few to compare


# ---- combined effects ------------------------------------------------------------------------------------------------

def test_claims_sharing_a_drop_cannot_both_be_spent():
    out = faab([(301, 107), (302, 107)])
    assert [c["status"] for c in out["claims"]] == [RANGE, RANGE]
    highs = [c["range"]["high"] for c in out["claims"]]
    comb = out["combined"]
    assert comb["exclusive_groups"] == [{"kind": "shared_drop", "ranks": [1, 2], "capacity": 1}]
    assert comb["max_spend_if_won"] == max(highs) and comb["high_total"] == sum(highs)


def test_add_only_claims_beyond_the_open_spots_cannot_all_be_spent():
    out = faab([(301, None), (302, None)])
    comb = out["combined"]
    assert comb["exclusive_groups"] == [{"kind": "open_spots", "ranks": [1, 2], "capacity": 1}]
    assert comb["max_spend_if_won"] == max(c["range"]["high"] for c in out["claims"])


def test_several_bids_against_one_budget_are_scaled_to_fit_if_all_win():
    out = faab([(301, 107), (302, 104)], share=100)
    comb = out["combined"]
    assert comb["max_spend_if_won"] == comb["high_total"] > 80 and comb["fits"] is False and comb["scaled"] is True
    assert sum(c["if_all_win"]["high"] for c in out["claims"]) <= 80
    assert all(c["if_all_win"]["low"] <= c["if_all_win"]["high"] <= c["range"]["high"] for c in out["claims"])
    fits = faab([(301, 107)], share=10)["combined"]
    assert fits["fits"] is True and fits["scaled"] is False


def test_combined_counts_only_ranged_claims_and_says_so():
    comb = combined([{"rank": 1, "status": RANGE, "range": {"low": 2, "high": 5}},
                     {"rank": 2, "status": VALUE_UNKNOWN, "range": None}], [], 80)
    assert comb["max_spend_if_won"] == 5 and comb["complete"] is False and comb["unranged_ranks"] == [2]


# ---- other teams -----------------------------------------------------------------------------------------------------

def test_other_teams_budgets_are_aggregated_only_when_all_are_known():
    out = faab([], teams=TEAMS + [{"team_id": 3, "faab_spent": 0}])
    assert out["budget"]["other_teams"] == {"teams": 2, "known": 2, "max_remaining": 100}
    partial = faab([], teams=TEAMS + [{"team_id": 3, "faab_spent": None}])
    assert partial["budget"]["other_teams"] == {"teams": 2, "known": 1, "max_remaining": None}
    assert field(partial, "league_spend")["status"] == "missing"


def test_the_plan_and_the_state_are_not_mutated_and_nothing_is_submitted():
    st = state()
    snap = copy.deepcopy(st)
    out = faab([(301, 107), (302, 107), (999, None)], st=st)
    assert st == snap and out["submitted"] is False
    assert any("Not submitted" in a for a in out["assumptions"])


def test_the_share_must_be_a_whole_percent():
    st = state()
    plan = evaluate_claim_plan(st, 1, [], ctx(st))
    for bad in (0, 101):
        with pytest.raises(ValueError):
            evaluate_faab(plan, provider="espn", provider_label="ESPN", settings={}, team_row={}, teams=[],
                          observed_rows=[], share=bad)


# ---- the API ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def fixture_api(tmp_path, monkeypatch):
    from espn_fixture import MY_SWID, FakeEspnClient
    from fastapi.testclient import TestClient

    from app.db.connection import open_db
    from app.espn.sync import run_espn_sync

    db_path = tmp_path / "faab.duckdb"
    run_espn_sync(lambda: open_db(db_path), SimpleNamespace(swid=MY_SWID, my_team_id=None), client=FakeEspnClient(),
                  save_raw_payloads=False)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db_path))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    from app.main import app
    return TestClient(app, raise_server_exceptions=False), db_path


def test_the_api_audits_the_synced_league(fixture_api):
    client, _ = fixture_api
    res = client.get("/api/waivers/faab?claims=309:206,209,99999")
    assert res.status_code == 200, res.text[:300]
    d = res.json()
    assert d["contract"] == CONTRACT and d["submitted"] is False
    assert d["capability"]["guidance"] == "available" and d["capability"]["observed_bids"] == "available"
    assert d["budget"]["remaining"] == 80 and d["observed"]["with_bid"] == 1
    assert [c["rank"] for c in d["claims"]] == [1, 2, 3]
    assert d["provenance"]["contract"] == "signal-provenance.v1"


def test_the_api_never_reads_failed_or_pending_bids(fixture_api):
    """A manager's losing or pending claims are private to them: only executed waiver claims are observed."""
    client, db_path = fixture_api
    from app.db.connection import open_db
    with open_db(db_path) as con:
        lg = con.execute("SELECT league_id, season FROM leagues LIMIT 1").fetchone()
        for tid, status in (("secret-failed", "FAILED_INVALIDPLAYERSOURCE"), ("secret-pending", "PENDING")):
            con.execute("INSERT INTO transactions (league_id, season, transaction_id, type, status, team_id, "
                        "scoring_period, bid_amount) VALUES (?, ?, ?, 'WAIVER', ?, 3, 2, 77)",
                        [lg[0], lg[1], tid, status])
            con.execute("INSERT INTO transaction_items (transaction_id, item_index, item_type, espn_player_id, "
                        "to_team_id) VALUES (?, 0, 'ADD', 309, 3)", [tid])
    d = client.get("/api/waivers/faab").json()
    assert d["observed"]["with_bid"] == 1 and 77 not in [r["bid"] for r in d["observed"]["recent"]]
    body = json.dumps(d)
    assert "secret-" not in body and '"team_id"' not in json.dumps(d["observed"])


@pytest.mark.parametrize("query", ["?share=0", "?share=101", "?share=x", "?claims=309:206:5", "?claims=abc",
                                   "?claims=309,,209", "?team_id=-1",
                                   "?claims=" + ",".join(str(300 + i) for i in range(13))])
def test_the_api_refuses_malformed_input(fixture_api, query):
    client, _ = fixture_api
    assert client.get("/api/waivers/faab" + query).status_code == 422


def test_the_share_changes_the_answer_and_the_team_must_be_in_the_league(fixture_api):
    client, _ = fixture_api
    a = client.get("/api/waivers/faab?share=10").json()
    b = client.get("/api/waivers/faab?share=50").json()
    assert a["inputs"]["share_pct"] == 10 and b["inputs"]["share_pct"] == 50
    assert client.get("/api/waivers/faab?team_id=77").status_code == 404


def test_the_route_is_get_only():
    from app.api.routing import iter_routes
    from app.main import app
    mine = [r for r in iter_routes(app) if r.path.endswith("/api/waivers/faab")]
    assert mine and all(r.methods == {"GET"} for r in mine)
    assert {r.path for r in mine} == {"/api/waivers/faab", "/v2/leagues/{league_id}/api/waivers/faab"}


def test_the_sleeper_api_states_its_fields(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    from fastapi.testclient import TestClient
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("build_e2e_sleeper", scripts / "build_e2e_sleeper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = tmp_path / "sleeper.duckdb"
    mod.build(db)
    monkeypatch.setenv("FANTASY_DB_PATH", str(db))
    monkeypatch.setenv("ESPN_SEASON", "2026")
    from app.main import app
    d = TestClient(app).get("/api/waivers/faab").json()
    fields = {f["field"]: f for f in d["capability"]["fields"]}
    assert fields["waiver_status"]["status"] == "not_supplied"
    assert fields["acquisition_type"]["source"] == "settings.waiver_type (2 is FAAB)"
    assert "ESPN" not in json.dumps(d["capability"])

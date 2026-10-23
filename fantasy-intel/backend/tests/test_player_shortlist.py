"""Start-decision shortlist (analytics/player_shortlist.py, GET /api/compare/shortlist, issue #175).

Temp DuckDB only, on the Player Comparison Lab's synthetic world (test_player_compare.world): league current week 6,
ESPN projects every player 5 + id points a week (Ace WR 6, Kicker K 7, Zed RB 8, Quinn QB 9, Wally WR 10, Ghost WR 11,
Nova WR none), and the Fantasy Intel runs cover Ace and Zed only."""

import pytest
from fastapi.testclient import TestClient

from app.analytics import player_compare as pc
from app.analytics import player_shortlist as sl
from app.db.connection import connect, init_db

from test_forward_ours import CURRENT, SEASON, ours_value
from test_player_compare import ACE, KICKER, NOVA, QUINN, WALLY, ZED, fingerprint, set_scoring, world  # noqa: F401

SLOTS = '{"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 6, "IR": 1, "LB": 1}'
MINE, OTHER = 1, 2


def league_setup(con, *, slots=SLOTS, sleeper=False):
    con.execute("UPDATE leagues SET roster_slots = ?, settings_raw = ?",
                [slots, '{"provider": "sleeper"}' if sleeper else '{}'])
    for tid, mine in ((MINE, True), (OTHER, False)):
        con.execute("INSERT INTO fantasy_teams (league_id, season, team_id, name, is_mine) VALUES (1, ?, ?, ?, ?)",
                    [SEASON, tid, f"Team {tid}", mine])
    for pid, status, team in ((ACE, "ONTEAM", MINE), (ZED, "ONTEAM", OTHER), (WALLY, "FREEAGENT", None),
                              (QUINN, "WAIVERS", None)):
        con.execute("UPDATE fantasy_players SET availability_status = ?, on_team_id = ? WHERE espn_player_id = ?",
                    [status, team, pid])


@pytest.fixture
def w(con):
    world(con)
    league_setup(con)
    return con


def ids(out):
    return [p["card"]["player"]["espn_player_id"] for p in out["players"]]


# ------------------------------------------------------------------------------------------------ ids


@pytest.mark.parametrize("raw, message", [
    ("1", "2 to 4 players; got 1"), ("", "got 0"), ("1,2,3,4,5", "got 5"), ("1,x", "'x' is not a player id"),
    ("1,1.5", "'1.5' is not a player id"), ("3,3", "on the shortlist twice"),
])
def test_parse_ids_rejects_bad_input(raw, message):
    with pytest.raises(sl.ShortlistError, match=message):
        sl.parse_ids(raw)


def test_parse_ids_keeps_order_and_negative_defense_ids():
    assert sl.parse_ids(" 7, -16001 ,3,") == [7, -16001, 3]


# ------------------------------------------------------------------------------------------ the shortlist


def test_flex_shortlist_reuses_the_lab_cards_and_the_lineup_engines_eligibility(w):
    out = sl.shortlist(w, [ACE, ZED, WALLY, QUINN], slot="FLEX")
    assert ids(out) == [ACE, ZED, WALLY, QUINN] and out["slot"] == "FLEX" and out["as_of_week"] == CURRENT
    # the same card the two-player lab builds, field for field
    assert out["players"][0]["card"] == pc.compare(w, ACE, ZED)["players"][0]
    assert [p["slot_eligible"] for p in out["players"]] == [True, True, True, False]
    assert out["players"][3]["eligible_slots"] == ["QB"]
    assert out["players"][1]["eligible_slots"] == ["RB", "FLEX"]
    assert [s["slot"] for s in out["slots"]] == ["QB", "RB", "WR", "TE", "K", "D/ST", "FLEX"]
    assert next(s for s in out["slots"] if s["slot"] == "RB")["count"] == 2
    assert next(s for s in out["slots"] if s["slot"] == "FLEX")["positions"] == ["RB", "WR", "TE"]
    assert out["unsupported_slots"] == ["LB"] and any("LB" in x for x in out["limitations"])
    best = out["best_projected"]
    assert (best["status"], best["leaders"], best["value"], best["gap"], best["complete"]) == \
        ("leader", [WALLY], 10.0, 2.0, True)
    assert best["source"] == "espn" and best["source_label"] == "ESPN" and best["considered"] == [ACE, ZED, WALLY]
    assert best["excluded"] == [{"espn_player_id": QUINN, "reason": "not_eligible",
                                 "text": "Quinn QB (QB) cannot start at FLEX."}]
    assert "start" not in best["text"].lower().replace("cannot start", "")      # never a start/sit verdict


def test_inapplicable_and_missing_metrics_are_none_never_zero(w):
    out = sl.shortlist(w, [QUINN, KICKER, ZED])
    quinn, kicker, _ = (p["card"] for p in out["players"])
    assert quinn["usage"]["metrics"]["target_share"] == {
        "value": None, "applicable": False, "why": pc.WHY_QB_RECEIVING}
    assert all(m["value"] is None and not m["applicable"]
               for k, m in kicker["usage"]["metrics"].items() if k != "league_pts_pg")


def test_a_missing_projection_makes_best_projected_incomplete_not_a_zero(w):
    best = sl.shortlist(w, [ACE, NOVA], slot="WR")["best_projected"]
    assert best["status"] == "incomplete" and best["complete"] is False and best["leaders"] == [ACE]
    assert best["excluded"][0]["espn_player_id"] == NOVA and best["excluded"][0]["reason"] == "no_projection"
    assert "best of those that do" in best["text"]


def test_projections_within_the_edge_are_a_tie(w):
    w.execute("UPDATE espn_player_weekly_points SET projected_points = 9.7 WHERE espn_player_id = ? "
              "AND scoring_period = ?", [ZED, CURRENT])
    best = sl.shortlist(w, [ZED, WALLY, ACE], slot="FLEX")["best_projected"]
    assert best["status"] == "close" and best["leaders"] == [WALLY, ZED] and best["gap"] == 4.0   # to Ace, the first non-leader


def test_no_slot_means_no_eligibility_filter(w):
    out = sl.shortlist(w, [ACE, QUINN])
    assert [p["slot_eligible"] for p in out["players"]] == [None, None]
    assert out["best_projected"]["leaders"] == [QUINN]


def test_independent_source_ranks_on_the_model_and_names_it(w):
    out = sl.shortlist(w, [ACE, ZED], slot="FLEX", source="independent")
    best = out["best_projected"]
    assert out["source"] == "independent" and best["source"] == "ours" and best["source_label"] == "Fantasy Intel"
    assert best["value"] == ours_value(CURRENT, ZED) and best["status"] == "close"       # 16.3 vs 16.1
    assert "Fantasy Intel" in best["text"] and "ESPN" not in best["text"]


def test_bye_is_left_out_with_its_reason():
    nw = {"on_bye": True, "espn": None, "ours": None}
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "WR", "slot_eligible": True, "next_week": nw},
        {"espn_player_id": 2, "name": "B", "position": "WR", "slot_eligible": True,
         "next_week": {"on_bye": False, "espn": 0.0, "ours": None}},
    ], "espn", 9, "WR")
    assert best["excluded"] == [{"espn_player_id": 1, "reason": "bye", "text": "A is on bye in week 9."}]
    assert best["status"] == "only" and best["leaders"] == [2] and best["value"] == 0.0 and best["complete"]


def test_nobody_with_a_projection_names_no_leader():
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "WR", "slot_eligible": True, "next_week": None},
        {"espn_player_id": 2, "name": "B", "position": "QB", "slot_eligible": False, "next_week": None},
    ], "espn", 6, "WR")
    assert best["status"] == "none" and best["leaders"] == [] and best["value"] is None and not best["complete"]
    # a source with no value for anyone is never named (D-14): "Unavailable", and the text names none
    assert best["source_label"] == "Unavailable" and "ESPN" not in best["text"]


def test_a_source_with_values_only_for_an_ineligible_player_is_still_named():
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "QB", "slot_eligible": False,
         "next_week": {"on_bye": False, "espn": 18.0, "ours": None}},
        {"espn_player_id": 2, "name": "B", "position": "WR", "slot_eligible": True,
         "next_week": {"on_bye": False, "espn": None, "ours": None}},
    ], "espn", 6, "WR")
    assert best["source_label"] == "ESPN" and best["status"] == "none"
    assert sl.best_projected([], "ours", 6, None)["source_label"] == "Unavailable"


def test_roster_status_is_relative_to_the_team(w):
    team = {"team_id": OTHER, "name": "Team 2", "is_owner_default": False}
    out = sl.shortlist(w, [ACE, ZED, WALLY, QUINN], team=team)
    assert [p["roster"] for p in out["players"]] == ["other_team", "this_team", "free_agent", "waivers"]
    assert out["team"] == team
    assert sl.roster_status("ONTEAM", None, 1) == "unknown" and sl.roster_status(None, None, 1) == "unknown"


def test_sleeper_free_agents_carry_the_waiver_caveat(con):
    world(con)
    league_setup(con, sleeper=True)
    out = sl.shortlist(con, [ACE, WALLY])
    assert out["provider"] == "sleeper" and sl.SLEEPER_WAIVERS in out["limitations"]


def test_scored_for_the_active_league(w):
    set_scoring(w, 0.5)
    out = sl.shortlist(w, [ACE, ZED])
    assert out["scoring"]["label"] == "Half PPR (0.5 pt per reception)" and out["scoring"]["settings_synced"]


def test_unknown_slot_and_unsynced_slots_are_refused(w):
    with pytest.raises(sl.ShortlistError, match="OP is not a starting slot in this league; choose one of QB"):
        sl.shortlist(w, [ACE, ZED], slot="OP")
    w.execute("UPDATE leagues SET roster_slots = NULL")
    with pytest.raises(sl.ShortlistError, match="not synced"):
        sl.shortlist(w, [ACE, ZED], slot="WR")
    out = sl.shortlist(w, [ACE, ZED])
    assert out["slots"] == [] and any("lineup slots are not synced" in x for x in out["limitations"])


# ------------------------------------------------------------------------------------------------ the API


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "shortlist.duckdb"
    c = connect(path)
    init_db(c)
    world(c)
    league_setup(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_api_answers_and_validates(api):
    ok = api.get("/api/compare/shortlist", params={"ids": f"{ACE},{ZED},{WALLY}", "slot": "FLEX"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["best_projected"]["leaders"] == [WALLY] and body["team"]["team_id"] == MINE
    assert [p["roster"] for p in body["players"]] == ["this_team", "other_team", "free_agent"]
    team = api.get("/api/compare/shortlist", params={"ids": f"{ACE},{ZED}", "team_id": OTHER}).json()
    assert [p["roster"] for p in team["players"]] == ["other_team", "this_team"]
    for params in ({"ids": str(ACE)}, {"ids": f"{ACE},x"}, {"ids": f"{ACE},{ACE}"}, {"ids": "1,2,3,4,5"},
                   {"ids": f"{ACE},{ZED}", "slot": "OP"}, {"ids": f"{ACE},{ZED}", "source": "vegas"}, {}):
        assert api.get("/api/compare/shortlist", params=params).status_code == 422, params
    unknown = api.get("/api/compare/shortlist", params={"ids": f"{ACE},{ZED},999"})
    assert unknown.status_code == 404 and unknown.json()["detail"] == "Unknown ESPN player id 999 (player 3)"
    assert api.get("/api/compare/shortlist", params={"ids": f"{ACE},{ZED}", "team_id": 77}).status_code == 404
    # the two-player lab is unchanged beside it
    assert api.get("/api/compare", params={"a": ACE, "b": ZED}).json()["players"][0] == body["players"][0]["card"]


def test_api_without_a_league_is_503(tmp_path, monkeypatch):
    path = tmp_path / "empty.duckdb"
    c = connect(path)
    init_db(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    assert TestClient(app).get("/api/compare/shortlist", params={"ids": "1,2"}).status_code == 503

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
    # review L1: an id past BIGINT, int()'s underscores and other scripts' digits are 422s, never a 500 or a new
    # cache entry for the same player
    ("1," + "9" * 40, "is not a player id"), ("1,12345678901", "is not a player id"), ("1,2_01", "'2_01' is not"),
    ("1,\u0661\u0662", "is not a player id"), ("1,+5", "is not a player id"),
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
    assert (best["state"], best["leaders"], best["value"], best["gap"]) == ("single_leader", [WALLY], 10.0, 2.0)
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
    assert best["state"] == "incomplete_single" and best["leaders"] == [ACE]
    assert best["excluded"][0]["espn_player_id"] == NOVA and best["excluded"][0]["reason"] == "no_projection"
    assert "best of those that do" in best["text"]


def test_projections_within_the_edge_are_a_tie(w):
    w.execute("UPDATE espn_player_weekly_points SET projected_points = 9.7 WHERE espn_player_id = ? "
              "AND scoring_period = ?", [ZED, CURRENT])
    best = sl.shortlist(w, [ZED, WALLY, ACE], slot="FLEX")["best_projected"]
    assert best["state"] == "tied" and best["leaders"] == [WALLY, ZED] and best["gap"] == 4.0   # to Ace, the first non-leader


def test_no_slot_ranks_players_who_share_a_slot(w):
    out = sl.shortlist(w, [ACE, ZED])                    # WR and RB: both FLEX
    assert [p["slot_eligible"] for p in out["players"]] == [None, None]
    assert out["best_projected"]["leaders"] == [ZED] and out["best_projected"]["state"] == "single_leader"


def test_no_slot_and_no_shared_slot_names_no_one(w):
    """Review L3: a QB and a WR (no OP in this league) or a QB and a K never compete for one slot, so with "Any"
    nobody is best projected and the page asks for a slot."""
    for ids in ([ACE, QUINN], [QUINN, KICKER]):
        best = sl.shortlist(w, ids)["best_projected"]
        assert (best["state"], best["leaders"], best["value"], best["considered"]) == ("no_common_slot", [], None, [])
        assert best["text"] == sl.NO_COMMON_SLOT and best["excluded"] == []
    # with the slot chosen the eligible player is still ranked
    assert sl.shortlist(w, [ACE, QUINN], slot="QB")["best_projected"]["leaders"] == [QUINN]
    # a league that starts OP: a QB and a WR do share a slot
    w.execute("UPDATE leagues SET roster_slots = '{\"QB\": 1, \"WR\": 2, \"OP\": 1}'")
    assert sl.shortlist(w, [ACE, QUINN])["best_projected"]["leaders"] == [QUINN]


def test_share_a_slot_without_synced_slots_is_known_only_for_one_position():
    """Review round 3: unsynced slots and mixed positions are unknown (None), never "they never share a slot"."""
    assert sl.share_a_slot(["WR", "WR"], []) is True
    assert sl.share_a_slot(["WR", "RB"], []) is None and sl.share_a_slot([None, None], []) is None
    assert sl.share_a_slot(["WR", "RB", "TE"], ["QB", "FLEX"]) and not sl.share_a_slot(["QB", "K"], ["QB", "K", "OP"])


def test_independent_source_ranks_on_the_model_and_names_it(w):
    out = sl.shortlist(w, [ACE, ZED], slot="FLEX", source="independent")
    best = out["best_projected"]
    assert out["source"] == "independent" and best["source"] == "ours" and best["source_label"] == "Fantasy Intel"
    assert best["value"] == ours_value(CURRENT, ZED) and best["state"] == "tied"       # 16.3 vs 16.1
    assert "Fantasy Intel" in best["text"] and "ESPN" not in best["text"]


def test_bye_is_left_out_with_its_reason():
    nw = {"on_bye": True, "espn": None, "ours": None}
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "WR", "slot_eligible": True, "next_week": nw},
        {"espn_player_id": 2, "name": "B", "position": "WR", "slot_eligible": True,
         "next_week": {"on_bye": False, "espn": 0.0, "ours": None}},
    ], "espn", 9, "WR")
    assert best["excluded"] == [{"espn_player_id": 1, "reason": "bye", "text": "A is on bye in week 9."}]
    assert best["state"] == "only" and best["leaders"] == [2] and best["value"] == 0.0
    assert "with an ESPN projection" in best["text"]                                     # review L4


def test_nobody_with_a_projection_names_no_leader():
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "WR", "slot_eligible": True, "next_week": None},
        {"espn_player_id": 2, "name": "B", "position": "QB", "slot_eligible": False, "next_week": None},
    ], "espn", 6, "WR")
    assert best["state"] == "none_projected" and best["leaders"] == [] and best["value"] is None
    assert best["text"].startswith("No player who can fill WR has a projection")
    # a source with no value for anyone is never named (D-14): "Unavailable", and the text names none
    assert best["source_label"] == "Unavailable" and "ESPN" not in best["text"]


def test_a_source_with_values_only_for_an_ineligible_player_is_still_named():
    best = sl.best_projected([
        {"espn_player_id": 1, "name": "A", "position": "QB", "slot_eligible": False,
         "next_week": {"on_bye": False, "espn": 18.0, "ours": None}},
        {"espn_player_id": 2, "name": "B", "position": "WR", "slot_eligible": True,
         "next_week": {"on_bye": False, "espn": None, "ours": None}},
    ], "espn", 6, "WR")
    assert best["source_label"] == "ESPN" and best["state"] == "none_projected"
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
                   {"ids": f"{ACE},{ZED}", "slot": "OP"}, {"ids": f"{ACE},{ZED}", "source": "vegas"}, {},
                   {"ids": f"{ACE}," + "9" * 40}, {"ids": f"{ACE},2_01"}):
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


def test_a_slot_nobody_can_fill_says_so():
    """Review L4: "No player here can fill D/ST", not "no player who can fill D/ST has a projection"."""
    entries = [{"espn_player_id": i, "name": n, "position": "WR", "slot_eligible": False,
                "next_week": {"on_bye": False, "espn": 9.0, "ours": None}} for i, n in ((1, "A"), (2, "B"))]
    best = sl.best_projected(entries, "espn", 6, "D/ST")
    assert best["text"] == "No player here can fill D/ST, so none is best projected." and best["state"] == "none_eligible"
    assert sl._a("Fantasy Intel") == "a Fantasy Intel" and sl._a("ESPN") == "an ESPN"


def _e(pid, pos="WR", *, elig=True, proj=10.0, bye=False, window=True):
    nw = {"on_bye": bye, "espn": None if bye else proj, "ours": None} if window else None
    return {"espn_player_id": pid, "name": f"P{pid}", "position": pos, "slot_eligible": elig, "next_week": nw}


# Review round 3: the verdict is one explicit state. Every state, with what each one names.
STATE_MATRIX = [
    # (case, entries, slot, shared, state, leaders)
    ("one clear leader", [_e(1, proj=12), _e(2, proj=9)], "WR", True, "single_leader", [1]),
    ("tied at the top", [_e(1, proj=12), _e(2, proj=11.8), _e(3, proj=5)], "WR", True, "tied", [1, 2]),
    ("only one can play", [_e(1, proj=12), _e(2, bye=True), _e(3, pos="QB", elig=False)], "WR", True, "only", [1]),
    ("only one projected, one missing", [_e(1, proj=12), _e(2, proj=None)], "WR", True, "incomplete_single", [1]),
    ("leader, one missing", [_e(1, proj=12), _e(2, proj=9), _e(3, proj=None)], "WR", True, "incomplete_single", [1]),
    ("tied, one missing", [_e(1, proj=12), _e(2, proj=11.8), _e(3, proj=None)], "WR", True, "incomplete_tied", [1, 2]),
    ("no window counts as missing", [_e(1, proj=12), _e(2, window=False)], "WR", True, "incomplete_single", [1]),
    ("no shared slot", [_e(1, "QB"), _e(2, "K")], None, False, "no_common_slot", []),
    ("slots not synced", [_e(1, "QB"), _e(2, "WR")], None, None, "slots_unknown", []),
    ("slot nobody fills", [_e(1, elig=False), _e(2, elig=False)], "D/ST", True, "none_eligible", []),
    ("nobody projected", [_e(1, proj=None), _e(2, bye=True)], "WR", True, "none_projected", []),
    # Codex round 4: an unsupported starting slot (IDP LB) a listed player could fill makes a shared slot unknown
    ("two LBs in a normal league", [_e(1, "LB"), _e(2, "LB")], None, False, "unsupported_slot", []),
    ("LB the league's only slot", [_e(1, "LB"), _e(2, "LB")], None, True, "unsupported_slot", []),
    ("eligible ones all on bye", [_e(1, bye=True), _e(2, pos="QB", elig=False)], "WR", True, "none_projected", []),
]


@pytest.mark.parametrize("case, entries, slot, shared, state, leaders", STATE_MATRIX, ids=[c[0] for c in STATE_MATRIX])
def test_every_verdict_state(case, entries, slot, shared, state, leaders):
    unsupported = ["LB"] if state == "unsupported_slot" else []
    best = sl.best_projected(entries, "espn", 6, slot, shared=shared, unsupported=unsupported)
    assert best["state"] == state and best["leaders"] == leaders
    assert (state in sl.RANKED_STATES) == bool(leaders) and state in sl.RANKED_STATES + sl.UNRANKED_STATES
    assert (best["value"] is None) == (not leaders)
    if state.startswith("incomplete"):
        assert "best of those that do" in best["text"]
    if state == "unsupported_slot":
        assert "starts LB, which the lineup engine does not support yet" in best["text"]
    if state in ("no_common_slot", "slots_unknown", "unsupported_slot"):
        assert best["considered"] == [] and best["excluded"] == []
    if state == "slots_unknown":
        assert "not synced" in best["text"] and "pick a slot" not in best["text"].lower()


def test_states_cover_the_matrix():
    assert {c[4] for c in STATE_MATRIX} == set(sl.RANKED_STATES + sl.UNRANKED_STATES)


def test_unsynced_slots_and_mixed_positions_are_slots_unknown(w):
    w.execute("UPDATE leagues SET roster_slots = NULL")
    best = sl.shortlist(w, [ACE, QUINN])["best_projected"]
    assert best["state"] == "slots_unknown" and best["text"] == sl.SLOTS_UNKNOWN
    assert sl.shortlist(w, [ACE, WALLY])["best_projected"]["state"] == "single_leader"      # one position: compared


def test_unsupported_slots_make_a_shared_slot_unknown():
    """Codex round 4: two LB players in a league that starts LB beside the usual slots, or with LB as its only slot,
    are neither "never share a slot" nor ranked; engine positions in such a league are decided as before."""
    labels = ["QB", "RB", "WR", "TE", "FLEX"]
    assert sl.share_a_slot(["LB", "LB"], labels) is False                      # the supported slots alone would say so
    assert sl.unsupported_for(["LB", "LB"], labels, ["LB"]) == ["LB"]          # ... but LB takes both: unknown
    assert sl.share_a_slot(["LB", "LB"], []) is True                           # LB the only slot: not ranked either
    assert sl.unsupported_for(["LB", "LB"], [], ["LB"]) == ["LB"]
    assert sl.unsupported_for(["WR", "RB"], labels, ["LB"]) == []              # engine players: FLEX decides
    assert sl.unsupported_for(["QB", "K"], ["QB", "K"], ["P"]) == []           # still no_common_slot
    assert sl.unsupported_for(["P", "QB"], ["QB"], ["P"]) == ["P"]
    assert sl.unsupported_for(["DL", "QB"], ["QB"], ["DP"]) == ["DP"]          # DL fits no supported slot: unknown


def test_shortlist_of_lb_players_is_unsupported_slot(w):
    w.execute("UPDATE fantasy_players SET position = 'LB' WHERE espn_player_id IN (?, ?)", [ACE, WALLY])
    best = sl.shortlist(w, [ACE, WALLY])["best_projected"]
    assert best["state"] == "unsupported_slot" and best["leaders"] == [] and "LB" in best["text"]
    w.execute("UPDATE leagues SET roster_slots = '{\"LB\": 2, \"BE\": 4}'")
    out = sl.shortlist(w, [ACE, WALLY])
    assert out["best_projected"]["state"] == "unsupported_slot"
    assert not any("lineup slots are not synced" in x for x in out["limitations"])

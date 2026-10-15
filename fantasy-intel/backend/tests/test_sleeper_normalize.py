"""Sleeper JSON -> models -> internal row shapes. Recorded-shape fixtures, no network."""

from datetime import datetime

import pytest

from app.analytics.lineup import lineup_slots
from app.sleeper.models import (NflState, parse_league, parse_player, parse_players, parse_rosters, parse_state,
                                parse_users, sid)
from app.sleeper.normalize import build_rows, current_week, league_row, schedule_weeks, starting_slots
from sleeper_fixtures import LEAGUE_ID, raw

NOW = datetime(2026, 10, 14, 12, 0)


@pytest.fixture
def data():
    from app.sleeper.models import SleeperLeagueData

    rosters = parse_rosters(raw("rosters.json"))
    return SleeperLeagueData(league=parse_league(raw("league.json")), users=parse_users(raw("users.json")),
                             rosters=rosters, state=parse_state(raw("state.json")),
                             players=parse_players(raw("players.json")))


@pytest.fixture
def rows(data):
    return build_rows(data, NOW, my_user_id="700000000000000103")


# --------------------------------------------------------------------- parsing --

def test_sid_only_ever_produces_strings():
    assert sid(1404207029957996999) == "1404207029957996999"        # exact, beyond float precision
    assert sid(" 00-0036900") == "00-0036900"
    assert sid("0") is None and sid("") is None and sid(None) is None and sid(True) is None


def test_league_ids_stay_strings(data):
    lg = data.league
    assert (lg.league_id, lg.previous_league_id, lg.draft_id) == (LEAGUE_ID, "1050000000000000002", "1180000000000000003")
    assert lg.bracket_id == "1404207029957996999"
    assert lg.season == 2026 and lg.total_rosters == 4


def test_rosters_parse_ids_points_and_slots(data):
    r1, r2, r3, r4 = data.rosters                                     # sorted by roster_id
    assert [r.roster_id for r in data.rosters] == [1, 2, 3, 4]
    assert r1.owner_id == "700000000000000101" and r1.co_owners == ()
    assert r2.co_owners == ("700000000000000103",)
    assert r3.owner_id is None and r3.players == ()
    assert r4.players == () and r4.starters == ()                    # null lists
    assert r1.reserve == ("5850",) and r1.taxi == ("12500",)
    assert r1.starters[6:9] == (None, None, None)                     # "0" = empty slot
    assert (r1.points_for, r1.points_against) == (725.64, 616.78)
    assert (r2.points_for, r2.points_against) == (501.05, 560.4)
    assert r3.points_for == 300.0 and r4.points_for is None           # missing stays None
    assert r2.division == 1 and r1.division is None


def test_roster_id_must_be_an_int():
    with pytest.raises(ValueError, match="roster_id"):
        parse_rosters([{"roster_id": "1"}])


def test_users_parse_team_names(data):
    by_id = {u.user_id: u for u in data.users}
    assert by_id["700000000000000101"].team_name == "Alpha Squad"
    assert by_id["700000000000000102"].team_name is None
    assert by_id["700000000000000103"].team_name is None            # whitespace-only name ignored
    assert by_id["700000000000000101"].is_owner is True


def test_players_parse_defenses_and_names(data):
    det, lar = data.players["DET"], data.players["LAR"]
    assert det.is_team_defense and det.team == "DET" and det.full_name == "Detroit Lions"
    assert lar.is_team_defense and lar.team == "LAR"
    assert not data.players["4046"].is_team_defense
    assert data.players["11632"].player_id == "11632"                 # id from the map key
    assert data.players["11632"].full_name == "Rookie Runner"         # first + last
    assert data.players["7564"].gsis_id == "00-0036900"               # Sleeper pads some gsis ids
    assert data.players["4046"].espn_id == "3139477"                  # numeric in JSON, str here


def test_defense_detected_from_team_code_id_alone():
    p = parse_player("KC", {"first_name": "Kansas City"})
    assert p.is_team_defense and p.team == "KC"


def test_state_parse():
    s = parse_state(raw("state.json"))
    assert (s.season, s.week, s.season_type) == (2026, 6, "regular")


# ------------------------------------------------------------------ league row --

def test_league_row_matches_internal_shape(rows):
    lg = rows["league"]
    assert lg["provider"] == "sleeper" and lg["league_id"] == LEAGUE_ID
    assert (lg["season"], lg["name"], lg["size"]) == (2026, "Fixture Dynasty League", 4)
    assert lg["league_type"] == "dynasty"
    assert lg["reception_points"] == 0.5
    assert lg["is_faab"] is True and lg["faab_budget"] == 100
    assert (lg["current_week"], lg["current_matchup_period"]) == (6, 6)
    assert (lg["reg_season_matchups"], lg["last_regular_season_week"], lg["final_scoring_period"]) == (14, 14, 16)
    assert lg["playoff_teams"] == 4
    assert lg["trade_deadline"] is None and lg["trade_deadline_week"] == 11
    assert lg["roster_slots"] == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "OP": 1, "K": 1, "D/ST": 1,
                                  "BE": 3, "IR": 1, "TAXI": 1}


def test_roster_slots_feed_the_existing_lineup_engine(rows):
    assert lineup_slots(rows["league"]["roster_slots"]) == ["QB", "RB", "RB", "WR", "WR", "TE", "K", "D/ST", "FLEX", "OP"]


@pytest.mark.parametrize("settings,expected", [
    ({"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 0}, (14, 14, 17)),
    ({"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 1}, (14, 14, 18)),
    ({"playoff_week_start": 14, "playoff_teams": 4, "playoff_round_type": 2}, (13, 13, 17)),
    ({"playoff_week_start": 14, "playoff_teams": 8, "start_week": 2}, (12, 13, 16)),
    ({"playoff_week_start": 0, "playoff_teams": 6}, (None, None, None)),
    ({"playoff_week_start": 15, "playoff_teams": 6, "playoff_round_type": 9}, (14, 14, None)),
])
def test_schedule_weeks(settings, expected):
    lg = parse_league({"league_id": "1", "settings": settings})
    s = schedule_weeks(lg)
    assert (s["reg_season_matchups"], s["last_regular_season_week"], s["final_scoring_period"]) == expected


def test_current_week_only_when_state_matches_the_league_season(data):
    lg = data.league
    assert current_week(lg, NflState(2026, "regular", 6, 6, 6), 16) == 6
    assert current_week(lg, NflState(2026, "regular", 18, 18, 18), 16) == 16      # clamped like ESPN
    assert current_week(lg, NflState(2027, "regular", 3, 3, 3), 16) is None      # another season
    assert current_week(lg, NflState(2026, "pre", 1, 1, 1), 16) is None


def test_waiver_type_unknown_leaves_is_faab_none():
    lg = parse_league({"league_id": "1", "season": "2026", "settings": {}})
    row = league_row(lg, NflState(2026, "regular", 1, 1, 1), NOW)
    assert row["is_faab"] is None and row["final_scoring_period"] is None


# ----------------------------------------------------------------------- teams --

def test_team_rows(rows):
    t = {r["team_id"]: r for r in rows["teams"]}
    assert t[1]["name"] == "Alpha Squad" and t[1]["owner_names"] == "alpha_mgr"
    assert t[2]["name"] == "bravo_mgr"                                   # no team name -> display name
    assert t[2]["owner_ids"] == ["700000000000000102", "700000000000000103"]
    assert t[2]["owner_names"] == "bravo_mgr, charlie_co"
    assert t[3]["name"] == "Team 3" and t[3]["owner_ids"] == [] and t[3]["owner_names"] is None
    assert t[4]["name"] == "Team 4" and t[4]["owner_ids"] == ["700000000000009999"]
    assert (t[1]["wins"], t[1]["losses"], t[1]["points_for"]) == (4, 1, 725.64)
    assert t[2]["waiver_rank"] == 1 and t[2]["faab_spent"] == 37 and t[2]["total_moves"] == 9
    assert [r["is_mine"] for r in rows["teams"]] == [False, True, False, False]   # co-owner counts
    assert all(isinstance(o, str) for r in rows["teams"] for o in r["owner_ids"])
    assert all(r["league_id"] == LEAGUE_ID for r in rows["teams"])


def test_is_mine_unknown_without_a_user(data):
    assert {r["is_mine"] for r in build_rows(data, NOW)["teams"]} == {None}


def test_members(rows):
    assert [m["member_id"] for m in rows["members"]] == ["700000000000000101", "700000000000000102",
                                                         "700000000000000103"]


# --------------------------------------------------------------------- lineups --

def test_starting_slots_use_internal_labels(data):
    assert starting_slots(data.league) == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "OP", "K", "D/ST"]


def test_lineup_rows(rows):
    by_team: dict[int, list] = {}
    for r in rows["lineups"]:
        by_team.setdefault(r["team_id"], []).append((r["player_id"], r["lineup_slot"], r["is_starter"]))
    assert by_team[1] == [
        ("4046", "QB", True), ("6794", "RB", True), ("99999", "RB", True), ("7564", "WR", True),
        ("4984", "WR", True), ("8130", "TE", True), ("DET", "D/ST", True),
        ("5850", "IR", False), ("12500", "TAXI", False)]
    assert by_team[2] == [
        ("11632", "RB", True), ("6801", "RB", True), ("9509", "WR", True), ("2133", "WR", True),
        ("10229", "K", True), ("LAR", "D/ST", True)]
    assert 3 not in by_team and 4 not in by_team
    row = next(r for r in rows["lineups"] if r["player_id"] == "6794")
    assert row["injury_status"] == "Questionable" and row["scoring_period"] == 6
    assert next(r for r in rows["lineups"] if r["player_id"] == "99999")["injury_status"] is None


def test_bench_players_are_listed():
    lg = parse_league({"league_id": "1", "roster_positions": ["QB", "BN", "BN"]})
    rs = parse_rosters([{"roster_id": 1, "players": ["10", "11", "12"], "starters": ["10"]}])
    from app.sleeper.normalize import lineup_rows

    assert [(r["player_id"], r["lineup_slot"]) for r in lineup_rows(lg, rs, {}, 3, NOW)] == [
        ("10", "QB"), ("11", "BE"), ("12", "BE")]


# --------------------------------------------------------------------- players --

def test_player_rows(rows):
    p = {r["player_id"]: r for r in rows["players"]}
    assert p["DET"]["position"] == "D/ST" and p["DET"]["pro_team"] == "DET" and p["DET"]["is_team_defense"]
    assert p["LAR"]["pro_team"] == "LA"                                  # nflverse abbreviation
    assert p["8130"]["pro_team"] == "LA" and p["6801"]["pro_team"] == "LV"
    assert p["10229"]["pro_team"] is None                               # free agent
    assert p["6801"]["eligible_positions"] == ["RB", "WR"]
    assert all(isinstance(k, str) for k in p)

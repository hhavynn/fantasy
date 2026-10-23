"""Opportunity tree (analytics/opportunity_tree.py, GET /api/injury-impact/opportunity-tree), issue #190.

Temp DuckDB only, one small synthetic league (docs/opportunity_tree.md is the contract). Every number and name is an
invented test input. Season 2026, current week 7, weeks 1-6 played by KC-BUF and DEN-MIA every week, and again in
week 7 (kickoff 2026-10-25 17:00 UTC, NOW is two days before). Every player is ACT on the weekly roster of his team
in weeks 1-6, so a game without usage is a game he missed while on the team.

    KC   injured starter WITH history. Ace RB played 1-4, missed 5-6, NFL report Out in week 7 (ongoing).
         Bench RB (opponent's roster) 6 touches/g with Ace, 17 without: an observed beneficiary (+11 over 2 games).
         Third RB (free agent) 2 -> 4 carries: +2, under the threshold, not named.  Depth: Ace 1, Bench 2, Third 3.
         Bench RB has external route participation in weeks 5-6 (provider "synthetic-routes"); nobody else does.
    BUF  injured starter WITHOUT history. Buf RB1 played 1-6, NFL report Out in week 7 (a new absence).
         Buf RB2 (waivers, depth 2) 5 touches/g, Buf RB3 (free agent, depth 3) 1 carry/g. Only hypotheses possible.
    DEN  returning starter WITH evidence. Den WR1 played 1-3, missed 4-6, listed on the week-7 NFL report with no
         designation (full practice). Den WR2 (MY team) 4 targets/g with him, 9 without: gained while he was out.
         Den TE 3 targets every game. Depth: WR1 1, WR2 2, TE 1.
    MIA  NO depth chart. Mia RB1 played 1-4, missed 5-6, nothing this week says he is back (no report row, no ESPN
         status): returning without evidence. Mia RB2 3 touches/g with him, 12 without. Mia WR1 ESPN OUT, played
         1-6 (new absence, no history, no depth chart): abstains.
"""

import json
import shutil
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics import opportunity_tree as ot
from app.db.connection import connect, init_db

SEASON, CURRENT = 2026, 7
NOW = datetime(2026, 10, 23, 12, 0)
MY_TEAM, OTHER_TEAM = 3, 5
WEEKS = range(1, 7)

# pid: (name, position, NFL team, availability_status, on_team_id, ESPN injury status)
PLAYERS = {
    101: ("Ace RB", "RB", "KC", "ONTEAM", MY_TEAM, None),
    102: ("Bench RB", "RB", "KC", "ONTEAM", OTHER_TEAM, None),
    103: ("Third RB", "RB", "KC", "FREEAGENT", None, None),
    201: ("Buf RB1", "RB", "BUF", "ONTEAM", OTHER_TEAM, None),
    202: ("Buf RB2", "RB", "BUF", "WAIVERS", None, None),
    203: ("Buf RB3", "RB", "BUF", "FREEAGENT", None, None),
    301: ("Den WR1", "WR", "DEN", "ONTEAM", OTHER_TEAM, None),
    302: ("Den WR2", "WR", "DEN", "ONTEAM", MY_TEAM, None),
    303: ("Den TE", "TE", "DEN", "FREEAGENT", None, None),
    401: ("Mia RB1", "RB", "MIA", "ONTEAM", OTHER_TEAM, None),
    402: ("Mia RB2", "RB", "MIA", "FREEAGENT", None, None),
    403: ("Mia WR1", "WR", "MIA", "ONTEAM", OTHER_TEAM, "OUT"),
}
G = {pid: f"00-T{pid}" for pid in PLAYERS}

# pid: {week: (offense_pct, carries, targets)}; a missing week = no usage row (did not play).
USAGE = {
    101: {w: (0.7, 14, 4) for w in (1, 2, 3, 4)},
    102: {**{w: (0.3, 4, 2) for w in (1, 2, 3, 4)}, **{w: (0.7, 14, 3) for w in (5, 6)}},
    103: {**{w: (0.1, 2, 0) for w in (1, 2, 3, 4)}, **{w: (0.15, 4, 0) for w in (5, 6)}},
    201: {w: (0.7, 15, 3) for w in WEEKS},
    202: {w: (0.3, 4, 1) for w in WEEKS},
    203: {w: (0.05, 1, 0) for w in WEEKS},
    301: {w: (0.9, 0, 9) for w in (1, 2, 3)},
    302: {**{w: (0.6, 0, 4) for w in (1, 2, 3)}, **{w: (0.9, 0, 9) for w in (4, 5, 6)}},
    303: {w: (0.5, 0, 3) for w in WEEKS},
    401: {w: (0.7, 13, 3) for w in (1, 2, 3, 4)},
    402: {**{w: (0.2, 3, 0) for w in (1, 2, 3, 4)}, **{w: (0.6, 10, 2) for w in (5, 6)}},
    403: {w: (0.85, 0, 7) for w in WEEKS},
}
ROUTES = {(102, 5): 0.4, (102, 6): 0.5}
DEPTH = (("KC", 101, "RB", 1), ("KC", 102, "RB", 2), ("KC", 103, "RB", 3),
         ("BUF", 201, "RB", 1), ("BUF", 202, "RB", 2), ("BUF", 203, "RB", 3),
         ("DEN", 301, "WR", 1), ("DEN", 302, "WR", 2), ("DEN", 303, "TE", 1))


def build(con, *, provider=None):
    settings = json.dumps({"provider": provider} if provider else {})
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period,
                   reg_season_matchups, scoring_items, settings_raw, synced_at)
                   VALUES (1, ?, 'Tree League', ?, 17, 14, '[]', ?, '2026-10-23 10:00:00')""",
                [SEASON, CURRENT, settings])
    for tid, name, mine in ((MY_TEAM, "My Squad", True), (OTHER_TEAM, "Team Five", False)):
        con.execute("""INSERT INTO fantasy_teams (league_id, season, team_id, abbrev, name, is_mine, synced_at)
                       VALUES (1, ?, ?, ?, ?, ?, '2026-10-23 10:00:00')""", [SEASON, tid, f"T{tid}", name, mine])
    for pid, (name, pos, team, status, on_team, injury) in PLAYERS.items():
        con.execute("""INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status,
                       availability_status, on_team_id, percent_owned, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [pid, name, pos, team, injury, status, on_team, 30.0, datetime(2026, 10, 23, 10)])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (?, ?, 'high')",
                    [pid, G[pid]])
    for pid, weeks in USAGE.items():
        team, pos = PLAYERS[pid][2], PLAYERS[pid][1]
        for w, (pct, car, tgt) in weeks.items():
            con.execute("""INSERT INTO player_week_usage (season, week, gsis_id, team, position, offense_snaps,
                           offense_pct, targets, carries, opportunities, fantasy_points_ppr, route_participation,
                           route_data_provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [SEASON, w, G[pid], team, pos, round(pct * 60), pct, tgt, car, tgt + car, float(tgt + car),
                         ROUTES.get((pid, w)), "synthetic-routes" if (pid, w) in ROUTES else None])
    con.execute("""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR,
                   gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)""")
    for w in [*WEEKS, CURRENT]:
        day = "2026-10-25" if w == CURRENT else f"2026-09-{6 + w:02d}"
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, '13:00', 'KC', 'BUF')", [SEASON, w, day])
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, '13:00', 'DEN', 'MIA')", [SEASON, w, day])
    con.execute("""CREATE TABLE nfl_depth_charts (dt VARCHAR, team VARCHAR, player_name VARCHAR, gsis_id VARCHAR,
                   pos_abb VARCHAR, pos_rank INT, season INT)""")
    for team, pid, pos, rank in DEPTH:
        con.execute("INSERT INTO nfl_depth_charts VALUES ('2026-10-22T12:00:00Z', ?, ?, ?, ?, ?, ?)",
                    [team, PLAYERS[pid][0], G[pid], pos, rank, SEASON])
    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, team VARCHAR, "
                "gsis_id VARCHAR, status VARCHAR)")
    for pid, (_, _, team, *_rest) in PLAYERS.items():
        for w in WEEKS:
            con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', ?, ?, 'ACT')", [SEASON, w, team, G[pid]])
    con.execute("""CREATE TABLE nfl_injuries (season INT, game_type VARCHAR, team VARCHAR, week INT, gsis_id VARCHAR,
                   position VARCHAR, full_name VARCHAR, report_status VARCHAR, report_primary_injury VARCHAR,
                   practice_status VARCHAR)""")
    for pid in (101, 201):
        con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', ?, ?, ?, 'RB', ?, 'Out', 'Ankle', "
                    "'Did Not Participate In Practice')", [SEASON, PLAYERS[pid][2], CURRENT, G[pid], PLAYERS[pid][0]])
    con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', 'DEN', ?, ?, 'WR', 'Den WR1', NULL, 'Hamstring', "
                "'Full Participation in Practice')", [SEASON, CURRENT, G[301]])


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("tree_template") / "league.duckdb"
    c = connect(path)
    init_db(c)
    build(c)
    c.execute("CHECKPOINT")
    c.close()
    return path


@pytest.fixture
def league(template_db, tmp_path):
    path = tmp_path / "league.duckdb"
    shutil.copyfile(template_db, path)
    c = connect(path)
    yield c
    c.close()


def get(con, **kw):
    kw.setdefault("now", NOW)
    return ot.build_opportunity_tree(con, **kw)


def team(body, name):
    return next(t for t in body["teams"] if t["team"] == name)


def scenario(body, pid):
    return next(s for t in body["teams"] for s in t["scenarios"] if s["subject"]["espn_player_id"] == pid)


def effects(s):
    return {e["espn_player_id"]: e for e in s["effects"]}


# ============================================================================= injured starter


def test_injured_starter_with_history_names_only_the_observed_beneficiary(league):
    s = scenario(get(league), 101)
    assert s["kind"] == "teammate_unavailable" and s["certainty"] == "designated_out"
    assert s["sample"] == {"team_games_with": 4, "team_games_without": 2, "weeks_without": [5, 6]}
    assert s["outcome"] == "supported" and s["abstain"] is None
    e = effects(s)
    assert set(e) == {102}                       # Third RB's +2 is under the threshold: not named
    bench = e[102]
    assert bench["basis"] == "observed" and bench["reasons"] == ["observed_gain"]
    assert bench["observed"]["with"]["games"] == 4 and bench["observed"]["without"]["games"] == 2
    assert bench["observed"]["delta_per_game"] == 11.0


def test_injured_starter_without_history_gives_labelled_hypotheses_only(league):
    s = scenario(get(league), 201)
    assert s["kind"] == "teammate_unavailable" and s["sample"]["team_games_without"] == 0
    assert s["outcome"] == "hypothesis"
    e = effects(s)
    assert all(x["basis"] == "hypothesis" for x in e.values())
    assert e[202]["reasons"] == ["next_on_depth_chart", "pro_rata_estimate"]
    assert all(x["observed"]["delta_per_game"] is None for x in e.values())   # no observed claim is made


def test_no_depth_chart_and_no_history_abstains(league):
    body = get(league)
    s = scenario(body, 403)
    assert team(body, "MIA")["depth_chart"] == {"known": False, "as_of": None}
    assert s["outcome"] == "abstain" and s["effects"] == []
    assert s["abstain"]["code"] == "depth_unknown"
    assert team(body, "MIA")["groups"][0]["ordered_by"] == "snap_share"


def test_a_usage_gap_abstains_every_scenario_of_that_team(league):
    league.execute("DELETE FROM player_week_usage WHERE team = 'KC' AND week = 6")
    body = get(league)
    kc = team(body, "KC")
    assert kc["coverage"]["status"] == "gap" and kc["coverage"]["missing_weeks"] == [6]
    assert all(s["outcome"] == "abstain" and s["effects"] == [] and s["abstain"]["code"] == "coverage_gap"
               for s in kc["scenarios"])
    assert team(body, "BUF")["coverage"]["status"] == "complete"


def test_no_schedule_means_unverified_coverage_and_abstention(league):
    league.execute("DROP TABLE nfl_schedules")
    body = get(league)
    assert body["counts"]["abstained"] == body["counts"]["unavailable"] + body["counts"]["returning"]
    assert any(n["code"] == "no_schedule" for n in body["notices"])
    assert all(s["abstain"]["code"] == "coverage_unverified" for t in body["teams"] for s in t["scenarios"])


# ============================================================================= returning starter


def test_returning_starter_with_report_evidence_shows_who_gained_while_he_was_out(league):
    s = scenario(get(league), 301)
    assert s["kind"] == "teammate_returning" and s["certainty"] == "status_cleared"
    assert s["evidence"] == [{"source": "nfl_report", "week": 7, "report_status": None,
                              "practice_status": "Full Participation in Practice"}]
    assert s["subject"]["absence"]["consecutive_team_games_missed"] == 3
    assert s["sample"]["team_games_with"] == 3 and s["sample"]["team_games_without"] == 3
    assert s["outcome"] == "supported"
    wr2 = effects(s)[302]
    assert wr2["reasons"] == ["gained_while_out"] and wr2["observed"]["delta_per_game"] == 5.0
    assert set(effects(s)) == {302}               # the TE's volume did not change


def test_returning_starter_without_evidence_abstains(league):
    s = scenario(get(league), 401)
    assert s["kind"] == "teammate_returning" and s["certainty"] == "unconfirmed" and s["evidence"] == []
    assert s["outcome"] == "abstain" and s["abstain"]["code"] == "no_return_evidence" and s["effects"] == []


def test_questionable_after_missing_the_last_game_is_a_return_if_he_plays(league):
    league.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', 'MIA', ?, ?, 'RB', 'Mia RB1', 'Questionable', "
                   "'Knee', 'Limited Participation in Practice')", [SEASON, CURRENT, G[401]])
    body = get(league)
    kinds = [s["kind"] for t in body["teams"] for s in t["scenarios"] if s["subject"]["espn_player_id"] == 401]
    assert kinds == ["teammate_returning"]          # one scenario: the change from his last game is his return
    s = scenario(body, 401)
    assert s["certainty"] == "if_he_plays" and s["outcome"] == "supported"
    assert effects(s)[402]["observed"]["delta_per_game"] == 9.0


# ============================================================================= depth chart and ownership


def test_depth_chart_peers_shares_and_personnel(league):
    kc = team(get(league), "KC")
    assert kc["depth_chart"]["known"] and [g["position"] for g in kc["groups"]] == ["RB"]
    peers = {p["espn_player_id"]: p for p in kc["groups"][0]["peers"]}
    assert [p["espn_player_id"] for p in kc["groups"][0]["peers"]] == [101, 102, 103]
    assert peers[101]["starter_in_scenario"] and peers[101]["availability"]["level"] == "OUT"
    bench = peers[102]["shares"]
    assert bench["weeks"] == [4, 5, 6] and bench["games_played"] == 3
    assert bench["route_share"] == 0.45 and bench["route_games"] == 2       # only weeks with provider values
    assert peers[103]["shares"]["route_share"] is None                       # missing, never 0
    assert bench["touch_share"] == pytest.approx(40 / 68, abs=1e-3)          # 6+17+17 of 26+21+21 team touches
    assert peers[101]["personnel"] == [{"kind": "missed", "week": 5, "games": 2}]


def test_returned_player_personnel_and_reserve_list(league):
    league.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week IN (4, 5)", [G[103]])  # back in week 6
    league.execute("UPDATE nfl_rosters_weekly SET status = 'RES' WHERE gsis_id = ? AND week = 6", [G[101]])
    peers = {p["espn_player_id"]: p for p in team(get(league), "KC")["groups"][0]["peers"]}
    assert peers[103]["personnel"] == [{"kind": "returned", "week": 6, "games": 2}]
    assert {"kind": "reserve_list", "week": 6, "games": None, "roster_status": "RES"} in peers[101]["personnel"]


def test_ownership_is_league_specific_and_never_claimable_when_rostered(league):
    body = get(league)
    bench = effects(scenario(body, 101))[102]
    assert bench["roster"]["status"] == "ROSTERED" and bench["roster"]["fantasy_team"] == "Team Five"
    assert bench["ownership"] == {"claim": "opponent", "claimable": False, "needs_claim": None}
    hyp = effects(scenario(body, 201))
    assert hyp[202]["ownership"] == {"claim": "waivers", "claimable": True, "needs_claim": True}
    mine = effects(scenario(body, 301))[302]
    assert mine["roster"]["is_mine"] and mine["ownership"]["claim"] == "yours" and not mine["ownership"]["claimable"]
    peers = {p["espn_player_id"]: p for p in team(body, "KC")["groups"][0]["peers"]}
    assert peers[103]["ownership"] == {"claim": "free_agent", "claimable": True, "needs_claim": False}
    assert body["league_context"]["waiver_status_known"] is True


def test_perspective_changes_who_counts_as_yours(league):
    body = get(league, team_id=OTHER_TEAM)
    bench = effects(scenario(body, 101))[102]
    assert bench["ownership"]["claim"] == "yours"
    assert effects(scenario(body, 301))[302]["ownership"] == {"claim": "opponent", "claimable": False,
                                                              "needs_claim": None}
    with pytest.raises(LookupError):
        get(league, team_id=99)


def test_sleeper_free_agents_are_never_no_claim_needed(tmp_path):
    c = connect(tmp_path / "sleeper.duckdb")
    init_db(c)
    build(c, provider="sleeper")
    body = get(c)
    assert body["league_context"]["provider"] == "sleeper" and body["league_context"]["waiver_status_known"] is False
    peers = {p["espn_player_id"]: p for p in team(body, "KC")["groups"][0]["peers"]}
    assert peers[103]["ownership"] == {"claim": "available_claim_unknown", "claimable": True, "needs_claim": None}
    assert any(n["code"] == "waiver_status_unknown" for n in body["notices"])
    c.close()


def test_counts_and_team_order(league):
    body = get(league)
    assert body["counts"] == {"teams": 4, "unavailable": 3, "returning": 2, "supported": 2, "hypothesis": 1,
                              "no_supported_beneficiary": 0, "abstained": 2}
    # Teams touching the selected roster first (KC: Ace is mine; DEN: WR2 is mine), then the rest.
    assert [t["team"] for t in body["teams"]] == ["DEN", "KC", "BUF", "MIA"]


def test_no_research_module_is_imported():
    """Built on the app's injury-impact engine only: the research-only context modules stay out of the app."""
    import ast
    tree = ast.parse(open(ot.__file__).read())
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"__future__", "collections", "datetime", "db.connection", "", "availability", "league_rules"}


# ============================================================================= API


@pytest.fixture
def api(template_db, tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    shutil.copyfile(template_db, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_route_returns_the_contract_shape_and_errors(api):
    res = api.get("/api/injury-impact/opportunity-tree")
    assert res.status_code == 200
    body = res.json()
    for key in ("version", "season", "week", "league_context", "sources", "method", "counts", "notices", "teams"):
        assert key in body, key
    assert body["sources"]["route_providers"] == ["synthetic-routes"]
    assert api.get("/api/injury-impact/opportunity-tree", params={"team_id": OTHER_TEAM}).json()[
        "league_context"]["my_team_id"] == OTHER_TEAM
    assert api.get("/api/injury-impact/opportunity-tree", params={"team_id": 99}).status_code == 404

"""Injury Impact Center (analytics/injury_impact.py, GET /api/injury-impact).

Temp DuckDB only, one small synthetic league (docs/injury_impact.md is the contract). Every number is an invented
test input. Season 2026, current week 7, weeks 1-6 played by KC, BUF, DEN and MIA. DEN and MIA are on a bye in
week 7; KC hosts BUF on 2026-10-25 13:00 ET (17:00 UTC). NOW is 53 hours before that kickoff.

    KC  Ace RB      OUT on the week-7 NFL report (ESPN says nothing). MY team. 14 carries + 4 targets, 70% snaps,
                    played every game: a new absence with no game without him.
        Bench RB    6 touches/g (4 carries + 2 targets), other team.     Third RB  2 carries/g, free agent.
        Hurt RB     ESPN OUT, 1 carry/g, 10% snaps, free agent: too small a role to list.
        Wide WR     ESPN QUESTIONABLE, MY team, 8 targets/g, 90% snaps; missed week 3 (on the roster, 0 snaps).
        Slot WR     4 targets/g, 10 in week 3 (the game without Wide WR). On waivers. ESPN projection 9.5.
        Tight TE    ESPN INJURY_RESERVE, other team, played weeks 1-2 only: a long-term absence.
        Ghost WR    ESPN INJURY_RESERVE, no game this season: not listed.
    BUF Star QB     ESPN OUT, other team, 35 attempts/g in weeks 1-4, missed 5-6.  Back QB  depth chart QB2,
                    started weeks 5-6 (30 attempts).  Buf WR  9 targets/g, 15 PPR with Star QB, 9 without.
    DEN Den RB      ESPN QUESTIONABLE, 12 carries/g; DEN is on a bye in week 7.
"""

import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analytics import injury_freshness as fresh
from app.analytics import injury_impact as ii
from app.db.connection import connect, init_db

SEASON, CURRENT = 2026, 7
NOW = datetime(2026, 10, 23, 12, 0)
MY_TEAM, OTHER_TEAM = 3, 5

# pid: (name, position, team, availability_status, on_team_id, ESPN injury status)
PLAYERS = {
    101: ("Ace RB", "RB", "KC", "ONTEAM", MY_TEAM, None),
    102: ("Bench RB", "RB", "KC", "ONTEAM", OTHER_TEAM, None),
    103: ("Third RB", "RB", "KC", "FREEAGENT", None, None),
    104: ("Hurt RB", "RB", "KC", "FREEAGENT", None, "OUT"),
    201: ("Wide WR", "WR", "KC", "ONTEAM", MY_TEAM, "QUESTIONABLE"),
    202: ("Slot WR", "WR", "KC", "WAIVERS", None, None),
    301: ("Tight TE", "TE", "KC", "ONTEAM", OTHER_TEAM, "INJURY_RESERVE"),
    902: ("Ghost WR", "WR", "KC", "FREEAGENT", None, "INJURY_RESERVE"),
    401: ("Star QB", "QB", "BUF", "ONTEAM", OTHER_TEAM, "OUT"),
    402: ("Back QB", "QB", "BUF", "FREEAGENT", None, None),
    403: ("Buf WR", "WR", "BUF", "FREEAGENT", None, None),
    501: ("Den RB", "RB", "DEN", "FREEAGENT", None, "QUESTIONABLE"),
}
G = {pid: f"00-G{pid}" for pid in PLAYERS}
WEEKS = range(1, 7)

# pid: {week: (offense_pct, carries, targets, xfp, ppr)}; a missing week = no row, (0, ...) = a 0-snap row.
USAGE = {
    101: {w: (0.7, 14, 4, 15.0, 14.0) for w in WEEKS},
    102: {w: (0.3, 4, 2, 5.0, 5.0) for w in WEEKS},
    103: {w: (0.1, 2, 0, 1.0, 1.0) for w in WEEKS},
    104: {w: (0.1, 1, 0, 0.5, 0.5) for w in WEEKS},
    201: {**{w: (0.9, 0, 8, 13.0, 12.0) for w in WEEKS}, 3: (0.0, 0, 0, None, None)},
    202: {**{w: (0.6, 0, 4, 6.0, 6.0) for w in WEEKS}, 3: (0.8, 0, 10, 14.0, 13.0)},
    301: {w: (0.8, 0, 5, 8.0, 8.0) for w in (1, 2)},
    401: {w: (1.0, 3, 0, 20.0, 18.0) for w in (1, 2, 3, 4)},
    402: {w: (1.0, 2, 0, 14.0, 11.0) for w in (5, 6)},
    403: {**{w: (0.85, 0, 9, 14.0, 15.0) for w in (1, 2, 3, 4)}, **{w: (0.85, 0, 9, 12.0, 9.0) for w in (5, 6)}},
    501: {w: (0.6, 12, 1, 11.0, 10.0) for w in WEEKS},
}
ATTEMPTS = {(401, w): 35 for w in (1, 2, 3, 4)} | {(402, w): 30 for w in (5, 6)}
ESPN_PROJ = {102: 8.0, 202: 9.5, 402: 14.5}


def build(con, *, report=True, espn_retrieval=True, players_synced=NOW - timedelta(hours=2), teams=True):
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period,
                   reg_season_matchups, scoring_items, settings_raw, synced_at)
                   VALUES (1, ?, 'Test League', ?, 17, 14, '[]', '{}', '2026-10-23 10:00:00')""", [SEASON, CURRENT])
    if teams:
        for tid, name, mine in ((MY_TEAM, "My Squad", True), (OTHER_TEAM, "Team Five", False)):
            con.execute("""INSERT INTO fantasy_teams (league_id, season, team_id, abbrev, name, is_mine, synced_at)
                           VALUES (1, ?, ?, ?, ?, ?, '2026-10-23 10:00:00')""", [SEASON, tid, f"T{tid}", name, mine])
    for pid, (name, pos, team, status, on_team, injury) in PLAYERS.items():
        con.execute("""INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status,
                       availability_status, on_team_id, percent_owned, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [pid, name, pos, team, injury, status, on_team, 50.0, players_synced])
        con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (?, ?, 'high')",
                    [pid, G[pid]])
    for pid, weeks in USAGE.items():
        team, pos = PLAYERS[pid][2], PLAYERS[pid][1]
        for w, (pct, car, tgt, xfp, ppr) in weeks.items():
            con.execute("""INSERT INTO player_week_usage (season, week, gsis_id, team, position, offense_snaps,
                           offense_pct, targets, carries, target_share, opportunities, xfp, fantasy_points_ppr)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [SEASON, w, G[pid], team, pos, round(pct * 60), pct, tgt, car,
                         None if tgt == 0 else tgt / 30, tgt + car, xfp, ppr])
    con.execute("""CREATE TABLE nfl_weekly_stats (player_id VARCHAR, season INT, week INT, season_type VARCHAR,
                   position VARCHAR, attempts INT)""")
    for (pid, w), att in ATTEMPTS.items():
        con.execute("INSERT INTO nfl_weekly_stats VALUES (?, ?, ?, 'REG', 'QB', ?)", [G[pid], SEASON, w, att])
    for pid, pts in ESPN_PROJ.items():
        con.execute("INSERT INTO espn_player_weekly_points VALUES (?, ?, ?, NULL, ?, '2026-10-23 10:00:00')",
                    [SEASON, pid, CURRENT, pts])
    con.execute("""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR,
                   gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)""")
    for w in WEEKS:
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, '13:00', 'KC', 'BUF')",
                    [SEASON, w, f"2026-09-{6 + w:02d}"])
        con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, '13:00', 'DEN', 'MIA')",
                    [SEASON, w, f"2026-09-{6 + w:02d}"])
    con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', '2026-10-25', '13:00', 'KC', 'BUF')",
                [SEASON, CURRENT])
    con.execute("""CREATE TABLE nfl_depth_charts (dt VARCHAR, team VARCHAR, player_name VARCHAR, gsis_id VARCHAR,
                   pos_abb VARCHAR, pos_rank INT, season INT)""")
    for team, pid, pos, rank in (("KC", 101, "RB", 1), ("KC", 102, "RB", 2), ("KC", 103, "RB", 3),
                                 ("BUF", 401, "QB", 1), ("BUF", 402, "QB", 2)):
        con.execute("INSERT INTO nfl_depth_charts VALUES ('2026-10-22T12:00:00Z', ?, ?, ?, ?, ?, ?)",
                    [team, PLAYERS[pid][0], G[pid], pos, rank, SEASON])
    con.execute("""CREATE TABLE nfl_injuries (season INT, game_type VARCHAR, team VARCHAR, week INT, gsis_id VARCHAR,
                   position VARCHAR, full_name VARCHAR, report_status VARCHAR, report_primary_injury VARCHAR,
                   practice_status VARCHAR)""")
    con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', 'KC', 6, ?, 'RB', 'Ace RB', NULL, NULL, 'Full')",
                [SEASON, G[101]])
    if report:
        con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', 'KC', ?, ?, 'RB', 'Ace RB', 'Out', 'Ankle', "
                    "'Did Not Participate In Practice')", [SEASON, CURRENT, G[101]])
    if espn_retrieval:
        fresh.record_observations(con, fresh.espn_status_observations(
            [{"espn_player_id": 401, "pro_team": "BUF", "injury_status": "OUT"}], SEASON,
            retrieved_at=NOW - timedelta(hours=2)))


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("injury_template") / "league.duckdb"
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


def fresh_db(tmp_path, **kw):
    c = connect(tmp_path / "custom.duckdb")
    init_db(c)
    build(c, **kw)
    return c


def get(con, **kw):
    kw.setdefault("now", NOW)
    return ii.build_injury_impact(con, **kw)


def by_id(body):
    return {e["espn_player_id"]: e for e in body["players"]}


def mates(entry):
    return {m["espn_player_id"]: m for m in entry["impact"]["teammates"]}


# ============================================================================= who is listed


def test_lists_meaningful_injured_players_in_decision_order(league):
    body = get(league)
    assert body["week"] == CURRENT and body["season"] == SEASON
    # Not-yet-played, not long-term first (most severe, then biggest role), long-term after, bye week last.
    assert [e["espn_player_id"] for e in body["players"]] == [401, 101, 201, 301, 501]
    assert body["counts"] == {"total": 5, "matched": 5, "out": 3, "questionable": 2, "long_term": 1,
                              "affects_my_roster": 3, "skipped": {"no_games": 1, "small_role": 1, "no_team": 0}}


def test_status_takes_the_more_severe_of_espn_and_the_nfl_report(league):
    ace = by_id(get(league))[101]
    assert ace["status"]["level"] == "OUT" and ace["status"]["espn_injury_status"] is None
    assert ace["status"]["nfl_report_status"] == "Out" and ace["status"]["injury"] == "Ankle"
    assert ace["status"]["practice_status"] == "Did Not Participate In Practice"
    assert by_id(get(league))[201]["status"]["level"] == "QUESTIONABLE"
    assert by_id(get(league))[301]["status"]["level"] == "IR"


def test_absence_kind_counts_consecutive_team_games_missed(league):
    e = by_id(get(league))
    assert e[101]["absence"] == {"kind": "new", "last_played_week": 6, "consecutive_team_games_missed": 0}
    assert e[201]["absence"]["kind"] == "new"           # missed week 3, but played since
    assert e[401]["absence"] == {"kind": "ongoing", "last_played_week": 4, "consecutive_team_games_missed": 2}
    assert e[301]["absence"] == {"kind": "long_term", "last_played_week": 2, "consecutive_team_games_missed": 4}


def test_workload_is_observed_from_games_played_only(league):
    e = by_id(get(league))
    ace = e[101]["workload"]
    assert ace["games"] == 6 and ace["team_games"] == 6 and ace["snap_share"] == 0.7
    assert ace["carries_per_game"] == 14 and ace["targets_per_game"] == 4 and ace["xfp_per_game"] == 15.0
    wide = e[201]["workload"]
    assert wide["games"] == 5 and wide["recent_weeks"] == [2, 4, 5, 6]
    week3 = next(w for w in wide["weekly"] if w["week"] == 3)
    assert week3 == {"week": 3, "played": False, "snap_share": None, "opportunities": None}   # a miss, never a zero
    assert e[401]["workload"]["pass_attempts_per_game"] == 35


def test_bye_week_and_game_context(league):
    e = by_id(get(league))
    assert e[101]["game"] == {"week": 7, "opponent": "BUF", "is_home": True, "kickoff": "2026-10-25T17:00:00Z",
                              "started": False, "on_bye": False}
    assert e[401]["game"]["is_home"] is False
    assert e[501]["game"]["on_bye"] is True and e[501]["game"]["opponent"] is None


def test_kicked_off_game_is_flagged_and_sorted_after_upcoming(league):
    body = get(league, now=datetime(2026, 10, 25, 18, 0))
    e = by_id(body)
    assert e[101]["game"]["started"] is True
    assert all(x["game"]["started"] or x["game"]["on_bye"] for x in body["players"])


# ============================================================================= teammates: observed vs estimate


def test_pro_rata_estimate_splits_vacated_volume_over_healthy_teammates(league):
    ace = by_id(get(league))[101]
    imp = ace["impact"]
    assert imp["unit"] == "touches" and imp["unit_label"] == "carries + targets"
    assert imp["vacated_per_game"] == 18.0 and imp["team_games_without"] == 0
    m = mates(ace)
    assert m[102]["estimate"]["share"] == 0.75 and m[102]["estimate"]["extra_per_game"] == 13.5
    assert m[103]["estimate"]["share"] == 0.25 and m[103]["estimate"]["extra_per_game"] == 4.5
    assert m[102]["estimate"]["basis_weeks"] == [4, 5, 6]
    # A teammate who is out himself gets no share and is marked unhealthy.
    assert m[104]["healthy"] is False and m[104]["estimate"] is None
    assert round(sum(x["estimate"]["share"] for x in m.values() if x["estimate"]), 6) == 1.0


def test_no_game_without_him_means_no_observed_split(league):
    ace = by_id(get(league))[101]
    assert ace["impact"]["clarity"]["label"] == "no_history"
    assert all(m["observed"]["without"] is None and m["observed"]["delta_per_game"] is None
               for m in ace["impact"]["teammates"])
    assert mates(ace)[102]["observed"]["with"]["games"] == 6


def test_observed_with_and_without_counts_games_and_delta(league):
    wide = by_id(get(league))[201]
    imp = wide["impact"]
    assert imp["unit"] == "targets" and imp["vacated_per_game"] == 8.0 and imp["team_games_without"] == 1
    slot = mates(wide)[202]
    assert slot["observed"]["with"]["games"] == 5 and slot["observed"]["with"]["volume_per_game"] == 4.0
    assert slot["observed"]["without"]["games"] == 1 and slot["observed"]["without"]["volume_per_game"] == 10.0
    assert slot["observed"]["delta_per_game"] == 6.0
    assert imp["clarity"]["label"] == "thin_history"
    # Estimate shares by targets over healthy WR/TE/RB teammates (Slot 4, Bench RB 2 per game).
    assert slot["estimate"]["share"] == pytest.approx(0.667, abs=1e-3)
    assert mates(wide)[102]["estimate"]["share"] == pytest.approx(0.333, abs=1e-3)


def test_ongoing_absence_splits_by_the_role_before_he_went_out(tmp_path):
    """Teammates' recent usage already includes the absorbed volume; the split must use the games he played."""
    c = fresh_db(tmp_path)
    # Third RB took over weeks 5-6 while Ace sat (Ace missed 5-6 in this variant).
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week IN (5, 6)", [G[101]])
    c.execute("UPDATE player_week_usage SET carries = 16, opportunities = 16 WHERE gsis_id = ? AND week IN (5, 6)",
              [G[103]])
    ace = by_id(get(c))[101]
    assert ace["absence"]["kind"] == "ongoing"
    m = mates(ace)
    assert m[103]["estimate"]["basis_weeks"] == [2, 3, 4]
    assert m[103]["estimate"]["share"] == 0.25          # 2 of 8 touches when Ace played, not his recent 16
    assert m[103]["observed"]["delta_per_game"] == 14.0  # observed: 16 without vs 2 with
    assert ace["impact"]["clarity"]["label"] == "split"  # estimate and observed point at different players
    c.close()


def add_rosters(con, overrides=None):
    """Weekly NFL rosters: every listed player ACT on his team in weeks 1-6, except `overrides`
    {pid: {week: status | None}} (None = not on the team that week)."""
    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, team VARCHAR, "
                "gsis_id VARCHAR, status VARCHAR)")
    for pid, (_, _, team, *_rest) in PLAYERS.items():
        for w in WEEKS:
            status = (overrides or {}).get(pid, {}).get(w, "ACT")
            if status is not None:
                con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', ?, ?, ?)", [SEASON, w, team, G[pid], status])


def test_zero_snap_backup_counts_as_zero_with_him_not_missing(tmp_path):
    """A backup who never touched the ball while the starter played, then took over: 'with' is 0 per game over the
    games he was active for, so his observed gain is real, not hidden behind a missing baseline."""
    c = fresh_db(tmp_path)
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week IN (5, 6)", [G[101]])     # Ace misses 5-6
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week <= 4", [G[103]])          # Third RB: no snaps
    c.execute("UPDATE player_week_usage SET carries = 16, opportunities = 16 WHERE gsis_id = ? AND week IN (5, 6)",
              [G[103]])
    add_rosters(c, {103: {1: "RES"}})        # on injured reserve in week 1: unavailable, not a zero
    third = mates(by_id(get(c))[101])[103]
    assert third["observed"]["with"] == {"games": 3, "played": 0, "volume_per_game": 0.0, "snap_share": 0.0,
                                         "target_share": 0.0, "ppr_per_game": 0.0}
    assert third["observed"]["without"]["volume_per_game"] == 16.0 and third["observed"]["without"]["played"] == 2
    assert third["observed"]["delta_per_game"] == 16.0
    c.close()


def test_a_player_not_on_the_team_is_missing_not_zero(tmp_path):
    """No roster row and no usage = not on the team that week: left out of the sample, never counted as zero."""
    c = fresh_db(tmp_path)
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week <= 3", [G[202]])
    add_rosters(c, {202: {1: None, 2: None, 3: None}})       # Slot WR joined KC in week 4
    slot = mates(by_id(get(c))[201])[202]
    # Wide WR missed week 3, before Slot WR arrived: the only game without him has no Slot WR sample.
    assert slot["observed"]["without"] is None and slot["observed"]["delta_per_game"] is None
    assert slot["observed"]["with"]["games"] == 3
    c.close()


@pytest.mark.parametrize("with_rosters", [True, False])
def test_games_before_he_joined_the_team_are_not_absences(tmp_path, with_rosters):
    """A week-4 arrival who played weeks 4-6 has no game 'without him': weeks 1-3 predate his tenure."""
    c = fresh_db(tmp_path)
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week <= 3", [G[101]])
    if with_rosters:
        add_rosters(c, {101: {1: None, 2: None, 3: None}})
    ace = by_id(get(c))[101]
    assert ace["impact"]["team_games_without"] == 0
    assert ace["impact"]["clarity"]["label"] == "no_history"
    assert ace["workload"]["team_games"] == 3 and [p["week"] for p in ace["workload"]["weekly"]] == [4, 5, 6]
    assert all(m["observed"]["without"] is None for m in ace["impact"]["teammates"])
    c.close()


def test_games_he_missed_while_on_the_team_are_absences(tmp_path):
    """On the roster (inactive or on reserve) and not playing: those games are genuine absences."""
    c = fresh_db(tmp_path)
    c.execute("DELETE FROM player_week_usage WHERE gsis_id = ? AND week IN (2, 3)", [G[101]])
    add_rosters(c, {101: {2: "INA", 3: "RES"}})
    ace = by_id(get(c))[101]
    assert ace["impact"]["team_games_without"] == 2
    assert mates(ace)[102]["observed"]["without"]["games"] == 2
    c.close()


def test_never_names_a_single_winner(league):
    body = get(league)
    for e in body["players"]:
        assert "winner" not in e["impact"] and "beneficiary" not in e["impact"]
        assert e["impact"]["clarity"]["label"] in ("no_history", "thin_history", "split", "leaning")
    assert "not a validated projection" in body["method"]["estimate"]


def test_qb_absence_names_the_next_qb_and_receivers_without_an_estimate(league):
    star = by_id(get(league))[401]
    imp = star["impact"]
    assert imp["unit"] == "pass_attempts" and imp["vacated_per_game"] == 35
    back = imp["teammates"][0]
    assert back["espn_player_id"] == 402 and back["depth_rank"] == 2 and back["estimate"] is None
    assert back["observed"]["without"]["games"] == 2 and back["observed"]["without"]["volume_per_game"] == 30
    assert back["espn_projection"] == 14.5
    assert "Back QB" in imp["clarity"]["text"]
    wr = imp["receivers"][0]
    assert wr["espn_player_id"] == 403 and wr["observed"]["ppr_delta_per_game"] == -6.0


# ============================================================================= rosters and perspective


def test_roster_and_waiver_status_of_teammates(league):
    e = by_id(get(league))
    assert e[101]["roster"]["is_mine"] is True and e[101]["roster"]["fantasy_team"] == "My Squad"
    slot = mates(e[201])[202]
    assert slot["roster"]["status"] == "WAIVERS" and slot["roster"]["percent_owned"] == 50.0
    assert slot["espn_projection"] == 9.5
    assert mates(e[101])[103]["roster"]["status"] == "FREE_AGENT"
    assert e[301]["affects_my_roster"] is True       # Wide WR (mine) is a teammate of the TE


def test_team_perspective_changes_whose_roster_is_mine(league):
    body = get(league, team_id=OTHER_TEAM)
    e = by_id(body)
    assert body["league_context"]["my_team_id"] == OTHER_TEAM and body["league_context"]["perspective"] == "query"
    assert e[101]["roster"]["is_mine"] is False and e[401]["roster"]["is_mine"] is True
    with pytest.raises(LookupError):
        get(league, team_id=99)


def test_without_league_rosters_status_is_unknown_and_noted(tmp_path):
    c = fresh_db(tmp_path, teams=False)
    body = get(c)
    assert body["league_context"]["available"] is False
    assert any(n["code"] == "no_rosters" for n in body["notices"])
    assert all(not e["roster"]["is_mine"] for e in body["players"])
    c.close()


# ============================================================================= filters


def test_position_and_status_filters_do_not_change_counts(league):
    rb = get(league, position="rb")
    assert {e["position"] for e in rb["players"]} == {"RB"} and rb["counts"]["total"] == 5
    out = get(league, status="out")
    assert {e["espn_player_id"] for e in out["players"]} == {101, 401, 301}
    q = get(league, status="questionable")
    assert {e["espn_player_id"] for e in q["players"]} == {201, 501}


@pytest.mark.parametrize("kw", [{"position": "K"}, {"status": "healthy"}])
def test_bad_filters_raise_value_error(league, kw):
    with pytest.raises(ValueError):
        get(league, **kw)


# ============================================================================= freshness


def test_per_team_freshness_is_attached(league):
    e = by_id(get(league))
    assert e[401]["freshness"]["status"] == "fresh" and e[401]["freshness"]["source_used"] == "espn_status"
    # KC has a week-7 NFL designation but no logged retrieval of it: stale, never read as fresh.
    assert e[101]["freshness"]["status"] == "stale"
    assert e[501]["freshness"] is None              # DEN is not on the week's slate


def test_stale_and_missing_evidence_are_visible_notices(league, tmp_path):
    codes = {n["code"] for n in get(league)["notices"]}
    assert "stale_teams" in codes and "missing_teams" not in codes
    c = fresh_db(tmp_path, espn_retrieval=False)
    notes = {n["code"]: n for n in get(c)["notices"]}
    assert notes["missing_teams"]["tone"] == "negative" and "BUF" in notes["missing_teams"]["text"]
    assert "not a clean bill of health" in notes["missing_teams"]["text"]
    c.close()


def test_unpublished_week_report_and_old_espn_sync_are_noticed(tmp_path):
    c = fresh_db(tmp_path, report=False, players_synced=NOW - timedelta(hours=40))
    body = get(c)
    codes = {n["code"] for n in body["notices"]}
    assert {"report_not_published", "espn_stale"} <= codes
    assert body["freshness"]["injury_report"] == {"available": True, "latest_week": 6, "current_week_rows": 0}
    # Without the NFL report Ace RB is healthy per ESPN, so he is not listed; ESPN statuses still are.
    assert 101 not in by_id(body) and 201 in by_id(body)
    c.close()


def test_no_injury_table_at_all(tmp_path):
    c = fresh_db(tmp_path)
    c.execute("DROP TABLE nfl_injuries")
    body = get(c)
    assert any(n["code"] == "no_nfl_report" for n in body["notices"])
    assert body["freshness"]["injury_report"]["available"] is False
    c.close()


def test_unavailable_without_a_league(con):
    with pytest.raises(ii.InjuryImpactUnavailable):
        get(con)


def test_research_context_modules_are_not_imported():
    """The ifp-ctx teammate-context challenger is research only: this page never imports or presents it."""
    import ast
    tree = ast.parse(open(ii.__file__).read())
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("roster_context" in m or "context_challenger" in m for m in imported)


# ============================================================================= API


@pytest.fixture
def api(template_db, tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    shutil.copyfile(template_db, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_route_returns_the_contract_shape(api):
    res = api.get("/api/injury-impact")
    assert res.status_code == 200
    body = res.json()
    for key in ("season", "week", "position", "status", "generated_at", "league_context", "freshness", "method",
                "counts", "notices", "players"):
        assert key in body, key
    first = body["players"][0]
    for key in ("status", "absence", "workload", "roster", "game", "freshness", "impact", "affects_my_roster"):
        assert key in first, key


def test_route_params_and_errors(api):
    assert {e["position"] for e in api.get("/api/injury-impact", params={"position": "WR"}).json()["players"]} \
        == {"WR"}
    assert api.get("/api/injury-impact", params={"team_id": OTHER_TEAM}).json()["league_context"]["my_team_id"] \
        == OTHER_TEAM
    assert api.get("/api/injury-impact", params={"position": "K"}).status_code == 422
    assert api.get("/api/injury-impact", params={"status": "nope"}).status_code == 422
    assert api.get("/api/injury-impact", params={"team_id": 99}).status_code == 404


def test_route_is_503_when_the_league_is_not_synced(tmp_path, monkeypatch):
    path = tmp_path / "empty.duckdb"
    c = connect(path)
    init_db(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    assert TestClient(app).get("/api/injury-impact").status_code == 503

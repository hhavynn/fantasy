"""Availability timeline and roster digest (analytics/availability_timeline.py, GET
/api/injury-impact/availability-timeline), issue #176.

Temp DuckDB only, one small synthetic league; every name, id and time is an invented test input. Season 2026, current
week 7 (KC-BUF and DEN-MIA, LA-NYJ every week; week 7 kicks off 2026-10-25 17:00 UTC, NOW is two days before; week 6
kicked off 2026-10-18 17:00 UTC). My team (3) rosters:

    101 Ace RB (KC)       week 6 Questionable / LP, modified after the week-6 kickoff. Week 7: two rows whose local
                          clocks mislead: Out / DNP at 16:00 -04:00 (20:00 UTC) and Questionable / LP at 19:30 UTC.
                          The Out row is newer. ESPN says QUESTIONABLE: the sources disagree.
    102 Quiet WR (KC)     no report row; KC has a week-7 report: not listed -> unknown. ESPN ACTIVE.
    103 Den TE (DEN)      no row, and DEN has no week-7 report: unknown (no report for his team yet).
    104 Conflict WR (BUF) week 7: Questionable / LP and Out / DNP, neither with a time: conflicting.
    105 Back RB (MIA)     weekly roster RES weeks 3-6, ACT week 7 (back); week 7 listed without a game status, FP.
    106 Unmapped K        no NFL id.
    107 News WR (LA)      ESPN OUT, no report row; an owner-curated news entry (third party) with its sources.
    108 Fixture D/ST      a team defense: skipped.
    109 Cleared WR (KC)   week 6 Questionable / LP; not on the week-7 report.
Team 5 rosters 201 Other RB (KC, week 7 Out).
"""

import json
import shutil
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.analytics import availability_news as an
from app.analytics import availability_timeline as at
from app.db.connection import connect, init_db

SEASON, CURRENT = 2026, 7
NOW = datetime(2026, 10, 23, 12, 0)
MY_TEAM, OTHER_TEAM = 3, 5

# pid: (name, position, NFL team, ESPN status, fantasy team, lineup slot id, mapped)
PLAYERS = {
    101: ("Ace RB", "RB", "KC", "QUESTIONABLE", MY_TEAM, 2, True),
    102: ("Quiet WR", "WR", "KC", "ACTIVE", MY_TEAM, 4, True),
    103: ("Den TE", "TE", "DEN", None, MY_TEAM, 6, True),
    104: ("Conflict WR", "WR", "BUF", None, MY_TEAM, 4, True),
    105: ("Back RB", "RB", "MIA", None, MY_TEAM, 20, True),
    106: ("Unmapped K", "K", "SEA", None, MY_TEAM, 17, False),
    107: ("News WR", "WR", "LA", "OUT", MY_TEAM, 20, True),
    108: ("Fixture D/ST", "D/ST", "KC", None, MY_TEAM, 16, False),
    109: ("Cleared WR", "WR", "KC", None, MY_TEAM, 20, True),
    201: ("Other RB", "RB", "KC", None, OTHER_TEAM, 2, True),
}
G = {pid: f"00-T{pid}" for pid in PLAYERS}

# (week, team, pid, status, practice, injury, date_modified)
REPORTS = [
    (6, "KC", 101, "Questionable", "Limited Participation in Practice", "Ankle", "2026-10-18 18:00:00+00:00"),
    (7, "KC", 101, "Out", "Did Not Participate In Practice", "Ankle", "2026-10-23 16:00:00-04:00"),
    (7, "KC", 101, "Questionable", "Limited Participation in Practice", "Ankle", "2026-10-23 19:30:00+00:00"),
    (7, "BUF", 104, "Questionable", "Limited Participation in Practice", "Knee", None),
    (7, "BUF", 104, "Out", "Did Not Participate In Practice", "Knee", None),
    (7, "MIA", 105, None, "Full Participation in Practice", "Hamstring", "2026-10-22 21:00:00+00:00"),
    (6, "KC", 109, "Questionable", "Limited Participation in Practice", "Groin", "2026-10-16 20:00:00+00:00"),
    (7, "KC", 201, "Out", "Did Not Participate In Practice", "Foot", "2026-10-23 20:00:00+00:00"),
]
NEWS = {"schema": "availability-news/v1", "entries": [{
    "season": SEASON, "gsis_id": G[107], "espn_player_id": 107, "name": "News WR", "team": "LA", "kind": "out",
    "from_week": 7, "return_window": [10, 12], "reported_on": "2026-10-19",
    "note": "Synthetic note: coach expects 3-5 weeks.", "sources": ["https://example.test/news-wr", "javascript:x"]}]}


def build(con):
    con.execute("""INSERT INTO leagues (league_id, season, name, current_week, final_scoring_period,
                   reg_season_matchups, scoring_items, settings_raw, synced_at)
                   VALUES (1, ?, 'Timeline League', ?, 17, 14, '[]', '{"name": "Timeline League"}',
                   '2026-10-23 10:00:00')""", [SEASON, CURRENT])
    for tid, name, mine in ((MY_TEAM, "My Squad", True), (OTHER_TEAM, "Team Five", False)):
        con.execute("""INSERT INTO fantasy_teams (league_id, season, team_id, abbrev, name, is_mine, synced_at)
                       VALUES (1, ?, ?, ?, ?, ?, '2026-10-23 10:00:00')""", [SEASON, tid, f"T{tid}", name, mine])
    for pid, (name, pos, team, status, owner, slot, mapped) in PLAYERS.items():
        con.execute("""INSERT INTO fantasy_players (espn_player_id, full_name, position, pro_team, injury_status,
                       availability_status, on_team_id, synced_at) VALUES (?, ?, ?, ?, ?, 'ONTEAM', ?, ?)""",
                    [pid, name, pos, team, status, owner, datetime(2026, 10, 23, 9)])
        if mapped:
            con.execute("INSERT INTO player_id_map (espn_player_id, gsis_id, match_confidence) VALUES (?, ?, 'high')",
                        [pid, G[pid]])
        con.execute("""INSERT INTO lineup_snapshots (league_id, season, scoring_period, team_id, espn_player_id,
                       lineup_slot_id, lineup_slot, is_starter, injury_status, synced_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-23 09:00:00')""",
                    [SEASON, CURRENT, owner, pid, slot, "BE" if slot == 20 else "S", slot != 20, status])
    con.execute("""CREATE TABLE nfl_schedules (season INT, week INT, game_type VARCHAR, gameday VARCHAR,
                   gametime VARCHAR, home_team VARCHAR, away_team VARCHAR)""")
    for w in range(1, CURRENT + 1):
        day = f"2026-10-{11 + 7 * (w - 5):02d}" if w >= 5 else f"2026-09-{6 + w:02d}"
        for h, a in (("KC", "BUF"), ("DEN", "MIA"), ("LA", "NYJ")):
            con.execute("INSERT INTO nfl_schedules VALUES (?, ?, 'REG', ?, '13:00', ?, ?)", [SEASON, w, day, h, a])
    con.execute("""CREATE TABLE nfl_injuries (season INT, game_type VARCHAR, team VARCHAR, week INT, gsis_id VARCHAR,
                   full_name VARCHAR, report_status VARCHAR, report_primary_injury VARCHAR, practice_status VARCHAR,
                   date_modified TIMESTAMPTZ)""")
    for week, team, pid, status, practice, injury, modified in REPORTS:
        con.execute("INSERT INTO nfl_injuries VALUES (?, 'REG', ?, ?, ?, ?, ?, ?, ?, ?::TIMESTAMPTZ)",
                    [SEASON, team, week, G[pid], PLAYERS[pid][0], status, injury, practice, modified])
    con.execute("CREATE TABLE nfl_rosters_weekly (season INT, week INT, game_type VARCHAR, team VARCHAR, "
                "gsis_id VARCHAR, status VARCHAR)")
    for pid, (_, _, team, *_rest) in PLAYERS.items():
        for w in range(1, CURRENT + 1):
            st = "RES" if pid == 105 and 3 <= w <= 6 else "ACT"
            con.execute("INSERT INTO nfl_rosters_weekly VALUES (?, ?, 'REG', ?, ?, ?)", [SEASON, w, team, G[pid], st])
    con.execute("""INSERT INTO injury_retrievals (retrieval_id, source, scope, season, week, team, retrieved_at,
                   content_sha, row_count, rows_with_publication_time, recorded_at)
                   VALUES ('r1', 'nflverse', 'team_week', ?, 7, 'KC', '2026-10-23 21:00:00', 'x', 3, 3,
                           '2026-10-23 21:00:00'),
                          ('r2', 'nflverse', 'feed', ?, NULL, NULL, '2026-10-23 21:00:00', 'y', 8, 6,
                           '2026-10-23 21:00:00')""", [SEASON, SEASON])


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("timeline_template") / "league.duckdb"
    c = connect(path)
    init_db(c)
    build(c)
    c.execute("CHECKPOINT")
    c.close()
    return path


@pytest.fixture
def news_file(tmp_path):
    p = tmp_path / "news.json"
    p.write_text(json.dumps(NEWS))
    return p


@pytest.fixture
def league(template_db, tmp_path):
    path = tmp_path / "league.duckdb"
    shutil.copyfile(template_db, path)
    c = connect(path)
    yield c
    c.close()


def get(con, news_path, **kw):
    kw.setdefault("now", NOW)
    return at.build_availability_timeline(con, news_path=news_path, **kw)


def player(body, pid):
    return next(p for p in body["players"] if p["espn_player_id"] == pid)


def official(p, week=None):
    return [e for e in p["events"] if e["kind"] == "official_report" and (week is None or e["week"] == week)]


# ============================================================================= no report -> unknown


def test_a_player_with_no_report_is_unknown_never_healthy_or_full_practice(league, news_file):
    body = get(league, news_file)
    quiet, den, unmapped = player(body, 102), player(body, 103), player(body, 106)
    assert quiet["headline"]["level"] == "unknown" and quiet["headline"]["coverage"] == "not_listed"
    assert den["headline"]["level"] == "unknown" and den["headline"]["coverage"] == "no_team_report"
    assert unmapped["headline"]["level"] == "unknown" and unmapped["headline"]["coverage"] == "unmapped"
    for p in (quiet, den, unmapped):
        assert p["practice"] == {"code": "unknown", "label": "Unknown", "raw": None, "scope": None,
                                 "reason": p["headline"]["coverage"]}
        text = p["headline"]["text"].lower()
        assert "unknown" in text and "healthy" not in text.replace("clean bill of health", "")
        assert "full" not in text
    # Start/Sit reads him as available (no designation); the page says so without calling him healthy.
    assert quiet["lineup"] == {"status": "AVAILABLE", "sources": ["ESPN ACTIVE"]}
    assert "no designation" in quiet["headline"]["text"]


def test_no_injury_feed_at_all_makes_every_player_and_practice_unknown(league, news_file):
    league.execute("DROP TABLE nfl_injuries")
    body = get(league, news_file)
    codes = {n["code"] for n in body["notices"]}
    assert {"no_nfl_report", "practice_granularity"} <= codes
    assert all(p["practice"]["code"] == "unknown" for p in body["players"])
    assert player(body, 101)["headline"]["coverage"] == "no_feed"
    # ESPN's designation still counts (the lineup tools read it), sourced to ESPN only.
    assert player(body, 101)["headline"]["level"] == "questionable"
    assert player(body, 101)["headline"]["sources"] == ["ESPN QUESTIONABLE"]
    assert body["counts"]["practice_known"] == 0
    assert body["practice"]["daily_available"] is False


def test_an_unpublished_week_is_unknown_not_carried_over(league, news_file):
    league.execute("DELETE FROM nfl_injuries WHERE week = 7")
    body = get(league, news_file)
    assert "report_not_published" in {n["code"] for n in body["notices"]}
    cleared = player(body, 109)
    assert cleared["headline"]["level"] == "unknown" and cleared["headline"]["coverage"] == "not_published"
    assert cleared["change"] is None          # last week's Questionable is not carried into this week


# ============================================================================= superseding and timezones


def test_newer_rows_replace_older_ones_by_utc_time_not_local_clock(league, news_file):
    ace = player(get(league, news_file), 101)
    week7 = official(ace, 7)
    cur = next(e for e in week7 if e["relation"] == "current")
    old = next(e for e in week7 if e["relation"] == "superseded")
    assert cur["status"] == "Out" and cur["practice"]["code"] == "DNP"
    assert cur["published_at"] == "2026-10-23T20:00:00Z"          # 16:00 -04:00
    assert old["published_at"] == "2026-10-23T19:30:00Z" and old["replaced_by"] == cur["id"]
    # Last week's report is replaced by this week's.
    (w6,) = official(ace, 6)
    assert w6["relation"] == "earlier_week" and w6["replaced_by"] == cur["id"]
    assert ace["change"] == {"kind": "status_change", "from": "Questionable / LP", "to": "Out / DNP"}
    assert ace["high_signal"] is True
    assert ace["practice"]["code"] == "DNP" and ace["practice"]["scope"] == "final_report"


def test_every_time_is_utc_and_events_are_in_chronological_order(league, news_file):
    body = get(league, news_file)
    assert body["generated_at"].endswith("Z")
    for p in body["players"]:
        keys = [at._sort_key(e) for e in p["events"]]
        assert keys == sorted(keys)
        for e in p["events"]:
            for k in ("at", "published_at", "retrieved_at"):
                assert e[k] is None or e[k].endswith("Z"), (p["full_name"], k, e[k])
    ace = player(body, 101)
    assert [e["kind"] for e in ace["events"]][-1] == "kickoff"
    assert next(e for e in ace["events"] if e["kind"] == "kickoff")["at"] == "2026-10-25T17:00:00Z"
    assert official(ace, 7)[0]["retrieved_at"] == "2026-10-23T21:00:00Z"


def test_a_row_modified_after_kickoff_is_marked_late(league, news_file):
    body = get(league, news_file)
    (w6,) = official(player(body, 101), 6)
    assert w6["after_kickoff"] is True                      # 18:00 UTC after the 17:00 UTC kickoff
    assert all(e["after_kickoff"] is False for e in official(player(body, 101), 7))
    assert "after_kickoff" in {n["code"] for n in body["notices"]}


# ============================================================================= contradictions


def test_unordered_rows_that_disagree_are_conflicting_and_resolved_like_the_lineup_tools(league, news_file):
    body = get(league, news_file)
    c = player(body, 104)
    rel = {e["status"]: e["relation"] for e in official(c, 7)}
    assert rel == {"Out": "current", "Questionable": "conflicting"}
    assert c["headline"]["conflicting_reports"] is True
    assert c["headline"]["level"] == "out" and c["lineup"]["status"] == "OUT"
    assert "conflicting_reports" in {n["code"] for n in body["notices"]}


def test_official_and_provider_disagreement_is_named_and_matches_start_sit(league, news_file):
    from app.analytics.availability import availability
    ace = player(get(league, news_file), 101)
    assert ace["lineup"]["status"] == availability("QUESTIONABLE", 2, "Out")["status"] == "OUT"
    assert ace["headline"]["level"] == "out"
    d = ace["headline"]["disagreement"]
    assert "NFL report says Out" in d and "ESPN says Questionable" in d and "more severe (Out)" in d


# ============================================================================= sources


def test_return_to_play_comes_from_the_official_weekly_roster(league, news_file):
    back = player(get(league, news_file), 105)
    ev = next(e for e in back["events"] if e["kind"] == "roster_status" and e["week"] == 7)
    assert ev["return_to_play"] is True and ev["at"] is None and ev["at_basis"] == "week_only"
    assert ev["source"]["type"] == "official" and ev["source"]["url"].startswith("https://")
    assert back["headline"]["level"] == "listed" and back["practice"]["code"] == "FP"
    assert back["change"]["kind"] == "new_listing" and back["high_signal"]


def test_third_party_news_supplements_never_replaces_and_cites_its_reports(league, news_file):
    news = player(get(league, news_file), 107)
    ev = next(e for e in news["events"] if e["kind"] == "third_party")
    assert ev["source"] == {"type": "third_party", "label": "Owner-curated news summary", "url": None,
                            "urls": ["https://example.test/news-wr"]}            # the javascript: link is dropped
    assert ev["relation"] == "supplements" and ev["date"] == "2026-10-19" and ev["at"] is None
    assert news["headline"]["level"] == "out" and news["headline"]["sources"] == ["ESPN OUT"]
    prov = next(e for e in news["events"] if e["kind"] == "provider_status")
    assert prov["source"]["type"] == "provider" and prov["published_at"] is None
    assert prov["retrieved_at"] == "2026-10-23T09:00:00Z" and prov["at_basis"] == "retrieved"


def test_news_parser_keeps_only_http_sources():
    (e,) = an.parse(NEWS)
    assert e.sources == ("https://example.test/news-wr",)


def test_every_event_cites_a_source(league, news_file):
    body = get(league, news_file)
    for p in body["players"]:
        for e in p["events"]:
            assert e["source"]["type"] in ("official", "provider", "third_party", "schedule")
            assert e["source"]["label"]
            assert e["source"]["type"] == "provider" or e["source"]["url"] or e["source"]["urls"]
    assert [s["key"] for s in body["sources"]] == ["nfl_report", "nfl_roster", "provider", "news", "schedule"]
    assert body["sources"][0]["publication_time"] == "partial"     # two rows carry no date_modified


def test_a_dropped_listing_is_a_change_not_a_clean_bill(league, news_file):
    cleared = player(get(league, news_file), 109)
    assert cleared["change"] == {"kind": "dropped_off_report", "from": "Questionable / LP", "to": None}
    assert cleared["headline"]["level"] == "unknown"


# ============================================================================= roster filtering


def test_the_digest_covers_only_the_selected_teams_roster(league, news_file):
    mine = get(league, news_file)
    assert {p["espn_player_id"] for p in mine["players"]} == {101, 102, 103, 104, 105, 106, 107, 109}
    assert mine["counts"]["team_defenses_skipped"] == 1 and mine["league_context"]["my_team_id"] == MY_TEAM
    other = get(league, news_file, team_id=OTHER_TEAM)
    assert [p["espn_player_id"] for p in other["players"]] == [201]
    assert other["league_context"] == {"available": True, "league_name": "Timeline League", "my_team_id": OTHER_TEAM,
                                       "team_name": "Team Five", "perspective": "query"}
    with pytest.raises(LookupError):
        get(league, news_file, team_id=99)


def test_digest_order_puts_designations_first_and_unknowns_last(league, news_file):
    levels = [p["headline"]["level"] for p in get(league, news_file)["players"]]
    assert levels == sorted(levels, key=at.LEVEL_RANK.__getitem__)
    assert levels[-1] == "unknown"


def test_practice_codes():
    assert at.practice_code("Did Not Participate In Practice") == "DNP"
    assert at.practice_code("Limited Participation in Practice") == "LP"
    assert at.practice_code("Full Participation in Practice") == "FP"
    assert at.practice_code("Something new") == "OTHER"
    assert at.practice_code(None) is None and at.practice_code("  ") is None


# ============================================================================= API


@pytest.fixture
def api(template_db, tmp_path, monkeypatch):
    path = tmp_path / "api.duckdb"
    shutil.copyfile(template_db, path)
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    return TestClient(app)


def test_route_returns_the_contract_shape_and_errors(api):
    res = api.get("/api/injury-impact/availability-timeline")
    assert res.status_code == 200
    body = res.json()
    for key in ("version", "season", "week", "window", "league_context", "sources", "practice", "method", "counts",
                "notices", "players"):
        assert key in body, key
    assert body["version"] == at.VERSION and body["window"] == {"from_week": 4, "to_week": 7}
    assert api.get("/api/injury-impact/availability-timeline", params={"team_id": OTHER_TEAM}).json()[
        "league_context"]["my_team_id"] == OTHER_TEAM
    r = api.get("/api/injury-impact/availability-timeline", params={"team_id": 99})
    assert r.status_code == 404 and "Team 99" in r.json()["detail"]
    assert api.get("/api/injury-impact/availability-timeline", params={"team_id": "abc"}).status_code == 422


def test_route_is_503_without_a_league(tmp_path, monkeypatch):
    path = tmp_path / "empty.duckdb"
    c = connect(path)
    init_db(c)
    c.close()
    monkeypatch.setenv("FANTASY_DB_PATH", str(path))
    from app.main import app
    assert TestClient(app).get("/api/injury-impact/availability-timeline").status_code == 503


# ============================================================================= /v2: authorization before parameters

from cp_seed import U1, seed_user  # noqa: E402
from league_fixtures import publish_local  # noqa: E402
from test_league_scope import client as v2_client, served  # noqa: E402,F401  (fixtures)
from test_leagues_api import bearer, env  # noqa: E402,F401  (fixture)

PATH = "api/injury-impact/availability-timeline"


def test_v2_hidden_other_or_unknown_league_is_the_same_404_whatever_the_team_id(pg, served, v2_client, template_db,
                                                                               tmp_path):
    """A private league read anonymously or by a signed-in non-member, and an unknown league, answer the same 404
    before `team_id` is parsed: a malformed or out-of-range team never reveals that the league exists."""
    pub = publish_local(pg, template_db, served, tmp_path / "w", visibility="private")
    with pg.connect() as con:
        seed_user(con, U1)
        con.commit()
    for lg in (pub.league_id, "lg_" + "z" * 24):
        for headers in ({}, bearer(U1)):
            for q in ("team_id=abc", "team_id=9999999", f"team_id={MY_TEAM}", ""):
                r = v2_client.get(f"/v2/leagues/{lg}/{PATH}?{q}", headers=headers)
                assert (r.status_code, r.json()) == (404, {"detail": "League not found."}), (lg, q)


def test_v2_public_league_filters_to_the_requested_team_of_that_league(pg, served, v2_client, template_db, tmp_path):
    pub = publish_local(pg, template_db, served, tmp_path / "w", visibility="public")
    base = f"/v2/leagues/{pub.league_id}/{PATH}"
    r = v2_client.get(f"{base}?team_id={OTHER_TEAM}")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "private, no-store"
    assert [p["espn_player_id"] for p in r.json()["players"]] == [201]
    r = v2_client.get(f"{base}?team_id={MY_TEAM}")
    assert 201 not in {p["espn_player_id"] for p in r.json()["players"]}
    assert v2_client.get(f"{base}?team_id=99").status_code == 404

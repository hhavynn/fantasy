"""Soccer adapter: recognition, sources and formats. Names are real; every figure here is synthetic."""

from __future__ import annotations

from datetime import datetime

import duckdb
import httpx
import pytest
from conftest import NOW, make_post

from social_intel import cli
from social_intel.contract import utcnow
from social_intel.core.http import Fetcher
from social_intel.core.store import Store
from social_intel.engine import Engine
from social_intel.ideas import HookRotation, generate
from social_intel.sports.soccer.adapter import SoccerAdapter
from social_intel.sports.soccer.clubs import CLUB_BY_ID, CLUBS
from social_intel.sports.soccer.entities import SoccerIndex
from social_intel.sports.soccer.stats import (
    ASA_SOURCE,
    SCHEMA,
    SoccerEnricher,
    csv_season,
    parse_asa_players,
    parse_fixtures,
    parse_fpl_players,
    parse_mls,
    parse_results,
    sync,
)
from social_intel.sports.soccer.sync_entities import parse_asa, parse_fpl, parse_wikidata

PLAYERS = [
    {"id": "fpl:1", "name": "Bukayo Saka", "team": "arsenal", "position": "FW", "popularity": 0.8},
    {"id": "fpl:2", "name": "Bruno Fernandes", "team": "man-utd", "position": "MF", "popularity": 0.7, "aliases": ["B.Fernandes"]},
    {"id": "fpl:3", "name": "Enzo Fernandez", "team": "chelsea", "position": "MF", "popularity": 0.5},
    {"id": "fpl:4", "name": "Declan Rice", "team": "arsenal", "position": "MF", "popularity": 0.7},
    {"id": "wd:Q1", "name": "Kylian Mbappé", "team": "real-madrid", "position": "FW", "popularity": 1.0},
]


@pytest.fixture
def soccer():
    return SoccerAdapter(index=SoccerIndex(PLAYERS), now=NOW)


def annotated(adapter, text, community="r/soccer", **kw):
    p = make_post("x", text, community=community, **kw)
    adapter.annotate(p)
    return p


def test_club_table_covers_the_leagues_without_duplicates():
    assert len(CLUBS) >= 126 and len(CLUB_BY_ID) == len(CLUBS)
    aliases = [a for c in CLUBS for a in (*c.aliases, *c.context_aliases)]
    assert len(aliases) == len(set(aliases))


def test_nicknames_resolve_to_clubs(soccer):
    assert soccer.index.find_clubs("The Gunners beat Man Utd") == ["arsenal", "man-utd"]


def test_longest_alias_wins(soccer):
    assert soccer.index.find_clubs("Inter Miami sign a striker") == ["inter-miami"]


def test_ambiguous_names_need_soccer_context_and_a_capital(soccer):
    assert soccer.index.find_clubs("Spurs beat the Lakers in overtime") == []
    assert soccer.index.find_clubs("that was a nice goal") == []
    assert soccer.index.find_clubs("Spurs sign a new striker") == ["tottenham"]
    assert soccer.index.find_clubs("Nice scored a late goal") == ["nice"]


def test_players_resolve_conservatively(soccer):
    idx = soccer.index
    assert idx.find_players("Mbappe scored twice") == ["wd:Q1"]
    assert idx.find_players("Rice was great") == []
    assert idx.find_players("Declan Rice was great") == ["fpl:4"]
    assert idx.find_players("Fernandes scored") == []
    assert idx.find_players("Fernandes scored", ["man-utd"]) == ["fpl:2"]


def test_bare_names_that_usually_mean_someone_else_do_not_match():
    idx = SoccerIndex([{"id": "a:1", "name": "Junior Alonso", "team": "atlanta-united", "popularity": 0.5},
                       {"id": "f:1", "name": "Shea Charles", "team": "fulham", "popularity": 0.5},
                       {"id": "f:2", "name": "Ricky Fringe", "team": "fc-dallas", "popularity": 0.2},
                       {"id": "f:3", "name": "Rico Lewis", "team": "man-city", "popularity": 0.7}])
    assert idx.find_players("Xabi Alonso on the transfer rumours") == []
    assert idx.find_players("Charles Leclerc beat Lewis Hamilton") == []
    assert idx.find_players("Fringe scored") == []
    assert idx.find_players("Rico Lewis started") == ["f:3"]


def test_a_surname_that_is_also_a_first_name_needs_the_club():
    idx = SoccerIndex([{"id": "f:1", "name": "David Raya Martin", "team": "arsenal", "popularity": 0.8},
                       {"id": "f:2", "name": "Martin Odegaard", "team": "arsenal", "popularity": 0.8}])
    assert idx.find_players("Martin Semmens on the deal") == []
    assert idx.find_players("Martin Odegaard scored") == ["f:2"]


def test_stars_outside_the_leagues_are_known(soccer):
    assert soccer.index.find_players("CR7 and Messi again") == ["star:cristiano-ronaldo", "star:lionel-messi"]


def test_annotate_adds_the_players_club_and_subreddit(soccer):
    p = annotated(soccer, "Saka is world class", community="r/reddevils")
    assert p.entities["players"] == ["fpl:1"]
    assert set(p.entities["teams"]) == {"arsenal", "man-utd"} and "rise" in p.topics


def test_relevance_drops_unrelated_posts(soccer):
    assert soccer.is_relevant(annotated(soccer, "Anything at all", community="r/soccer"))
    assert not soccer.is_relevant(annotated(soccer, "The Eagles won on Sunday", community="news"))


def test_source_config_sends_no_nfl_sources(soccer):
    cfg = soccer.source_config()
    blob = repr(cfg).lower()
    assert "nfl" not in blob and "fantasy" not in blob
    assert "soccer" in cfg.extra["activity_subreddits"] and "Gunners" in cfg.extra["team_subreddits"]


@pytest.mark.parametrize("topics,want", [
    (["award"], ["parody"]), (["transfer"], ["parody", "parallel_universe"]),
    (["refereeing"], ["explainer"]), ([], ["parody"]),
])
def test_formats_follow_topics(soccer, topics, want):
    assert soccer.choose_formats({"topics": topics}) == want


@pytest.mark.parametrize("topics,want", [
    (["injury", "transfer"], []), (["serious", "award"], []), (["injury", "refereeing"], ["explainer"]),
    (["award", "transfer", "serious"], ["parody", "parallel_universe"]),
])
def test_injury_and_serious_stories_never_get_fiction_formats(soccer, topics, want):
    assert soccer.choose_formats({"topics": topics}) == want


def test_serious_stories_are_tagged(soccer):
    assert "serious" in soccer.tag_topics("Former striker has died aged 54")


def test_parody_ideas_carry_the_content_rules(soccer):
    c = {"cluster_id": "c", "topic_id": "t", "label": "Kylian Mbappé: award", "sport": "soccer", "topics": ["award"],
         "score": {"total": 60, "flags": [], "components": []}, "top_posts": [], "entities": {"players": ["wd:Q1"]}}
    idea = generate([c], soccer, now=NOW)[0]
    assert idea.format == "parody" and len(idea.hooks) == 3 and "parody" in idea.working_title
    assert any("no face-swaps" in n for n in idea.notes)


def test_explainer_title_names_the_rule_topic_not_the_story(soccer):
    c = {"cluster_id": "c", "topic_id": "t", "label": "Harry Kane: award", "sport": "soccer",
         "topics": ["award", "refereeing"], "score": {"total": 60, "flags": [], "components": []}, "top_posts": []}
    idea = soccer.build_idea(c, "explainer", HookRotation(), soccer.event_window(NOW), {}, NOW)
    assert "refereeing" in idea.working_title and "award" not in idea.working_title


def test_rank_end_to_end_names_the_club(store, soccer):
    posts = [make_post(k, f"Arsenal transfer news: {k} {w}", author=k, community="r/soccer", likes=200)
             for k, w in (("a", "bid accepted"), ("b", "fee agreed"), ("c", "medical booked"))]
    for p in posts:
        soccer.annotate(p)
    store.upsert_posts(posts, "soccer")
    rows = Engine(store, soccer).rank(now=NOW)
    assert rows and rows[0]["label"].startswith("Arsenal")
    assert rows[0]["enrichment"]["stats"] == []


def test_parse_fpl_maps_clubs():
    data = {"teams": [{"id": 1, "name": "Nott'm Forest"}, {"id": 2, "name": "Man Utd"}],
            "elements": [{"code": 9, "first_name": "Morgan", "second_name": "Gibbs-White", "web_name": "Gibbs-White",
                          "team": 1, "element_type": 3, "selected_by_percent": "25.0"},
                         {"code": 8, "first_name": "Bruno", "second_name": "Fernandes", "web_name": "B.Fernandes",
                          "team": 2, "element_type": 3, "selected_by_percent": "60.0"}]}
    rows = parse_fpl(data, SoccerIndex())
    assert [r["team"] for r in rows] == ["nottingham-forest", "man-utd"]
    assert rows[0]["popularity"] == 0.65 and rows[1]["popularity"] == 1.0


def test_parse_asa_uses_the_latest_team():
    rows = parse_asa([{"player_id": "p", "player_name": "Lionel Messi"}],
                     [{"player_id": "p", "team_id": ["old", "t"], "general_position": "ST", "minutes_played": 2500},
                      {"player_id": "ghost", "team_id": "t"}],
                     [{"team_id": "t", "team_name": "D.C. United"}], SoccerIndex())
    assert rows == [{"id": "asa:p", "name": "Lionel Messi", "team": "dc-united", "position": "ST", "popularity": 0.5}]


def test_parse_wikidata_keeps_the_latest_spell_at_a_known_club():
    def row(pid, club, start):
        return {"p": {"value": f"http://www.wikidata.org/entity/{pid}"}, "pLabel": {"value": "Kylian Mbappé"},
                "clubLabel": {"value": club}, "links": {"value": "133"}, "start": {"value": start}}

    rows = parse_wikidata([row("Q1", "Paris Saint-Germain F.C.", "2018-01-01"), row("Q1", "Real Madrid CF", "2024-07-01"),
                           row("Q2", "Some Unknown Club", "2024-07-01")], SoccerIndex())
    assert rows == [{"id": "wd:Q1", "name": "Kylian Mbappé", "team": "real-madrid", "popularity": 1.0}]


def test_select_writes_a_soccer_package_for_make_soccer(tmp_path, monkeypatch, soccer, capsys):
    monkeypatch.setattr(cli, "STUDIO_EPISODES", tmp_path / "episodes")
    monkeypatch.setattr(cli, "DATA", tmp_path / "data")
    monkeypatch.setattr(cli, "make_adapter", lambda sport, now=None: soccer)
    db = tmp_path / "s.duckdb"
    store = Store(db)
    posts = [make_post(k, f"Arsenal red card row: {k} VAR referee", author=k, community="r/soccer", likes=200)
             for k in "abc"]
    for p in posts:
        p.created_at, p.collected_at = None, utcnow()
        soccer.annotate(p)
    store.upsert_posts(posts, "soccer")
    store.close()
    base = ["--sport", "soccer", "--db", str(db)]
    assert cli.main(base + ["rank"]) == 0 and cli.main(base + ["ideas", "--full"]) == 0
    store = Store(db)
    idea = store.con.execute("SELECT idea_id FROM ideas WHERE sport = 'soccer' LIMIT 1").fetchone()[0]
    store.close()
    assert cli.main(base + ["select", idea]) == 0
    out = capsys.readouterr().out
    assert "make-soccer --package" in out and str(tmp_path / "episodes" / "soccer") in out


def test_sync_command_is_soccer_only(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "s.duckdb"), "sync-entities"]) == 2
    assert "soccer" in capsys.readouterr().out


# ---------------------------------------------------------------- statistics

def test_csv_season_rolls_over_in_july():
    assert csv_season(datetime(2026, 10, 1)) == "2627" and csv_season(datetime(2027, 3, 1)) == "2627"
    assert csv_season(datetime(2027, 7, 1)) == "2728"


RESULTS = """﻿Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HxG,AxG
E0,12/10/2026,15:00,Arsenal,Man United,2,0,H,1.8,0.4
E0,18/10/2026,15:00,Tottenham,Arsenal,1,1,D,1.0,1.5
E0,25/10/2026,15:00,Arsenal,Nott'm Forest,0,1,A,1.2,0.9
E0,31/12/2026,15:00,Arsenal,Mystery Town,,,,,
"""


def stats_db(tmp_path):
    idx = SoccerIndex(PLAYERS)
    rows, unknown = parse_results(RESULTS, "Premier League", idx, "2026-10-30")
    assert len(rows) == 3 and unknown == {"Mystery Town"}
    path = tmp_path / "soccer.duckdb"
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    con.executemany("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    fixtures = parse_fixtures("Div,Date,Time,HomeTeam,AwayTeam\nE0,01/11/2026,16:30,Chelsea,Arsenal\n"
                              "B1,31/10/2026,15:00,Waregem,Genk\n", idx, "2026-10-30")
    assert len(fixtures) == 1
    con.executemany("INSERT INTO fixtures VALUES (?,?,?,?,?,?)", fixtures)
    con.execute("INSERT INTO player_seasons VALUES ('FPL bootstrap-static', 'fpl:1', 700, 5, 3, 4.26, 2.1, '2026-10-30')")
    con.close()
    return path, idx


def test_enrich_gives_sourced_club_and_player_stats(tmp_path):
    path, idx = stats_db(tmp_path)
    out = SoccerEnricher(path, idx, NOW.replace(month=10, day=30)).enrich(["fpl:1"], ["arsenal"])
    by = {s["label"]: s for s in out["stats"]}
    assert by["Arsenal last 3 league games"]["value"] == "L-D-W"
    assert "Arsenal Premier League position" not in by  # only 4 of 20 clubs in this fixture
    assert by["Arsenal xG difference, last 3 league games"]["value"] == 2.2
    assert by["Bukayo Saka xG this season"]["value"] == 4.26
    assert all(s["source"] and s["as_of"] for s in out["stats"]) and out["analytic_coverage"] == 1.0


def test_position_needs_the_whole_league(tmp_path):
    path, idx = stats_db(tmp_path)
    con = duckdb.connect(str(path))
    others = [c.id for c in CLUBS if c.league == "Premier League" and c.id not in
              ("arsenal", "man-utd", "tottenham", "nottingham-forest")][:16]
    for a, b in zip(others[::2], others[1::2]):
        con.execute("INSERT INTO matches VALUES ('t', 'Premier League', DATE '2026-10-01', ?, ?, 0, 0, NULL, NULL, 'x')", [a, b])
    con.close()
    out = SoccerEnricher(path, idx, NOW.replace(month=10, day=30)).enrich([], ["arsenal"])
    assert {s["label"]: s["value"] for s in out["stats"]}["Arsenal Premier League position"] == 1


def test_enrich_is_point_in_time(tmp_path):
    path, idx = stats_db(tmp_path)
    out = SoccerEnricher(path, idx, NOW.replace(month=10, day=20)).enrich([], ["arsenal"])
    by = {s["label"]: s["value"] for s in out["stats"]}
    assert by["Arsenal last 2 league games"] == "D-W"


def test_enrich_without_stats_reports_no_coverage(tmp_path):
    out = SoccerEnricher(tmp_path / "missing.duckdb", SoccerIndex(), NOW).enrich([], ["arsenal"])
    assert out["analytic_coverage"] == 0.0 and out["stats"] == []


def test_event_window_uses_the_next_fixture(tmp_path):
    path, idx = stats_db(tmp_path)
    a = SoccerAdapter(index=idx, stats_db=path, now=NOW)
    w = a.event_window(NOW.replace(month=10, day=30))
    assert "Premier League" in w.label and w.expires.day == 1


def test_event_window_falls_back_without_fixtures(soccer):
    assert soccer.event_window(NOW).label == "next Saturday matchday"


def test_parse_mls_keeps_the_season(tmp_path):
    text = ("Country,League,Season,Date,Time,Home,Away,HG,AG\nUSA,MLS,2025,01/03/2025,20:00,LAFC,Austin FC,1,0\n"
            "USA,MLS,2026,29/10/2026,02:30,Vancouver Whitecaps,San Diego FC,4,1\n")
    rows, unknown = parse_mls(text, 2026, SoccerIndex(), "2026-10-30")
    assert not unknown and [(r[3], r[4], r[5]) for r in rows] == [("vancouver-whitecaps", "san-diego-fc", 4)]


def test_player_parsers_use_entity_ids():
    fpl = parse_fpl_players({"elements": [{"code": 1, "minutes": 90, "goals_scored": 1, "assists": 0,
                                           "expected_goals": "0.74", "expected_assists": "0.10"},
                                          {"code": 2, "minutes": 0, "goals_scored": 0, "assists": 0,
                                           "expected_goals": "0", "expected_assists": "0"}]}, "2026-10-30")
    assert [r[1] for r in fpl] == ["fpl:1"] and fpl[0][5] == 0.74
    asa = parse_asa_players([{"player_id": "p", "minutes_played": 900, "goals": 2, "primary_assists": 1,
                              "xgoals": 1.5, "xassists": 0.5}], "2026-10-30")
    assert asa[0][1] == "asa:p" and asa[0][0] == ASA_SOURCE


def test_sync_keeps_a_failed_sources_rows(tmp_path):
    path = tmp_path / "soccer.duckdb"
    good = fetcher_for_stats(RESULTS)
    counts, errors = sync(path, good, NOW.replace(month=10, day=30), SoccerIndex(PLAYERS))
    assert counts["Premier League"] == 3 and any("unmapped" in e for e in errors)
    bad = fetcher_for_stats(None)
    counts, errors = sync(path, bad, NOW.replace(month=10, day=30), SoccerIndex(PLAYERS))
    assert "Premier League" not in counts and errors
    con = duckdb.connect(str(path), read_only=True)
    assert con.execute("SELECT count(*) FROM matches WHERE league = 'Premier League'").fetchone()[0] == 3
    con.close()


def fetcher_for_stats(body):
    def handler(req):
        if body is None:
            return httpx.Response(404, text="")
        if "/E0.csv" in str(req.url):
            return httpx.Response(200, text=body)
        return httpx.Response(404, text="")

    return Fetcher(transport=httpx.MockTransport(handler), sleep=lambda s: None, min_interval_s=0, retries=0)

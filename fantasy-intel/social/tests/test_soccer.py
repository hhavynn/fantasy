"""Soccer adapter: recognition, sources and formats. Names are real; every figure here is synthetic."""

from __future__ import annotations

import pytest
from conftest import NOW, make_post

from social_intel import cli
from social_intel.engine import Engine
from social_intel.ideas import generate
from social_intel.sports.soccer.adapter import SoccerAdapter
from social_intel.sports.soccer.clubs import CLUB_BY_ID, CLUBS
from social_intel.sports.soccer.entities import SoccerIndex
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


def test_parody_ideas_carry_the_content_rules(soccer):
    c = {"cluster_id": "c", "topic_id": "t", "label": "Kylian Mbappé: award", "sport": "soccer", "topics": ["award"],
         "score": {"total": 60, "flags": [], "components": []}, "top_posts": [], "entities": {"players": ["wd:Q1"]}}
    idea = generate([c], soccer, now=NOW)[0]
    assert idea.format == "parody" and len(idea.hooks) == 3 and "parody" in idea.working_title
    assert any("no face-swaps" in n for n in idea.notes)


def test_rank_end_to_end_names_the_club(store, soccer):
    posts = [make_post(k, f"Arsenal transfer news: {k} {w}", author=k, community="r/soccer", likes=200)
             for k, w in (("a", "bid accepted"), ("b", "fee agreed"), ("c", "medical booked"))]
    for p in posts:
        soccer.annotate(p)
    store.upsert_posts(posts, "soccer")
    rows = Engine(store, soccer).rank(now=NOW)
    assert rows and rows[0]["label"].startswith("Arsenal")
    assert rows[0]["enrichment"]["analytic_coverage"] == 0.0


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


def test_sync_command_is_soccer_only(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "s.duckdb"), "sync-entities"]) == 2
    assert "soccer" in capsys.readouterr().out

"""Player/team extraction, topic tags, clustering and repost handling."""

from __future__ import annotations

from conftest import make_post

from social_intel.core.cluster import cluster_posts, independent_sources
from social_intel.sports.football.topics import tag_topics


def ids(index, text):
    return [index.players[i].name + "/" + index.players[i].team for i in index.find_players(text)]


def test_full_names_nicknames_and_possessives(index):
    assert ids(index, "Jaxson Dart's rushing upside is real") == ["Jaxson Dart/NYG"]
    assert ids(index, "CMC back at practice") == ["Christian McCaffrey/SF"]
    assert ids(index, "ARSB or Ja'Marr Chase this week?") == ["Amon-Ra St. Brown/DET", "Ja'Marr Chase/CIN"]


def test_same_name_picks_the_fantasy_relevant_player(index):
    assert ids(index, "Josh Allen threw for 300") == ["Josh Allen/BUF"]


def test_ambiguous_surname_needs_team_context(index):
    assert ids(index, "Allen is a must start") == []
    assert ids(index, "Allen is the Jets RB1 now") == ["Braelon Allen/NYJ"]


def test_common_words_and_lowercase_never_match_alone(index):
    assert ids(index, "I love this lineup, chase the upside") == []
    assert ids(index, "Love threw 3 picks") == []  # 'Love' alone is too ambiguous
    assert ids(index, "Jordan Love threw 3 picks") == ["Jordan Love/GB"]
    assert ids(index, "Nabers is a WR1") == ["Malik Nabers/NYG"]
    assert ids(index, "nabers is a wr1") == []  # single tokens need capitalisation


def test_teams(index):
    assert set(index.find_teams("The Giants and the Niners, also KC fans")) == {"NYG", "SF"}  # 'KC' alone is ambiguous
    assert index.find_teams("NYJ backfield") == ["NYJ"]


def test_topics():
    assert {"waivers", "breakout"} <= set(tag_topics("Pick up Skattebo, league winner breakout"))
    assert "injury" in tag_topics("Ruled out with a hamstring strain")
    assert "trade" in tag_topics("Would you accept this trade?")


def annotate(adapter, posts):
    for p in posts:
        adapter.annotate(p)
    return posts


def test_cluster_groups_same_player_and_splits_others(adapter):
    posts = annotate(adapter, [
        make_post("a", "Jaxson Dart is a league winner"),
        make_post("b", "Is Jaxson Dart actually good or is it the rushing?", author="u2"),
        make_post("c", "Dart 2 rushing TDs again, Giants QB1", platform="bluesky", author="u3"),
        make_post("d", "Travis Kelce looks washed", author="u4"),
    ])
    clusters = cluster_posts(posts, "nfl", label_fn=adapter.label)
    by_size = sorted(clusters, key=lambda c: -len(c.post_keys))
    assert len(by_size[0].post_keys) == 3 and by_size[0].platforms == ["bluesky", "reddit"]
    assert "00-0040001" in by_size[0].entities["players"]
    assert by_size[0].label.startswith("Jaxson Dart")


def test_multi_player_question_does_not_chain_narratives(adapter):
    posts = annotate(adapter, [
        make_post("a", "Jaxson Dart breakout is real"),
        make_post("b", "Jaxson Dart rushing floor is elite", author="u2"),
        make_post("c", "Travis Kelce is done", author="u3"),
        make_post("d", "Travis Kelce targets fell off a cliff", author="u4"),
        make_post("e", "WDIS: Jaxson Dart or Travis Kelce in superflex?", author="u5"),
    ])
    clusters = cluster_posts(posts, "nfl", entity_terms=adapter.name_tokens)
    dart = next(c for c in clusters if "a" in [p.external_id for p in posts if p.key in c.post_keys])
    assert not any(p.external_id in {"c", "d"} for p in posts if p.key in dart.post_keys)


def test_reposts_and_copies_collapse_to_one_voice(adapter):
    text = "BREAKING: Cam Skattebo expected to handle lead role vs Eagles"
    posts = annotate(adapter, [
        make_post("a", text, platform="x", author="insider"),
        make_post("b", text + " https://t.co/xyz", platform="x", author="aggregator"),
        make_post("c", "rt", platform="x", author="fan", is_repost=True, parent_id="a"),
        make_post("d", "Skattebo usage has been elite all year, this tracks", author="analyst"),
    ])
    clusters = cluster_posts(posts, "nfl")
    assert len(clusters) == 1
    assert independent_sources([p for p in posts]) == 2  # copy + repost are not new voices


def test_templated_attention_lines_never_merge_by_text(adapter):
    posts = annotate(adapter, [
        make_post("s1", "Jaxson Dart trending add: added in 300,000 Sleeper leagues in 24h (#1 add)", platform="sleeper"),
        make_post("s2", "Travis Kelce trending add: added in 290,000 Sleeper leagues in 24h (#2 add)", platform="sleeper"),
    ])
    assert len(cluster_posts(posts, "nfl", entity_terms=adapter.name_tokens)) == 2


def test_roundups_stay_alone(adapter):
    roundup = "Week 6 rankings: Jaxson Dart, Travis Kelce, Malik Nabers, Josh Allen, Cam Skattebo"
    posts = annotate(adapter, [make_post("r", roundup), make_post("a", "Jaxson Dart is a league winner", author="b")])
    clusters = cluster_posts(posts, "nfl", entity_terms=adapter.name_tokens)
    assert len(clusters) == 2


def test_subject_anchor_joins_backup_news_to_starter(adapter):
    posts = annotate(adapter, [
        make_post("a", "Jaxson Dart placed on IR, Giants turn to the backup", platform="x", author="insider"),
        make_post("b", "Jaxson Dart to IR is brutal for my superflex team", author="fan"),
    ])
    assert len(cluster_posts(posts, "nfl", entity_terms=adapter.name_tokens)) == 1

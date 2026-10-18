"""Storage dedup/merging, health, and the CLI review workflow end to end (fixture data)."""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import NOW, make_post

from social_intel import cli
from social_intel.contract import Engagement


def test_upsert_dedups_and_appends_snapshots(store):
    p = make_post("a", "Jaxson Dart", likes=10, comments=2)
    assert store.upsert_posts([p, p], "nfl") == 1
    p2 = make_post("a", "Jaxson Dart", likes=50, comments=9)
    p2.collected_at = NOW + timedelta(hours=1)
    assert store.upsert_posts([p2], "nfl") == 0
    snaps = store.snapshots([p.key], "nfl")[p.key]
    assert [e.likes for _, e in snaps] == [10, 50]


def test_complementary_sources_merge(store):
    rss = make_post("t1", "Title only from RSS")
    arc = make_post("t1", "Title only from RSS", hours_ago=0)
    arc.created_at = None
    arc.extra = {"comment_rate_per_h": 40.0}
    store.upsert_posts([rss], "nfl")
    store.upsert_posts([arc], "nfl")
    got = store.recent_posts("nfl", 72, NOW)[0]
    assert got.created_at is not None and got.extra["comment_rate_per_h"] == 40.0


def test_no_snapshot_when_source_has_no_engagement(store):
    p = make_post("n", "news headline", platform="news")
    p.engagement = Engagement()
    store.upsert_posts([p], "nfl")
    assert store.snapshots([p.key], "nfl") == {}


def test_cli_workflow(tmp_path, monkeypatch, capsys, adapter):
    db = tmp_path / "social.duckdb"
    monkeypatch.setattr(cli, "STUDIO_EPISODES", tmp_path / "episodes")
    monkeypatch.setattr(cli, "make_adapter", lambda sport, now=None: adapter)
    from social_intel.core.store import Store
    s = Store(db)
    posts = [make_post("a", "Malik Nabers is the WR1, buy low now", likes=900, comments=700, hours_ago=1),
             make_post("b", "Malik Nabers buy low window is closing", platform="bluesky", likes=300, comments=90, author="b"),
             make_post("c", "Travis Kelce is washed, sell", likes=400, comments=500, author="c")]
    for p in posts:
        p.created_at = None  # first-seen time = now, so the window always includes them
        adapter.annotate(p)
    s.upsert_posts(posts, "nfl")
    s.close()
    base = ["--db", str(db)]
    assert cli.main(base + ["rank"]) == 0
    assert "Malik Nabers" in capsys.readouterr().out
    assert cli.main(base + ["list", "--player", "nabers"]) == 0
    out = capsys.readouterr().out
    assert "Nabers" in out and "Kelce" not in out
    assert cli.main(base + ["show", "1"]) == 0
    out = capsys.readouterr().out
    assert "Score components" in out and "https://example.test/" in out
    assert cli.main(base + ["ideas"]) == 0
    out = capsys.readouterr().out
    idea_id = out.split()[0]
    assert cli.main(base + ["idea", idea_id]) == 0
    assert "Hooks" in capsys.readouterr().out
    assert cli.main(base + ["select", idea_id]) == 0
    pkg_path = next((tmp_path / "episodes").rglob(f"{idea_id}.json"))
    pkg = json.loads(pkg_path.read_text())
    assert pkg["idea"]["idea_id"] == idea_id and pkg["cluster"]["top_posts"]
    assert cli.main(base + ["export", "--out", str(tmp_path / "b.json"), "--fixture"]) == 0
    bundle = json.loads((tmp_path / "b.json").read_text())
    assert bundle["data_label"] == "fixture" and bundle["ideas"]
    assert cli.main(base + ["health"]) == 0


def test_rolling_signal_stays_in_window(store):
    first = make_post("add:1", "X trending add: added in 1,000 Sleeper leagues", platform="sleeper", likes=1000,
                      hours_ago=0, extra={"rolling": True})
    store.upsert_posts([first], "nfl")
    later = make_post("add:1", "X trending add: added in 9,000 Sleeper leagues", platform="sleeper", likes=9000,
                      hours_ago=0, extra={"rolling": True})
    later.created_at = later.collected_at = NOW + timedelta(days=4)
    store.upsert_posts([later], "nfl")
    got = store.recent_posts("nfl", 72, NOW + timedelta(days=4))
    assert len(got) == 1 and got[0].engagement.likes == 9000 and "9,000" in got[0].text


def test_empty_ranking_replaces_the_previous_one(store):
    store.save_ranking("r1", "nfl", [{"cluster_id": "c1", "score": {"total": 50}}])
    assert store.latest_ranking("nfl")
    store.save_ranking("r2", "nfl", [])
    assert store.latest_ranking("nfl") == []


def test_same_post_is_separate_per_sport(store):
    p = make_post("t", "Trending search: big game", platform="google_trends", views=5000)
    store.upsert_posts([p], "nfl")
    p.entities = {"teams": ["X"]}
    assert store.upsert_posts([p], "soccer") == 1
    assert len(store.recent_posts("nfl", 72, NOW)) == 1 and len(store.recent_posts("soccer", 72, NOW)) == 1
    assert store.recent_posts("nfl", 72, NOW)[0].entities == {}
    assert len(store.snapshots([p.key], "soccer")[p.key]) == 1


def test_migrates_v1_database(tmp_path):
    import duckdb

    from social_intel.core.store import Store
    db = tmp_path / "v1.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    con.execute("""CREATE TABLE posts (key VARCHAR PRIMARY KEY, sport VARCHAR, platform VARCHAR NOT NULL,
        external_id VARCHAR NOT NULL, url VARCHAR NOT NULL, created_at TIMESTAMPTZ, first_collected_at TIMESTAMPTZ NOT NULL,
        last_collected_at TIMESTAMPTZ NOT NULL, title VARCHAR, text VARCHAR, author VARCHAR, community VARCHAR,
        hashtags VARCHAR[], collection_method VARCHAR, confidence DOUBLE, lang VARCHAR, parent_id VARCHAR,
        is_repost BOOLEAN, content_hash VARCHAR, extra JSON, entities JSON, topics VARCHAR[])""")
    con.execute("CREATE TABLE engagement_snapshots (key VARCHAR NOT NULL, collected_at TIMESTAMPTZ NOT NULL, "
                "likes BIGINT, comments BIGINT, shares BIGINT, views BIGINT, upvote_ratio DOUBLE)")
    con.execute("INSERT INTO posts (key, sport, platform, external_id, url, created_at, first_collected_at, "
                "last_collected_at, text) VALUES ('k1', 'nfl', 'reddit', 'a', 'u', ?, ?, ?, 'old post')", [NOW, NOW, NOW])
    con.execute("INSERT INTO engagement_snapshots VALUES ('k1', ?, 5, 1, NULL, NULL, NULL)", [NOW])
    con.close()
    s = Store(db)
    try:
        assert [p.text for p in s.recent_posts("nfl", 72, NOW)] == ["old post"]
        assert s.recent_posts("nfl", 72, NOW)[0].engagement.likes == 5
        assert s.con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
    finally:
        s.close()


def test_studio_subject_for_out_player_is_the_replacement():
    lead = {"name": "Roman Wilson", "team": "PIT", "position": "WR", "roster_status": "ACT", "gsis_id": "g1",
            "injury": {"week": 6, "report_status": "Out"}, "data_direction": "down"}
    repl = {"name": "Germie Bernard", "team": "PIT", "position": "WR", "roster_status": "ACT", "gsis_id": "g2",
            "data_direction": "up"}
    cluster = {"enrichment": {"players": [lead, repl], "upcoming_week": 6}, "top_posts": []}
    subjects, why = cli.studio_subjects({"format": "injury_ripple"}, cluster)
    assert [p["name"] for p in subjects] == ["Germie Bernard", "Roman Wilson"] and "out" in why
    subjects, _ = cli.studio_subjects({"format": "opinion_vs_numbers"}, cluster)
    assert subjects[0]["name"] == "Roman Wilson"

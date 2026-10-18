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
    snaps = store.snapshots([p.key])[p.key]
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
    assert store.snapshots([p.key]) == {}


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

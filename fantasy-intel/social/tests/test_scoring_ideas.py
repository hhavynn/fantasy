"""Trend score maths, flags, point-in-time enrichment, idea generation and the JSON contract."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import duckdb
import jsonschema
import pytest
from conftest import NOW, make_post

from social_intel import ideas as ideas_mod
from social_intel.contract import Engagement, TrendCluster, json_schema
from social_intel.core.cluster import cluster_posts
from social_intel.core.scoring import PENALTIES, WEIGHTS, ScoringContext, post_velocity, score_cluster
from social_intel.engine import Engine
from social_intel.sports.football.enrich import FootballEnricher


def one_cluster(adapter, posts) -> TrendCluster:
    for p in posts:
        adapter.annotate(p)
    cs = cluster_posts(posts, "nfl", label_fn=adapter.label, entity_terms=adapter.name_tokens)
    assert len(cs) == 1
    return cs[0]


def ctx(**kw) -> ScoringContext:
    base = dict(now=NOW, topic_weights={"breakout": 0.9, "waivers": 1.0, "injury": 0.95, "bust": 0.8})
    base.update(kw)
    return ScoringContext(**base)


def test_weights_sum_to_one_and_score_is_bounded(adapter):
    assert abs(sum(WEIGHTS.values()) - 1) < 1e-9
    posts = [make_post("a", "Jaxson Dart league winner breakout?", likes=5000, comments=4000, hours_ago=1),
             make_post("b", "Jaxson Dart breakout, pick up now", platform="bluesky", likes=900, comments=300, author="b")]
    c = one_cluster(adapter, posts)
    s = score_cluster(c, posts, ctx())
    assert 0 <= s.total <= 100
    assert {x.name for x in s.components} == set(WEIGHTS)
    assert all(0 <= x.value <= 1 and x.detail for x in s.components)
    assert s.explanation


def test_velocity_prefers_snapshots_over_lifetime_average():
    p = make_post("a", "x", likes=100, comments=0, hours_ago=10)
    assert post_velocity(p, ctx()) == pytest.approx(10.0)
    snaps = {p.key: [(NOW - timedelta(hours=1), Engagement(likes=100)), (NOW, Engagement(likes=700))]}
    assert post_velocity(p, ctx(snapshots=snaps)) == pytest.approx(600.0)
    p.extra["comment_rate_per_h"] = 300
    assert post_velocity(p, ctx(snapshots=snaps)) == pytest.approx(900.0)  # measured comment pace wins


def test_missing_engagement_is_not_a_crash_or_a_zero_score(adapter):
    posts = [make_post("a", "Jaxson Dart breakout? Giants QB looks legit", hours_ago=1),
             make_post("b", "Jaxson Dart waiver pickup", platform="news", author="ESPN", hours_ago=2)]
    c = one_cluster(adapter, posts)
    s = score_cluster(c, posts, ctx())
    rel = next(x for x in s.components if x.name == "relative_popularity")
    assert rel.value == 0 and "no engagement" in rel.detail
    assert s.total > 0


def test_flags_stale_rumor_covered_and_penalties(adapter):
    stale = [make_post("a", "Jaxson Dart breakout", likes=50, hours_ago=150)]
    s = score_cluster(one_cluster(adapter, stale), stale, ctx())
    assert "stale" in s.flags
    rumor = [make_post("r", "Hearing Cam Skattebo could be traded, per sources, unconfirmed", likes=50)]
    c = one_cluster(adapter, rumor)
    s2 = score_cluster(c, rumor, ctx())
    assert "unconfirmed_rumor" in s2.flags
    from social_intel.core.cluster import stable_cluster_id
    s3 = score_cluster(c, rumor, ctx(covered={stable_cluster_id(c)}))
    assert "already_covered" in s3.flags and s3.total < s2.total
    assert s3.total <= s2.total * PENALTIES["already_covered"] + 0.2  # penalty plus zero novelty


def test_recent_video_lowers_novelty(adapter):
    posts = [make_post("a", "Travis Kelce washed?", likes=100)]
    c = one_cluster(adapter, posts)
    fresh = score_cluster(c, posts, ctx())
    covered = score_cluster(c, posts, ctx(covered_entities={"00-0040012": "2026-w06-short-travis-kelce"}))
    nov = {x.name: x for x in covered.components}["novelty"]
    assert nov.value < 1 and "2026-w06" in nov.detail and covered.total < fresh.total


def test_suspicious_engagement_flag(adapter):
    posts = [make_post("a", "Malik Nabers is the WR1", likes=90000, comments=10)]
    s = score_cluster(one_cluster(adapter, posts), posts, ctx())
    assert "suspicious_engagement" in s.flags


def test_cross_platform_momentum_beats_single_platform(adapter):
    one = [make_post(f"r{i}", f"Cam Skattebo breakout take {i}", likes=200, comments=100, author=f"u{i}") for i in range(3)]
    many = one[:1] + [make_post("b", "Cam Skattebo breakout", platform="bluesky", likes=200, comments=50, author="b"),
                      make_post("y", "Cam Skattebo breakout", platform="youtube", views=90000, likes=4000, author="y"),
                      make_post("s", "Cam Skattebo trending add", platform="sleeper", likes=90000, author="s")]
    a = score_cluster(one_cluster(adapter, one), one, ctx())
    b = score_cluster(one_cluster(adapter, many), many, ctx())
    ca = {x.name: x.value for x in a.components}["cross_platform"]
    cb = {x.name: x.value for x in b.components}["cross_platform"]
    assert cb > ca


# ---------------------------------------------------------------- point-in-time enrichment

@pytest.fixture
def analytics_db(tmp_path):
    path = tmp_path / "fantasy.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE player_week_usage (season INT, week INT, gsis_id VARCHAR, team VARCHAR, opponent VARCHAR,
        position VARCHAR, offense_pct DOUBLE, target_share DOUBLE, backfield_rush_share DOUBLE, air_yards_share DOUBLE,
        targets INT, carries INT, rz_targets INT, rz_carries INT, xfp DOUBLE, fantasy_points_ppr DOUBLE)""")
    rows = [(2026, w, "00-0040002", "NYG", "DAL", "RB", s, 0.1, b, 0.0, 3, c, 1, 2, 12.0, f)
            for w, s, b, c, f in [(1, .40, .35, 8, 6.0), (2, .42, .36, 9, 7.0), (3, .45, .40, 10, 8.0),
                                  (4, .75, .80, 20, 22.0), (5, .78, .85, 22, 25.0), (6, .10, .05, 1, 0.5)]]
    con.executemany("INSERT INTO player_week_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE nfl_schedules (season INT, game_type VARCHAR, week INT, gameday VARCHAR)")
    con.executemany("INSERT INTO nfl_schedules VALUES (2026, 'REG', ?, ?)",
                    [(w, f"2026-09-{6 + 7 * (w - 1):02d}" if w < 5 else f"2026-10-{(6 + 7 * (w - 1)) - 30:02d}")
                     for w in range(1, 8)])
    con.execute("CREATE TABLE sync_runs (source VARCHAR, status VARCHAR, finished_at TIMESTAMP)")
    con.execute("INSERT INTO sync_runs VALUES ('nfl', 'ok', '2026-10-15 09:00:00')")
    con.execute("""CREATE TABLE nfl_rosters_weekly (season INT, week INT, gsis_id VARCHAR, full_name VARCHAR, team VARCHAR,
        position VARCHAR, status VARCHAR, espn_id VARCHAR)""")
    con.execute("INSERT INTO nfl_rosters_weekly VALUES (2026, 6, '00-0040002', 'Cam Skattebo', 'NYG', 'RB', 'ACT', NULL)")
    for t, cols in {"player_scores": "season INT, gsis_id VARCHAR, as_of_week INT, trend_label VARCHAR, role_trend VARCHAR, "
                                     "market_signal VARCHAR, value_gap_label VARCHAR, fp_per_game DOUBLE, xfp_per_game DOUBLE, "
                                     "fp_minus_xfp DOUBLE, opportunity_score_100 DOUBLE, value_score_100 DOUBLE, reasons VARCHAR",
                    "nfl_injuries": "season INT, gsis_id VARCHAR, week INT, report_status VARCHAR, report_primary_injury VARCHAR, "
                                    "practice_status VARCHAR",
                    "team_context": "season INT, team VARCHAR, through_week INT, next_week INT, next_opponent VARCHAR, "
                                    "next_implied_total DOUBLE"}.items():
        con.execute(f"CREATE TABLE {t} ({cols})")
    con.close()
    return path


def test_enrichment_never_uses_games_after_now(analytics_db):
    # Week 6 was played 2026-10-11; at 2026-10-09 only weeks 1-5 are history
    from datetime import datetime, timezone
    before = FootballEnricher(analytics_db, 2026, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    p = before.player("00-0040002")
    assert before.cut.through_week == 5 and max(w["week"] for w in p["weekly"]) == 5
    assert p["data_direction"] == "up"  # weeks 4-5 vs 1-3
    after = FootballEnricher(analytics_db, 2026, now=NOW)
    q = after.player("00-0040002")
    assert after.cut.through_week == 6 and q["trend"]["backfield_rush_share"]["recent_weeks"] == [5, 6]
    for s in p["stats"]:
        assert s["source"].startswith("fantasy.duckdb:") and s["as_of"]


def test_enrichment_verdict_contradicts_bust_take(analytics_db):
    from datetime import datetime, timezone
    e = FootballEnricher(analytics_db, 2026, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    out = e.enrich(["00-0040002"], ["bust"], {"positive": 0, "negative": 3})
    assert out["verdicts"][0]["relation"] == "contradicts" and out["analytic_coverage"] > 0.5


# ---------------------------------------------------------------- ideas + contract

def ranked(adapter, store, analytics_db, posts):
    from datetime import datetime, timezone
    when = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    adapter.db_path = analytics_db
    adapter.now = when
    for p in posts:
        p.created_at = when - timedelta(hours=3)
        p.collected_at = when
        adapter.annotate(p)
    store.upsert_posts(posts, "nfl")
    eng = Engine(store, adapter)
    return eng.rank(now=when), eng, when


def bust_posts():
    return [make_post("a", "Cam Skattebo is a bust, sell now. Overrated", likes=800, comments=900),
            make_post("b", "Unpopular opinion: Cam Skattebo is a fraud, I'm selling", likes=300, comments=400, author="u2"),
            make_post("c", "Cam Skattebo dropped in 20,000 leagues", platform="sleeper", likes=20000, author="sleeper",
                      extra={"direction": "drop", "subject": "Cam Skattebo"})]


def test_ideas_are_sourced_distinct_and_valid(adapter, store, analytics_db):
    rows, eng, when = ranked(adapter, store, analytics_db, bust_posts())
    batch = ideas_mod.generate(rows, adapter, now=when)
    assert batch, "a scored cluster with analytics must yield ideas"
    assert batch[0].format in {"internet_vs_data", "contrarian_breakdown"}  # crowd says bust, usage says up
    for i in batch:
        assert len(i.hooks) == 3 and len(set(i.hooks)) == 3
        assert all(not ideas_mod.BANNED.search(h) for h in i.hooks)
        assert i.source_links and i.key_stats and i.relevance_expires
        stat_numbers = " ".join(str(s.value) for s in i.key_stats) + " " + " ".join(i.supporting_args)
        for h in i.hooks:
            for n in re.findall(r"\d+", h):
                assert n in stat_numbers or n in (" ".join(i.supporting_args)), f"unsourced number {n} in {h!r}"
    hooks = [h for i in batch for h in i.hooks]
    assert len(set(hooks)) == len(hooks)


def test_export_bundle_matches_schema(adapter, store, analytics_db):
    rows, eng, when = ranked(adapter, store, analytics_db, bust_posts())
    batch = [i.to_dict() for i in ideas_mod.generate(rows, adapter, now=when)]
    bundle = {"contract_version": "social-intel/1.0", "sport": "nfl", "generated_at": "2026-10-09T12:00:00Z",
              "data_label": "fixture", "clusters": rows, "ideas": batch,
              "posts": [p.to_dict() for p in store.recent_posts("nfl", 72, when)]}
    jsonschema.validate(json.loads(json.dumps(bundle, default=str)), json_schema())


def test_already_selected_topic_is_skipped_next_time(adapter, store, analytics_db):
    rows, eng, when = ranked(adapter, store, analytics_db, bust_posts())
    batch = ideas_mod.generate(rows, adapter, now=when)
    i = batch[0]
    store.save_ideas([i.to_dict()], {i.idea_id: f"{rows[0]['topic_id']}|{i.format}"})
    store.set_idea_status(i.idea_id, "selected")
    assert store.used_fingerprints() == {f"{rows[0]['topic_id']}|{i.format}"}
    again = eng.rank(now=when)  # the next run sees the topic as made, whatever the format
    assert "already_covered" in again[0]["score"]["flags"]
    assert not ideas_mod.generate(again, adapter, now=when)


def test_provenance_survives_to_the_idea(adapter, store, analytics_db):
    rows, eng, when = ranked(adapter, store, analytics_db, bust_posts())
    i = ideas_mod.generate(rows, adapter, now=when)[0]
    urls = {tp["url"] for tp in rows[0]["top_posts"]}
    assert set(i.source_links) <= urls and all(u.startswith("https://") for u in i.source_links)
    assert all(s.source for s in i.key_stats)


def test_attention_gate_keeps_quiet_posts_low(adapter):
    quiet = [make_post("q", "Jaxson Dart breakout pick up now", likes=0, comments=0, hours_ago=0.5)]
    loud = [make_post("l", "Jaxson Dart breakout pick up now", likes=3000, comments=2500, hours_ago=0.5)]
    sq = score_cluster(one_cluster(adapter, quiet), quiet, ctx())
    sl = score_cluster(one_cluster(adapter, loud), loud, ctx())
    assert sq.total < 35 < sl.total and "attention gate" in sq.explanation


def test_injured_lead_gets_replacement_story_and_stale_out_is_caught():
    lead = {"name": "Jalen Hurts", "team": "PHI", "position": "QB", "roster_status": "RES", "trend": {}, "stats": [],
            "injury": {"week": 6, "report_status": "Out"}}
    backup = {"name": "Tanner McKee", "team": "PHI", "position": "QB", "roster_status": "ACT", "stats": []}
    c = {"enrichment": {"players": [lead, backup], "upcoming_week": 6}, "topics": ["injury"], "score": {"components": []}}
    assert ideas_mod.choose_formats(c) == ["injury_ripple"]
    assert ideas_mod.rival(c, allow_out_lead=True)["name"] == "Tanner McKee"
    c["enrichment"]["status_cues"] = {"return": 2, "out": 0, "return_links": ["https://x.test/1"]}
    assert ideas_mod.availability(lead, 6, c["enrichment"]["status_cues"]) == "uncertain"


def test_status_cues_from_reports():
    from social_intel.engine import status_cues
    ps = [make_post("n1", "Patriots activate A.J. Brown off injured reserve", platform="news"),
          make_post("n2", "Eagles placed QB Jalen Hurts on injured reserve", platform="x"),
          make_post("r1", "A.J. Brown returns!", platform="reddit")]
    cues = status_cues(ps)
    assert cues["return"] == 1 and cues["out"] == 1 and cues["return_links"] == ["https://example.test/news/n1"]

"""Interpretable 0-100 trend score.

Ten components, each 0-1 with a one-line reason, combined with fixed weights; then multiplicative
penalties for flags (stale, unconfirmed rumour, sarcasm-driven, suspicious engagement, recycled,
already covered). The weights are the editorial policy: change them in ``WEIGHTS``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from social_intel.contract import Engagement, ScoreComponent, SocialPost, TrendCluster, TrendScore, utcnow
from social_intel.core.signals import text_signals

WEIGHTS = {
    "velocity": 0.18,
    "relative_popularity": 0.12,
    "cross_platform": 0.10,
    "freshness": 0.10,
    "debate": 0.10,
    "fantasy_relevance": 0.12,
    "entity_popularity": 0.08,
    "analytic_opportunity": 0.10,
    "novelty": 0.06,
    "meme_potential": 0.04,
}
PENALTIES = {
    "stale": 0.55,
    "unconfirmed_rumor": 0.85,
    "sarcasm_driven": 0.9,
    "suspicious_engagement": 0.7,
    "recycled": 0.6,
    "already_covered": 0.45,
    "roundup": 0.5,
}
# Components that measure attention. Everything else (relevance, our data, novelty...) says how good a
# video the topic would make, which only matters if people are paying attention: those contributions
# are scaled by the attention gate, 0.25 + 0.75 x max(attention components).
ATTENTION = ("velocity", "relative_popularity", "cross_platform")
GATE_FLOOR = 0.25
# interactions/hour that counts as "very hot" on each platform (weighted total, see Engagement.total)
HOT_RATE = {"reddit": 400.0, "bluesky": 60.0, "youtube": 1500.0, "x": 800.0, "tiktok": 6000.0,
            "instagram": 3000.0, "sleeper": 1500.0, "wikipedia": 400.0}
# attention-only sources: they say people care, not what they are saying
ATTENTION_PLATFORMS = {"sleeper", "google_trends", "wikipedia"}
DEFAULT_BASELINE = {"reddit": 60.0, "bluesky": 25.0, "youtube": 500.0, "x": 100.0, "tiktok": 2000.0,
                    "instagram": 800.0}
FRESH_HALF_LIFE_H = 20.0
STALE_AFTER_H = 96.0


@dataclass
class ScoringContext:
    now: datetime = field(default_factory=utcnow)
    baselines: dict[tuple[str, str], float] = field(default_factory=dict)
    snapshots: dict[str, list[tuple[datetime, Engagement]]] = field(default_factory=dict)
    first_seen_text: dict[str, datetime] = field(default_factory=dict)  # content hash -> earliest sighting
    covered: set[str] = field(default_factory=set)  # stable topic ids already produced
    covered_entities: dict[str, str] = field(default_factory=dict)  # entity -> episode id (recent videos)
    topic_weights: dict[str, float] = field(default_factory=dict)
    entity_popularity: dict[str, float] = field(default_factory=dict)


def _sq(x: float) -> float:
    return max(0.0, min(1.0, x))


def post_velocity(p: SocialPost, ctx: ScoringContext) -> float:
    """Interactions per hour: from engagement snapshots when we have two, else lifetime average."""
    measured = 3.0 * float(p.extra.get("comment_rate_per_h") or 0)  # fresh comments counted by the archive
    snaps = ctx.snapshots.get(p.key) or []
    if len(snaps) >= 2:
        (t0, e0), (t1, e1) = snaps[-2], snaps[-1]
        hours = max((t1 - t0).total_seconds() / 3600, 0.1)
        return max(measured, (e1.total() - e0.total()) / hours)
    age = p.age_hours(ctx.now)
    if age is None:
        return measured
    return max(measured, p.engagement.total() / max(age, 0.5))


def score_cluster(c: TrendCluster, posts: list[SocialPost], ctx: ScoringContext) -> TrendScore:
    comps: list[ScoreComponent] = []
    flags: list[str] = []
    discussion = [p for p in posts if p.platform not in ATTENTION_PLATFORMS]
    attention = [p for p in posts if p.platform in ATTENTION_PLATFORMS]

    # 1. velocity: best per-platform hotness, plus coverage volume for count-only sources (news)
    vel_by_platform: dict[str, float] = {}
    for p in posts:
        v = post_velocity(p, ctx) / HOT_RATE.get(p.platform, 500.0)
        vel_by_platform[p.platform] = vel_by_platform.get(p.platform, 0.0) + v
    news_n = sum(1 for p in posts if p.platform == "news")
    raw = max(vel_by_platform.values(), default=0.0) + 0.12 * news_n
    velocity = _sq(math.log1p(4 * raw) / math.log1p(4))
    lead = max(vel_by_platform, key=vel_by_platform.get) if vel_by_platform else "-"
    comps.append(ScoreComponent("velocity", velocity, WEIGHTS["velocity"],
                                f"{raw:.2f}x hot rate (lead: {lead})" + (f", {news_n} news items" if news_n else "")))

    # 2. relative popularity vs where it was posted
    ratios = []
    for p in discussion:
        if p.engagement.total() <= 0:
            continue
        # the community median, but never below the platform default: a search feed full of zero-like posts
        # would otherwise make 8 likes look like a viral hit
        base = max(ctx.baselines.get((p.platform, p.community or ""), 0.0), DEFAULT_BASELINE.get(p.platform, 0.0))
        if base:
            ratios.append(p.engagement.total() / max(base, 1.0))
    best_ratio = max(ratios, default=0.0)
    rel = _sq(math.log2(1 + best_ratio) / math.log2(1 + 10))  # 10x the community median = 1.0
    comps.append(ScoreComponent("relative_popularity", rel, WEIGHTS["relative_popularity"],
                                f"top post {best_ratio:.1f}x its community median" if ratios else "no engagement data"))

    # 3. cross-platform momentum and independent voices
    plats = {p.platform for p in posts}
    cross = _sq((len(plats) - 1) / 3) * 0.7 + _sq((c.independent_sources - 1) / 8) * 0.3
    comps.append(ScoreComponent("cross_platform", cross, WEIGHTS["cross_platform"],
                                f"{len(plats)} platforms ({', '.join(sorted(plats))}), "
                                f"{c.independent_sources} independent voices"))

    # 4. freshness of the conversation (newest discussion post)
    ages = [a for p in (discussion or posts) if (a := p.age_hours(ctx.now)) is not None]
    newest = min(ages) if ages else None
    fresh = 0.5 ** (newest / FRESH_HALF_LIFE_H) if newest is not None else 0.3
    comps.append(ScoreComponent("freshness", fresh, WEIGHTS["freshness"],
                                f"newest post {newest:.0f}h old" if newest is not None else "no timestamps"))
    if newest is not None and newest > STALE_AFTER_H:
        flags.append("stale")

    # 5. debate potential
    sigs = [text_signals(p.text) for p in discussion]
    debate_share = sum(s.debate or s.question or s.contrarian for s in sigs) / max(len(sigs), 1)
    ratios_ud = [p.engagement.upvote_ratio for p in discussion if p.engagement.upvote_ratio is not None]
    contested = sum(1 for r in ratios_ud if r < 0.8) / max(len(ratios_ud), 1) if ratios_ud else 0.0
    talky = [p.engagement.comments / max(p.engagement.likes or 1, 1) for p in discussion
             if p.engagement.comments is not None and p.engagement.likes]
    talk = _sq((statistics.median(talky) if talky else 0) / 1.0)  # >= 1 comment per upvote = heated
    debate = _sq(0.5 * debate_share + 0.25 * contested + 0.25 * talk)
    comps.append(ScoreComponent("debate", debate, WEIGHTS["debate"],
                                f"{debate_share:.0%} posts argue/ask, {contested:.0%} contested votes, "
                                f"comment/upvote {statistics.median(talky) if talky else 0:.2f}"))

    # 6. audience relevance of the topics
    tw = ctx.topic_weights
    rel_topics = sorted((tw.get(t, 0.3) for t in c.topics), reverse=True)[:3]
    fr = _sq(sum(rel_topics) / 3 * 1.2) if rel_topics else 0.15
    if not c.entities.get("players") and not c.entities.get("teams"):
        fr *= 0.5
    comps.append(ScoreComponent("fantasy_relevance", fr, WEIGHTS["fantasy_relevance"],
                                f"topics: {', '.join(c.topics[:4]) or 'none'}"))

    # 7. how big an audience the entities carry
    pops = [ctx.entity_popularity.get(e, 0.0) for e in (c.entities.get("players") or [])[:3]]
    ep = max(pops, default=0.25 if c.entities.get("teams") else 0.0)
    comps.append(ScoreComponent("entity_popularity", ep, WEIGHTS["entity_popularity"],
                                f"most popular entity {ep:.2f}"))

    # 8. analytic opportunity: filled by the sport adapter's enrichment
    ao = float(c.enrichment.get("analytic_coverage", 0.0))
    comps.append(ScoreComponent("analytic_opportunity", ao, WEIGHTS["analytic_opportunity"],
                                c.enrichment.get("analytic_note", "no enrichment")))

    # 9. novelty: have we already made this?
    from social_intel.core.cluster import stable_cluster_id
    sid = stable_cluster_id(c)
    covered_by = [ctx.covered_entities[e] for e in (c.entities.get("players") or [])[:1] if e in ctx.covered_entities]
    if sid in ctx.covered:
        nov, why = 0.0, "this exact topic already sent to production"
        flags.append("already_covered")
    elif covered_by:
        nov, why = 0.4, f"lead player featured recently ({covered_by[0]})"
    else:
        nov, why = 1.0, "not covered yet"
    comps.append(ScoreComponent("novelty", nov, WEIGHTS["novelty"], why))

    # 10. meme potential
    meme_share = sum(s.sarcasm or s.hype for s in sigs) / max(len(sigs), 1)
    meme = _sq(0.6 * meme_share + (0.4 if "meme" in c.topics else 0) + (0.2 if "bust" in c.topics else 0))
    comps.append(ScoreComponent("meme_potential", meme, WEIGHTS["meme_potential"],
                                f"{meme_share:.0%} posts joking/hyping"))

    # ---- flags
    rumor = [s for s in sigs if s.rumor and not s.confirmed]
    if rumor and len(rumor) >= max(1, len(sigs) // 2) and c.independent_sources <= 2:
        flags.append("unconfirmed_rumor")
    if sigs and sum(s.sarcasm for s in sigs) / len(sigs) > 0.6:
        flags.append("sarcasm_driven")
    from social_intel.core.cluster import is_roundup
    if discussion and all(is_roundup(p) for p in discussion):
        flags.append("roundup")  # a rankings/start-sit list: useful context, rarely one video
    if _suspicious(discussion, ctx):
        flags.append("suspicious_engagement")
    oldest_text = [ctx.first_seen_text[h] for h in c.enrichment.get("_content_hashes", []) if h in ctx.first_seen_text]
    if oldest_text and c.first_seen and min(oldest_text) < c.first_seen - timedelta(days=7):
        flags.append("recycled")

    gate = GATE_FLOOR + (1 - GATE_FLOOR) * max(x.value for x in comps if x.name in ATTENTION)
    total = sum(x.value * x.weight * (1.0 if x.name in ATTENTION else gate) for x in comps) * 100
    for f in flags:
        total *= PENALTIES.get(f, 1.0)
    total = round(max(0.0, min(100.0, total)), 1)
    top = sorted(comps, key=lambda x: x.value * x.weight, reverse=True)[:3]
    explanation = "; ".join(f"{x.name.replace('_', ' ')} {x.value:.2f} ({x.detail})" for x in top)
    explanation += f" | attention gate {gate:.2f}"
    if flags:
        explanation += " | flags: " + ", ".join(flags)
    if attention and not discussion:
        explanation += " | attention spike only (no discussion text yet)"
    return TrendScore(total=total, components=comps, flags=flags, explanation=explanation)


def _suspicious(posts: list[SocialPost], ctx: ScoringContext) -> bool:
    """Engagement shapes organic discussion rarely has: a huge like count with almost no comments
    on a discussion platform, or a step jump between two snapshots far above the post's own pace."""
    for p in posts:
        e = p.engagement
        if p.platform in ("reddit", "bluesky", "x") and (e.likes or 0) > 2000 and (e.comments or 0) < (e.likes or 0) / 300:
            return True
        if p.platform in ("youtube", "tiktok") and (e.views or 0) > 200_000 and (e.likes or 0) < (e.views or 0) / 2000:
            return True
        snaps = ctx.snapshots.get(p.key) or []
        if len(snaps) >= 3:
            deltas = [b[1].total() - a[1].total() for a, b in zip(snaps, snaps[1:])]
            if deltas[-1] > 20 * max(statistics.median(deltas[:-1]), 1) and deltas[-1] > 5000:
                return True
    return False

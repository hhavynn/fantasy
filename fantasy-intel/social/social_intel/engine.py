"""Discover -> cluster -> enrich -> rank, for any sport adapter.

``collect`` runs the collectors (fault-isolated, cooldown-aware) and stores annotated posts.
``rank`` reads the recent window back, clusters it, enriches the strongest clusters with the
sport's analytics, scores everything and saves the ranking. Ideas are generated from a ranking by
``ideas.generate``.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from social_intel.collectors import REGISTRY, run_collectors
from social_intel.collectors.base import RunResult
from social_intel.contract import SocialPost, utcnow
from social_intel.core.cluster import ClusterParams, cluster_posts, stable_cluster_id
from social_intel.core.http import Fetcher
from social_intel.core.scoring import ATTENTION_PLATFORMS, ScoringContext, post_velocity, score_cluster
from social_intel.core.signals import text_signals
from social_intel.core.store import Store, content_hash

# frequent, fast sources first; slow daily ones (Wikipedia) last so they never delay the rest
DEFAULT_COLLECTORS = sorted((n for n, c in REGISTRY.items() if not c.optional), key=lambda n: REGISTRY[n].interval_minutes)


@dataclass
class Engine:
    store: Store
    adapter: Any  # SportAdapter
    fetcher: Fetcher | None = None
    history_path: Path | None = None  # studio episodes/history.jsonl: what we already made

    @property
    def sport(self) -> str:
        return self.adapter.sport

    # ------------------------------------------------------------------ collect

    def collect(self, names: list[str] | None = None, force: bool = False, now: datetime | None = None) -> list[RunResult]:
        cfg = self.adapter.source_config()
        if hasattr(self.adapter, "dynamic_config"):
            self.adapter.dynamic_config(cfg, self.store)
        fetcher = self.fetcher or Fetcher(cache_dir=self.store.path.parent / "http_cache")
        return run_collectors(self.store, fetcher, cfg, names or DEFAULT_COLLECTORS, annotate=self.adapter.annotate,
                              force=force, now=now)

    # ------------------------------------------------------------------ rank

    def rank(self, since_hours: float = 72, now: datetime | None = None, enrich_top: int = 40,
             params: ClusterParams | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        posts = self.store.recent_posts(self.sport, since_hours, now)
        for p in posts:  # the entity index may have improved since collection
            self.adapter.annotate(p)
        posts = [p for p in posts if self.adapter.is_relevant(p)]
        by_key = {p.key: p for p in posts}
        clusters = cluster_posts(posts, self.sport, params, label_fn=self.adapter.label,
                                 entity_terms=getattr(self.adapter, "name_tokens", None))

        covered, covered_entities = self._coverage()
        ctx = ScoringContext(
            now=now,
            baselines=self.store.community_baselines(self.sport),
            snapshots=self.store.snapshots(list(by_key)),
            covered=covered | self.store.used_fingerprints(),
            covered_entities=covered_entities,
            topic_weights=self.adapter.topic_weights(),
            entity_popularity=self.adapter.entity_popularity(),
        )
        hashes = {k: content_hash(p.text) for k, p in by_key.items()}
        ctx.first_seen_text = self.store.earliest_seen(list(set(hashes.values())))

        # cheap pre-score without enrichment, then enrich only the contenders
        for c in clusters:
            c.enrichment = {"_content_hashes": [hashes[k] for k in c.post_keys]}
            c.score = score_cluster(c, [by_key[k] for k in c.post_keys], ctx)
        clusters.sort(key=lambda c: c.score.total, reverse=True)
        for c in clusters[:enrich_top]:
            c.stance = stance([by_key[k] for k in c.post_keys], self.adapter.tag_topics, *self.adapter.direction_topics,
                             lead=(c.entities.get("players") or [None])[0])
            if c.entities.get("players"):
                c.enrichment.update(self.adapter.enrich(c))
                c.enrichment["status_cues"] = status_cues([by_key[k] for k in c.post_keys])
            c.score = score_cluster(c, [by_key[k] for k in c.post_keys], ctx)
        clusters.sort(key=lambda c: c.score.total, reverse=True)

        out = []
        for c in clusters:
            ps = [by_key[k] for k in c.post_keys]
            c.top_posts = top_posts(ps, ctx)
            if not c.stance:
                c.stance = stance(ps, self.adapter.tag_topics, *self.adapter.direction_topics,
                                  lead=(c.entities.get("players") or [None])[0])
            c.enrichment.pop("_content_hashes", None)
            d = c.to_dict()
            d["topic_id"] = stable_cluster_id(c)
            d["entity_names"] = {pid: e.name for pid in (c.entities.get("players") or [])[:5]
                                 if (e := self.adapter.entity(pid))}
            out.append(d)
        self.store.save_ranking(uuid.uuid4().hex[:12], self.sport, out)
        return out

    def _coverage(self) -> tuple[set[str], dict[str, str]]:
        """Players featured in the studio's recent videos (episodes/history.jsonl)."""
        covered_entities: dict[str, str] = {}
        if self.history_path and self.history_path.exists():
            rows = [json.loads(line) for line in self.history_path.read_text().splitlines() if line.strip()]
            for r in rows[-12:]:
                for pid in r.get("players", [])[:1] if r.get("format") == "long" else r.get("players", []):
                    covered_entities[pid] = r.get("episode_id", "")
        return set(), covered_entities


RETURN_CUE = re.compile(r"\bactivat|\boff (the )?(ir|injured reserve)\b|\bcleared\b|\bwill play\b|\bexpected to (play|start)\b|"
                        r"\bto start\b|\breturns?\b|\bback at practice\b", re.I)
OUT_CUE = re.compile(r"\bplaced\b[^.]{0,40}\bon (injured reserve|ir)\b|\bto ir\b|\bruled out\b|\bwill miss\b|\bout for\b|"
                     r"\bwon'?t play\b|\bwithout\b", re.I)


def status_cues(ps: list[SocialPost]) -> dict[str, Any]:
    """What reporters (news + X) say about availability, newer than our weekly roster/injury tables.
    Used to catch a stale 'out' before an idea says it out loud."""
    ret, out, links = 0, 0, []
    for p in ps:
        if p.platform not in ("news", "x"):
            continue
        r, o = bool(RETURN_CUE.search(p.text)), bool(OUT_CUE.search(p.text))
        ret, out = ret + (r and not o), out + (o and not r)
        if r and not o and len(links) < 3:
            links.append(p.url)
    return {"return": ret, "out": out, "return_links": links}


def top_posts(ps: list[SocialPost], ctx: ScoringContext, n: int = 6) -> list[dict[str, Any]]:
    def weight(p: SocialPost) -> float:
        w = post_velocity(p, ctx) + p.engagement.total() * 0.05
        return w + (5 if p.platform == "news" else 0)

    seen, out = set(), []
    for p in sorted(ps, key=weight, reverse=True):
        h = content_hash(p.text)
        if h in seen:
            continue
        seen.add(h)
        e = p.engagement
        out.append({
            "platform": p.platform, "url": p.url, "community": p.community, "author": p.author,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "text": (p.title or p.text)[:240],
            "engagement": {k: v for k, v in (("likes", e.likes), ("comments", e.comments), ("shares", e.shares),
                                             ("views", e.views)) if v is not None},
            "comment_rate_per_h": p.extra.get("comment_rate_per_h"),
            "attention_only": p.platform in ATTENTION_PLATFORMS,
            "direction": p.extra.get("direction"),
            "subject": p.extra.get("subject"),
            "method": p.collection_method,
        })
        if len(out) >= n:
            break
    return out


def stance(ps: list[SocialPost], tagger: Callable[[str], list[str]], positive: set[str],
           negative: set[str], lead: str | None = None) -> dict[str, Any]:
    """Who is saying what: posts/comments leaning positive vs negative on the lead entity, and the
    most argumentative lines (verbatim excerpts, linked) for the 'what the internet says' beat."""
    pro, con, claims = 0, 0, []
    texts: list[tuple[str, str]] = []
    for p in ps:
        direction = p.extra.get("direction")  # Sleeper trending add/drop is a vote, not text
        if direction in ("add", "drop"):
            if lead and p.extra.get("gsis_id") == lead:  # a vote on the backup is not a vote on the starter
                pro, con = pro + (direction == "add"), con + (direction == "drop")
            continue
        if p.platform in ATTENTION_PLATFORMS:
            continue
        texts.append((p.title or p.text, p.url))
        texts.extend((t, p.url) for t in p.extra.get("sample_comments", [])[:4])
    for t, url in texts:
        topics = set(tagger(t))
        if topics & positive and not topics & negative:
            pro += 1
        elif topics & negative and not topics & positive:
            con += 1
        s = text_signals(t)
        if (s.debate or s.contrarian) and not s.sarcasm and 25 <= len(t) <= 280:
            claims.append({"text": t.strip().replace("\n", " ")[:220], "url": url})
    return {"positive": pro, "negative": con, "claims": claims[:6],
            "balance": round(min(pro, con) / max(pro, con), 2) if max(pro, con) else 0.0}

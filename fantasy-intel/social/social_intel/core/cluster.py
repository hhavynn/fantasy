"""Group posts about the same underlying topic.

Two posts belong together when they have the same subject player (the first player named), when
they share a team anchor AND their text is close, or when their text is near-identical regardless of
entities (the same take reposted). Similarity is cosine over TF-IDF vectors of word unigrams+bigrams plus
entity tokens, so "Dart is a league winner" and "Jaxson Dart breakout?" land together while two
unrelated Dart posts (injury vs. trade) can stay apart.

An optional embedding backend (``model2vec`` static embeddings: small, CPU-only, no torch) is used
when installed; TF-IDF is the always-available default.

Reposts/crossposts/identical text are collapsed before counting ``independent_sources``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from social_intel.contract import SocialPost, TrendCluster
from social_intel.core.store import content_hash

STOP = set("""
a an the and or but if then so to of in on at for with by from as is are was were be been being it its this that
these those i you he she we they me him her us them my your his our their what which who whom how why when where
just like get got going go gonna can could would should will do does did done have has had not no yes than too very
all any some more most much many one two three im ive dont doesnt didnt isnt arent thats theres hes shes its it's
about after before over under up down out off into again still even also only really now week game games team
nfl football fantasy https http www com reddit amp
""".split())
_TOKEN = re.compile(r"[a-z][a-z0-9']+")


def tokens(text: str) -> list[str]:
    toks = [t.strip("'") for t in _TOKEN.findall(text.lower())]
    toks = [t for t in toks if t not in STOP and len(t) > 2]
    return toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]


def tfidf_matrix(docs: Sequence[list[str]]) -> np.ndarray:
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    vocab = {t: i for i, t in enumerate(t for t, c in df.items() if c >= 1)}
    n = len(docs)
    m = np.zeros((n, len(vocab)), dtype=np.float32)
    for i, d in enumerate(docs):
        tf = Counter(d)
        for t, c in tf.items():
            j = vocab.get(t)
            if j is not None:
                m[i, j] = (1 + math.log(c)) * math.log((1 + n) / (1 + df[t]) + 1)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return m / norms


def _embedder() -> Callable[[list[str]], np.ndarray] | None:
    try:  # optional, never required
        from model2vec import StaticModel  # type: ignore
    except Exception:
        return None
    try:
        model = StaticModel.from_pretrained("minishlab/potion-base-8M")
    except Exception:
        return None

    def embed(texts: list[str]) -> np.ndarray:
        v = np.asarray(model.encode(texts), dtype=np.float32)
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        return v

    return embed


@dataclass
class ClusterParams:
    sim_with_entity: float = 0.3  # shared anchor entity + real text overlap (multi-entity posts)
    sim_without_entity: float = 0.55  # no shared entity: must be near-duplicate text
    use_embeddings: bool = False
    min_posts: int = 1


class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


MIN_TOKENS = 6  # content tokens (unigrams + bigrams) a post needs before text similarity alone can join it
ROUNDUP_PLAYERS = 4  # a post naming this many players is a rankings/start-sit roundup, not one story


def is_roundup(p: SocialPost) -> bool:
    return len(p.entities.get("players") or []) >= ROUNDUP_PLAYERS


TEMPLATED_PLATFORMS = {"sleeper", "wikipedia", "google_trends"}


def is_templated(p: SocialPost) -> bool:
    """Machine-written signal lines (attention sources): they join clusters only through entities."""
    return p.platform in TEMPLATED_PLATFORMS or bool(p.extra.get("templated"))


def anchors(p: SocialPost) -> set[str]:
    players = p.entities.get("players") or []
    if len(players) >= ROUNDUP_PLAYERS:
        return set()  # roundups join nothing by entity: they would chain every player into one blob
    if players:
        # the first-named player is the subject ("Hurts to IR, Eagles sign Blue"); anchoring on the subject
        # only means a post joins one narrative and can never bridge two players' clusters
        return {f"p:{players[0]}"}
    return {f"t:{x}" for x in (p.entities.get("teams") or [])[:1]}


def cluster_posts(posts: list[SocialPost], sport: str, params: ClusterParams | None = None,
                  label_fn: Callable[[TrendCluster], str] | None = None,
                  entity_terms: Callable[[str], set[str]] | None = None) -> list[TrendCluster]:
    """``entity_terms`` maps an entity id to its name tokens; they are removed before measuring text
    similarity, so two posts are "about the same thing" because of what they say, not because both
    spell out the same player's name (entity identity is handled by the anchors)."""
    params = params or ClusterParams()
    if not posts:
        return []
    docs = []
    for p in posts:
        toks = tokens(p.text)
        if entity_terms:
            names: set[str] = set()
            for ids in p.entities.values():
                for eid in ids:
                    names |= entity_terms(eid)
            toks = [t for t in toks if not any(part in names for part in t.split("_"))]
        docs.append(toks)
    sims = tfidf_matrix(docs)
    sim = sims @ sims.T
    if params.use_embeddings and (embed := _embedder()):
        e = embed([p.text[:500] for p in posts])
        sim = 0.5 * sim + 0.5 * (e @ e.T)

    n = len(posts)
    dsu = _DSU(n)
    anc = [anchors(p) for p in posts]
    by_anchor: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(anc):
        for x in a:
            by_anchor[x].append(i)
    # posts with the same subject player always join; posts anchored only on a team need text overlap
    for anchor, idxs in by_anchor.items():
        for a_i, i in enumerate(idxs):
            for j in idxs[a_i + 1:]:
                if anchor.startswith("p:") or sim[i, j] >= params.sim_with_entity:
                    dsu.union(i, j)
    # near-duplicates without an entity in common, and reposts / identical text
    hashes: dict[str, int] = {}
    ext: dict[str, int] = {}
    for i, p in enumerate(posts):
        ext[p.external_id] = i
        if is_templated(p):
            continue
        h = content_hash(p.text)
        if h in hashes:
            dsu.union(hashes[h], i)
        else:
            hashes[h] = i
    for i, p in enumerate(posts):
        if p.parent_id:
            parent = ext.get(p.parent_id.removeprefix("t3_"))
            if parent is not None:
                dsu.union(parent, i)
    hi = np.argwhere(np.triu(sim, 1) >= params.sim_without_entity)
    roundup = [is_roundup(p) for p in posts]
    templated = [is_templated(p) for p in posts]
    for i, j in hi:
        i, j = int(i), int(j)
        if templated[i] or templated[j]:
            continue  # "X trending add: added in N leagues" lines are alike by construction
        if (roundup[i] or roundup[j]) and sim[i, j] < 0.85:
            continue  # two different weekly roundups share boilerplate, not a topic
        if min(len(docs[i]), len(docs[j])) < MIN_TOKENS:
            continue  # "<name> ruled out" vs "<name> cleared": with names removed, short headlines look alike
        if anc[i] and anc[j] and anc[i] != anc[j] and sim[i, j] < 0.8:
            continue  # different subjects need near-verbatim text to be one story
        dsu.union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)

    clusters = []
    for members in groups.values():
        if len(members) < params.min_posts:
            continue
        ps = [posts[i] for i in members]
        clusters.append(_build(ps, sport, docs, members, label_fn))
    return clusters


def independent_sources(posts: list[SocialPost]) -> int:
    """Distinct (author or community) voices after collapsing reposts and identical text."""
    seen_text: set[str] = set()
    voices: set[str] = set()
    for p in posts:
        if p.is_repost:
            continue
        h = content_hash(p.text)
        if h in seen_text:
            continue
        seen_text.add(h)
        voices.add(f"{p.platform}:{(p.author or p.community or p.external_id).lower()}")
    return max(len(voices), 1 if posts else 0)


def _build(ps: list[SocialPost], sport: str, docs, members, label_fn) -> TrendCluster:
    ent_counts: dict[str, Counter[str]] = defaultdict(Counter)
    topic_counts: Counter[str] = Counter()
    kw: Counter[str] = Counter()
    for p, i in zip(ps, members):
        for kind, ids in p.entities.items():
            ent_counts[kind].update(set(ids))
        topic_counts.update(set(p.topics))
        kw.update(t for t in set(docs[i]) if "_" not in t)
    times = [p.created_at or p.collected_at for p in ps]
    key_src = "|".join(sorted(p.key for p in ps))
    entities = {k: [e for e, _ in c.most_common()] for k, c in ent_counts.items()}
    c = TrendCluster(
        cluster_id=hashlib.sha1(key_src.encode()).hexdigest()[:12],
        label="",
        sport=sport,
        post_keys=[p.key for p in ps],
        platforms=sorted({p.platform for p in ps}),
        independent_sources=independent_sources(ps),
        first_seen=min(times),
        last_seen=max(times),
        entities=entities,
        topics=[t for t, _ in topic_counts.most_common()],
        keywords=[w for w, _ in kw.most_common(8)],
    )
    c.label = label_fn(c) if label_fn else (", ".join(c.keywords[:3]) or "misc")
    return c


def stable_cluster_id(cluster: TrendCluster) -> str:
    """Topic identity that survives new posts joining: anchor entity + dominant topic. Used for
    novelty ("did we already cover this?") rather than the post-set hash."""
    ent = (cluster.entities.get("players") or cluster.entities.get("teams") or ["-"])[0]
    topic = cluster.topics[0] if cluster.topics else "general"
    return f"{cluster.sport}:{ent}:{topic}"

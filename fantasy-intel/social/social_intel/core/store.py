"""DuckDB storage for collected posts, engagement history, source health and ranked output.

Lives in its own file (``data/social.duckdb`` by default) so the analytics database never needs a
migration and a collector run can never lock the API's database. Enrichment attaches the analytics
database read-only.

Posts are upserted by ``key`` (platform + external id): the text/metadata row keeps the first and
latest collection times, and every collection appends one ``engagement_snapshots`` row, which is
what velocity scoring reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb

from social_intel.contract import Engagement, SocialPost, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR);
CREATE TABLE IF NOT EXISTS posts (
    key VARCHAR PRIMARY KEY,
    sport VARCHAR,
    platform VARCHAR NOT NULL,
    external_id VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    created_at TIMESTAMPTZ,
    first_collected_at TIMESTAMPTZ NOT NULL,
    last_collected_at TIMESTAMPTZ NOT NULL,
    title VARCHAR,
    text VARCHAR,
    author VARCHAR,
    community VARCHAR,
    hashtags VARCHAR[],
    collection_method VARCHAR,
    confidence DOUBLE,
    lang VARCHAR,
    parent_id VARCHAR,
    is_repost BOOLEAN,
    content_hash VARCHAR,
    extra JSON,
    entities JSON,
    topics VARCHAR[]
);
CREATE TABLE IF NOT EXISTS engagement_snapshots (
    key VARCHAR NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    likes BIGINT, comments BIGINT, shares BIGINT, views BIGINT, upvote_ratio DOUBLE
);
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id VARCHAR, collector VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    status VARCHAR, posts INTEGER, new_posts INTEGER, requests INTEGER, error VARCHAR, blocked BOOLEAN
);
CREATE TABLE IF NOT EXISTS source_state (
    collector VARCHAR PRIMARY KEY, last_success TIMESTAMPTZ, last_attempt TIMESTAMPTZ,
    consecutive_failures INTEGER, cooldown_until TIMESTAMPTZ, cursor JSON
);
CREATE TABLE IF NOT EXISTS ranked_clusters (
    rank_run VARCHAR, generated_at TIMESTAMPTZ, sport VARCHAR, cluster_id VARCHAR, score DOUBLE, body JSON
);
CREATE TABLE IF NOT EXISTS ideas (
    idea_id VARCHAR PRIMARY KEY, cluster_id VARCHAR, sport VARCHAR, generated_at TIMESTAMPTZ,
    status VARCHAR, fingerprint VARCHAR, body JSON
);
"""

_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")


def content_hash(text: str) -> str:
    """Hash of normalised text: catches the same take cross-posted or reposted with a new link."""
    t = _URL.sub("", text.lower())
    t = re.sub(r"[^a-z0-9 ]", "", _WS.sub(" ", t)).strip()
    return hashlib.sha1(t[:280].encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = self._connect()
        self.con.execute(SCHEMA)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(SCHEMA_VERSION)])

    def _connect(self, retries: int = 20) -> duckdb.DuckDBPyConnection:
        err: Exception | None = None
        for _ in range(retries):
            try:
                return duckdb.connect(str(self.path))
            except duckdb.IOException as e:  # another process holds the write lock
                err = e
                time.sleep(0.25)
        raise RuntimeError(f"social database busy: {err}")

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def tx(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self.con.execute("BEGIN")
        try:
            yield self.con
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    # ---------------------------------------------------------------- posts

    def upsert_posts(self, posts: list[SocialPost], sport: str) -> int:
        """Insert new posts, refresh mutable fields on known ones, append an engagement snapshot for
        each. Returns how many posts were new."""
        if not posts:
            return 0
        # one row per key per batch (a post can appear in hot + rising): keep the latest engagement
        uniq: dict[str, SocialPost] = {}
        for p in posts:
            uniq[p.key] = p
        keys = list(uniq)
        known = {r[0] for r in self.con.execute(
            "SELECT key FROM posts WHERE key IN (SELECT unnest(?))", [keys]).fetchall()}
        new = 0
        with self.tx() as con:
            for p in uniq.values():
                e = p.engagement
                if p.key in known:
                    # sources complement each other (RSS has timestamps, the archive has comment
                    # activity): fill gaps, keep the longer text, merge extras
                    con.execute(
                        """UPDATE posts SET last_collected_at = ?,
                             text = CASE WHEN length(?) > length(coalesce(text, '')) THEN ? ELSE text END,
                             title = coalesce(title, ?), created_at = coalesce(created_at, ?),
                             author = coalesce(author, ?), community = coalesce(community, ?),
                             entities = ?, topics = ?, extra = json_merge_patch(coalesce(extra, '{}'), ?)
                           WHERE key = ?""",
                        [p.collected_at, p.text, p.text, p.title, p.created_at, p.author, p.community,
                         json.dumps(p.entities), p.topics, json.dumps(p.extra, default=str), p.key])
                else:
                    new += 1
                    con.execute(
                        "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [p.key, sport, p.platform, p.external_id, p.url, p.created_at, p.collected_at,
                         p.collected_at, p.title, p.text, p.author, p.community, p.hashtags,
                         p.collection_method, p.confidence, p.lang, p.parent_id, p.is_repost,
                         content_hash(p.text), json.dumps(p.extra, default=str), json.dumps(p.entities),
                         p.topics])
                if any(v is not None for v in (e.likes, e.comments, e.shares, e.views)):
                    con.execute("INSERT INTO engagement_snapshots VALUES (?,?,?,?,?,?,?)",
                                [p.key, p.collected_at, e.likes, e.comments, e.shares, e.views, e.upvote_ratio])
        return new

    def update_annotations(self, key: str, entities: dict, topics: list[str]) -> None:
        self.con.execute("UPDATE posts SET entities = ?, topics = ? WHERE key = ?",
                         [json.dumps(entities), topics, key])

    def recent_posts(self, sport: str, since_hours: float = 72, now: datetime | None = None) -> list[SocialPost]:
        """Posts created (or, if the source has no timestamp, first seen) in the window, each with its
        latest engagement."""
        now = now or utcnow()
        cutoff = now - timedelta(hours=since_hours)
        rows = self.con.execute(
            """
            WITH latest AS (
                SELECT key, arg_max(likes, collected_at) AS likes, arg_max(comments, collected_at) AS comments,
                       arg_max(shares, collected_at) AS shares, arg_max(views, collected_at) AS views,
                       arg_max(upvote_ratio, collected_at) AS upvote_ratio
                FROM engagement_snapshots GROUP BY key
            )
            SELECT p.platform, p.external_id, p.url, p.created_at, p.last_collected_at, p.text, p.author,
                   p.community, p.title, p.hashtags, p.collection_method, p.confidence, p.lang, p.parent_id,
                   p.is_repost, p.extra, p.entities, p.topics,
                   l.likes, l.comments, l.shares, l.views, l.upvote_ratio
            FROM posts p LEFT JOIN latest l USING (key)
            WHERE p.sport = ? AND coalesce(p.created_at, p.first_collected_at) >= ?
            """,
            [sport, cutoff]).fetchall()
        out = []
        for r in rows:
            out.append(SocialPost(
                platform=r[0], external_id=r[1], url=r[2], created_at=_tz(r[3]), collected_at=_tz(r[4]),
                text=r[5] or "", author=r[6], community=r[7], title=r[8], hashtags=list(r[9] or []),
                collection_method=r[10] or "", confidence=r[11] if r[11] is not None else 1.0, lang=r[12],
                parent_id=r[13], is_repost=bool(r[14]), extra=json.loads(r[15]) if r[15] else {},
                entities=json.loads(r[16]) if r[16] else {}, topics=list(r[17] or []),
                engagement=Engagement(likes=r[18], comments=r[19], shares=r[20], views=r[21], upvote_ratio=r[22]),
            ))
        return out

    def snapshots(self, keys: list[str]) -> dict[str, list[tuple[datetime, Engagement]]]:
        if not keys:
            return {}
        rows = self.con.execute(
            """SELECT key, collected_at, likes, comments, shares, views, upvote_ratio FROM engagement_snapshots
               WHERE key IN (SELECT unnest(?)) ORDER BY key, collected_at""", [keys]).fetchall()
        out: dict[str, list[tuple[datetime, Engagement]]] = {}
        for k, t, *e in rows:
            out.setdefault(k, []).append((_tz(t), Engagement(*e)))
        return out

    def community_baselines(self, sport: str, days: int = 14) -> dict[tuple[str, str], float]:
        """Median latest interaction total per (platform, community) over the last ``days``: the
        yardstick for "unusually popular for where it was posted"."""
        rows = self.con.execute(
            """
            WITH latest AS (
                SELECT key, arg_max(coalesce(likes,0) + 3*coalesce(comments,0) + 2*coalesce(shares,0)
                                    + 0.01*coalesce(views,0), collected_at) AS total
                FROM engagement_snapshots GROUP BY key
            )
            SELECT p.platform, coalesce(p.community, ''), median(l.total), count(*)
            FROM posts p JOIN latest l USING (key)
            WHERE p.sport = ? AND p.first_collected_at >= now() - to_days(?)
            GROUP BY ALL
            """, [sport, days]).fetchall()
        return {(r[0], r[1]): float(r[2]) for r in rows if r[3] >= 5 and r[2] is not None}

    def known_content_hashes(self, sport: str) -> dict[str, list[str]]:
        rows = self.con.execute(
            "SELECT content_hash, list(key ORDER BY first_collected_at) FROM posts WHERE sport = ? GROUP BY 1",
            [sport]).fetchall()
        return {h: ks for h, ks in rows}

    def earliest_seen(self, content_hashes: list[str]) -> dict[str, datetime]:
        """First time we ever saw each normalised text: recycled rumours resurface with old hashes."""
        if not content_hashes:
            return {}
        rows = self.con.execute(
            """SELECT content_hash, min(coalesce(created_at, first_collected_at)) FROM posts
               WHERE content_hash IN (SELECT unnest(?)) GROUP BY 1""", [content_hashes]).fetchall()
        return {h: _tz(t) for h, t in rows}

    # ---------------------------------------------------------------- source health

    def record_run(self, run_id: str, collector: str, started: datetime, status: str, posts: int,
                   new_posts: int, requests: int, error: str | None, blocked: bool) -> None:
        now = utcnow()
        self.con.execute("INSERT INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [run_id, collector, started, now, status, posts, new_posts, requests, error, blocked])
        prev = self.con.execute("SELECT consecutive_failures, cursor FROM source_state WHERE collector = ?",
                                [collector]).fetchone()
        fails = 0 if status == "ok" else (prev[0] if prev else 0) + 1
        # back off a failing source: 15 min, 30, 60 ... capped at 12 h; a block cools down longer
        cooldown = None
        if fails:
            minutes = min(15 * 2 ** (fails - 1) * (3 if blocked else 1), 720)
            cooldown = now + timedelta(minutes=minutes)
        last_success = now if status == "ok" else None
        self.con.execute(
            """INSERT INTO source_state VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (collector) DO UPDATE SET
                 last_success = coalesce(excluded.last_success, source_state.last_success),
                 last_attempt = excluded.last_attempt, consecutive_failures = excluded.consecutive_failures,
                 cooldown_until = excluded.cooldown_until""",
            [collector, last_success, now, fails, cooldown, prev[1] if prev else None])

    def get_cursor(self, collector: str) -> dict[str, Any]:
        row = self.con.execute("SELECT cursor FROM source_state WHERE collector = ?", [collector]).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def set_cursor(self, collector: str, cursor: dict[str, Any]) -> None:
        self.con.execute(
            """INSERT INTO source_state (collector, consecutive_failures, cursor) VALUES (?, 0, ?)
               ON CONFLICT (collector) DO UPDATE SET cursor = excluded.cursor""",
            [collector, json.dumps(cursor)])

    def source_state(self, collector: str) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT last_success, last_attempt, consecutive_failures, cooldown_until FROM source_state "
            "WHERE collector = ?", [collector]).fetchone()
        if not row:
            return None
        return {"last_success": _tz(row[0]), "last_attempt": _tz(row[1]), "consecutive_failures": row[2] or 0,
                "cooldown_until": _tz(row[3])}

    def health(self) -> list[dict[str, Any]]:
        rows = self.con.execute(
            """
            SELECT s.collector, s.last_success, s.last_attempt, s.consecutive_failures, s.cooldown_until,
                   (SELECT status FROM collection_runs r WHERE r.collector = s.collector
                     ORDER BY finished_at DESC LIMIT 1),
                   (SELECT error FROM collection_runs r WHERE r.collector = s.collector
                     ORDER BY finished_at DESC LIMIT 1),
                   (SELECT sum(posts) FROM collection_runs r WHERE r.collector = s.collector
                     AND r.finished_at >= now() - INTERVAL 24 HOUR),
                   (SELECT count(*) FILTER (WHERE status = 'ok') / greatest(count(*), 1)
                     FROM collection_runs r WHERE r.collector = s.collector
                     AND r.finished_at >= now() - INTERVAL 7 DAY)
            FROM source_state s ORDER BY 1
            """).fetchall()
        keys = ["collector", "last_success", "last_attempt", "consecutive_failures", "cooldown_until",
                "last_status", "last_error", "posts_24h", "success_rate_7d"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("last_success", "last_attempt", "cooldown_until"):
                d[k] = _tz(d[k])
            out.append(d)
        return out

    # ---------------------------------------------------------------- ranked output and ideas

    def save_ranking(self, rank_run: str, sport: str, clusters: list[dict[str, Any]]) -> None:
        now = utcnow()
        with self.tx() as con:
            for c in clusters:
                con.execute("INSERT INTO ranked_clusters VALUES (?,?,?,?,?,?)",
                            [rank_run, now, sport, c["cluster_id"], c["score"]["total"], json.dumps(c, default=str)])

    def latest_ranking(self, sport: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            """SELECT body FROM ranked_clusters WHERE rank_run = (
                 SELECT rank_run FROM ranked_clusters WHERE sport = ? ORDER BY generated_at DESC LIMIT 1)
               ORDER BY score DESC""", [sport]).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_ideas(self, ideas: list[dict[str, Any]], fingerprints: dict[str, str]) -> None:
        now = utcnow()
        with self.tx() as con:
            for i in ideas:
                con.execute(
                    """INSERT INTO ideas VALUES (?,?,?,?,'new',?,?)
                       ON CONFLICT (idea_id) DO UPDATE SET body = excluded.body, generated_at = excluded.generated_at""",
                    [i["idea_id"], i["cluster_id"], i["sport"], now, fingerprints.get(i["idea_id"], ""),
                     json.dumps(i, default=str)])

    def ideas(self, sport: str, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT body, status FROM ideas WHERE sport = ?"
        args: list[Any] = [sport]
        if status:
            q += " AND status = ?"
            args.append(status)
        rows = self.con.execute(q + " ORDER BY generated_at DESC", args).fetchall()
        return [{**json.loads(b), "status": s} for b, s in rows]

    def get_idea(self, idea_id: str) -> dict[str, Any] | None:
        row = self.con.execute("SELECT body, status FROM ideas WHERE idea_id = ? OR idea_id LIKE ?",
                               [idea_id, idea_id + "%"]).fetchone()
        return {**json.loads(row[0]), "status": row[1]} if row else None

    def set_idea_status(self, idea_id: str, status: str) -> None:
        self.con.execute("UPDATE ideas SET status = ? WHERE idea_id = ?", [status, idea_id])

    def used_fingerprints(self) -> set[str]:
        """Topic fingerprints of ideas already sent to production."""
        return {r[0] for r in self.con.execute(
            "SELECT fingerprint FROM ideas WHERE status IN ('selected', 'sent') AND fingerprint <> ''").fetchall()}


def _tz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

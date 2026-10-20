"""Publish one league's files and move its fenced pointer (multi-league Phase 2).

    take the next fence -> split the analytics database -> upload global/<gv>.duckdb (skipped when that exact
    content is already there) -> upload leagues/<lg>/<dv>.duckdb -> one fi_sync transaction: fenced upsert of
    fi.league_snapshots, history row, keep three -> delete objects no pointer or history references.

The pointer moves only through the spec's fenced upsert (§3.4). The fence is reserved when the publish starts, so of
two overlapping publishers the one that started later wins whichever finishes first; a publisher holding an older
fence than the one already published changes nothing visible and deletes its own object. Readers see the old pair of files or the new
pair, never a mix. Runs as `fi_sync` (never the service role); prints ids, versions and counts only.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from . import r2
from .connection import utcnow
from .league_files import global_key, league_key
from .league_split import content_hash, split_database

KEEP = 3
GLOBAL_STAMP = "content-addressed"        # the global file's internal stamp: identical content, identical bytes
GLOBAL_GRACE = timedelta(days=1)          # an unreferenced global object younger than this is never collected

_UPSERT = """
INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version, committed_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (league_id) DO UPDATE
  SET object_key = EXCLUDED.object_key, data_version = EXCLUDED.data_version, fence = EXCLUDED.fence,
      global_version = EXCLUDED.global_version, committed_at = EXCLUDED.committed_at
  WHERE fi.league_snapshots.fence < EXCLUDED.fence
RETURNING league_id
"""


class ObjectWriter(Protocol):
    def put(self, key: str, path: Path) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class R2Writer:
    """The bucket, with a read/write token (laptop or GitHub Actions only; the API's token is read-only)."""

    def __init__(self, cfg: r2.R2Config):
        self.cfg = cfg

    def put(self, key: str, path: Path) -> None:
        r2.put_file(self.cfg, key, path)

    def keys(self, prefix: str) -> list[str]:
        return r2.list_keys(self.cfg, prefix)

    def delete(self, key: str) -> None:
        r2.delete(self.cfg, key)


class DirWriter:
    """A local object directory (LEAGUE_FILES_DIR) with the same layout, for development."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, key: str, path: Path) -> None:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(Path(path).read_bytes())
        tmp.replace(dest)

    def keys(self, prefix: str) -> list[str]:
        base = self.root / prefix
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and not p.name.endswith(".part")) \
            if base.exists() else []

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


@dataclass(frozen=True)
class PublishResult:
    league_id: str
    data_version: str
    global_version: str
    fence: int
    applied: bool          # False: a newer publish already holds the pointer; nothing visible changed
    deleted: tuple[str, ...]


def publish_league(src_db: Path, writer: ObjectWriter, sync_con, league_id: str, *, stamp: str | None = None,
                   keep: int = KEEP) -> PublishResult:
    """Publish `src_db` (one league's analytics database, the laptop path) as `league_id`, and its global tables as
    the current global file. `sync_con` is a psycopg connection as fi_sync; this function commits on it."""
    with sync_con.transaction():     # reserve the fence first: a publish that starts later always holds a larger one
        row = sync_con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s AND deleted_at IS NULL "
                               "RETURNING sync_fence", [league_id]).fetchone()
    if row is None:
        raise ValueError(f"league {league_id} is not registered (or is deleted) in the control plane")
    fence = row[0]
    stamp = stamp or utcnow().isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as tmp:
        out = split_database(Path(src_db), Path(tmp) / "league.duckdb", Path(tmp) / "global.duckdb", stamp=stamp,
                             global_stamp=GLOBAL_STAMP)
        global_version = publish_global(out.global_path, out.global_version, writer, sync_con)
        applied, deleted = commit_league(writer, sync_con, league_id, out.league_path, out.league_version,
                                         global_version, fence, keep=keep)
    return PublishResult(league_id, out.league_version, global_version, fence, applied, deleted)


def publish_global(global_path: Path, global_version: str, writer: ObjectWriter, sync_con) -> str:
    """Make `global_path` the current global file (fi.global_snapshot) and return the version to reference. When its
    content equals the current global file's, nothing is uploaded and the current version is returned."""
    digest = content_hash(global_path)
    current = sync_con.execute("SELECT global_version, content_hash FROM fi.global_snapshot").fetchone()
    sync_con.rollback()
    if current is not None and current[1] == digest:
        return current[0]
    writer.put(global_key(global_version), global_path)
    with sync_con.transaction():
        sync_con.execute("""INSERT INTO fi.global_snapshot (global_version, content_hash) VALUES (%s, %s)
                            ON CONFLICT (id) DO UPDATE SET global_version = EXCLUDED.global_version,
                              content_hash = EXCLUDED.content_hash, committed_at = now()""", [global_version, digest])
    return global_version


def commit_league(writer: ObjectWriter, sync_con, league_id: str, league_path: Path, league_version: str,
                  global_version: str, fence: int, *, keep: int = KEEP) -> tuple[bool, tuple[str, ...]]:
    """Upload the league file and move the pointer with the spec's fenced upsert (§3.4), holding `fence` (reserved
    before the build: by publish_league, or by a sync job's claim). Returns (applied, collected keys); not applied
    means a newer publish holds the pointer and this object was deleted again."""
    lkey = league_key(league_id, league_version)
    writer.put(lkey, league_path)
    with sync_con.transaction():
        applied = sync_con.execute(_UPSERT, [league_id, lkey, league_version, fence, global_version]).fetchone()
        if applied is not None:
            sync_con.execute("INSERT INTO fi.league_snapshot_history (league_id, object_key, data_version, fence, "
                             "global_version, committed_at) VALUES (%s, %s, %s, %s, %s, now())",
                             [league_id, lkey, league_version, fence, global_version])
            sync_con.execute("""DELETE FROM fi.league_snapshot_history WHERE league_id = %s AND id NOT IN (
                                  SELECT id FROM fi.league_snapshot_history WHERE league_id = %s
                                  ORDER BY fence DESC, id DESC LIMIT %s)""", [league_id, league_id, keep])
    deleted = _collect(writer, sync_con, league_id, stale=None if applied is not None else lkey)
    return applied is not None, deleted


def _stamp(key: str) -> str:
    return key.rsplit("/", 1)[-1].split("-", 1)[0]


def _collect(writer: ObjectWriter, sync_con, league_id: str, *, stale: str | None = None) -> tuple[str, ...]:
    """Delete this league's objects that no pointer or history row references, and global objects no league
    references (the newest global object is always kept). An unreferenced league object stamped after the current
    pointer may belong to a newer publish whose pointer has not moved yet: kept, except `stale`, the object of this
    publish when it lost to a newer fence."""
    with sync_con.transaction():
        league_refs = {r[0] for r in sync_con.execute(
            "SELECT object_key FROM fi.league_snapshots WHERE league_id = %s UNION "
            "SELECT object_key FROM fi.league_snapshot_history WHERE league_id = %s", [league_id, league_id])}
        current = sync_con.execute("SELECT object_key FROM fi.league_snapshots WHERE league_id = %s",
                                   [league_id]).fetchone()
        global_refs = {global_key(r[0]) for r in sync_con.execute(
            "SELECT global_version FROM fi.league_snapshots UNION SELECT global_version FROM fi.league_snapshot_history "
            "UNION SELECT global_version FROM fi.global_snapshot")}
    deleted = []
    newest = _stamp(current[0]) if current else ""
    for key in writer.keys(f"leagues/{league_id}/"):
        if key not in league_refs and (key == stale or _stamp(key) < newest):
            writer.delete(key)
            deleted.append(key)
    # A version's raw payloads (sync jobs, app/sync/publish.py) live and die with its league object.
    for key in writer.keys(f"raw/{league_id}/"):
        version = key.rsplit("/", 1)[-1].removesuffix(".json.gz")
        lkey = league_key(league_id, version)
        if lkey not in league_refs and (lkey == stale or _stamp(lkey) < newest):
            writer.delete(key)
            deleted.append(key)
    # A global object another publisher has just uploaded is not referenced until its pointer moves; keep every
    # unreferenced global younger than GLOBAL_GRACE (by the stamp in its name) and always the newest one.
    cutoff = (utcnow() - GLOBAL_GRACE).strftime("%Y%m%dT%H%M%SZ")
    globals_ = sorted(writer.keys("global/"))
    for key in globals_[:-1]:
        if key not in global_refs and _stamp(key) < cutoff:
            writer.delete(key)
            deleted.append(key)
    return tuple(deleted)

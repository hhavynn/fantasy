"""Publish one league's files and move its fenced pointer (multi-league Phase 2).

    split the analytics database -> upload global/<gv>.duckdb (skipped when that exact content is already there)
    -> upload leagues/<lg>/<dv>.duckdb -> one fi_sync transaction: take the next fence, fenced upsert of
    fi.league_snapshots, history row, keep three -> delete objects no pointer or history references.

The pointer moves only through the spec's fenced upsert (§3.4): a publisher holding an older fence than the one
already published changes nothing visible and its object is collected. Readers see the old pair of files or the new
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
from .league_split import SplitResult, split_database

KEEP = 3
GLOBAL_STAMP = "content-addressed"
GLOBAL_GRACE = timedelta(days=1)          # an unreferenced global object younger than this is never collected    # the global file's internal stamp: identical content, identical bytes

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
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*.duckdb")) if base.exists() else []

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
    """Publish `src_db` (one league's analytics database) as `league_id`. `sync_con` is a psycopg connection as
    fi_sync; this function commits on it."""
    stamp = stamp or utcnow().isoformat(timespec="seconds")
    row = sync_con.execute("SELECT id FROM fi.leagues WHERE id = %s AND deleted_at IS NULL", [league_id]).fetchone()
    sync_con.rollback()
    if row is None:
        raise ValueError(f"league {league_id} is not registered (or is deleted) in the control plane")
    with tempfile.TemporaryDirectory() as tmp:
        out = split_database(Path(src_db), Path(tmp) / "league.duckdb", Path(tmp) / "global.duckdb", stamp=stamp,
                             global_stamp=GLOBAL_STAMP)
        global_version = out.global_version
        content = global_version.split("-", 1)[1]                 # <sha256[:24]>-<bytes>
        existing = [k for k in writer.keys("global/") if k.endswith(f"-{content}.duckdb")]
        if existing:                                               # same global data already uploaded: reuse it
            global_version = sorted(existing)[0].removeprefix("global/").removesuffix(".duckdb")
        else:
            writer.put(global_key(global_version), out.global_path)
        lkey = league_key(league_id, out.league_version)
        writer.put(lkey, out.league_path)
        out = SplitResult(out.league_path, out.global_path, out.league_version, global_version, out.league_id,
                          out.season, out.schema_version)

    with sync_con.transaction():
        fence = sync_con.execute("UPDATE fi.leagues SET sync_fence = sync_fence + 1 WHERE id = %s RETURNING sync_fence",
                                 [league_id]).fetchone()[0]
        applied = sync_con.execute(_UPSERT, [league_id, lkey, out.league_version, fence, out.global_version]).fetchone()
        if applied is not None:
            sync_con.execute("INSERT INTO fi.league_snapshot_history (league_id, object_key, data_version, fence, "
                             "global_version, committed_at) VALUES (%s, %s, %s, %s, %s, now())",
                             [league_id, lkey, out.league_version, fence, out.global_version])
            sync_con.execute("""DELETE FROM fi.league_snapshot_history WHERE league_id = %s AND id NOT IN (
                                  SELECT id FROM fi.league_snapshot_history WHERE league_id = %s
                                  ORDER BY fence DESC, id DESC LIMIT %s)""", [league_id, league_id, keep])
    deleted = _collect(writer, sync_con, league_id)
    return PublishResult(league_id, out.league_version, out.global_version, fence, applied is not None, deleted)


def _collect(writer: ObjectWriter, sync_con, league_id: str) -> tuple[str, ...]:
    """Delete this league's objects that no pointer or history row references, and global objects no league
    references (the newest global object is always kept)."""
    with sync_con.transaction():
        league_refs = {r[0] for r in sync_con.execute(
            "SELECT object_key FROM fi.league_snapshots WHERE league_id = %s UNION "
            "SELECT object_key FROM fi.league_snapshot_history WHERE league_id = %s", [league_id, league_id])}
        global_refs = {global_key(r[0]) for r in sync_con.execute(
            "SELECT global_version FROM fi.league_snapshots UNION SELECT global_version FROM fi.league_snapshot_history")}
    deleted = []
    for key in writer.keys(f"leagues/{league_id}/"):
        if key not in league_refs:
            writer.delete(key)
            deleted.append(key)
    # A global object another publisher has just uploaded is not referenced until its pointer moves; keep every
    # unreferenced global younger than GLOBAL_GRACE (by the stamp in its name) and always the newest one.
    cutoff = (utcnow() - GLOBAL_GRACE).strftime("%Y%m%dT%H%M%SZ")
    globals_ = sorted(writer.keys("global/"))
    for key in globals_[:-1]:
        stamp = key.removeprefix("global/").split("-", 1)[0]
        if key not in global_refs and stamp < cutoff:
            writer.delete(key)
            deleted.append(key)
    return tuple(deleted)

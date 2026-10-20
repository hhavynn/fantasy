"""Per-instance cache of immutable league and global DuckDB files (multi-league Phase 2).

Objects are immutable: `leagues/<lg_id>/<version>.duckdb` and `global/<version>.duckdb`, where the version is
`<stamp>Z-<sha256[:24]>-<bytes>` (app/db/league_split.py). A version therefore never goes stale, and every download
is verified against its own name. Keys are built here from a validated league id and version only; nothing a
request supplies reaches a path or an object key.

The cache keeps files in `cache_dir` (Vercel: /tmp) under a byte bound (350 MB by default, /tmp is 500 MB).
Downloads go to a temporary name and are renamed into place, so a reader never sees a partial file; one download
per object at a time (other requests for the same object wait for it); least-recently-used files are evicted to
stay under the bound, and `on_evict(path)` lets the connection cache close what it had open on them.

The object store is R2 (read-only token) in production, or a local directory (`LEAGUE_FILES_DIR`) for development
and the local golden check.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Protocol

from . import r2
from .league_split import VERSION_RE

DEFAULT_MAX_BYTES = 350 << 20
_LEAGUE_ID = re.compile(r"lg_[A-Za-z0-9_-]{16,64}")


class ObjectKeyError(ValueError):
    """A league id or version that is not ours. Callers never get this from request input: authorization and the
    database constraints come first; it is defence in depth."""


def _check_version(version: str) -> re.Match:
    m = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    if m is None:
        raise ObjectKeyError("malformed version")
    return m


def league_key(league_id: str, version: str) -> str:
    if not isinstance(league_id, str) or not _LEAGUE_ID.fullmatch(league_id):
        raise ObjectKeyError("malformed league id")
    _check_version(version)
    return f"leagues/{league_id}/{version}.duckdb"


def global_key(version: str) -> str:
    _check_version(version)
    return f"global/{version}.duckdb"


class ObjectStore(Protocol):
    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None: ...


class R2Store:
    """Production: the private bucket, read with the API's Object Read token."""

    def __init__(self, cfg: r2.R2Config):
        self.cfg = cfg

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        r2.download(self.cfg, key, dest, size=size, expected_sha256_prefix=sha256_prefix)


class DirStore:
    """Development: objects laid out under a directory with the same keys."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        src = self.root / key
        if not src.is_file():
            raise r2.R2Error(404, "NoSuchKey", key)
        part = dest.with_name(dest.name + ".part")
        try:
            shutil.copyfile(src, part)
            if part.stat().st_size != size:
                raise r2.R2Error(None, "SizeMismatch", key)
            if not r2.file_sha256(part).startswith(sha256_prefix):
                raise r2.R2Error(None, "ChecksumMismatch", key)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)


class LeagueFileCache:
    def __init__(self, store: ObjectStore, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 on_evict: Callable[[Path], None] | None = None):
        self.store, self.cache_dir, self.max_bytes = store, Path(cache_dir), int(max_bytes)
        self.on_evict = on_evict
        self._lock = threading.Lock()                       # guards _files and _downloading
        self._files: OrderedDict[str, tuple[Path, int]] = OrderedDict()   # key -> (path, bytes), LRU order
        self._downloading: dict[str, threading.Event] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.cache_dir.iterdir():          # a fresh index: files from an earlier process are dropped
            if leftover.is_file():
                leftover.unlink(missing_ok=True)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return sum(size for _, size in self._files.values())

    def league_file(self, league_id: str, version: str) -> Path:
        return self._get(league_key(league_id, version), version)

    def global_file(self, version: str) -> Path:
        return self._get(global_key(version), version)

    def _local_path(self, key: str) -> Path:
        return self.cache_dir / key.replace("/", "__")

    def _get(self, key: str, version: str) -> Path:
        m = _check_version(version)
        size = int(m["bytes"])
        if size > self.max_bytes:
            raise ValueError(f"{key} is larger than the cache ({size} > {self.max_bytes} bytes)")
        while True:
            with self._lock:
                hit = self._files.get(key)
                if hit is not None and hit[0].exists():
                    self._files.move_to_end(key)
                    return hit[0]
                waiting = self._downloading.get(key)
                if waiting is None:
                    done = self._downloading[key] = threading.Event()
                    break
            waiting.wait()                                   # someone else is fetching this object; then re-check
        path = self._local_path(key)
        try:
            self._make_room(size)
            self.store.fetch(key, path, sha256_prefix=m["sha"], size=size)
            with self._lock:
                self._files[key] = (path, size)
                self._files.move_to_end(key)
            self._make_room(0)
            return path
        finally:
            with self._lock:
                self._downloading.pop(key, None)
            done.set()

    def _make_room(self, incoming: int) -> None:
        """Evict least-recently-used files until `incoming` more bytes fit under the bound."""
        evicted = []
        with self._lock:
            used = sum(size for _, size in self._files.values())
            while self._files and used + incoming > self.max_bytes:
                _, (path, size) = self._files.popitem(last=False)
                used -= size
                evicted.append(path)
        for path in evicted:
            if self.on_evict is not None:
                self.on_evict(path)                          # close connections first, then free the space
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

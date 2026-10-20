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


class CacheFull(RuntimeError):
    """Every cached file is in use by a running request and the new one does not fit: the caller answers 503."""


class LeagueFileCache:
    """Files are *pinned* while a request uses them (`pin()` / `release()`): eviction skips pinned files, so a path a
    request has resolved is never unlinked before the request has opened it. In-flight downloads reserve their bytes,
    so concurrent first loads cannot push the directory past the bound together. Each process has its own
    subdirectory (several processes on one host never delete each other's files); directories of dead processes are
    removed at start."""

    def __init__(self, store: ObjectStore, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 on_evict: Callable[[Path], None] | None = None):
        self.store, self.max_bytes = store, int(max_bytes)
        self.on_evict = on_evict
        self._lock = threading.Lock()                       # guards every field below
        self._files: OrderedDict[str, tuple[Path, int]] = OrderedDict()   # key -> (path, bytes), LRU order
        self._downloading: dict[str, threading.Event] = {}
        self._reserved: dict[str, int] = {}                 # key -> bytes of an in-flight download
        self._pins: dict[str, int] = {}                     # key -> requests currently using the file
        root = Path(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        _remove_dead_process_dirs(root)
        self.cache_dir = root / f"p{os.getpid()}"
        shutil.rmtree(self.cache_dir, ignore_errors=True)  # a fresh index for this process
        self.cache_dir.mkdir(parents=True)

    @property
    def bytes_used(self) -> int:
        """Bytes on disk: cached files plus in-flight downloads."""
        with self._lock:
            return sum(size for _, size in self._files.values()) + sum(self._reserved.values())

    def league_file(self, league_id: str, version: str) -> Path:
        return self._get(league_key(league_id, version), version)

    def global_file(self, version: str) -> Path:
        return self._get(global_key(version), version)

    def pin_league(self, league_id: str, version: str) -> Path:
        """`league_file`, held until `release()`: never evicted meanwhile."""
        return self._get(league_key(league_id, version), version, pin=True)

    def pin_global(self, version: str) -> Path:
        return self._get(global_key(version), version, pin=True)

    def release(self, path: Path) -> None:
        with self._lock:
            for key, (p, _) in self._files.items():
                if p == path and self._pins.get(key):
                    self._pins[key] -= 1
                    if not self._pins[key]:
                        del self._pins[key]
                    return

    def _local_path(self, key: str) -> Path:
        return self.cache_dir / key.replace("/", "__")

    def _get(self, key: str, version: str, *, pin: bool = False) -> Path:
        m = _check_version(version)
        size = int(m["bytes"])
        if size > self.max_bytes:
            raise ValueError(f"{key} is larger than the cache ({size} > {self.max_bytes} bytes)")
        while True:
            with self._lock:
                hit = self._files.get(key)
                if hit is not None and hit[0].exists():
                    self._files.move_to_end(key)
                    if pin:
                        self._pins[key] = self._pins.get(key, 0) + 1
                    return hit[0]
                waiting = self._downloading.get(key)
                if waiting is None:
                    done = self._downloading[key] = threading.Event()
                    break
            waiting.wait()                                   # someone else is fetching this object; then re-check
        path = self._local_path(key)
        try:
            self._make_room(key, size)                       # reserves `size` for this download
            try:
                self.store.fetch(key, path, sha256_prefix=m["sha"], size=size)
            finally:
                with self._lock:
                    self._reserved.pop(key, None)
            with self._lock:
                self._files[key] = (path, size)
                self._files.move_to_end(key)
                if pin:
                    self._pins[key] = self._pins.get(key, 0) + 1
            return path
        finally:
            with self._lock:
                self._downloading.pop(key, None)
            done.set()

    def _make_room(self, key: str, incoming: int) -> None:
        """Evict least-recently-used unpinned files until `incoming` more bytes fit, then reserve them."""
        evicted = []
        with self._lock:
            used = sum(size for _, size in self._files.values()) + sum(self._reserved.values())
            for old in list(self._files):
                if used + incoming <= self.max_bytes:
                    break
                if self._pins.get(old):
                    continue
                path, size = self._files.pop(old)
                used -= size
                evicted.append(path)
            if used + incoming > self.max_bytes:
                raise CacheFull(f"no room for {key}: every cached file is in use")
            self._reserved[key] = incoming
        for path in evicted:
            if self.on_evict is not None:
                self.on_evict(path)                          # forget open instances, then free the space
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _remove_dead_process_dirs(root: Path) -> None:
    for d in root.glob("p*"):
        try:
            pid = int(d.name[1:])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)                                  # alive (or not ours to signal): leave it
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except PermissionError:
            pass

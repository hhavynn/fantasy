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
and the local golden check. A request-path R2 read is retried once after a short jittered pause on a transient failure
(network, 429, 5xx); never on a 403/404 or a checksum mismatch.

Degraded reads (`pin_pair`, issue #201): when a league's pointer has moved to files this instance does not hold yet,
the new pair is fetched in the background and the request waits at most `budget_s` for it. If it is not ready (slow or
R2 unavailable), the request is served the last pair *this league* was served from in this process, still cached, and
the result says so (`degraded`), so the page can say "data may be older" instead of timing out. The fallback is keyed
by the validated league id only: a league never falls back to another league's files.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import r2
from .league_split import VERSION_RE

DEFAULT_MAX_BYTES = 350 << 20
DEFAULT_COLD_BUDGET_S = 3.0          # longest a request waits for a newer pair while an older one can be served
DEFAULT_ROOM_WAIT_S = 8.0            # a full cache (every file pinned) waits this long for a request to finish
FAILED_FETCH_RETRY_S = 30.0          # a failed background fetch of one pair is not retried sooner (R2 down)
_MAX_PREFETCHES = 4                  # background fetches in flight per process; more wait in the request instead
_LAST_GOOD_LEAGUES = 4096            # leagues whose last served pair is remembered (ids and versions only)
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


def _transient(err: r2.R2Error) -> bool:
    """Worth one retry: a network failure or a throttled / failing server. Never a 403/404 (retrying cannot help) or
    a checksum/size mismatch (the object itself is wrong)."""
    if err.status is None:
        return err.code not in ("ChecksumMismatch", "SizeMismatch")
    return err.status == 429 or 500 <= err.status <= 599


class R2Store:
    """Production: the private bucket, read with the API's Object Read token. One retry after a 0.2-0.5 s pause on a
    transient failure: a request-path read, so the pause stays short and the attempts stay at two."""

    def __init__(self, cfg: r2.R2Config, *, attempts: int = 2, sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.attempts, self.sleep = cfg, max(1, attempts), sleep

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        for attempt in range(1, self.attempts + 1):
            try:
                r2.download(self.cfg, key, dest, size=size, expected_sha256_prefix=sha256_prefix)
                return
            except r2.R2Error as err:
                if attempt == self.attempts or not _transient(err):
                    raise
            self.sleep(0.2 + random.random() * 0.3)


class DirStore:
    """Development: objects laid out under a directory with the same keys."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def fetch(self, key: str, dest: Path, *, sha256_prefix: str, size: int) -> None:
        src = self.root / key
        if not src.is_file():
            raise r2.R2Error(404, "NoSuchKey", key)
        part = r2._part_path(dest)
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


@dataclass(frozen=True)
class PinnedPair:
    """A league file and the global file it was built against, both pinned until `release_pair()`.

    `data_version` / `global_version` name the files actually pinned: the pointer's, or with `degraded` set, the last
    pair this league was served from ("slow": the newer pair did not arrive within the budget; "unavailable": fetching
    it failed). `cold` is True when this call had to fetch (or wait for) a file."""
    league_path: Path
    global_path: Path
    data_version: str
    global_version: str
    degraded: str | None = None
    cold: bool = False


class LeagueFileCache:
    """Files are *pinned* while a request uses them (`pin()` / `release()`): eviction skips pinned files, so a path a
    request has resolved is never unlinked before the request has opened it. In-flight downloads reserve their bytes,
    so concurrent first loads cannot push the directory past the bound together; when they would, the newcomer waits
    (up to `room_wait_s`) for running requests to release files instead of failing at once. Each process has its own
    subdirectory (several processes on one host never delete each other's files); directories of dead processes are
    removed at start."""

    def __init__(self, store: ObjectStore, cache_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 on_evict: Callable[[Path], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 room_wait_s: float = DEFAULT_ROOM_WAIT_S):
        self.store, self.max_bytes = store, int(max_bytes)
        self.on_evict, self.clock, self.room_wait_s = on_evict, clock, max(0.0, room_wait_s)
        self._lock = threading.Lock()                       # guards every field below
        self._room = threading.Condition(self._lock)        # notified when a pin or a reservation is let go
        self._files: OrderedDict[str, tuple[Path, int]] = OrderedDict()   # key -> (path, bytes), LRU order
        self._downloading: dict[str, threading.Event] = {}
        self._reserved: dict[str, int] = {}                 # key -> bytes of an in-flight download
        self._pins: dict[str, int] = {}                     # key -> requests currently using the file
        self._last_good: OrderedDict[str, tuple[str, str]] = OrderedDict()   # league id -> last served pair
        self._prefetches: dict[tuple[str, str, str], Future] = {}             # pair -> background fetch
        self._failed_fetches: dict[tuple[str, str, str], tuple[float, BaseException]] = {}
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

    def pin_cached_league(self, league_id: str, version: str) -> Path | None:
        """Pin the league file only if it is already cached; never downloads."""
        return self._pin_cached(league_key(league_id, version))

    def pin_cached_global(self, version: str) -> Path | None:
        return self._pin_cached(global_key(version))

    def _pin_cached(self, key: str) -> Path | None:
        with self._lock:
            hit = self._files.get(key)
            if hit is None or key in self._downloading or not hit[0].exists():
                return None
            self._files.move_to_end(key)
            self._pins[key] = self._pins.get(key, 0) + 1
            return hit[0]

    def release_pair(self, pair: PinnedPair) -> None:
        self.release(pair.league_path)
        self.release(pair.global_path)

    def last_good(self, league_id: str) -> tuple[str, str] | None:
        """The (data version, global version) this league was last served from in this process, or None."""
        with self._lock:
            return self._last_good.get(league_id)

    def _remember(self, league_id: str, data_version: str, global_version: str) -> None:
        with self._lock:
            self._last_good[league_id] = (data_version, global_version)
            self._last_good.move_to_end(league_id)
            while len(self._last_good) > _LAST_GOOD_LEAGUES:
                self._last_good.popitem(last=False)

    def _pin_pair_cached(self, league_id: str, data_version: str, global_version: str) -> tuple[Path, Path] | None:
        league_path = self.pin_cached_league(league_id, data_version)
        if league_path is None:
            return None
        global_path = self.pin_cached_global(global_version)
        if global_path is None:
            self.release(league_path)
            return None
        return league_path, global_path

    def _pin_pair_blocking(self, league_id: str, data_version: str, global_version: str) -> tuple[Path, Path]:
        """The shared global file first: a request waiting for room then holds only the file every request needs,
        never its own league file while the one global download waits for room that league pins block."""
        global_path = self.pin_global(global_version)
        try:
            return self.pin_league(league_id, data_version), global_path
        except BaseException:
            self.release(global_path)
            raise

    def prefetch(self, league_id: str, data_version: str, global_version: str) -> Future | None:
        """Fetch a pair into the cache on a background thread (one per pair at a time; nothing is pinned). The future
        has the pair's start time as `started` and fails with the fetch's error. None when too many fetches are
        already running: the caller then waits in its own thread."""
        league_key(league_id, data_version)                 # validate before any thread starts
        global_key(global_version)
        pair = (league_id, data_version, global_version)
        with self._lock:
            fut = self._prefetches.get(pair)
            if fut is not None:
                return fut
            failed = self._failed_fetches.get(pair)
            if failed is not None and self.clock() - failed[0] < FAILED_FETCH_RETRY_S:
                fut = Future()
                fut.started = failed[0]
                fut.set_exception(failed[1])
                return fut
            if len(self._prefetches) >= _MAX_PREFETCHES:
                return None
            fut = self._prefetches[pair] = Future()
            fut.started = self.clock()

        def run():
            try:
                self.league_file(league_id, data_version)
                self.global_file(global_version)
            except BaseException as err:   # noqa: BLE001 - handed to the waiting requests through the future
                err = err.with_traceback(None)              # kept for a while: hold no frames
                with self._lock:
                    self._prefetches.pop(pair, None)
                    self._failed_fetches[pair] = (self.clock(), err)
                    while len(self._failed_fetches) > _MAX_PREFETCHES * 16:
                        self._failed_fetches.pop(next(iter(self._failed_fetches)))
                fut.set_exception(err)
                return
            with self._lock:
                self._prefetches.pop(pair, None)
                self._failed_fetches.pop(pair, None)
            fut.set_result(None)

        threading.Thread(target=run, name="league-file-prefetch", daemon=True).start()
        return fut

    def pin_pair(self, league_id: str, data_version: str, global_version: str, *,
                 budget_s: float = DEFAULT_COLD_BUDGET_S) -> PinnedPair:
        """Pin the league's files for the pointer (data_version, global_version).

        Already cached: pinned at once. Not cached and this league has no older pair still cached here: fetched in
        this request, as before (nothing else to serve; errors propagate). Not cached but an older pair of *this
        league* is: the new pair is fetched in the background and this request waits up to `budget_s` (counted from
        when that fetch started, so later requests do not each wait the whole budget); if it is not ready, the older
        pair is served with `degraded` set."""
        cached = self._pin_pair_cached(league_id, data_version, global_version)
        if cached is not None:
            self._remember(league_id, data_version, global_version)
            return PinnedPair(*cached, data_version, global_version)
        last = self.last_good(league_id)
        fut = None
        if last is not None and last != (data_version, global_version):
            fut = self.prefetch(league_id, data_version, global_version)
        if fut is None:
            paths = self._pin_pair_blocking(league_id, data_version, global_version)
            self._remember(league_id, data_version, global_version)
            return PinnedPair(*paths, data_version, global_version, cold=True)
        reason = None
        try:
            fut.result(timeout=max(0.0, budget_s - (self.clock() - fut.started)))
        except TimeoutError:
            reason = "slow"
        except Exception:   # noqa: BLE001 - R2 errors, a full cache: the older pair is served instead
            reason = "unavailable"
        if reason is None:
            paths = self._pin_pair_blocking(league_id, data_version, global_version)   # cached now: no download
            self._remember(league_id, data_version, global_version)
            return PinnedPair(*paths, data_version, global_version, cold=True)
        old = self._pin_pair_cached(league_id, *last)
        if old is not None:
            return PinnedPair(*old, *last, degraded=reason, cold=True)
        fut.result()       # the older pair was evicted meanwhile: wait for the new one after all (or raise its error)
        paths = self._pin_pair_blocking(league_id, data_version, global_version)
        self._remember(league_id, data_version, global_version)
        return PinnedPair(*paths, data_version, global_version, cold=True)

    def release(self, path: Path) -> None:
        with self._lock:
            for key, (p, _) in self._files.items():
                if p == path and self._pins.get(key):
                    self._pins[key] -= 1
                    if not self._pins[key]:
                        del self._pins[key]
                        self._room.notify_all()
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
            except BaseException:
                with self._lock:
                    self._reserved.pop(key, None)
                    self._room.notify_all()
                raise
            with self._lock:                                 # reservation -> index in one step: never uncounted
                self._reserved.pop(key, None)
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
        """Make room for `incoming` bytes and reserve them. When everything that could go is pinned by running
        requests (more leagues loading at once than the cache holds), wait up to `room_wait_s` for a request to finish
        and release its files rather than refusing at once; CacheFull (a 503) only after that."""
        deadline = time.monotonic() + self.room_wait_s
        while not self._try_make_room(key, incoming):
            with self._lock:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CacheFull(f"no room for {key}: every cached file is in use")
                self._room.wait(min(remaining, 0.25))       # woken by release(); the timeout covers a missed wake

    def _try_make_room(self, key: str, incoming: int) -> bool:
        """Evict least-recently-used unpinned files until `incoming` more bytes fit, then reserve them. An evicted key
        is marked as downloading until its file is unlinked, so a request for it waits instead of fetching a new copy
        to the same path that this unlink would then remove."""
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
                evicted.append((old, path, self._downloading.setdefault(old, threading.Event())))
            fits = used + incoming <= self.max_bytes
            if fits:
                self._reserved[key] = incoming
        try:
            for _, path, _ in evicted:
                if self.on_evict is not None:
                    self.on_evict(path)                      # forget open instances, then free the space
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        finally:
            with self._lock:
                for old, _, done in evicted:
                    if self._downloading.get(old) is done:
                        del self._downloading[old]
            for _, _, done in evicted:
                done.set()
        return fits


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

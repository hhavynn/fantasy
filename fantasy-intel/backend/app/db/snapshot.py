"""The published snapshot: one compact DuckDB file per publish in Cloudflare R2, plus a manifest.

Layout in the bucket (scripts/publish_snapshot.py writes it; the hosted API only reads it):

    <prefix>/<stamp>.duckdb   immutable, one per publish
    <prefix>/current.json     {"key", "published_at", "sha256", "bytes", "schema_version"}, written last

The API keeps the current file in the function's temp dir. A new publish is a new key, so a cached file
is never stale: the manifest (re-read at most every `ttl_s`) says which key to serve, a new key is
downloaded once per instance (concurrent requests wait for the same download), checked against its
sha256, renamed into place, and older files are removed (Vercel's /tmp is 500 MB). If the manifest
cannot be read, the instance keeps serving the file it has.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import r2

log = logging.getLogger(__name__)

MANIFEST = "current.json"


@dataclass(frozen=True)
class Manifest:
    key: str
    published_at: str
    sha256: str
    bytes: int
    schema_version: int | None = None

    @classmethod
    def parse(cls, raw: bytes) -> "Manifest":
        d = json.loads(raw)
        return cls(key=str(d["key"]), published_at=str(d["published_at"]), sha256=str(d["sha256"]),
                   bytes=int(d["bytes"]), schema_version=d.get("schema_version"))

    def to_json(self) -> bytes:
        return json.dumps(self.__dict__, sort_keys=True).encode()


class SnapshotCache:
    def __init__(self, cfg: r2.R2Config, prefix: str = "snapshots", cache_dir: Path | None = None,
                 ttl_s: float = 30.0, clock=time.monotonic):
        self.cfg, self.prefix, self.ttl_s, self.clock = cfg, prefix.strip("/"), ttl_s, clock
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "fantasy-snapshots")
        self._lock = threading.Lock()            # guards the fields below; never held across a download
        self._download_lock = threading.Lock()   # one download at a time per instance
        self._manifest: Manifest | None = None
        self._checked_at: float | None = None
        self._served: tuple[Path, Manifest] | None = None
        self._failed: tuple[str, float] | None = None   # (key, when) of the last failed background download
        self._background: threading.Thread | None = None

    def manifest(self) -> Manifest:
        now = self.clock()
        if self._manifest is not None and self._checked_at is not None and now - self._checked_at < self.ttl_s:
            return self._manifest
        try:
            fresh = Manifest.parse(r2.get_bytes(self.cfg, f"{self.prefix}/{MANIFEST}"))
        except (r2.R2Error, ValueError, KeyError) as err:
            if self._manifest is None:
                raise
            log.warning("snapshot manifest unavailable (%s); serving %s", err, self._manifest.key)
            self._checked_at = now   # back off for one TTL instead of retrying on every request
            return self._manifest
        self._manifest, self._checked_at = fresh, now
        return fresh

    def _path(self, m: Manifest) -> Path:
        # The digest is part of the name: a key re-published with other content is a different file.
        return self.cache_dir / f"{m.sha256[:16]}-{Path(m.key).name}"

    def _fetch(self, m: Manifest, path: Path) -> None:
        with self._download_lock:
            if path.exists():
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            r2.download(self.cfg, m.key, path, expected_sha256=m.sha256)

    def _switch_to(self, path: Path, m: Manifest) -> tuple[Path, Manifest]:
        """Serve `path` from now on and delete the other cached files (open read-only connections keep
        their inode). Call with self._lock held."""
        self._served, self._failed = (path, m), None
        for old in self.cache_dir.iterdir():
            if old != path and not old.name.endswith(".part"):
                old.unlink(missing_ok=True)
        return self._served

    def _fetch_in_background(self, m: Manifest, path: Path) -> None:
        try:
            self._fetch(m, path)
        except r2.R2Error as err:
            log.warning("snapshot %s unavailable (%s); still serving the previous one", m.key, err)
            with self._lock:
                self._failed = (m.key, self.clock())
            return
        with self._lock:
            if self._manifest is None or self._manifest.key == m.key:
                self._switch_to(path, m)

    def local_path(self) -> tuple[Path, Manifest]:
        """The local file to serve. With nothing cached yet this downloads the current snapshot first; once a
        file is cached, a newer publish is fetched by one background thread while requests keep getting the
        cached file, and a failed fetch is retried after one TTL rather than on every request."""
        with self._lock:
            m = self.manifest()
            path = self._path(m)
            if path.exists():
                return self._switch_to(path, m) if self._served != (path, m) else self._served
            if self._served is not None and self._served[0].exists():
                recently_failed = (self._failed is not None and self._failed[0] == m.key
                                   and self.clock() - self._failed[1] < self.ttl_s)
                busy = self._background is not None and self._background.is_alive()
                if not (recently_failed or busy):
                    self._background = threading.Thread(target=self._fetch_in_background, args=(m, path),
                                                        name="snapshot-download", daemon=True)
                    self._background.start()
                return self._served
        self._fetch(m, path)   # first request in this instance: nothing to fall back to, so wait
        with self._lock:
            return self._switch_to(path, m)

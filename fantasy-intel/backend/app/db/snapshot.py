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
        self._lock = threading.Lock()
        self._manifest: Manifest | None = None
        self._checked_at: float | None = None
        self._served: tuple[Path, Manifest] | None = None

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

    def local_path(self) -> tuple[Path, Manifest]:
        """The local file for the current manifest, downloading it first if this instance lacks it."""
        with self._lock:
            m = self.manifest()
            path = self.cache_dir / Path(m.key).name
            if not path.exists():
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    r2.download(self.cfg, m.key, path, expected_sha256=m.sha256)
                except r2.R2Error as err:
                    if self._served is None or not self._served[0].exists():
                        raise
                    log.warning("snapshot %s unavailable (%s); serving %s", m.key, err, self._served[1].key)
                    return self._served
                for old in self.cache_dir.iterdir():
                    if old != path:
                        old.unlink(missing_ok=True)   # open read-only connections keep their inode
            self._served = (path, m)
            return path, m

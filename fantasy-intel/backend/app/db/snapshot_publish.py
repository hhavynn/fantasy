"""Publish the local DuckDB file as an immutable snapshot in R2 for the hosted API (app/db/snapshot.py).

A compact copy (`COPY FROM DATABASE`, which drops the free blocks a long-lived local file accumulates) is
stamped with `schema_meta.published_at` (the API's data version), uploaded under a new key, and only then
is `current.json` replaced, so the API never sees a manifest for a file that is not fully there. The last
`keep` snapshots stay in the bucket for rollback; older ones are deleted.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb

from . import r2
from .connection import DatabaseBusy, schema_version, utcnow
from .snapshot import MANIFEST, Manifest


def build_compact(local_path: Path, out_path: Path, stamp: str) -> int:
    """Write a compact copy of `local_path` to `out_path` with the publish stamp; returns its schema version."""
    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(f"No local database at {local_path}. Run the sync scripts first.")
    con = duckdb.connect()
    try:
        try:
            con.execute(f"ATTACH '{str(local_path).replace(chr(39), chr(39) * 2)}' AS src (READ_ONLY)")
        except duckdb.IOException as err:  # a sync holds the write lock
            raise DatabaseBusy(str(err))
        con.execute("USE src")
        version = schema_version(con)
        if version is None:
            raise ValueError(f"{local_path} has no fantasy-intel schema. Run the sync scripts first.")
        con.execute(f"ATTACH '{str(out_path).replace(chr(39), chr(39) * 2)}' AS dst")
        con.execute("COPY FROM DATABASE src TO dst")
        con.execute("DELETE FROM dst.main.schema_meta WHERE key = 'published_at'")
        con.execute("INSERT INTO dst.main.schema_meta VALUES ('published_at', ?)", [stamp])
        con.execute("CHECKPOINT dst")
        return version
    finally:
        con.close()


def publish_snapshot(local_path: Path, cfg: r2.R2Config, prefix: str = "snapshots", keep: int = 3) -> Manifest:
    prefix = prefix.strip("/")
    stamp = utcnow().isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "snapshot.duckdb"
        version = build_compact(local_path, out, stamp)
        key = f"{prefix}/{stamp.replace(':', '').replace('-', '')}.duckdb"
        digest = r2.put_file(cfg, key, out)
        manifest = Manifest(key=key, published_at=stamp, sha256=digest, bytes=out.stat().st_size,
                            schema_version=version)
    r2.put_bytes(cfg, f"{prefix}/{MANIFEST}", manifest.to_json())
    snapshots = sorted(k for k in r2.list_keys(cfg, f"{prefix}/") if k.endswith(".duckdb"))
    for old in snapshots[:-keep] if keep > 0 else []:
        if old != key:
            r2.delete(cfg, old)
    return manifest

"""Publish what a sync job built: its raw payloads, then the league file through the spec's fenced upsert with the
fence the job got at claim time (app/db/league_publish.commit_league). A job holding an older fence than the
published one changes nothing visible and its objects are deleted again."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..db.league_files import league_key
from ..db.league_publish import ObjectWriter, commit_league
from .build import Built
from .queue import Claim


def raw_key(league_id: str, data_version: str) -> str:
    league_key(league_id, data_version)            # validates both
    return f"raw/{league_id}/{data_version}.json.gz"


def publish_job(writer: ObjectWriter, sync_con, claim: Claim, built: Built, *, raw: bytes | None,
                global_version: str) -> tuple[bool, tuple[str, ...]]:
    """Upload the raw payloads (when given) and the league file, then move the pointer if `claim.fence` is still the
    newest. Returns (applied, keys collected)."""
    if raw is not None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.json.gz"
            path.write_bytes(raw)
            writer.put(raw_key(claim.league_id, built.league_version), path)
    return commit_league(writer, sync_con, claim.league_id, built.league_path, built.league_version, global_version,
                         claim.fence)

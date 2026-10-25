"""Register a league in the test control plane and publish it with the real publisher into a local object directory."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg

from app.db.league_publish import DirWriter, publish_league
from cp_seed import seed_league


@dataclass(frozen=True)
class Published:
    league_id: str
    data_version: str
    global_version: str


def publish_local(pg, src_db: Path, objects: Path, work: Path, *, visibility="public", name="League", checked_at="now()",
                  stamp="2026-10-20T12:00:00", league_id: str | None = None,
                  external_id: str | None = None, provider: str = "espn") -> Published:
    """Register the league, then publish `src_db` exactly as production does (app/db/league_publish.py) as fi_sync,
    with the objects laid out under `objects` (the API reads them through LEAGUE_FILES_DIR). `work` is unused and
    kept for call-site compatibility."""
    import duckdb
    con = duckdb.connect(str(src_db), read_only=True)
    try:
        espn_id = con.execute("SELECT league_id FROM leagues").fetchone()[0]
    finally:
        con.close()
    with pg.connect() as su:
        lid = seed_league(su, league_id, visibility=visibility, name=name, checked_at=checked_at,
                          external_id=external_id or str(espn_id), provider=provider)
        su.commit()
    with psycopg.connect(pg.dsn_for("fi_sync"), prepare_threshold=None) as sync:
        res = publish_league(Path(src_db), DirWriter(objects), sync, lid, stamp=stamp)
    assert res.applied
    return Published(lid, res.data_version, res.global_version)

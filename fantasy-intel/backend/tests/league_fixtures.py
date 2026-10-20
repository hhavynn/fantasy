"""Register split league files in the test control plane and serve them from a local object directory."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.db.league_files import global_key, league_key
from app.db.league_split import split_database
from cp_seed import new_league_id, seed_league


@dataclass(frozen=True)
class Published:
    league_id: str
    data_version: str
    global_version: str


def publish_local(pg, src_db: Path, objects: Path, work: Path, *, visibility="public", name="League", checked_at="now()",
                  stamp="2026-10-20T12:00:00", league_id: str | None = None,
                  external_id: str | None = None) -> Published:
    """Split `src_db`, lay the two files out under `objects` with their real keys, register the league and point
    fi.league_snapshots at them (as the publisher does)."""
    work.mkdir(parents=True, exist_ok=True)
    out = split_database(src_db, work / "lg.duckdb", work / "gl.duckdb", stamp=stamp)
    with pg.connect() as con:
        lid = seed_league(con, league_id, visibility=visibility, name=name, checked_at=checked_at,
                          external_id=external_id or str(out.league_id))
        for key, path in ((league_key(lid, out.league_version), out.league_path),
                          (global_key(out.global_version), out.global_path)):
            dest = objects / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        con.execute("INSERT INTO fi.league_snapshots (league_id, object_key, data_version, fence, global_version) "
                    "VALUES (%s, %s, %s, 1, %s)", [lid, league_key(lid, out.league_version), out.league_version,
                                                  out.global_version])
        con.commit()
    return Published(lid, out.league_version, out.global_version)

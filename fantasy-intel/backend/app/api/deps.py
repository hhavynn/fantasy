from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import HTTPException

from ..config import get_settings
from ..db.connection import DatabaseBusy, connect, fetch_dicts

JSON_COLUMNS = {"components", "reasons", "competitors", "roster_slots", "owner_ids", "matchup_acquisitions",
                "stats", "details", "eligible_slots", "scoring_items", "waiver_process_days"}


@contextmanager
def db() -> Iterator[Any]:
    settings = get_settings()
    if not settings.db_path.exists():
        raise HTTPException(503, "No database yet. Run scripts/sync_espn.py and scripts/sync_nfl.py first.")
    try:
        con = connect(settings.db_path, read_only=True, retries=8)
    except DatabaseBusy:
        raise HTTPException(503, "Database is busy (a sync is probably running). Retry in a few seconds.")
    try:
        yield con
    finally:
        con.close()


def rows(con, sql: str, params=()) -> list[dict]:
    out = fetch_dicts(con, sql, params)
    for r in out:
        for k in JSON_COLUMNS & r.keys():
            if isinstance(r[k], str):
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return out


def one(con, sql: str, params=()) -> dict | None:
    r = rows(con, sql, params)
    return r[0] if r else None

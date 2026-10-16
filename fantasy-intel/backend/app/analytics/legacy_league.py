"""The single-league "most recently synced league" lookup, in one place.

The owner deployment keeps one league for one owner in one database, so single-league features pick the league
that synced last. That is a global selector (docs/multileague/ADR-0001-league-context-and-tenancy.md, "Audit"):
tests/test_league_scope_audit.py pins this module's one "latest synced" query and forbids new ones elsewhere.
New single-league features call latest_league() instead of writing their own, so the multi-league migration
(roadmap Phase 2b) replaces one function with a LeagueContext lookup instead of chasing every caller."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..db.connection import fetch_dicts, table_exists

_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


def latest_league(con, columns: Sequence[str] | None = None, *, season: int | None = None) -> dict | None:
    """The most recently synced `leagues` row (only `columns`, default all), optionally within `season`; None
    when there is no leagues table or no row. Column names are plain identifiers, never caller SQL."""
    if not table_exists(con, "leagues"):
        return None
    if columns is not None and (not columns or not all(_COLUMN.match(c) for c in columns)):
        raise ValueError(f"columns must be plain column names, got {list(columns)!r}")
    select = ", ".join(columns) if columns else "*"
    where, params = ("WHERE season = ?", [season]) if season is not None else ("", [])
    rows = fetch_dicts(con, f"SELECT {select} FROM leagues {where} ORDER BY synced_at DESC LIMIT 1", params)
    return rows[0] if rows else None

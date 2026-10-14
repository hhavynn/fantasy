"""Runs every derived-data step in order. Safe to run any time (idempotent)."""

from __future__ import annotations

import logging

from ..db.connection import SyncRun, table_exists
from ..nfl.mapping import rebuild_id_map
from .manager_loader import build_manager_metrics
from .player_scores import build_player_scores
from .usage import build_player_week_usage
from .waivers import build_waivers

log = logging.getLogger("analytics")


def run_analytics(con, season: int) -> dict:
    run = SyncRun(con, "analytics")
    stats: dict = {}
    has_espn = bool(con.execute("SELECT count(*) FROM leagues").fetchone()[0])
    has_nfl = table_exists(con, "nfl_weekly_stats") and bool(con.execute("SELECT count(*) FROM nfl_players").fetchone()[0])
    try:
        con.execute("BEGIN TRANSACTION")
        if has_espn and has_nfl:
            stats["id_map"] = rebuild_id_map(con, season)
        else:
            stats["id_map"] = {"skipped": "needs both ESPN and NFL data"}
        if has_nfl:
            stats["usage"] = build_player_week_usage(con, season)
            stats["player_scores"] = build_player_scores(con, season)
        if has_espn:
            stats["managers"] = build_manager_metrics(con)
            stats["waivers"] = build_waivers(con)
        con.execute("COMMIT")
    except Exception as err:
        con.execute("ROLLBACK")
        run.finish("error", f"{type(err).__name__}: {err}")
        raise
    run.finish("ok", "", stats)
    for step, s in stats.items():
        log.info("%s: %s", step, s)
    return stats

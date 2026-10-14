"""Runs every derived-data step in order. Safe to run any time (idempotent)."""

from __future__ import annotations

import logging

from ..db.connection import SyncRun, replace_where, table_exists, utcnow
from ..nfl.mapping import rebuild_id_map
from .context import build_context, build_team_game_lines
from .manager_loader import build_manager_metrics
from .player_scores import build_player_scores
from .usage import build_player_week_usage
from .validation import Issue, ValidationError, run_validations
from .waivers import build_waivers

log = logging.getLogger("analytics")


def _next_week(con, season: int, has_espn: bool) -> int | None:
    if has_espn:
        row = con.execute("SELECT current_week FROM leagues WHERE season = ? ORDER BY synced_at DESC LIMIT 1",
                          [season]).fetchone()
        if row and row[0]:
            return row[0]
    if table_exists(con, "nfl_weekly_stats"):
        row = con.execute("SELECT max(week) FROM nfl_weekly_stats WHERE season = ?", [season]).fetchone()
        return row[0] + 1 if row and row[0] else None
    return None


def _record_issues(con, run_id: str, issues: list[Issue]) -> None:
    now = utcnow()
    replace_where(con, "validation_issues", [{**i.as_row(), "run_id": run_id, "created_at": now} for i in issues],
                  "true")


def run_analytics(con, season: int, preferred_provider: str | None = None) -> dict:
    from ..config import get_settings
    preferred = preferred_provider or get_settings().external_preferred_provider
    run = SyncRun(con, "analytics")
    stats: dict = {}
    has_espn = bool(con.execute("SELECT count(*) FROM leagues").fetchone()[0])
    has_nfl = table_exists(con, "nfl_weekly_stats") and bool(con.execute("SELECT count(*) FROM nfl_players").fetchone()[0])
    issues: list[Issue] = []
    try:
        con.execute("BEGIN TRANSACTION")
        if has_espn and has_nfl:
            stats["id_map"] = rebuild_id_map(con, season)
        else:
            stats["id_map"] = {"skipped": "needs both ESPN and NFL data"}
        if has_nfl:
            stats["usage"] = build_player_week_usage(con, season, preferred)
            snap_warn = stats["usage"].get("team_snap_warnings") or []
            extra = [Issue("warning", "team_offense_snaps", "team snap estimate rejected; left NULL",
                           len(snap_warn), snap_warn[:5])] if snap_warn else []
            build_team_game_lines(con, season)   # needed by the implied-total validation
            issues = run_validations(con, season, extra, preferred)
            errors = [i for i in issues if i.severity == "error"]
            if errors:
                raise ValidationError(issues)
            stats["player_scores"] = build_player_scores(con, season)
            stats["context"] = build_context(con, season, _next_week(con, season, has_espn))
        if has_espn:
            stats["managers"] = build_manager_metrics(con)
            stats["waivers"] = build_waivers(con)
        _record_issues(con, run.run_id, issues)
        con.execute("COMMIT")
    except ValidationError as err:
        con.execute("ROLLBACK")
        _record_issues(con, run.run_id, err.issues)
        run.finish("error", f"validation failed: {err}")
        raise
    except Exception as err:
        con.execute("ROLLBACK")
        run.finish("error", f"{type(err).__name__}: {err}")
        raise
    stats["validation"] = {"errors": 0, "warnings": sum(1 for i in issues if i.severity == "warning")}
    run.finish("ok", f"warnings: {stats['validation']['warnings']}", stats)
    for step, s in stats.items():
        log.info("%s: %s", step, s)
    for i in issues:
        log.warning("validation %s %s: %s (%d)", i.severity, i.check_name, i.detail, i.count)
    return stats

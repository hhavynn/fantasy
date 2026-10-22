"""Player availability model + latest-injury-report selection.

Statuses are combined from ESPN (current) and the official NFL report for the
*current* week, taking the more severe. Each status carries a deterministic
availability weight used for immediate-week roster needs. The weights are risk
discounts, not play probabilities.
"""

from __future__ import annotations

from ..db.connection import fetch_dicts, table_columns, table_exists
from ..espn.constants import IR_SLOT

AVAILABLE, QUESTIONABLE, DOUBTFUL, OUT, IR, SUSPENDED, TAXI = (
    "AVAILABLE", "QUESTIONABLE", "DOUBTFUL", "OUT", "IR", "SUSPENDED", "TAXI")

# TAXI: a Sleeper taxi-squad player (analytics/reserve_slots.py) cannot start for his fantasy team whatever his health.
WEIGHTS = {AVAILABLE: 1.0, QUESTIONABLE: 0.75, DOUBTFUL: 0.15, OUT: 0.0, IR: 0.0, SUSPENDED: 0.0, TAXI: 0.0}
SEVERITY = {AVAILABLE: 0, QUESTIONABLE: 1, DOUBTFUL: 2, OUT: 3, SUSPENDED: 4, IR: 5, TAXI: 6}

ESPN_STATUS = {
    "ACTIVE": AVAILABLE, "NORMAL": AVAILABLE, "QUESTIONABLE": QUESTIONABLE, "DAY_TO_DAY": QUESTIONABLE,
    "DOUBTFUL": DOUBTFUL, "OUT": OUT, "INJURY_RESERVE": IR, "SUSPENSION": SUSPENDED,
}
NFL_REPORT_STATUS = {"Questionable": QUESTIONABLE, "Doubtful": DOUBTFUL, "Out": OUT}


def availability(espn_status: str | None, lineup_slot_id: int | None = None,
                 nfl_report_status: str | None = None, taxi: bool = False) -> dict:
    """-> {"status", "weight", "sources"}; the most severe signal wins. `taxi`: on a Sleeper taxi squad."""
    candidates = [(AVAILABLE, "default")]
    if espn_status:
        candidates.append((ESPN_STATUS.get(espn_status.upper(), AVAILABLE), f"ESPN {espn_status}"))
    if lineup_slot_id == IR_SLOT:
        candidates.append((IR, "ESPN IR slot"))
    if taxi:
        candidates.append((TAXI, "Sleeper taxi squad"))
    if nfl_report_status in NFL_REPORT_STATUS:
        candidates.append((NFL_REPORT_STATUS[nfl_report_status], f"NFL report {nfl_report_status}"))
    status = max(candidates, key=lambda c: SEVERITY[c[0]])[0]
    sources = [src for st, src in candidates if st == status and src != "default"]
    return {"status": status, "weight": WEIGHTS[status], "sources": sources}


_REPORT_SEVERITY_SQL = "CASE report_status WHEN 'Out' THEN 3 WHEN 'Doubtful' THEN 2 WHEN 'Questionable' THEN 1 ELSE 0 END"


def injury_order_sql(con) -> str:
    """ORDER BY for 'newest report first'. Uses date_modified when nflverse ships it;
    otherwise falls back to a deterministic (severity, practice status) tie-break."""
    cols = set(table_columns(con, "nfl_injuries"))
    parts = ["week DESC"]
    if "date_modified" in cols:
        parts.append("date_modified DESC NULLS LAST")
    parts += [f"{_REPORT_SEVERITY_SQL} DESC", "practice_status NULLS LAST", "report_primary_injury NULLS LAST"]
    return ", ".join(parts)


def latest_injuries(con, season: int, per_week: bool = False) -> dict:
    """Latest report per player (or per player-week). Keys: gsis_id or (gsis_id, week)."""
    if not table_exists(con, "nfl_injuries"):
        return {}
    part = "gsis_id, week" if per_week else "gsis_id"
    cols = set(table_columns(con, "nfl_injuries"))
    dm = ", date_modified" if "date_modified" in cols else ", NULL AS date_modified"
    rows = fetch_dicts(con, f"""
        SELECT gsis_id, week, report_status, report_primary_injury, practice_status{dm} FROM (
          SELECT *, row_number() OVER (PARTITION BY {part} ORDER BY {injury_order_sql(con)}) rn
          FROM nfl_injuries WHERE season = ? AND gsis_id IS NOT NULL) WHERE rn = 1
    """, [season])
    return {((r["gsis_id"], r["week"]) if per_week else r["gsis_id"]): r for r in rows}


def duplicate_injury_reports(con, season: int) -> tuple[int, bool]:
    """(# player-weeks with >1 report row, whether date_modified is available to order them)."""
    if not table_exists(con, "nfl_injuries"):
        return 0, False
    n = con.execute("""SELECT count(*) FROM (SELECT gsis_id, week FROM nfl_injuries WHERE season = ?
                       GROUP BY 1, 2 HAVING count(*) > 1)""", [season]).fetchone()[0]
    return n, "date_modified" in set(table_columns(con, "nfl_injuries"))

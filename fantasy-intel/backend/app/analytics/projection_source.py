"""Weekly projection source switch: ESPN (default) or Fantasy Intel's independent model.

Opt-in only. Every consumer defaults to ESPN; a request must pass source=independent to see the
independent numbers. Nothing here trains, syncs or writes, and it uses SQL only (the hosted API has
no research packages).

For one week, `weekly(con, season, week, source)` returns a projection per ESPN player id and where
each number came from (`origin`):

  ours       the independent model's expected points (p_play x points-if-he-plays): unconditional,
             like ESPN's weekly projection, so the two are interchangeable in a lineup.
  ours_zero  the independent run says he cannot score this week: a bye, or withheld as unavailable
             (ESPN OUT / IR / suspension, NFL-ineligible, not on the active roster). 0.0, as ESPN does.
  espn       no independent estimate exists for him (K and D/ST have no model; rookies without an NFL
             game, uncertain ID mapping, no participation estimate): ESPN's projection, labeled.

The independent run must be the current model version under the league's current scoring; without
one the switch reports `available: false` and the consumer keeps ESPN.
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists

SOURCES = ("espn", "independent")
OURS, OURS_ZERO, ESPN = "ours", "ours_zero", "espn"
ZERO_STATUSES = {"bye", "withheld_unavailable"}
MODEL_VERSION = "ifp-1.0.0"     # app.analytics.independent_projections.MODEL_VERSION (numpy-free copy, tested)


def normalize(source: str | None) -> str:
    s = (source or "espn").lower()
    if s not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    return s


def _espn(con, season: int, week: int) -> dict[int, float | None]:
    return {r["espn_player_id"]: None if r["projected_points"] is None else float(r["projected_points"])
            for r in fetch_dicts(con, """SELECT espn_player_id, projected_points FROM espn_player_weekly_points
                                         WHERE season = ? AND scoring_period = ?""", [season, week])}


def _scoring_fingerprint(con, season: int) -> str | None:
    """The live league's scoring fingerprint, computed the way the live run stamps it."""
    from .league_scoring import resolve_skill_scoring

    rows = fetch_dicts(con, "SELECT scoring_items FROM leagues WHERE season = ? ORDER BY synced_at DESC LIMIT 1",
                       [season]) if table_exists(con, "leagues") else []
    if not rows:
        return None
    items = rows[0]["scoring_items"]
    scoring = resolve_skill_scoring(json.loads(items) if isinstance(items, str) else items)
    return scoring.fingerprint if scoring.supported else None


def _run(con, season: int, week: int) -> dict | None:
    if not (table_exists(con, "independent_projection_runs") and table_exists(con, "independent_player_projections")):
        return None
    fp = _scoring_fingerprint(con, season)
    if fp is None:
        return None
    rows = fetch_dicts(con, """SELECT run_id, model_version, created_at, model_status FROM independent_projection_runs
                               WHERE season = ? AND week = ? AND model_version = ? AND scoring_fingerprint = ?
                               ORDER BY created_at DESC LIMIT 1""", [season, week, MODEL_VERSION, fp])
    return rows[0] if rows else None


def weekly(con, season: int, week: int, source: str) -> dict:
    """{source, available, reason, values {pid: float|None}, origin {pid: str}, run, model_status, counts}."""
    espn = _espn(con, season, week)
    base = {"source": source, "available": True, "reason": None, "values": espn,
            "origin": {pid: ESPN for pid in espn}, "run": None, "model_status": None, "counts": None}
    if source == "espn":
        return base
    run = _run(con, season, week)
    if run is None:
        return {**base, "available": False,
                "reason": f"No independent projection run of {MODEL_VERSION} under the current league scoring is "
                          f"stored for week {week}, so ESPN projections are shown."}
    values: dict[int, float | None] = dict(espn)
    origin: dict[int, str] = {pid: ESPN for pid in espn}
    counts = {OURS: 0, OURS_ZERO: 0, ESPN: 0}
    for r in fetch_dicts(con, """SELECT espn_player_id, coverage_status, expected_points FROM independent_player_projections
                                 WHERE run_id = ?""", [run["run_id"]]):
        pid = r["espn_player_id"]
        if r["coverage_status"] == "projected" and r["expected_points"] is not None:
            values[pid], origin[pid] = float(r["expected_points"]), OURS
        elif r["coverage_status"] in ZERO_STATUSES:
            values[pid], origin[pid] = 0.0, OURS_ZERO
    for o in origin.values():
        counts[o] += 1
    status = run["model_status"]
    status = json.loads(status) if isinstance(status, str) else (status or {})
    return {**base, "values": values, "origin": origin, "counts": counts,
            "run": {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"])},
            "model_status": {p: (s or {}).get("status") for p, s in status.items()}}


def describe(w: dict) -> dict:
    """The part of `weekly` a response carries (no per-player maps)."""
    return {k: w[k] for k in ("source", "available", "reason", "run", "model_status", "counts")}


NOTE = ("Independent projections are Fantasy Intel's own research model (expected points). QB is validated against "
        "trailing-average baselines only; RB, WR and TE are experimental; none is validated against ESPN. K and D/ST, "
        "and players the model cannot project, keep ESPN's number and are marked.")

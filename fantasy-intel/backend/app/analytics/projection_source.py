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

The independent run must be the approved production model (MODEL_VERSION; the registry manifest) under the league's
current scoring and, made by an approved artifact (production_pin: an ifp-2.0.0 run must use an artifact registered in the
database's approved_artifacts table, an ifp-1.0.0 run the sha pin once it is set; runs record the sha256 of the
artifact file bytes in artifact_id; a database without that table is read unverified). A week with
no production run falls back to the previous production model (FALLBACK_VERSION) and says so in `reason`;
with neither the switch reports `available: false` and the consumer keeps ESPN. Runs are immutable: the newest run that still has its stored rows is used, read from
independent_projection_rows (the legacy table only when the database has not been migrated to schema v8).
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists
from . import production_forecasts as pf

SOURCES = ("espn", "independent")
OURS, OURS_ZERO, ESPN = "ours", "ours_zero", "espn"
ZERO_STATUSES = {"bye", "withheld_unavailable"}
MODEL_VERSION = "ifp-2.0.0"     # approved production (registry manifest); app.analytics.challenger.CHALLENGER_VERSION
FALLBACK_VERSION = "ifp-1.0.0"  # previous production (app.analytics.independent_projections.MODEL_VERSION), read
                                # only for a week without a MODEL_VERSION run; numpy-free copies, tested
# The approved-artifact pin (for MODEL_VERSION only) lives in the leaf module production_pin, read at call time.


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
    """Newest run of the production model under the league's current scoring that still has its rows, else
    the same for FALLBACK_VERSION (production_forecasts.latest_run; runs are immutable, a later run never
    deletes an earlier one)."""
    fp = _scoring_fingerprint(con, season)
    if fp is None:
        return None
    # latest_run applies the artifact rule of production_pin per version: MODEL_VERSION runs must use a registered
    # artifact, FALLBACK_VERSION runs the sha pin once it is set; a rejected run is skipped like a run without rows
    for version in (MODEL_VERSION, FALLBACK_VERSION):
        run = pf.latest_run(con, season, week, model_version=version, scoring_fingerprint=fp)
        if run is not None:
            return run
    return None


def weekly(con, season: int, week: int, source: str) -> dict:
    """{source, available, reason, values {pid: float|None}, origin {pid: str}, run, model_status, counts}."""
    espn = _espn(con, season, week)
    base = {"source": source, "available": True, "reason": None, "values": espn,
            "origin": {pid: ESPN for pid in espn}, "run": None, "model_status": None, "counts": None}
    if source == "espn":
        return base
    run = _run(con, season, week)
    if run is None:
        gated = any(pf.gate_active(con, v) for v in (MODEL_VERSION, FALLBACK_VERSION))
        return {**base, "available": False,
                "reason": f"No independent projection run of {MODEL_VERSION} (or {FALLBACK_VERSION}) under the current "
                          f"league scoring is stored for week {week}, so ESPN projections are shown."
                          + (" Runs made by an artifact that is not approved are never shown." if gated else "")}
    values: dict[int, float | None] = dict(espn)
    origin: dict[int, str] = {pid: ESPN for pid in espn}
    counts = {OURS: 0, OURS_ZERO: 0, ESPN: 0}
    for r in fetch_dicts(con, f"""SELECT espn_player_id, coverage_status, expected_points FROM {pf.rows_source(con)}
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
    reason = None if run["model_version"] == MODEL_VERSION else (
        f"No {MODEL_VERSION} run is stored for week {week} yet, so the previous model {run['model_version']} is shown.")
    run_info = {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"])}
    check = run.get("artifact_check")
    if check == "verified":     # artifact_verified only appears when verification ran (the response shape is otherwise unchanged)
        run_info["artifact_verified"] = True
    elif check == "unverified":     # fail OPEN: this database cannot say which artifacts are approved
        run_info["artifact_verified"] = False
        note = ("The approval of this run's artifact could not be verified (this database has no approved_artifacts "
                "table yet), so the latest run is shown unverified.")
        reason = f"{reason} {note}" if reason else note
    return {**base, "reason": reason, "values": values, "origin": origin, "counts": counts, "run": run_info,
            "model_status": {p: (s or {}).get("status") for p, s in status.items()}}


def describe(w: dict) -> dict:
    """The part of `weekly` a response carries (no per-player maps)."""
    return {k: w[k] for k in ("source", "available", "reason", "run", "model_status", "counts")}


NOTE = ("Independent projections are Fantasy Intel's own research model (expected points, ifp-2.0.0, retrained "
        "weekly). It has not met its promotion criteria and nothing is validated against ESPN. K and D/ST, and "
        "players the model cannot project, keep ESPN's number and are marked.")

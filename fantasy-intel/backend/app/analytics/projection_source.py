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
  espn       no independent estimate exists for him (no verified K or D/ST run; rookies without an NFL
             game, uncertain ID mapping, no participation estimate): ESPN's projection, labeled.

Kickers (K) are not in the skill-position run: they come from the separate kicker model's verified run
(kicker_store.select_run: kfp-1.0.0, the league's kicker scoring, approved artifact, content hash), applied
by `_kicker_overlay` only to players the skill run left on ESPN, and reported under `kicker_run`. A week
without a verified kicker run keeps ESPN's kicker numbers.
D/ST works the same way with the D/ST model's verified run (dst_store.select_run: dfp-r1, the league's D/ST
scoring, approved artifact, content hash) via `_dst_overlay`, reported under `dst_run`.

Which run consumers see (`select_run`, the one selection every consumer view uses: this switch, and
GET /api/projections/independent):

  1. the league's current scoring must be supported (else nothing is shown: ESPN);
  2. candidates are runs of MODEL_VERSION (production, the registry manifest) under that scoring, newest first;
     only when none qualifies, runs of FALLBACK_VERSION (the retired previous production model, its rollback
     target), labeled as the fallback in `reason` and `governance`;
  3. a run qualifies only when its artifact is approved (production_forecasts.artifact_gate with the rules of
     production_pin: an ifp-2.1.0 or ifp-2.0.0 run's artifact must be registered for its version in the write-once
     approved_artifacts table; a database without registrations for the version is read unverified, fail open, and
     says so) AND its stored rows verify against its content hash
     (production_forecasts.integrity_problem, the same rule the evidence readers apply). A run that fails is
     skipped and reported in `skipped`, never shown. A database not migrated to schema v8 shows nothing.

This is the LIVE view: the newest qualifying run, whenever it was made. Accuracy evidence uses a stricter
population (production_forecasts.pregame_forecasts: per player, only rows made before his own kickoff);
`timing` says how many of the shown rows are pregame. Nothing here writes, so a late run never changes a
stored forecast.

Production status (GOVERNANCE, mirrored from the registry manifest and tested against it): ifp-2.1.0 (ifp-2.0.0
plus the injury workload handoff and return-rate availability, app.analytics.injury_model) is production by the
owner's decision, recorded as an owner override. ifp-2.0.0 is the fallback and rollback target; ifp-1.0.0 is retired
and no longer served (its pinned artifact rule still applies to readers that ask for it by version).
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists
from . import production_forecasts as pf

SOURCES = ("espn", "independent")
OURS, OURS_ZERO, ESPN = "ours", "ours_zero", "espn"
ZERO_STATUSES = {"bye", "withheld_unavailable"}
POSITIONS = ("QB", "RB", "WR", "TE")
MODEL_VERSION = "ifp-2.1.0"     # approved production (registry manifest); app.analytics.injury_model.MODEL_VERSION
FALLBACK_VERSION = "ifp-2.0.0"  # previous production (app.analytics.challenger.CHALLENGER_VERSION), read only for a
                                # week without a MODEL_VERSION run; numpy-free copies, tested
RETIRED_VERSION = "ifp-1.0.0"   # production before ifp-2.0.0; never served any more
# The artifact rules (approved_artifacts for the weekly-retrained versions, the ifp-1.0.0 sha pin) live in the leaf
# module production_pin and are applied by production_forecasts.artifact_gate, read at call time.
CONSUMER_VERSIONS = (MODEL_VERSION, FALLBACK_VERSION)
GATE_REASONS = (pf.NOT_REGISTERED, pf.NOT_APPROVED_ARTIFACT)

# What consumers may say about each served model. Mirrors the registry manifest (tested): ifp-2.1.0 is approved by
# the owner (recorded as an override), start_sit_ready is false for both.
GOVERNANCE = {
    MODEL_VERSION: {
        "role": "production", "approval": "owner_override", "start_sit_ready": False,
        "artifact_identity": "per-run: each weekly artifact is approved in approved_artifacts by the sha256 of its file",
        "status_note": "Production model: ifp-2.0.0 plus injury-aware workload and availability, retrained weekly."},
    FALLBACK_VERSION: {
        "role": "fallback", "approval": "retired_rollback_target", "start_sit_ready": False,
        "artifact_identity": "per-run: each weekly artifact is approved in approved_artifacts by the sha256 of its file",
        "status_note": f"Previous production model, shown only for a week without an eligible {MODEL_VERSION} run."},
}
OVERRIDE_STATUS = "owner_override"


def normalize(source: str | None) -> str:
    s = (source or "espn").lower()
    if s not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    return s


def _espn(con, season: int, week: int) -> dict[int, float | None]:
    return {r["espn_player_id"]: None if r["projected_points"] is None else float(r["projected_points"])
            for r in fetch_dicts(con, """SELECT espn_player_id, projected_points FROM espn_player_weekly_points
                                         WHERE season = ? AND scoring_period = ?""", [season, week])}


def _scoring(con, season: int) -> tuple[str | None, str | None]:
    """(the live league's scoring fingerprint computed the way the live run stamps it, why it is unusable)."""
    from .league_scoring import resolve_skill_scoring

    rows = fetch_dicts(con, "SELECT scoring_items FROM leagues WHERE season = ? ORDER BY synced_at DESC LIMIT 1",
                       [season]) if table_exists(con, "leagues") else []
    if not rows:
        return None, "the league's scoring settings are not synced"
    items = rows[0]["scoring_items"]
    scoring = resolve_skill_scoring(json.loads(items) if isinstance(items, str) else items)
    if not scoring.supported:
        return None, "the league's scoring is not supported by the independent model"
    return scoring.fingerprint, None


def _scoring_fingerprint(con, season: int) -> str | None:
    return _scoring(con, season)[0]


def _gate_rejected(s: dict) -> bool:
    return any(s["reason"].startswith(g) for g in GATE_REASONS)


def select_run(con, season: int, week: int, *, versions: tuple[str, ...] = CONSUMER_VERSIONS) -> dict:
    """The run consumers see for the week (module docstring). Returns {run, rows, fallback, scoring_fingerprint,
    skipped [{run_id, model_version, created_at, reason}], unavailable_reason}; run/rows are None when nothing
    qualifies. `rows` are exactly the rows that were verified, so callers never re-read them. The run carries
    `artifact_check` / `artifact_note` (production_forecasts.artifact_gate)."""
    out = {"run": None, "rows": None, "fallback": False, "scoring_fingerprint": None, "skipped": [],
           "unavailable_reason": None}
    fp, why = _scoring(con, season)
    if fp is None:
        return {**out, "unavailable_reason": f"No independent projections: {why}."}
    out["scoring_fingerprint"] = fp
    if table_exists(con, pf.RUNS) and not table_exists(con, pf.ROWS):
        return {**out, "unavailable_reason": "No independent projections: the database is not migrated to schema v8, "
                "so no stored run can be integrity-checked."}
    for version in versions:
        for run, rows, problem in pf.verified_runs(con, season, week, model_version=version, scoring_fingerprint=fp):
            if problem is None:
                return {**out, "run": run, "rows": rows, "fallback": version != versions[0]}
            out["skipped"].append({"run_id": run["run_id"], "model_version": run["model_version"],
                                   "created_at": str(run["created_at"]), "reason": problem})
    skipped = out["skipped"]
    if not skipped:
        why = (f"No independent projection run of {MODEL_VERSION} (or {FALLBACK_VERSION}) under the current league "
               f"scoring is stored for week {week}, so ESPN projections are shown.")
    else:
        detail = "; ".join(f"{s['run_id']} ({s['model_version']}): {s['reason']}" for s in skipped)
        lead = (f"No independent projection run of {MODEL_VERSION} (or {FALLBACK_VERSION}) with an approved artifact is "
                f"available for week {week}: a stored run was made by an artifact that is not approved (runs made by "
                "an artifact that is not approved are never shown)") if any(_gate_rejected(s) for s in skipped) else (
                f"No stored independent projection run for week {week} passed the integrity checks")
        why = f"{lead} ({detail}), so ESPN projections are shown."
    return {**out, "unavailable_reason": why}


def _run(con, season: int, week: int) -> dict | None:
    return select_run(con, season, week)["run"]


def governance(served_version: str | None) -> dict:
    """What a consumer may claim about the model it shows (GOVERNANCE)."""
    g = GOVERNANCE.get(served_version) or {}
    return {"production_version": MODEL_VERSION, "fallback_version": FALLBACK_VERSION,
            "served_version": served_version, "is_fallback": served_version == FALLBACK_VERSION,
            "production_approval": GOVERNANCE[MODEL_VERSION]["approval"],
            "espn_superiority_established": False, "start_sit_ready": False,
            "production_evidence": "Historical research: 2023-24 confirmation backtest (docs/research/"
                                   "injury_workload_handoff.md); accuracy is tracked weekly from frozen pregame "
                                   "forecasts.",
            "status_note": g.get("status_note")}


def consumer_status(run: dict) -> dict:
    """Per-position status consumers show. The stored model_status is frozen with the run at training time: an
    ifp-2.0.0 run says "shadow ... never read by consumers", which stopped being true at the owner's override, so
    both owner-approved versions (production and its fallback) read OVERRIDE_STATUS; any other version keeps its
    stored status."""
    status = run.get("model_status")
    status = json.loads(status) if isinstance(status, str) else (status or {})
    if run.get("model_version") in CONSUMER_VERSIONS:
        return {p: OVERRIDE_STATUS for p in POSITIONS}
    return {p: (s or {}).get("status") for p, s in status.items()}


def timing(con, run: dict, rows: list[dict]) -> dict:
    """How many shown rows were made before the player's own kickoff (the only rows that can be evidence)."""
    now_kick = pf.team_kickoffs(con, run["season"], run["week"])
    pre = sum(pf.is_pregame(run["created_at"], pf.own_kickoff(r, run["week"], now_kick)) for r in rows)
    return {"run_created_at": str(run["created_at"]), "rows_pregame": pre, "rows_not_pregame": len(rows) - pre,
            "note": "Live view: the newest verified run. Accuracy evidence counts only rows made before each player's "
                    "own kickoff; showing a later run never changes a stored forecast."}


def _fallback_reason(week: int, run: dict, skipped: list[dict]) -> str:
    prod = [s for s in skipped if s["model_version"] == MODEL_VERSION]
    if any(_gate_rejected(s) for s in prod):
        lead = (f"The {MODEL_VERSION} run for week {week} was made by an artifact that is not approved, so the "
                f"previous model {run['model_version']} is shown.")
    elif prod:
        lead = (f"The stored {MODEL_VERSION} run(s) for week {week} failed the integrity check (see skipped), so the "
                f"previous model {run['model_version']} is shown.")
    else:
        lead = f"No {MODEL_VERSION} run is stored for week {week} yet, so the previous model {run['model_version']} is shown."
    return (f"{lead} It is the labeled fallback (the retired previous production model and rollback target), not the "
            "production model.")


def weekly(con, season: int, week: int, source: str) -> dict:
    """{source, available, reason, values {pid: float|None}, origin {pid: str}, run, model_status, counts}; with
    source=independent also governance, timing and skipped."""
    espn = _espn(con, season, week)
    base = {"source": source, "available": True, "reason": None, "values": espn,
            "origin": {pid: ESPN for pid in espn}, "run": None, "model_status": None, "counts": None}
    if source == "espn":
        return base
    sel = select_run(con, season, week)
    extra = {"governance": governance(None), "timing": None, "skipped": sel["skipped"]}
    run = sel["run"]
    if run is None:
        return {**base, **extra, "available": False, "reason": sel["unavailable_reason"]}
    values: dict[int, float | None] = dict(espn)
    origin: dict[int, str] = {pid: ESPN for pid in espn}
    counts = {OURS: 0, OURS_ZERO: 0, ESPN: 0}
    for r in sel["rows"]:
        pid = r["espn_player_id"]
        if r["coverage_status"] == "projected" and r["expected_points"] is not None:
            values[pid], origin[pid] = float(r["expected_points"]), OURS
        elif r["coverage_status"] in ZERO_STATUSES:
            values[pid], origin[pid] = 0.0, OURS_ZERO
    kicker_run = _kicker_overlay(con, season, week, values, origin)
    dst_run = _dst_overlay(con, season, week, values, origin)
    for o in origin.values():
        counts[o] += 1
    reason = _fallback_reason(week, run, sel["skipped"]) if sel["fallback"] else None
    # forecast_horizon k = week - projected_from_week (NULL on a run stored before rest-of-season projections reads 0);
    # horizon_note is the experimental label of a rest-of-season run (None at horizon 0, the validated next-week forecast)
    run_info = {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"]),
                "forecast_horizon": pf.horizon_of(run), "projected_from_week": run.get("projected_from_week"),
                "horizon_note": pf.horizon_note(run)}
    check = run.get("artifact_check")
    if check == "verified":     # artifact_verified only appears when verification ran (the response shape is otherwise unchanged)
        run_info["artifact_verified"] = True
    elif check == "unverified":     # fail OPEN: this database cannot say which artifacts are approved
        run_info["artifact_verified"] = False
        note = (f"The approval of this run's artifact could not be verified ({run.get('artifact_note') or 'unknown'}), "
                "so the latest run is shown unverified.")
        reason = f"{reason} {note}" if reason else note
    return {**base, **extra, "reason": reason, "values": values, "origin": origin, "counts": counts, "run": run_info,
            "model_status": consumer_status(run), "governance": governance(run["model_version"]),
            "timing": timing(con, run, sel["rows"]), "kicker_run": kicker_run, "dst_run": dst_run}


def _kicker_overlay(con, season: int, week: int, values: dict, origin: dict) -> dict | None:
    """Kicker values from the verified kfp run (kicker_store.overlay), only for players still on ESPN. Mutates
    `values` / `origin`; returns the kicker run's info (None when no verified kicker run exists)."""
    from . import kicker_store

    k = kicker_store.overlay(con, season, week)
    for pid, v in k["values"].items():
        if origin.get(pid, ESPN) == ESPN:
            values[pid], origin[pid] = v, k["origin"][pid]
    return k["run"]


def _dst_overlay(con, season: int, week: int, values: dict, origin: dict) -> dict | None:
    """D/ST values from the verified dfp-r1 run (dst_store.overlay), only for players still on ESPN. Mutates
    `values` / `origin`; returns the D/ST run's info (None when no verified D/ST run exists: ESPN's D/ST numbers)."""
    from . import dst_store

    d = dst_store.overlay(con, season, week)
    for pid, v in d["values"].items():
        if origin.get(pid, ESPN) == ESPN:
            values[pid], origin[pid] = v, d["origin"][pid]
    return d["run"]


def describe(w: dict) -> dict:
    """The part of `weekly` a response carries (no per-player maps)."""
    keys = ("source", "available", "reason", "run", "model_status", "counts", "governance", "timing", "skipped",
            "kicker_run", "dst_run")
    return {k: w[k] for k in keys if k in w}


NOTE = (f"Independent projections are Fantasy Intel's own model (expected points). The production model {MODEL_VERSION} "
        "(retrained weekly) accounts for injuries: who inherits a missing teammate's workload, who takes it back on a "
        "return, and how often a player ruled out returns in later weeks. A week without an eligible "
        f"{MODEL_VERSION} run shows the previous model {FALLBACK_VERSION} as a labeled fallback. Kickers come from the "
        "separate kicker model kfp-1.0.0 when a verified kicker run exists. D/ST projections come from the "
        "D/ST model dfp-r1 when a verified D/ST run exists. Players the models cannot project keep ESPN's number and "
        "are marked.")

"""Opt-in "ours" series for the player forward view (GET /players/{id}/forward?source=independent).

The production model (ifp-2.0.0) is run for every remaining regular-season week (the rest-of-season cycle,
one immutable run per target week). This module reads those stored runs, one week at a time, through the
opt-in weekly switch (`projection_source.weekly`) and turns them into a per-week series and a total for ONE
player. It trains and writes nothing and is SQL-only, like the switch.

Opt-in only: `ours_forward` needs an explicit `source` and computes nothing unless it is "independent".
ESPN stays the default everywhere, so the forward view's ESPN output is unchanged unless a caller asks.

Per week the value comes from the newest eligible run for that week, labeled by origin:

  ours       the model's unconditional expected points;
  ours_zero  the run says he cannot score (bye, or withheld as unavailable): 0.0;
  espn       no independent estimate for this week (no run stored yet, K / D/ST, an abstention): ESPN's number,
             marked, and the week is NOT part of "ours";
  bye        the NFL schedule has him on a bye and no run said otherwise (not a projection, not required).

The ours rest-of-season total follows the same complete-or-null rule as ESPN's: it is NULL unless every
non-bye remaining regular-season week has an `ours` or `ours_zero` value. An ESPN-fallback week makes it
incomplete; a partial sum would look like an authoritative low projection.
"""

from __future__ import annotations

import json

from ..db.connection import fetch_dicts, table_exists
from . import production_forecasts as pf
from . import projection_source as ps

PRIMARY = ps.MODEL_VERSION
OURS_ORIGINS = (ps.OURS, ps.OURS_ZERO)
BYE = "bye"
LABEL_TAIL = "experimental beyond next week"
LABEL = f"Fantasy Intel projections ({PRIMARY}); {LABEL_TAIL}"


def normalize(source: str | None) -> str:
    """The same validation as the weekly switch (ValueError for an unknown source)."""
    return ps.normalize(source)


def label_for(versions: list[str]) -> str:
    """The label names the model versions actually shown, never just the production one."""
    return f"Fantasy Intel projections ({', '.join(versions or [PRIMARY])}); {LABEL_TAIL}"


def _stored_runs(con, season: int, weeks: list[int]) -> tuple[dict[int, dict], set[tuple[int, str]]]:
    """({week: run}, {(week, version) with a run the artifact rule rejected}) as `projection_source.weekly` would
    pick them, in ONE query: the newest run (created_at, run_id) of the production model, else of the previous
    model, under the league's current scoring, that the artifact gate accepts and that still has all of its rows
    (production_forecasts.latest_run's rule). The gate is production_forecasts.artifact_gate itself, built once per
    version; each chosen run carries `artifact_check` / `artifact_note` like latest_run's."""
    fp = ps._scoring_fingerprint(con, season)
    if fp is None or not table_exists(con, pf.RUNS) or pf.rows_source(con) is None:
        return {}, set()
    gates = {v: pf.artifact_gate(con, v) for v in (ps.MODEL_VERSION, ps.FALLBACK_VERSION)}
    runs = fetch_dicts(con, f"""SELECT * FROM {pf.RUNS} WHERE season = ? AND week IN (SELECT unnest(?))
                                AND scoring_fingerprint = ? AND model_version IN (?, ?)
                                ORDER BY created_at DESC, run_id DESC""",
                       [season, weeks, fp, ps.MODEL_VERSION, ps.FALLBACK_VERSION])
    checked = [(r, *gates[r["model_version"]](r)) for r in runs]
    rejected = {(r["week"], r["model_version"]) for r, status, _ in checked if status == "rejected"}
    accepted = [(r, status, why) for r, status, why in checked if status != "rejected"]
    intact = {r["run_id"] for r, _, _ in accepted}
    if accepted and table_exists(con, pf.ROWS):
        have = dict(con.execute(f"SELECT run_id, count(*) FROM {pf.ROWS} WHERE run_id IN (SELECT unnest(?)) "
                                "GROUP BY run_id", [sorted(intact)]).fetchall())
        intact = {r["run_id"] for r, _, _ in accepted if r.get("row_count") and have.get(r["run_id"]) == r["row_count"]}
    chosen: dict[int, dict] = {}
    for week in weeks:
        for version in (ps.MODEL_VERSION, ps.FALLBACK_VERSION):
            hit = next(((r, status, why) for r, status, why in accepted if r["week"] == week
                        and r["model_version"] == version and r["run_id"] in intact), None)
            if hit is not None:
                chosen[week] = {**hit[0], "artifact_check": hit[1], "artifact_note": hit[2]}
                break
    return chosen, rejected


def _week_reason(week: int, run: dict, rejected: set[tuple[int, str]]) -> str | None:
    """`projection_source.weekly`'s reason for a week that has a run (same wording; the equality test pins it)."""
    if run["model_version"] == PRIMARY:
        reason = None
    elif (week, PRIMARY) in rejected:
        reason = (f"The {PRIMARY} run for week {week} was made by an artifact that is not approved, so the "
                  f"previous model {run['model_version']} is shown.")
    else:
        reason = (f"No {PRIMARY} run is stored for week {week} yet, so the previous model "
                  f"{run['model_version']} is shown.")
    if run.get("artifact_check") == "unverified":
        note = (f"The approval of this run's artifact could not be verified ({run.get('artifact_note') or 'unknown'}), "
                "so the latest run is shown unverified.")
        reason = f"{reason} {note}" if reason else note
    return reason


def player_weeks(con, season: int, pid: int, weeks: list[int]) -> dict[int, dict]:
    """week -> {value, origin, run, reason} for ONE player, equal to `projection_source.weekly(...)` per week
    (values[pid], origin[pid], run, reason) but in a handful of queries: independent of the number of weeks."""
    espn = {r["scoring_period"]: None if r["projected_points"] is None else float(r["projected_points"])
            for r in fetch_dicts(con, """SELECT scoring_period, projected_points FROM espn_player_weekly_points
                                         WHERE season = ? AND espn_player_id = ?
                                           AND scoring_period IN (SELECT unnest(?))""", [season, pid, weeks])}
    runs, rejected = _stored_runs(con, season, weeks)
    rows = {}
    if runs:
        rows = {(r["run_id"]): r for r in fetch_dicts(
            con, f"""SELECT run_id, coverage_status, expected_points FROM {pf.rows_source(con)}
                     WHERE espn_player_id = ? AND run_id IN (SELECT unnest(?))""",
            [pid, sorted({r["run_id"] for r in runs.values()})])}
    out = {}
    for week in weeks:
        run, value, origin = runs.get(week), espn.get(week), ps.ESPN
        r = rows.get(run["run_id"]) if run else None
        if r and r["coverage_status"] == "projected" and r["expected_points"] is not None:
            value, origin = float(r["expected_points"]), ps.OURS
        elif r and r["coverage_status"] in ps.ZERO_STATUSES:
            value, origin = 0.0, ps.OURS_ZERO
        out[week] = {"value": value, "origin": origin, "run": run,
                     "reason": _week_reason(week, run, rejected) if run else None,
                     "any_rejected": any(w == week for w, _ in rejected)}
    return out


def _empty(reason: str, last_regular: int | None) -> dict:
    return {"available": False, "reason": reason, "label": LABEL, "model_version": PRIMARY,
            "model_versions": [], "mixed_models": False, "weeks": [],
            "ros_projected_points": None, "ros_weeks_expected": 0, "ros_weeks_available": 0, "ros_complete": False,
            "fallback_weeks": [], "missing_weeks": [], "other_model_weeks": [],
            "last_regular_season_week": last_regular, "run_created_at": None}


def ours_forward(con, season: int, pid: int, weeks: list[dict], *, source: str) -> dict | None:
    """The "ours" block for one player, or None unless source == "independent".

    `weeks` is the ESPN forward view's week list (projections.forward_weeks): it supplies each week's phase and
    bye flag (NFL schedule). Only the remaining regular-season weeks are projected here.

    Model versions: a week without a production-model run shows the previous model's run, labeled per week. The
    ours total counts ONLY production-model weeks: it is NULL (with a reason) when any non-bye week comes from
    another model or from ESPN, so a total never mixes models."""
    if normalize(source) != "independent":
        return None
    regular = [w for w in weeks if w["phase"] == "regular"]
    last_regular = max((w["week"] for w in regular), default=None)
    if not regular:
        return _empty("No remaining regular-season week (or the end of the regular season is unknown), "
                      "so there is nothing to project.", last_regular)
    stored = player_weeks(con, season, pid, [w["week"] for w in regular])
    entries, reasons = [], []
    for w in regular:
        got = stored[w["week"]]
        run, origin, value, on_bye = got["run"], got["origin"], got["value"], w["on_bye"]
        if origin == ps.ESPN and on_bye:       # a schedule bye is not an ESPN projection and not a gap to fill
            origin, value = BYE, None
        horizon = (run.get("forecast_horizon") or 0) if run else None     # NULL on legacy runs = 0
        entries.append({
            "week": w["week"], "value": value, "origin": origin, "on_bye": on_bye,
            "run_id": run["run_id"] if run else None, "model_version": run["model_version"] if run else None,
            "run_created_at": str(run["created_at"]) if run else None,
            "horizon": horizon, "projected_from_week": run.get("projected_from_week") if run else None,
            "experimental": horizon is not None and horizon > 0,
            # True: the run's artifact is registered / pinned; False: could not be verified (fail open); None: no rule
            "artifact_verified": {"verified": True, "unverified": False}.get(run.get("artifact_check")) if run else None,
        })
        if got["reason"] and got["reason"] not in reasons:
            reasons.append(got["reason"])
    if not any(e["run_id"] for e in entries):
        if any(stored[w["week"]]["any_rejected"] for w in regular):
            return _empty(f"No {PRIMARY} projection run with an approved artifact is available for the remaining "
                          "regular-season weeks (a stored run was made by an artifact that is not approved and is "
                          "never shown), so ESPN projections are shown.", last_regular)
        return _empty(f"No {PRIMARY} projection run is stored for any remaining regular-season week yet "
                      "(they are written by the rest-of-season learning cycle), so ESPN projections are shown.",
                      last_regular)
    versions = sorted({e["model_version"] for e in entries if e["model_version"]})
    required = [e for e in entries if e["on_bye"] is not True]
    counted = [e for e in required if e["origin"] in OURS_ORIGINS and e["model_version"] == PRIMARY]
    fallback = [e["week"] for e in entries if e["origin"] == ps.ESPN]
    other = [e["week"] for e in entries if e["model_version"] not in (None, PRIMARY)]
    missing = [e["week"] for e in required if e not in counted]
    complete = not missing
    total = round(sum(e["value"] for e in entries if e["origin"] in OURS_ORIGINS and e["model_version"] == PRIMARY
                      and e["value"] is not None), 2) if complete else None
    if other:
        reasons.append(f"Mixed models: the rest-of-season total counts {PRIMARY} weeks only, so it is hidden while "
                       f"week(s) {', '.join(map(str, other))} use another model." if len(versions) > 1 else
                       f"The rest-of-season total counts {PRIMARY} weeks only, so it is hidden.")
    elif fallback:
        reasons.append(f"ESPN's number is shown for week(s) {', '.join(map(str, fallback))}: no independent "
                       "estimate yet.")
    created = [e["run_created_at"] for e in entries if e["run_created_at"]]
    return {
        "available": True, "reason": " ".join(reasons) or None,
        "label": label_for(versions), "model_version": PRIMARY, "model_versions": versions,
        "mixed_models": len(versions) > 1, "weeks": entries,
        "ros_projected_points": total, "ros_weeks_expected": len(required),
        "ros_weeks_available": len(counted), "ros_complete": complete,
        "fallback_weeks": fallback, "missing_weeks": missing, "other_model_weeks": other,
        "last_regular_season_week": last_regular, "run_created_at": max(created) if created else None,
    }

"""Opt-in "ours" series for the player forward view (GET /players/{id}/forward?source=independent).

The production model (ifp-2.1.0) is run for every remaining regular-season week (the rest-of-season cycle,
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
KICKER_VERSION = "kfp-1.0.0"       # copy of kicker_store.MODEL_VERSION (tested)
KICKER_HORIZON_NOTE = "rest-of-season kicker forecast: experimental, not validated beyond the next week"
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


def _select_batch(con, season: int, weeks: list[int]) -> tuple[dict[int, dict], str | None]:
    """week -> {run, rows, skipped, fallback} exactly as `projection_source.select_run` picks them, for several
    weeks at once. The policy is the switch's own: the production model first, else the previous model; within a
    model the newest run (created_at DESC NULLS LAST, run_id DESC) of ANY horizon that production_forecasts'
    artifact gate does not reject and whose stored rows pass `integrity_problem`. Reused, not re-implemented:
    `artifact_gate` (built once per version), `integrity_problem`, `horizon_of` / `horizon_note`, `_scoring`.

    Queries: the league scoring (once), the gates (once), ONE runs query for every week, ONE rows query for the
    first candidate of every (week, model), then one rows query per further candidate, only when the one before
    it was rejected or failed verification. Integrity needs ALL of a run's rows, so rows are not filtered by player
    here; the caller picks its player out of the verified rows."""
    fp, why = ps._scoring(con, season)
    if fp is None:       # select_run's own reason, word for word: scoring missing or unsupported
        return {}, f"No independent projections: {why}."
    if table_exists(con, pf.RUNS) and not table_exists(con, pf.ROWS):
        return {}, ("No independent projections: the database is not migrated to schema v8, "
                    "so no stored run can be integrity-checked.")
    if not table_exists(con, pf.RUNS):
        return {}, None
    versions = ps.CONSUMER_VERSIONS
    gates = {v: pf.artifact_gate(con, v) for v in versions}
    cand: dict[tuple[int, str], list[dict]] = {}
    for run in fetch_dicts(con, f"""SELECT * FROM {pf.RUNS} WHERE season = ? AND week IN (SELECT unnest(?))
                                    AND scoring_fingerprint = ? AND model_version IN (SELECT unnest(?))
                                    ORDER BY created_at DESC NULLS LAST, run_id DESC""",
                           [season, weeks, fp, list(versions)]):
        status, why = gates[run["model_version"]](run)
        cand.setdefault((run["week"], run["model_version"]), []).append(
            {**run, "artifact_check": status, "artifact_note": why, "horizon": pf.horizon_of(run),
             "horizon_note": pf.horizon_note(run)})
    firsts = sorted({lst[0]["run_id"] for lst in cand.values() if lst[0]["artifact_check"] != "rejected"})
    preread: dict[str, list[dict]] = {rid: [] for rid in firsts}
    if firsts:
        for r in fetch_dicts(con, f"SELECT * FROM {pf.ROWS} WHERE run_id IN (SELECT unnest(?))", [firsts]):
            preread[r["run_id"]].append(r)
    out = {}
    for week in weeks:
        sel = {"run": None, "rows": None, "skipped": [], "fallback": False}
        for version in versions:
            for run in cand.get((week, version), []):
                if run["artifact_check"] == "rejected":
                    rows, problem = [], run["artifact_note"]
                else:
                    rows = preread[run["run_id"]] if run["run_id"] in preread else fetch_dicts(
                        con, f"SELECT * FROM {pf.ROWS} WHERE run_id = ?", [run["run_id"]])
                    problem = pf.integrity_problem(run, rows)
                if problem is None:
                    sel.update(run=run, rows=rows, fallback=version != versions[0])
                    break
                sel["skipped"].append({"run_id": run["run_id"], "model_version": run["model_version"],
                                       "created_at": str(run["created_at"]), "reason": problem})
            if sel["run"] is not None:
                break
        out[week] = sel
    return out, None


def _week_reason(week: int, sel: dict) -> str | None:
    """`projection_source.weekly`'s reason for a week that has a run (its own fallback text; the equality test pins
    the wording, including the unverified-artifact note)."""
    run = sel["run"]
    reason = ps._fallback_reason(week, run, sel["skipped"]) if sel["fallback"] else None
    if run.get("artifact_check") == "unverified":
        note = (f"The approval of this run's artifact could not be verified ({run.get('artifact_note') or 'unknown'}), "
                "so the latest run is shown unverified.")
        reason = f"{reason} {note}" if reason else note
    return reason


def player_weeks(con, season: int, pid: int, weeks: list[int]) -> dict[int, dict]:
    """week -> {value, origin, run, reason, skipped} for ONE player, equal to `projection_source.weekly(...)` per
    week (values[pid], origin[pid], run, reason, skipped) with a number of queries that grows with the number of
    rejected or unverifiable candidate runs, not with the number of weeks."""
    espn = {r["scoring_period"]: None if r["projected_points"] is None else float(r["projected_points"])
            for r in fetch_dicts(con, """SELECT scoring_period, projected_points FROM espn_player_weekly_points
                                         WHERE season = ? AND espn_player_id = ?
                                           AND scoring_period IN (SELECT unnest(?))""", [season, pid, weeks])}
    selected, unavailable = _select_batch(con, season, weeks)
    kicker = _kicker_weeks(con, season, pid, weeks)
    out = {}
    for week in weeks:
        sel = selected.get(week) or {"run": None, "rows": None, "skipped": [], "fallback": False}
        run, value, origin = sel["run"], espn.get(week), ps.ESPN
        r = next((x for x in sel["rows"] or [] if x["espn_player_id"] == pid), None)
        if r and r["coverage_status"] == "projected" and r["expected_points"] is not None:
            value, origin = float(r["expected_points"]), ps.OURS
        elif r and r["coverage_status"] in ps.ZERO_STATUSES:
            value, origin = 0.0, ps.OURS_ZERO
        k = kicker.get(week)
        if run is not None and origin == ps.ESPN and k is not None:      # projection_source._kicker_overlay
            value, origin = k["value"], k["origin"]
        out[week] = {"value": value, "origin": origin, "run": run, "skipped": sel["skipped"],
                     "reason": _week_reason(week, sel) if run else None, "unavailable_reason": unavailable,
                     "kicker_run": k["run"] if k else None}
    return out


def _kicker_weeks(con, season: int, pid: int, weeks: list[int]) -> dict[int, dict]:
    """week -> {value, origin, run} from the verified kicker run (kicker_store.overlay), only for weeks where this
    player has a kicker row at all (one query for everyone else)."""
    from . import kicker_store

    if not table_exists(con, kicker_store.ROWS):
        return {}
    have = {r["week"] for r in fetch_dicts(con, f"""SELECT DISTINCT week FROM {kicker_store.ROWS}
                                                   WHERE season = ? AND espn_player_id = ?
                                                     AND week IN (SELECT unnest(?))""", [season, pid, weeks])}
    out = {}
    for week in sorted(have):
        k = kicker_store.overlay(con, season, week)
        if pid in k["values"]:
            out[week] = {"value": k["values"][pid], "origin": k["origin"][pid], "run": k["run"]}
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
        horizon = run["horizon"] if run else None     # production_forecasts.horizon_of: NULL on a legacy run = 0
        if got.get("kicker_run") and origin in OURS_ORIGINS:      # a kicker's week comes from the kicker model's run
            kr = got["kicker_run"]
            entries.append({"week": w["week"], "value": value, "origin": origin, "on_bye": on_bye,
                            "run_id": kr["run_id"], "model_version": kr["model_version"],
                            "run_created_at": kr["created_at"], "horizon": kr["forecast_horizon"],
                            "forecast_horizon": kr["forecast_horizon"], "projected_from_week": None,
                            "horizon_note": KICKER_HORIZON_NOTE if kr["forecast_horizon"] else None,
                            "experimental": bool(kr["forecast_horizon"]),
                            "artifact_verified": kr["artifact_verified"]})
            if got["reason"] and got["reason"] not in reasons:
                reasons.append(got["reason"])
            continue
        entries.append({
            "week": w["week"], "value": value, "origin": origin, "on_bye": on_bye,
            "run_id": run["run_id"] if run else None, "model_version": run["model_version"] if run else None,
            "run_created_at": str(run["created_at"]) if run else None,
            # the same three fields projection_source.weekly puts on its run object (forecast_horizon: NULL reads 0)
            "horizon": horizon, "forecast_horizon": horizon,
            "projected_from_week": run.get("projected_from_week") if run else None,
            "horizon_note": run["horizon_note"] if run else None,
            "experimental": bool(run and run.get("horizon_note")),
            # True: the run's artifact is registered / pinned; False: could not be verified (fail open); None: no rule
            "artifact_verified": {"verified": True, "unverified": False}.get(run.get("artifact_check")) if run else None,
        })
        if got["reason"] and got["reason"] not in reasons:
            reasons.append(got["reason"])
    if not any(e["run_id"] for e in entries):
        why = next((g["unavailable_reason"] for g in stored.values() if g["unavailable_reason"]), None)
        if why:      # scoring missing / unsupported, or an unmigrated database: the switch's own explanation
            return _empty(why, last_regular)
        skipped = [x for w in regular for x in stored[w["week"]]["skipped"]]
        if any(ps._gate_rejected(x) for x in skipped):
            return _empty(f"No {PRIMARY} projection run with an approved artifact is available for the remaining "
                          "regular-season weeks (a stored run was made by an artifact that is not approved and is "
                          "never shown), so ESPN projections are shown.", last_regular)
        if skipped:
            return _empty("No stored independent projection run for the remaining regular-season weeks passed the "
                          "integrity checks, so ESPN projections are shown.", last_regular)
        return _empty(f"No {PRIMARY} projection run is stored for any remaining regular-season week yet "
                      "(they are written by the rest-of-season learning cycle), so ESPN projections are shown.",
                      last_regular)
    versions = sorted({e["model_version"] for e in entries if e["model_version"]})
    # a kicker's weeks come from the kicker model: it is that player's primary model (never mixed with ifp in a total)
    primary = KICKER_VERSION if versions and all(v.startswith("kfp-") for v in versions) else PRIMARY
    required = [e for e in entries if e["on_bye"] is not True]
    counted = [e for e in required if e["origin"] in OURS_ORIGINS and e["model_version"] == primary]
    fallback = [e["week"] for e in entries if e["origin"] == ps.ESPN]
    other = [e["week"] for e in entries if e["model_version"] not in (None, primary)]
    missing = [e["week"] for e in required if e not in counted]
    complete = not missing
    total = round(sum(e["value"] for e in entries if e["origin"] in OURS_ORIGINS and e["model_version"] == primary
                      and e["value"] is not None), 2) if complete else None
    if other:
        reasons.append(f"Mixed models: the rest-of-season total counts {primary} weeks only, so it is hidden while "
                       f"week(s) {', '.join(map(str, other))} use another model." if len(versions) > 1 else
                       f"The rest-of-season total counts {primary} weeks only, so it is hidden.")
    elif fallback:
        reasons.append(f"ESPN's number is shown for week(s) {', '.join(map(str, fallback))}: no independent "
                       "estimate yet.")
    created = [e["run_created_at"] for e in entries if e["run_created_at"]]
    return {
        "available": True, "reason": " ".join(reasons) or None,
        "label": label_for(versions), "model_version": primary, "model_versions": versions,
        "mixed_models": len(versions) > 1, "weeks": entries,
        "ros_projected_points": total, "ros_weeks_expected": len(required),
        "ros_weeks_available": len(counted), "ros_complete": complete,
        "fallback_weeks": fallback, "missing_weeks": missing, "other_model_weeks": other,
        "last_regular_season_week": last_regular, "run_created_at": max(created) if created else None,
    }

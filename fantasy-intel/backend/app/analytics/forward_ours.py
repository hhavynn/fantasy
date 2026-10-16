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

from ..db.connection import table_columns
from . import production_forecasts as pf
from . import projection_source as ps

LABEL = f"Fantasy Intel projections ({ps.MODEL_VERSION}); experimental beyond next week"
OURS_ORIGINS = (ps.OURS, ps.OURS_ZERO)
BYE = "bye"


def normalize(source: str | None) -> str:
    """The same validation as the weekly switch (ValueError for an unknown source)."""
    return ps.normalize(source)


def _run_meta(con, run_id: str | None) -> dict:
    """forecast_horizon / projected_from_week of a stored run (NULL on legacy runs and on a schema without them)."""
    out = {"horizon": None, "projected_from_week": None}
    if not run_id:
        return out
    cols = set(table_columns(con, pf.RUNS))
    wanted = [c for c in ("forecast_horizon", "projected_from_week") if c in cols]
    if not wanted:
        return out
    row = con.execute(f"SELECT {', '.join(wanted)} FROM {pf.RUNS} WHERE run_id = ?", [run_id]).fetchone()
    if row:
        got = dict(zip(wanted, row))
        out["horizon"], out["projected_from_week"] = got.get("forecast_horizon"), got.get("projected_from_week")
    return out


def _empty(reason: str, last_regular: int | None) -> dict:
    return {"available": False, "reason": reason, "label": LABEL, "model_version": ps.MODEL_VERSION, "weeks": [],
            "ros_projected_points": None, "ros_weeks_expected": 0, "ros_weeks_available": 0, "ros_complete": False,
            "fallback_weeks": [], "missing_weeks": [], "last_regular_season_week": last_regular, "run_created_at": None}


def ours_forward(con, season: int, pid: int, weeks: list[dict], *, source: str) -> dict | None:
    """The "ours" block for one player, or None unless source == "independent".

    `weeks` is the ESPN forward view's week list (projections.forward_weeks): it supplies each week's phase and
    bye flag (NFL schedule). Only the remaining regular-season weeks are projected here."""
    if normalize(source) != "independent":
        return None
    regular = [w for w in weeks if w["phase"] == "regular"]
    last_regular = max((w["week"] for w in regular), default=None)
    if not regular:
        return _empty("No remaining regular-season week (or the end of the regular season is unknown), "
                      "so there is nothing to project.", last_regular)
    first = regular[0]["week"]
    entries, reasons = [], []
    for w in regular:
        wk = ps.weekly(con, season, w["week"], "independent")
        run = wk["run"]
        origin = wk["origin"].get(pid, ps.ESPN)
        value = wk["values"].get(pid)
        on_bye = w["on_bye"]
        if origin == ps.ESPN and on_bye:       # a schedule bye is not an ESPN projection and not a gap to fill
            origin, value = BYE, None
        meta = _run_meta(con, run["run_id"] if run else None)
        horizon = meta["horizon"]
        entries.append({
            "week": w["week"], "value": value, "origin": origin, "on_bye": on_bye,
            "run_id": run["run_id"] if run else None, "model_version": run["model_version"] if run else None,
            "run_created_at": run["created_at"] if run else None,
            "horizon": horizon, "projected_from_week": meta["projected_from_week"],
            "experimental": (horizon if horizon is not None else w["week"] - first) >= 1,
        })
        if run and wk["reason"] and wk["reason"] not in reasons:   # a previous-model week; absent runs are listed below
            reasons.append(wk["reason"])
    if not any(e["run_id"] for e in entries):
        return _empty(f"No {ps.MODEL_VERSION} projection run is stored for any remaining regular-season week yet "
                      "(they are written by the rest-of-season learning cycle), so ESPN projections are shown.",
                      last_regular)
    required = [e for e in entries if e["on_bye"] is not True]
    counted = [e for e in entries if e["origin"] in OURS_ORIGINS]
    fallback = [e["week"] for e in entries if e["origin"] == ps.ESPN]
    complete = all(e["origin"] in OURS_ORIGINS for e in required)
    total = round(sum(e["value"] for e in counted if e["value"] is not None), 2) if complete else None
    missing = [e["week"] for e in required if e["origin"] not in OURS_ORIGINS]
    created = [e["run_created_at"] for e in entries if e["run_created_at"]]
    return {
        "available": True,
        "reason": " ".join(reasons) or (f"ESPN's number is shown for week(s) {', '.join(map(str, fallback))}: no "
                                         "independent estimate yet." if fallback else None),
        "label": LABEL, "model_version": ps.MODEL_VERSION, "weeks": entries,
        "ros_projected_points": total, "ros_weeks_expected": len(required),
        "ros_weeks_available": len(required) - len(missing), "ros_complete": complete,
        "fallback_weeks": fallback, "missing_weeks": missing,
        "last_regular_season_week": last_regular, "run_created_at": max(created) if created else None,
    }

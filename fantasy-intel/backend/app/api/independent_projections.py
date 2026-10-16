"""Read-only independent projection contract (docs/independent_projection_engine.md, "API").

GET /api/projections/independent?week=7[&position=WR][&team_id=3]

No training, no syncs, no ESPN calls and no writes. Independent estimates and ESPN projections
are separate fields; an independent value is never replaced by ESPN's, and unknown is null.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import production_forecasts as pf
from ..db.connection import table_exists
from .deps import db, one, rows

router = APIRouter(prefix="/api", tags=["independent projections"])

SCHEMA_VERSION = "independent-projections/v2"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")
NOTES = [
    "independent.projected_points is Fantasy Intelligence's NFL-evidence model; it never uses ESPN projections.",
    "espn.projected_points is ESPN's number as synced; it is shown for comparison only.",
    "A null independent value means no independent estimate (see coverage_status); it is not zero.",
    "independent.projected_points is conditional on the player appearing. independent.expected_points = "
    "participation.p_play * projected_points is the unconditional expectation (null without a participation estimate).",
    "Compare ESPN with expected_points (comparison.expected_minus_espn): both are unconditional. "
    "independent_minus_espn compares a conditional estimate and is marked not comparable when ESPN projects 0.",
    "Experimental positions are research output: do not treat them as better than ESPN.",
    "This view shows the latest stored run and is not restricted to the approved production artifact; the opt-in "
    "projection switch and the evidence readers are.",
]


def _evidence(p: dict, diff: float | None) -> list[dict]:
    """Deterministic: the model's own largest input contributions plus data-quality flags. ESPN's
    reasoning is unknown, so nothing is said about why ESPN differs."""
    if diff is None:
        return []
    out = [{"kind": "model_input", "text": e["text"], "contribution": e["contribution"], "feature": e["feature"]}
           for e in (p.get("explanation") or [])]
    out += [{"kind": "data_quality", "text": f, "contribution": None, "feature": None} for f in (p.get("flags") or [])]
    out += [{"kind": "missing_input", "text": m, "contribution": None, "feature": None}
            for m in (p.get("missing_inputs") or [])]
    return out


def _player(p: dict, run: dict | None) -> dict:
    status = (run or {}).get("model_status") or {}
    pos_status = status.get(p["position"]) or {}
    indep = p.get("projected_points")
    espn = p.get("espn_projected_points")
    diff = round(indep - espn, 2) if indep is not None and espn is not None else None
    expected = p.get("expected_points")
    part = p.get("participation")
    exp_diff = round(expected - espn, 2) if expected is not None and espn is not None else None
    interval = ({"low": p["interval_low"], "high": p["interval_high"], "nominal_coverage": 0.8}
                if p.get("interval_low") is not None and p.get("interval_high") is not None else None)
    return {
        "espn_player_id": p["espn_player_id"], "gsis_id": p.get("gsis_id"), "name": p.get("full_name"),
        "position": p["position"], "nfl_team": p.get("nfl_team"), "opponent": p.get("opponent"),
        "is_home": p.get("is_home"), "fantasy_team_id": p.get("on_team_id") or None,
        "injury_status": p.get("injury_status"),
        "independent": {
            "projected_points": indep, "interval_80": interval, "model": p.get("model"),
            "model_version": (run or {}).get("model_version"),
            "model_status": pos_status.get("status") if p["position"] in ("QB", "RB", "WR", "TE") else "unsupported",
            "start_sit_ready": bool(pos_status.get("start_sit_ready")),
            "coverage_status": p.get("coverage_status"), "games_used": p.get("games_used"),
            "latest_usage": ({"season": p["latest_usage_season"], "week": p["latest_usage_week"]}
                             if p.get("latest_usage_week") is not None else None),
            "missing_inputs": p.get("missing_inputs") or [], "flags": p.get("flags") or [],
            "explanation": p.get("explanation") or [], "is_espn_fallback": False,
            "expected_points": expected,
            "participation": None if not part else {
                "p_play": part.get("p_play"), "model_version": part.get("model_version"),
                "model_status": part.get("status"), "expected_points_status": part.get("expected_points_status"),
                "depth_rank": part.get("depth_rank"), "explanation": part.get("explanation") or [],
                "flags": part.get("flags") or [], "note": part.get("note")},
        },
        "espn": {"projected_points": espn, "synced_at": str(p["espn_synced_at"]) if p.get("espn_synced_at") else None},
        "comparison": {"independent_minus_espn": diff, **_comparability(indep, espn),
                       "expected_minus_espn": exp_diff, "expected_comparable": exp_diff is not None,
                       "evidence": _evidence(p, diff)},
    }


def _comparability(indep: float | None, espn: float | None) -> dict:
    """ESPN projects 0 for players it does not expect to play; the independent estimate is
    conditional on playing, so that difference is about participation, not performance."""
    if indep is None or espn is None:
        return {"comparable": False, "comparability_note": "one or both projections unavailable"}
    if espn == 0:
        return {"comparable": False, "comparability_note": "ESPN projects 0 (it does not expect him to play); the "
                "independent estimate assumes he plays, so the difference is not a performance disagreement"}
    return {"comparable": True, "comparability_note": None}


def build_response(con, week: int | None, position: str | None, team_id: int | None) -> dict:
    lg = one(con, "SELECT league_id, season, current_week FROM leagues ORDER BY synced_at DESC LIMIT 1") \
        if table_exists(con, "leagues") else None
    if lg is None:
        raise LookupError("No ESPN league synced yet.")
    week = week or lg["current_week"]
    if position and position not in POSITIONS:
        raise ValueError(f"position must be one of {', '.join(POSITIONS)}")
    base = {"schema_version": SCHEMA_VERSION, "league": {"id": lg["league_id"], "season": lg["season"]},
            "week": week, "notes": NOTES}
    rows_table = pf.rows_source(con)
    if not (table_exists(con, "independent_projection_runs") and rows_table):
        return {**base, "available": False, "unavailable_reason": "Independent projections have not been computed "
                "(run scripts/backtest_independent_projections.py project).", "run": None, "model_status": None,
                "players": []}
    run = pf.latest_run(con, lg["season"], week)   # newest run that still has its rows; runs are immutable
    if run is None:
        return {**base, "available": False, "unavailable_reason": f"No independent projection run for week {week}.",
                "run": None, "model_status": None, "players": []}
    import json
    for k in ("model_status", "evaluation", "participation_status", "participation_evaluation"):
        if isinstance(run.get(k), str):
            run[k] = json.loads(run[k])
    where, params = ["p.season = ?", "p.week = ?", "p.run_id = ?"], [lg["season"], week, run["run_id"]]
    if position:
        where.append("p.position = ?")
        params.append(position)
    if team_id is not None:
        where.append("fp.on_team_id = ?")
        params.append(team_id)
    players = rows(con, f"""
        SELECT p.*, fp.full_name, fp.on_team_id, fp.injury_status,
               e.projected_points AS espn_projected_points, e.synced_at AS espn_synced_at
        FROM {rows_table} p
        LEFT JOIN fantasy_players fp ON fp.espn_player_id = p.espn_player_id
        LEFT JOIN espn_player_weekly_points e ON e.espn_player_id = p.espn_player_id
             AND e.season = p.season AND e.scoring_period = p.week
        WHERE {' AND '.join(where)}
        ORDER BY p.projected_points DESC NULLS LAST, p.espn_player_id""", params)
    for p in players:
        for k in ("missing_inputs", "flags", "explanation", "participation"):
            if isinstance(p.get(k), str):
                p[k] = json.loads(p[k])
    return {**base, "available": True, "unavailable_reason": None,
            "run": {"run_id": run["run_id"], "model_version": run["model_version"],
                    "artifact_created_at": run["artifact_created_at"], "created_at": str(run["created_at"]),
                    "scoring_fingerprint": run["scoring_fingerprint"],
                    "artifact_id": run.get("artifact_id"), "content_sha256": run.get("content_sha256"),
                    # rest-of-season runs: horizon k = week - projected_from_week (0 = the pregame forecast); the
                    # note is None at horizon 0 and "not validated beyond the next week" otherwise
                    "forecast_horizon": run.get("horizon", 0), "projected_from_week": run.get("projected_from_week"),
                    "experimental_note": run.get("horizon_note"),
                    "feature_cutoff": {"data_through_season": run["data_through_season"],
                                       "data_through_week": run["data_through_week"],
                                       "rule": "only NFL games completed before the target week"},
                    "nfl_synced_at": str(run["nfl_synced_at"]) if run.get("nfl_synced_at") else None,
                    "heldout_evaluation": run.get("evaluation"),
                    "participation": ({"model_version": run["participation_version"],
                                       "status": run.get("participation_status"),
                                       "heldout_evaluation": run.get("participation_evaluation")}
                                      if run.get("participation_version") else None)},
            "model_status": run["model_status"], "players": [_player(p, run) for p in players]}


@router.get("/projections/independent")
def independent_projections(week: int | None = Query(None, ge=1, le=18),
                            position: str | None = Query(None, description="QB | RB | WR | TE | K | D/ST"),
                            team_id: int | None = None):
    """Independent (non-ESPN) weekly projections next to ESPN's, with evidence and model status."""
    with db() as con:
        try:
            return build_response(con, week, position.upper() if position else None, team_id)
        except LookupError as err:
            raise HTTPException(503, str(err))
        except ValueError as err:
            raise HTTPException(422, str(err))

"""Read-only kicker projection contract (docs/kicker_projection_engine.md, "API").

GET /api/projections/kickers?week=7[&team_id=3]

The verified kfp kicker run for the week (kicker_store.select_run: the league's kicker scoring, approved
artifact, content hash) next to ESPN's kicker projections. No training, syncs, ESPN calls, weather requests or
writes. A null independent value is "no estimate" (see coverage_status), never zero.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import kicker_store as ks
from ..analytics.legacy_league import latest_league
from ..db.connection import fetch_dicts, table_exists
from .deps import db

router = APIRouter(prefix="/api", tags=["kicker projections"])

SCHEMA_VERSION = "kicker-projections/v1"
GOVERNANCE = {
    "model_version": ks.MODEL_VERSION, "role": "independent kicker model (separate from the ifp skill-position models)",
    "start_sit_ready": False, "espn_superiority_established": False,
    "status_note": ("Fantasy Intel's kicker model: more accurate than trailing kicker averages on 2023-2025 held-out "
                    "seasons, and tracked against ESPN every week since 2026."),
}
NOTES = [
    "projected_points is conditional on the kicker kicking in the game; expected_points = p_play x projected_points.",
    "stat_line holds the model's expected FG tries and makes by distance and expected PAT tries and makes.",
    "Weather is a forecast (NWS) shown with its retrieval time; it is a model input only when "
    "context.weather.used_by_model is true (kfp-1.1.0: never).",
    "A retractable roof's game-day position is unknown before kickoff and is never assumed.",
]


def player(r: dict, fantasy: dict, espn: dict, model_version: str = ks.MODEL_VERSION) -> dict:
    f, e = fantasy.get(r["espn_player_id"]) or {}, espn.get(r["espn_player_id"]) or {}
    exp = r.get("expected_points")
    ep = e.get("projected_points")
    return {"espn_player_id": r["espn_player_id"], "gsis_id": r.get("gsis_id"), "name": f.get("full_name"),
            "nfl_team": r.get("nfl_team"), "opponent": r.get("opponent"), "is_home": r.get("is_home"),
            "fantasy_team_id": f.get("on_team_id") or None, "injury_status": f.get("injury_status"),
            "independent": {"projected_points": r.get("projected_points"), "expected_points": exp,
                            "p_play": r.get("p_play"),
                            "interval_80": ({"low": r["interval_low"], "high": r["interval_high"], "nominal_coverage": 0.8}
                                            if r.get("interval_low") is not None else None),
                            "coverage_status": r.get("coverage_status"), "stat_line": r.get("stat_line"),
                            "context": r.get("context"), "explanation": r.get("explanation") or [],
                            "flags": r.get("flags") or [], "missing_inputs": r.get("missing_inputs") or [],
                            "pregame": r.get("pregame"), "model_version": model_version},
            "espn": {"projected_points": ep},
            "comparison": {"expected_minus_espn": round(exp - ep, 2) if exp is not None and ep is not None else None}}


def build_response(con, week: int | None, team_id: int | None) -> dict:
    lg = latest_league(con, ["league_id", "season", "current_week"])
    if lg is None:
        raise LookupError("No ESPN league synced yet.")
    week = week or lg["current_week"]
    base = {"schema_version": SCHEMA_VERSION, "league": {"id": lg["league_id"], "season": lg["season"]}, "week": week,
            "governance": GOVERNANCE, "notes": NOTES}
    sel = ks.select_run(con, lg["season"], week)
    run = sel["run"]
    if run is None:
        return {**base, "available": False, "unavailable_reason": sel["unavailable_reason"], "skipped_runs": sel["skipped"],
                "run": None, "players": []}
    ids = [r["espn_player_id"] for r in sel["rows"]]
    fantasy = {f["espn_player_id"]: f for f in fetch_dicts(
        con, "SELECT espn_player_id, full_name, on_team_id, injury_status FROM fantasy_players "
             "WHERE espn_player_id IN (SELECT unnest(?))", [ids])} if table_exists(con, "fantasy_players") else {}
    espn = {e["espn_player_id"]: e for e in fetch_dicts(
        con, "SELECT espn_player_id, projected_points FROM espn_player_weekly_points WHERE season = ? "
             "AND scoring_period = ? AND espn_player_id IN (SELECT unnest(?))", [lg["season"], week, ids])} \
        if table_exists(con, "espn_player_weekly_points") else {}
    players = [player(r, fantasy, espn, run["model_version"]) for r in sel["rows"]]
    if team_id is not None:
        players = [p for p in players if p["fantasy_team_id"] == team_id]
    players.sort(key=lambda p: (p["independent"]["expected_points"] is None, -(p["independent"]["expected_points"] or 0),
                                p["espn_player_id"]))
    # a week projected before kfp-1.1.0 is served from its kfp-1.0.0 run, labeled as such
    base["governance"] = {**GOVERNANCE, "model_version": run["model_version"]}
    return {**base, "available": True, "unavailable_reason": None, "skipped_runs": sel["skipped"],
            "run": {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"]),
                    "artifact_id": run.get("artifact_id"), "artifact_verified": run.get("artifact_check") == "verified",
                    "artifact_note": run.get("artifact_note"), "content_sha256": run.get("content_sha256"),
                    "scoring_fingerprint": run.get("scoring_fingerprint"), "scoring": run.get("scoring"),
                    "feature_cutoff": {"data_through_season": run.get("data_through_season"),
                                       "data_through_week": run.get("data_through_week")},
                    "forecast_horizon": int(run.get("forecast_horizon") or 0), "model_status": run.get("model_status"),
                    "heldout_evaluation": run.get("evaluation"), "weather_summary": run.get("weather_summary")},
            "players": players}


@router.get("/projections/kickers")
def kicker_projections(week: int | None = Query(None, ge=1, le=18), team_id: int | None = None):
    """Independent kicker projections (kfp) with stat lines, context and ESPN's number for comparison."""
    with db() as con:
        try:
            return build_response(con, week, team_id)
        except LookupError as err:
            raise HTTPException(503, str(err))

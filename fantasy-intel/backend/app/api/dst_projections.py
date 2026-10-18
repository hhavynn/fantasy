"""Read-only D/ST projection contract (docs/research/dst_projection_model.md section 8).

GET /api/projections/defenses?week=7[&team_id=3]

The verified dfp-r1 D/ST run for the week (dst_store.select_run: the league's D/ST scoring, approved artifact,
content hash) next to ESPN's D/ST projections. No training, syncs, ESPN calls or writes. A null independent value is
"no estimate" (see coverage_status), never zero.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import dst_store as ds
from ..analytics.legacy_league import latest_league
from ..db.connection import fetch_dicts, table_exists
from .deps import db

router = APIRouter(prefix="/api", tags=["D/ST projections"])

SCHEMA_VERSION = "dst-projections/v1"
NOTES = [
    "expected_points is the D/ST's projected fantasy points under this league's D/ST scoring (a D/ST always plays "
    "its game, so it equals projected_points; a bye is 0).",
    "stat_line holds the model's expected sacks, interceptions, fumble recoveries, points and yards allowed, and the "
    "chance of at least one D/ST touchdown.",
    "context holds the betting line the run used, as stored when the run was made: the spread, the total and the "
    "opponent's implied team total.",
]


def player(r: dict, fantasy: dict, espn: dict) -> dict:
    f, e = fantasy.get(r["espn_player_id"]) or {}, espn.get(r["espn_player_id"]) or {}
    exp = r.get("expected_points")
    ep = e.get("projected_points")
    return {"espn_player_id": r["espn_player_id"], "name": f.get("full_name"), "nfl_team": r.get("nfl_team"),
            "opponent": r.get("opponent"), "is_home": r.get("is_home"),
            "fantasy_team_id": f.get("on_team_id") or None,
            "independent": {"projected_points": r.get("projected_points"), "expected_points": exp,
                            "interval_80": ({"low": r["interval_low"], "high": r["interval_high"], "nominal_coverage": 0.8}
                                            if r.get("interval_low") is not None else None),
                            "coverage_status": r.get("coverage_status"), "stat_line": r.get("stat_line"),
                            "context": r.get("context"), "explanation": r.get("explanation") or [],
                            "flags": r.get("flags") or [], "missing_inputs": r.get("missing_inputs") or [],
                            "pregame": r.get("pregame"), "model_version": ds.MODEL_VERSION},
            "espn": {"projected_points": ep},
            "comparison": {"expected_minus_espn": round(exp - ep, 2) if exp is not None and ep is not None else None}}


def build_response(con, week: int | None, team_id: int | None) -> dict:
    lg = latest_league(con, ["league_id", "season", "current_week"])
    if lg is None:
        raise LookupError("No ESPN league synced yet.")
    week = week or lg["current_week"]
    base = {"schema_version": SCHEMA_VERSION, "league": {"id": lg["league_id"], "season": lg["season"]}, "week": week,
            "governance": ds.GOVERNANCE, "notes": NOTES}
    sel = ds.select_run(con, lg["season"], week)
    run = sel["run"]
    if run is None:
        return {**base, "available": False, "unavailable_reason": sel["unavailable_reason"],
                "skipped_runs": sel["skipped"], "run": None, "players": []}
    ids = [r["espn_player_id"] for r in sel["rows"]]
    fantasy = {f["espn_player_id"]: f for f in fetch_dicts(
        con, "SELECT espn_player_id, full_name, on_team_id FROM fantasy_players "
             "WHERE espn_player_id IN (SELECT unnest(?))", [ids])} if table_exists(con, "fantasy_players") else {}
    espn = {e["espn_player_id"]: e for e in fetch_dicts(
        con, "SELECT espn_player_id, projected_points FROM espn_player_weekly_points WHERE season = ? "
             "AND scoring_period = ? AND espn_player_id IN (SELECT unnest(?))", [lg["season"], week, ids])} \
        if table_exists(con, "espn_player_weekly_points") else {}
    players = [player(r, fantasy, espn) for r in sel["rows"]]
    if team_id is not None:
        players = [p for p in players if p["fantasy_team_id"] == team_id]
    players.sort(key=lambda p: (p["independent"]["coverage_status"] != "projected",
                                -(p["independent"]["expected_points"] or 0), p["espn_player_id"]))
    return {**base, "available": True, "unavailable_reason": None, "skipped_runs": sel["skipped"],
            "run": {"run_id": run["run_id"], "model_version": run["model_version"], "created_at": str(run["created_at"]),
                    "artifact_id": run.get("artifact_id"), "artifact_verified": run.get("artifact_check") == "verified",
                    "artifact_note": run.get("artifact_note"), "content_sha256": run.get("content_sha256"),
                    "scoring_fingerprint": run.get("scoring_fingerprint"),
                    "feature_cutoff": {"data_through_season": run.get("data_through_season"),
                                       "data_through_week": run.get("data_through_week")},
                    "nfl_synced_at": str(run["nfl_synced_at"]) if run.get("nfl_synced_at") else None,
                    "forecast_horizon": int(run.get("forecast_horizon") or 0)},
            "players": players}


@router.get("/projections/defenses")
def dst_projections(week: int | None = Query(None, ge=1, le=18), team_id: int | None = None):
    """D/ST projections (dfp-r1) with expected sacks, takeaways and points allowed, the line used, and ESPN's number."""
    with db() as con:
        try:
            return build_response(con, week, team_id)
        except LookupError as err:
            raise HTTPException(503, str(err))

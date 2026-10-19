"""Read-only independent projection contract (docs/independent_projection_engine.md, "API").

GET /api/projections/independent?week=7[&position=WR][&team_id=3]

No training, no syncs, no ESPN calls and no writes. Independent estimates and ESPN projections
are separate fields; an independent value is never replaced by ESPN's, and unknown is null.

The run shown is the one every consumer view shows (projection_source.select_run): the production model
(ifp-2.0.0) under the league's current scoring, else the labeled ifp-1.0.0 fallback, and only a run whose stored
rows verify against its content hash and whose artifact is approved. Players are served from exactly the rows
that were verified. `governance` says what the served model's approval does and does not establish.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..analytics import production_forecasts as pf
from ..analytics import projection_source as ps
from ..db.connection import fetch_dicts, table_exists
from .deps import db, one

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
    "The production model ifp-2.0.0 was approved by an owner override, not by evidence: its validation is "
    "historical research only and it is not shown to be better than ESPN (see governance). No position is "
    "Start/Sit-ready. A week without an eligible ifp-2.0.0 run shows the ifp-1.0.0 fallback, labeled.",
    "This is the live view (the newest verified run). independent.pregame says whether a row was made before the "
    "player's own kickoff; only pregame rows are ever used as accuracy evidence.",
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


def _player(p: dict, run: dict | None, status: dict) -> dict:
    allowed = ps.GOVERNANCE.get((run or {}).get("model_version"), {}).get("start_sit_ready", False)
    pos_status = ((run or {}).get("model_status") or {}).get(p["position"]) or {}
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
            "model_status": status.get(p["position"]) if p["position"] in ps.POSITIONS else "unsupported",
            "start_sit_ready": bool(allowed and pos_status.get("start_sit_ready")),
            "pregame": p.get("pregame"),
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
            "week": week, "notes": NOTES, "governance": ps.governance(None), "kicker_run": None}
    base["ranges"] = None       # floor / ceiling block (_range_overlay) once a run is served
    if not (table_exists(con, pf.RUNS) and pf.rows_source(con)):
        return {**base, "available": False, "unavailable_reason": "Independent projections have not been computed "
                "(run scripts/backtest_independent_projections.py project).", "run": None, "model_status": None,
                "players": []}
    sel = ps.select_run(con, lg["season"], week)
    run = sel["run"]
    if run is None:
        return {**base, "available": False, "unavailable_reason": sel["unavailable_reason"], "run": None,
                "model_status": None, "players": []}
    import json
    for k in ("model_status", "evaluation", "participation_status", "participation_evaluation"):
        if isinstance(run.get(k), str):
            run[k] = json.loads(run[k])
    now_kick = pf.team_kickoffs(con, lg["season"], week)
    players = []
    for r in sel["rows"]:
        if position and r["position"] != position:
            continue
        p = dict(r)
        for k in ("missing_inputs", "flags", "explanation", "participation"):
            if isinstance(p.get(k), str):
                p[k] = json.loads(p[k])
        p["pregame"] = pf.is_pregame(run["created_at"], pf.own_kickoff(r, week, now_kick))
        players.append(p)
    ids = [p["espn_player_id"] for p in players]
    fantasy = {f["espn_player_id"]: f for f in fetch_dicts(
        con, "SELECT espn_player_id, full_name, on_team_id, injury_status FROM fantasy_players "
             "WHERE espn_player_id IN (SELECT unnest(?))", [ids])} if ids and table_exists(con, "fantasy_players") else {}
    espn = {e["espn_player_id"]: e for e in fetch_dicts(
        con, "SELECT espn_player_id, projected_points, synced_at FROM espn_player_weekly_points "
             "WHERE season = ? AND scoring_period = ? AND espn_player_id IN (SELECT unnest(?))",
        [lg["season"], week, ids])} if ids and table_exists(con, "espn_player_weekly_points") else {}
    for p in players:
        f, e = fantasy.get(p["espn_player_id"]) or {}, espn.get(p["espn_player_id"]) or {}
        p.update(full_name=f.get("full_name"), on_team_id=f.get("on_team_id"), injury_status=f.get("injury_status"),
                 espn_projected_points=e.get("projected_points"), espn_synced_at=e.get("synced_at"))
    if team_id is not None:
        players = [p for p in players if p["on_team_id"] == team_id]
    players.sort(key=lambda p: (p["projected_points"] is None, -(p["projected_points"] or 0), p["espn_player_id"]))
    status = ps.consumer_status(run)
    out_players = [_player(p, run, status) for p in players]
    kicker_run = _kicker_overlay(con, lg["season"], week, out_players)
    ranges = _range_overlay(con, run, sel["rows"], out_players)
    return {**base, "available": True, "unavailable_reason": None, "kicker_run": kicker_run, "ranges": ranges,
            "governance": ps.governance(run["model_version"]),
            "run": {"run_id": run["run_id"], "model_version": run["model_version"], "is_fallback": sel["fallback"],
                    "fallback_reason": (f"No eligible {ps.MODEL_VERSION} run for week {week}; the retired previous "
                                        f"model {run['model_version']} is shown.") if sel["fallback"] else None,
                    "skipped_runs": sel["skipped"],
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
                    "artifact_model_status": run["model_status"],
                    "heldout_evaluation": run.get("evaluation"),
                    "participation": ({"model_version": run["participation_version"],
                                       "status": run.get("participation_status"),
                                       "heldout_evaluation": run.get("participation_evaluation")}
                                      if run.get("participation_version") else None)},
            "model_status": _status_detail(run, status), "players": out_players}


def _kicker_overlay(con, season: int, week: int, players: list[dict]) -> dict | None:
    """K players (unsupported by the skill-position run) take the verified kicker run's row (kicker_store.select_run):
    the `independent` block then describes the kicker model, never the ifp run. Returns the kicker run's identity."""
    from ..analytics import kicker_store as ks
    from .kicker_projections import player as kicker_player

    if not any(p["position"] == "K" for p in players):
        return None
    sel = ks.select_run(con, season, week)
    if sel["run"] is None:
        return {"available": False, "reason": sel["unavailable_reason"]}
    rows = {r["espn_player_id"]: r for r in sel["rows"]}
    for p in players:
        r = rows.get(p["espn_player_id"])
        if p["position"] != "K" or r is None:
            continue
        k = kicker_player(r, {}, {})["independent"]
        p["independent"] = {**p["independent"], "projected_points": k["projected_points"],
                            "expected_points": k["expected_points"], "interval_80": k["interval_80"],
                            "model": "kicker_opportunity_conversion", "model_version": ks.MODEL_VERSION,
                            "model_status": "kicker_model", "coverage_status": k["coverage_status"],
                            "flags": k["flags"], "missing_inputs": k["missing_inputs"], "explanation": k["explanation"],
                            "pregame": k["pregame"], "games_used": None, "latest_usage": None,
                            "participation": {"p_play": k["p_play"], "model_version": ks.MODEL_VERSION,
                                              "model_status": "empirical designation table", "explanation": [],
                                              "flags": [], "note": None, "expected_points_status": None,
                                              "depth_rank": None},
                            "stat_line": k["stat_line"], "kicking_context": k["context"]}
        # The comparison was built from the unsupported ifp row (no estimate): rebuild every field from the kicker row.
        espn, indep, exp = p["espn"]["projected_points"], k["projected_points"], k["expected_points"]
        diff = round(indep - espn, 2) if indep is not None and espn is not None else None
        exp_diff = round(exp - espn, 2) if exp is not None and espn is not None else None
        p["comparison"] = {"independent_minus_espn": diff, **_comparability(indep, espn),
                           "expected_minus_espn": exp_diff, "expected_comparable": exp_diff is not None,
                           "evidence": _evidence(k, diff)}
    run = sel["run"]
    return {"available": True, "run_id": run["run_id"], "model_version": run["model_version"],
            "created_at": str(run["created_at"]), "artifact_verified": run.get("artifact_check") == "verified"}


def _range_overlay(con, run: dict, rows: list[dict], players: list[dict]) -> dict:
    """independent.range = {floor, median, ceiling} (points if he plays, app.analytics.projection_ranges) on every
    player whose served row carries one, else null. Returns what the ranges are and which positions carry them."""
    from ..analytics import projection_ranges as pr

    tables = pr.load(con)
    problem = pr.run_problem(tables, run)
    by_id = {r["espn_player_id"]: r for r in rows}
    for p in players:
        r = by_id.get(p["espn_player_id"])
        p["independent"]["range"] = (pr.row_range(tables, r) if problem is None and r is not None
                                     and p["independent"].get("model_version") == run["model_version"] else None)
    return pr.describe(tables, problem)


def _status_detail(run: dict, status: dict) -> dict:
    """Per position {status, start_sit_ready, validated_for, reasons} as consumers may state it. The run's own
    stored status (frozen at training time) is under run.artifact_model_status."""
    g = ps.GOVERNANCE.get(run["model_version"], {})
    out = {}
    for pos in ps.POSITIONS:
        stored = (run.get("model_status") or {}).get(pos) or {}
        override = status.get(pos) == ps.OVERRIDE_STATUS
        out[pos] = {"status": status.get(pos),
                    "start_sit_ready": bool(g.get("start_sit_ready") and stored.get("start_sit_ready")),
                    "validated_for": None if override else stored.get("validated_for"),
                    "reasons": [g["status_note"]] if override else (stored.get("reasons") or [])}
    return out


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

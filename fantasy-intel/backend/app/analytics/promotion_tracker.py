"""Promotion tracker for the independent projection engine (read-only).

How far each position (QB/RB/WR/TE) is from the frozen PROMOTION_CRITERIA in
forecast_backtest. Nothing here decides or changes a status:

* Model status and the held-out baseline comparison come from the evaluation stored with the
  latest live run (independent_projection_runs: the artifact's status and held-out summary).
* The ESPN evidence reuses forecast_backtest.espn_pregame (ESPN projection captured strictly
  before the player's kickoff) and forecast_backtest.espn_comparison (the frozen >= N matched
  observations and MAE <= ESPN rule). "Ours" is the stored weekly-run projection of the primary
  model (features from games before the week only); the label is the league points of a
  completed appearance, the same population as the frozen criterion. Runs are immutable and
  run-keyed (production_forecasts): each player's number is the latest eligible run created strictly
  before his own kickoff, from a run whose stored rows reproduce its content hash. Databases not yet
  migrated to schema v8 fall back to the legacy one-row-per-week table (no per-player kickoff check).

Promotion still needs a rerun of `evaluate` and the user's approval; independent projections
are not read by lineups, start/sit, trades or waivers.
"""

from __future__ import annotations

import json
from datetime import datetime

import polars as pl

from ..db.connection import fetch_dicts, table_columns, table_exists
from .forecast_backtest import PROMOTION_CRITERIA, espn_comparison, espn_pregame
from .forecast_features import POSITIONS, load_panel
from .independent_projection_store import league_scoring
from .independent_projections import MODEL_VERSION
from . import production_forecasts as pf
from .production_forecasts import ROWS, eligible_run_ids, pregame_forecasts
from .production_forecasts import first_kickoffs as _first_kickoffs
from .projection_backtest import _kickoff, _utc

SCHEMA_VERSION = "independent-promotion/v1"
NOTES = [
    "Read-only progress toward the frozen PROMOTION_CRITERIA (forecast_backtest.py). Nothing on this page "
    "promotes a model: promotion needs a rerun of evaluate and your approval.",
    "Independent projections are research output. They are not used in lineups, start/sit, trades or waivers.",
    "Matched = a player-week with an ESPN projection captured strictly before the player's kickoff, a completed "
    "appearance (league points from nflverse box scores) and a stored independent projection of the current model "
    "under the current league scoring, from an artifact fitted before that week's first kickoff.",
    "MAE is in league points and conditional on the player appearing, the population of the frozen criterion.",
]


def _criteria() -> dict:
    c = PROMOTION_CRITERIA
    return {"heldout_min_observations_per_position": c["heldout_min_observations_per_position"],
            "min_relative_mae_improvement_vs_best_trailing_baseline":
                c["min_relative_mae_improvement_vs_best_trailing_baseline"],
            "require_bootstrap_ci_upper_below_zero": c["require_bootstrap_ci_upper_below_zero"],
            "espn_min_matched_per_position": c["espn_min_matched_per_position"],
            "espn_rule": c["espn_rule"]}


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


def latest_run(con, season: int, fingerprint: str | None) -> dict | None:
    """Newest run of the current model under the league's current scoring. A run of another model
    version or scoring carries another model's held-out status, so it is never shown here."""
    if fingerprint is None or not table_exists(con, "independent_projection_runs"):
        return None
    gate = pf.artifact_gate(con, MODEL_VERSION)                # the artifact rule of production_pin for this model
    has_artifact = "artifact_id" in table_columns(con, "independent_projection_runs")
    rows = fetch_dicts(con, f"""SELECT run_id, week, model_version, created_at, model_status, evaluation
                                {", artifact_id" if has_artifact else ""}
                                FROM independent_projection_runs
                                WHERE season = ? AND model_version = ? AND scoring_fingerprint = ?
                                ORDER BY created_at DESC""", [season, MODEL_VERSION, fingerprint])
    rows = [r for r in rows if gate(r)[0] != "rejected"]
    if not rows:
        return None
    run = rows[0]
    run["model_status"], run["evaluation"] = _json(run["model_status"]) or {}, _json(run["evaluation"]) or {}
    return run


def _run_missing_reason(con, season: int, fingerprint: str | None) -> str:
    project = ("From backend/, run `uv run python ../scripts/backtest_independent_projections.py project --week N` "
               "(it needs the fitted artifacts in data/models; see the runbook in docs/independent_projection_engine.md).")
    if fingerprint is None:
        return "League scoring is not supported by the independent model, so no held-out status applies."
    other = fetch_dicts(con, "SELECT count(*) AS n FROM independent_projection_runs WHERE season = ?",
                        [season])[0]["n"] if table_exists(con, "independent_projection_runs") else 0
    if other:
        pin = " or artifact (not an approved one)" if pf.gate_active(con, MODEL_VERSION) else ""
        return (f"Stored runs are from another model version{pin} or league scoring, so the held-out status of "
                f"{MODEL_VERSION} under the current scoring is not shown. {project}")
    return f"No independent projection run is stored for this season, so the held-out status is not shown. {project}"


def model_evidence(run: dict | None, position: str) -> dict:
    """Stored held-out result for one position. Copied, never recomputed."""
    if run is None:
        return {"status": None, "start_sit_ready": None, "reasons": [], "heldout_n": None,
                "relative_mae_improvement": None, "best_trailing_baseline": None, "mae_primary": None,
                "mae_best_baseline": None, "ci95_primary_minus_baseline": None}
    st = run["model_status"].get(position) or {}
    ev = run["evaluation"].get(position) or {}
    metrics = ev.get("metrics") or {}
    base = ev.get("best_trailing_baseline")
    return {"status": st.get("status"), "start_sit_ready": st.get("start_sit_ready"),
            "reasons": st.get("reasons") or [], "heldout_n": ev.get("common_support_n"),
            "relative_mae_improvement": ev.get("relative_mae_improvement"), "best_trailing_baseline": base,
            "mae_primary": (metrics.get("primary") or {}).get("mae"),
            "mae_best_baseline": (metrics.get(base) or {}).get("mae") if base else None,
            "ci95_primary_minus_baseline": (ev.get("primary_minus_best_baseline_mae") or {}).get("ci95")}


def first_kickoffs(con, season: int) -> dict[int, datetime]:
    return _first_kickoffs(con, season)


def _legacy_eligible_runs(con, season: int, fingerprint: str) -> tuple[list[str], list[dict]]:
    """Unmigrated database (no independent_projection_rows): the pre-v8 rule, run metadata only."""
    kick = first_kickoffs(con, season)
    keep, excluded = [], []
    gate = pf.artifact_gate(con, MODEL_VERSION)
    for r in fetch_dicts(con, """SELECT * FROM independent_projection_runs WHERE season = ?
                                 ORDER BY week, run_id""", [season]):
        fitted, first = _utc(r["artifact_created_at"]), kick.get(r["week"])
        if r["model_version"] != MODEL_VERSION:
            reason = f"model {r['model_version']}, not {MODEL_VERSION}"
        elif r["scoring_fingerprint"] != fingerprint:
            reason = "made under different league scoring"
        elif gate(r)[0] == "rejected":
            reason = gate(r)[1]
        elif fitted is None or first is None:
            reason = "artifact fit time or week kickoff unknown"
        elif fitted >= first:
            reason = "artifact fitted after the week's first kickoff (it may have trained on that week)"
        else:
            keep.append(r["run_id"])
            continue
        excluded.append({"run_id": r["run_id"], "week": r["week"], "reason": reason})
    return keep, excluded


def eligible_runs(con, season: int, fingerprint: str) -> tuple[list[str], list[dict]]:
    """Runs whose projections may count as evidence: the current model, the league's current scoring,
    an artifact fitted before the target week's first kickoff (`fit` trains on every completed
    week, so a later artifact has seen the results it would be judged on), and stored rows that
    reproduce the run's content hash. Returns (run ids, excluded runs with a reason)."""
    if not table_exists(con, "independent_projection_runs"):
        return [], []
    if not table_exists(con, ROWS):
        return _legacy_eligible_runs(con, season, fingerprint)
    return eligible_run_ids(con, season, MODEL_VERSION, fingerprint)


def _stored_projections(con, season: int, fingerprint: str) -> tuple[pl.DataFrame, list[dict]]:
    """Per player-week, the projected_points of the latest eligible run created before his own kickoff."""
    schema = {"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "pred_primary": pl.Float64}
    if not table_exists(con, "independent_projection_runs"):
        return pl.DataFrame(schema=schema), []
    if not table_exists(con, ROWS):   # unmigrated copy: legacy table, read-only, run-level eligibility only
        run_ids, excluded = _legacy_eligible_runs(con, season, fingerprint)
        if not run_ids or not table_exists(con, "independent_player_projections"):
            return pl.DataFrame(schema=schema), excluded
        rows = fetch_dicts(con, """
            SELECT season, week, gsis_id, any_value(projected_points) AS pred_primary
            FROM independent_player_projections
            WHERE season = ? AND run_id IN (SELECT unnest(?)) AND coverage_status = 'projected'
              AND projected_points IS NOT NULL AND gsis_id IS NOT NULL
            GROUP BY 1, 2, 3 HAVING count(*) = 1""", [season, run_ids])
        return (pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)), excluded
    forecasts, excluded = pregame_forecasts(con, season, None, model_version=MODEL_VERSION,
                                            scoring_fingerprint=fingerprint)
    seen: dict[tuple, list] = {}
    for r in forecasts:
        if r["coverage_status"] == "projected" and r["projected_points"] is not None and r["gsis_id"]:
            seen.setdefault((r["season"], r["week"], r["gsis_id"]), []).append(float(r["projected_points"]))
    rows = [{"season": k[0], "week": k[1], "gsis_id": k[2], "pred_primary": v[0]}
            for k, v in sorted(seen.items()) if len(v) == 1]     # a gsis id on two ESPN ids is ambiguous: dropped
    return (pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)), excluded


def espn_evidence(con, season: int, scoring) -> dict:
    """Matched pregame-ESPN observations so far, per position and per week."""
    need = PROMOTION_CRITERIA["espn_min_matched_per_position"]
    empty = {"available": False, "unavailable_reason": None, "matched_observations": 0,
             "by_position": {p: {"n": 0, "pregame_completed": 0, "required": need, "criterion_met": False,
                                 "mae_independent": None, "mae_espn": None} for p in POSITIONS},
             "by_week": [], "excluded_runs": []}
    if not scoring.supported:
        return {**empty, "unavailable_reason": "League scoring is not supported by the independent model."}
    try:
        panel = load_panel(con, scoring, [season])
    except ValueError as err:
        return {**empty, "unavailable_reason": str(err)}
    labels = panel.select("season", "week", "gsis_id", "position", pl.col("league_points").alias("label"))
    espn = espn_pregame(con, season)
    pool = labels.join(espn, on=["season", "week", "gsis_id"], how="inner")
    stored, excluded = _stored_projections(con, season, scoring.fingerprint)
    preds = labels.join(stored, on=["season", "week", "gsis_id"], how="inner")
    comp = espn_comparison(preds, espn)
    matched = preds.join(espn, on=["season", "week", "gsis_id"], how="inner")
    by_pos = {}
    for pos in POSITIONS:
        c = comp["by_position"].get(pos) or {}
        by_pos[pos] = {"n": c.get("n", 0), "pregame_completed": pool.filter(pl.col("position") == pos).height,
                       "required": need, "criterion_met": bool(c.get("criterion_met")),
                       "mae_independent": (c.get("primary") or {}).get("mae") if c.get("n") else None,
                       "mae_espn": (c.get("espn") or {}).get("mae") if c.get("n") else None}
    weeks = sorted(set(pool["week"].to_list()) | set(matched["week"].to_list()))
    by_week = [{"week": int(w), "pregame_completed": pool.filter(pl.col("week") == w).height,
                "matched": matched.filter(pl.col("week") == w).height} for w in weeks]
    return {"available": True, "unavailable_reason": None, "matched_observations": comp["matched_observations"],
            "by_position": by_pos, "by_week": by_week, "excluded_runs": excluded}


def pregame_snapshot(con, season: int, week: int) -> dict:
    """Was an ESPN projection snapshot for `week` captured before the week's first kickoff? A later
    snapshot still counts for players whose own game kicks off after it (espn_pregame is per player)."""
    out = {"week": week, "first_kickoff": None, "last_kickoff": None, "latest_pregame_snapshot_at": None,
           "latest_snapshot_at": None, "captured": False, "players": 0}
    if not table_exists(con, "nfl_schedules"):
        return out
    kicks = [k for k in (_kickoff(g) for g in fetch_dicts(
        con, "SELECT gameday, gametime FROM nfl_schedules WHERE season = ? AND week = ? AND game_type = 'REG'",
        [season, week])) if k is not None]
    if not kicks:
        return out
    first = min(kicks)
    out["first_kickoff"], out["last_kickoff"] = first.isoformat(), max(kicks).isoformat()
    if not table_exists(con, "espn_projection_history"):
        return out
    rows = fetch_dicts(con, """SELECT synced_at, count(DISTINCT espn_player_id) AS players
                               FROM espn_projection_history
                               WHERE season = ? AND scoring_period = ? AND projected_points IS NOT NULL
                               GROUP BY 1""", [season, week])
    stamps = [(_utc(r["synced_at"]), r["players"]) for r in rows if _utc(r["synced_at"]) is not None]
    if stamps:
        out["latest_snapshot_at"] = max(s for s, _ in stamps).isoformat()
    pre = [(s, n) for s, n in stamps if s < first]
    if pre:
        s, n = max(pre)
        out.update(latest_pregame_snapshot_at=s.isoformat(), captured=True, players=n)
    return out


def build_tracker(con) -> dict:
    lg = fetch_dicts(con, "SELECT season, current_week FROM leagues ORDER BY synced_at DESC LIMIT 1") \
        if table_exists(con, "leagues") else []
    if not lg:
        raise LookupError("No ESPN league synced yet.")
    season, week = lg[0]["season"], lg[0]["current_week"]
    _, scoring = league_scoring(con, season)
    fingerprint = scoring.fingerprint if scoring.supported else None
    run = latest_run(con, season, fingerprint)
    espn = espn_evidence(con, season, scoring)
    criteria = _criteria()
    positions = [{"position": pos, "model": model_evidence(run, pos), "espn": espn["by_position"][pos]}
                 for pos in POSITIONS]
    return {"schema_version": SCHEMA_VERSION, "available": True, "unavailable_reason": None,
            "season": season, "current_week": week, "notes": NOTES,
            "criteria": criteria, "model_version": MODEL_VERSION,
            "run": None if run is None else {"run_id": run["run_id"], "week": run["week"],
                                              "model_version": run["model_version"],
                                              "created_at": str(run["created_at"])},
            "run_missing_reason": None if run is not None else _run_missing_reason(con, season, fingerprint),
            "espn": {k: espn[k] for k in ("available", "unavailable_reason", "matched_observations", "by_week",
                                               "excluded_runs")},
            "pregame_snapshot": pregame_snapshot(con, season, week),
            "positions": positions}

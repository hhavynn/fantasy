"""Prospective benchmark of the production independent model against ESPN (read-only research code).

Question answered: on exactly the same completed player-weeks, is the model Fantasy Intel would
have shown BEFORE kickoff closer to the league points than ESPN's own pregame projection?

Nothing here writes a database, trains, syncs or changes a projection. It generalises
`promotion_tracker.espn_evidence`: the same identity, pregame and scoring rules, plus outcome
finality (`weekly_learning.label_outcomes`), a consumer-visible comparison with ESPN fallback,
horizons and a week-clustered evidence gate. Nothing imports this module.

Rules (docs/espn_benchmark.md):

* Identity: only a high-confidence ESPN->GSIS crosswalk row (not flagged "conflict", not shared by
  two ESPN ids). Anything else is `uncertain_id_mapping` and is excluded, never guessed.
* ESPN pregame: the latest `espn_projection_history` snapshot with `synced_at` strictly before the
  kickoff of the player's OWN game. Unknown kickoff means not pregame.
* Independent pregame: the forecast row comes from an injectable `forecast_reader` (the contract of
  production_forecasts.pregame_forecasts). Every row is re-verified here: run created strictly
  before the player's own kickoff, artifact fitted strictly before the week's FIRST kickoff,
  current model version and league-scoring fingerprint.
* Outcomes: weekly_learning.label_outcomes. Conditional metrics use APPEARED only; unconditional
  metrics use APPEARED + CONFIRMED_ABSENT (actual 0). Pending, missing-ingestion, bye and
  unresolved-identity rows are never scored.
* Origin follows projection_source.weekly: `ours`, `ours_zero`, or `espn` (fallback). The model
  never gets credit for a fallback row.
* Every comparison runs on an equal population: both predictions exist for every scored row.
* Evidence gate: a verdict other than `insufficient_evidence` needs at least
  PROMOTION_CRITERIA["espn_min_matched_per_position"] model-covered rows AND `min_weeks` distinct
  weeks, and `ours_better` / `espn_better` only when the 95% week-clustered bootstrap CI of
  MAE(ours) - MAE(espn) excludes 0.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import polars as pl

from ..db.connection import fetch_dicts, table_exists
from . import weekly_learning as wl
from .forecast_backtest import PROMOTION_CRITERIA, metrics, week_bootstrap_mae_diff
from .forecast_features import POSITIONS, Sources, load_sources
from .independent_projection_store import league_scoring
from .independent_projections import MODEL_VERSION
from .projection_backtest import _kickoff, _utc
from .promotion_tracker import first_kickoffs

# Origin semantics of projection_source.weekly, copied (not imported: only Start/Sit and Matchup may import the
# switch, see test_projection_switch_is_opt_in_and_limited_to_weekly_views); a test keeps the copy in sync.
OURS, OURS_ZERO, ESPN = "ours", "ours_zero", "espn"
ZERO_STATUSES = {"bye", "withheld_unavailable"}

SCHEMA_VERSION = "espn-benchmark/v1"
SEED = 20261014

ROW_KEYS = ("season", "week", "gsis_id", "espn_player_id", "position", "team", "kickoff", "outcome_status",
            "actual_points", "espn_pred", "espn_captured_at", "espn_horizon_h", "ours_independent_pred",
            "ours_conditional_pred", "ours_origin", "ours_consumer_pred", "run_id", "run_created_at",
            "ours_horizon_h", "exclusion_reason")

VERDICTS = ("insufficient_evidence", "no_significant_difference", "ours_better", "espn_better")
COMPARISONS = ("pure_independent", "consumer_visible", "conditional", "unconditional", "horizon_matched")

# first failing check wins; every dropped row carries exactly one of these
EXCLUSIONS = (
    "unsupported_position", "uncertain_id_mapping", "team_unknown", "bye", "kickoff_unknown",
    "no_espn_snapshot", "espn_not_pregame", "no_independent_forecast_for_week", "independent_not_pregame",
    "independent_model_mismatch", "independent_scoring_mismatch", "independent_artifact_time_unknown",
    "independent_artifact_after_first_kickoff", "independent_id_mismatch", "outcome_pending_game",
    "outcome_missing_ingestion", "outcome_bye", "outcome_unresolved_identity")

NOTES = [
    "Read-only. Nothing here promotes a model or changes a projection; ESPN stays the default source.",
    "MAE difference = MAE(ours) - MAE(ESPN); negative means the independent model is closer. Bias is "
    "mean(actual - prediction): positive means under-projection (forecast_backtest.metrics convention).",
    "Every comparison is run on an equal population: both predictions exist for every scored row.",
    "Horizon = hours between the forecast's information time (run created / ESPN synced) and the player's own "
    "kickoff. The two sources rarely share a timestamp; horizon_matched shows the subset where they nearly do.",
    "The 95% interval resamples whole NFL weeks (player-weeks of a week are correlated), seeded.",
    "A verdict other than insufficient_evidence needs enough model-covered rows and distinct weeks; "
    "fallback rows (ESPN shown) are never counted as model evidence.",
]


@dataclass(frozen=True)
class BenchmarkConfig:
    resamples: int = 1000
    seed: int = SEED
    min_weeks: int = 4
    horizon_window_h: float = 24.0


# ----------------------------------------------------------------------------- legacy reader


def legacy_forecast_reader(con, season: int, weeks: Iterable[int] | None = None, *, model_version: str,
                           scoring_fingerprint: str) -> tuple[list[dict], list[dict]]:
    """production_forecasts.pregame_forecasts contract over the CURRENT tables
    (independent_projection_runs + independent_player_projections), kickoff-checked per player.

    The legacy table keeps one row per (season, week, player): a later `project --week N` replaces
    the rows of an earlier run (the defect Workstream A fixes), so only the surviving run's forecast
    can be recovered and earlier pregame forecasts are not. Run eligibility mirrors
    promotion_tracker.eligible_runs (model version, scoring fingerprint, artifact fitted before the
    week's first kickoff); a row additionally needs run created_at strictly before the kickoff of
    the player's own team. Rows without a known kickoff are not pregame. Artifact identity is not
    stored in the legacy tables (`artifact_id` is None)."""
    if not (table_exists(con, "independent_projection_runs") and table_exists(con, "independent_player_projections")):
        return [], []
    want = None if weeks is None else sorted({int(w) for w in weeks})
    first = first_kickoffs(con, season)
    kick: dict[tuple, datetime | None] = {}
    if table_exists(con, "nfl_schedules"):
        for g in fetch_dicts(con, "SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
                                  "WHERE season = ? AND game_type = 'REG'", [season]):
            for t in (g["home_team"], g["away_team"]):
                kick[(g["week"], t)] = _kickoff(g)
    runs = {r["run_id"]: r for r in fetch_dicts(
        con, """SELECT run_id, week, model_version, scoring_fingerprint, artifact_created_at, created_at
                FROM independent_projection_runs WHERE season = ? ORDER BY week, run_id""", [season])
            if want is None or r["week"] in want}
    keep: dict[str, dict] = {}
    exclusions: list[dict] = []
    for rid, r in runs.items():
        fitted, fk = _utc(r["artifact_created_at"]), first.get(r["week"])
        if r["model_version"] != model_version:
            reason = f"model {r['model_version']}, not {model_version}"
        elif r["scoring_fingerprint"] != scoring_fingerprint:
            reason = "made under different league scoring"
        elif fitted is None or fk is None:
            reason = "artifact fit time or week kickoff unknown"
        elif fitted >= fk:
            reason = "artifact fitted after the week's first kickoff (it may have trained on that week)"
        elif _utc(r["created_at"]) is None:
            reason = "run creation time unknown"
        else:
            keep[rid] = {**r, "fitted": fitted}
            continue
        exclusions.append({"run_id": rid, "week": r["week"], "reason": reason})
    if not keep:
        return [], exclusions
    out: list[dict] = []
    dropped: dict[tuple, int] = {}
    for p in fetch_dicts(con, """SELECT season, week, espn_player_id, gsis_id, run_id, position, nfl_team,
                                        coverage_status, projected_points, p_play, expected_points
                                 FROM independent_player_projections
                                 WHERE season = ? AND run_id IN (SELECT unnest(?))
                                 ORDER BY week, espn_player_id""", [season, sorted(keep)]):
        run = keep[p["run_id"]]
        created = _utc(run["created_at"])
        ko = kick.get((p["week"], p["nfl_team"]))
        if ko is None or not created < ko:
            why = ("kickoff unknown (no scheduled game for the team, or an ambiguous kickoff time)" if ko is None
                   else "run created at or after the player's own kickoff")
            dropped[(p["run_id"], p["week"], why)] = dropped.get((p["run_id"], p["week"], why), 0) + 1
            continue
        out.append({"season": p["season"], "week": p["week"], "espn_player_id": p["espn_player_id"],
                    "gsis_id": p["gsis_id"], "position": p["position"], "run_id": p["run_id"],
                    "run_created_at": created, "model_version": run["model_version"], "artifact_id": None,
                    "artifact_created_at": run["fitted"], "scoring_fingerprint": run["scoring_fingerprint"],
                    "nfl_team": p["nfl_team"], "kickoff": ko, "pregame": True,
                    "coverage_status": p["coverage_status"], "projected_points": p["projected_points"],
                    "p_play": p["p_play"], "expected_points": p["expected_points"]})
    for (rid, week, why), n in sorted(dropped.items()):
        exclusions.append({"run_id": rid, "week": week, "reason": why, "players": n})
    return out, exclusions


ForecastReader = Callable[..., "tuple[list[dict], list[dict]]"]


# ----------------------------------------------------------------------------- evaluation rows


def _hours(kickoff: datetime | None, t: datetime | None) -> float | None:
    return None if kickoff is None or t is None else round((kickoff - t).total_seconds() / 3600.0, 3)


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _origin(fc: dict | None) -> tuple[str, float | None, float | None, float | None]:
    """(origin, independent unconditional pred, conditional pred, consumer-visible model value) with
    projection_source.weekly semantics. A missing, abstaining or participation-less forecast is the
    ESPN fallback and carries no model prediction at all."""
    if fc is None:
        return ESPN, None, None, None
    cov, exp = fc.get("coverage_status"), _num(fc.get("expected_points"))
    if cov == "projected" and exp is not None:
        return OURS, exp, _num(fc.get("projected_points")), exp
    if cov in ZERO_STATUSES:
        return OURS_ZERO, 0.0, None, 0.0
    return ESPN, None, None, None


def _crosswalk(con) -> dict[int, dict]:
    if not table_exists(con, "player_id_map"):
        return {}
    rows = fetch_dicts(con, "SELECT espn_player_id, gsis_id, match_confidence, notes, espn_position, nfl_team "
                            "FROM player_id_map")
    per_gsis: dict[str, int] = {}
    for r in rows:
        if r["gsis_id"] and r["match_confidence"] == "high":
            per_gsis[r["gsis_id"]] = per_gsis.get(r["gsis_id"], 0) + 1
    out = {}
    for r in rows:
        ok = (r["match_confidence"] == "high" and r["gsis_id"] and "conflict" not in (r["notes"] or "").lower()
              and per_gsis.get(r["gsis_id"], 0) == 1)
        out[r["espn_player_id"]] = {"gsis_id": r["gsis_id"] if ok else None, "position": r["espn_position"],
                                    "ok": bool(ok)}
    return out


def _schedule(con, season: int) -> tuple[dict, set]:
    kick: dict[tuple, datetime | None] = {}
    scheduled: set = set()
    if table_exists(con, "nfl_schedules"):
        for g in fetch_dicts(con, "SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
                                  "WHERE season = ? AND game_type = 'REG'", [season]):
            for t in (g["home_team"], g["away_team"]):
                kick[(g["week"], t)] = _kickoff(g)
                scheduled.add((g["week"], t))
    return kick, scheduled


def _espn_snapshots(con, season: int, weeks: list[int]) -> dict[tuple, list[tuple[datetime, float]]]:
    out: dict[tuple, list[tuple[datetime, float]]] = {}
    if not table_exists(con, "espn_projection_history"):
        return out
    for r in fetch_dicts(con, """SELECT scoring_period AS week, espn_player_id, projected_points, synced_at
                                 FROM espn_projection_history
                                 WHERE season = ? AND scoring_period IN (SELECT unnest(?))
                                   AND projected_points IS NOT NULL""", [season, weeks]):
        t, v = _utc(r["synced_at"]), _num(r["projected_points"])
        if t is not None and v is not None:
            out.setdefault((r["week"], r["espn_player_id"]), []).append((t, v))
    return out


def _all_weeks(con, season: int) -> list[int]:
    if not table_exists(con, "espn_projection_history"):
        return []
    return [r["w"] for r in fetch_dicts(con, "SELECT DISTINCT scoring_period AS w FROM espn_projection_history "
                                             "WHERE season = ? AND scoring_period >= 1 ORDER BY 1", [season])]


def _verify(fc: dict, *, k: datetime, fk: datetime | None, model_version: str, fingerprint: str,
            gsis: str | None) -> str | None:
    """Defence in depth: the reader should only return pregame, eligible rows; never trust it."""
    created, fitted = _utc(fc.get("run_created_at")), _utc(fc.get("artifact_created_at"))
    if created is None or fc.get("pregame") is False or not created < k:
        return "independent_not_pregame"
    if fc.get("model_version") != model_version:
        return "independent_model_mismatch"
    if fc.get("scoring_fingerprint") != fingerprint:
        return "independent_scoring_mismatch"
    if fitted is None or fk is None:
        return "independent_artifact_time_unknown"
    if fitted >= fk:
        return "independent_artifact_after_first_kickoff"
    if fc.get("gsis_id") and fc["gsis_id"] != gsis:
        return "independent_id_mismatch"
    return None


def evaluation_rows(con, season: int, weeks: Iterable[int] | None = None, *,
                    forecast_reader: ForecastReader | None = None, model_version: str = MODEL_VERSION,
                    scoring=None, sources: Sources | None = None,
                    fetched_at: datetime | dict | None = None) -> tuple[list[dict], dict]:
    """One evaluation row per (season, week, espn_player_id) known to ESPN history or to the
    forecast reader, with `exclusion_reason` set for every row that must not be scored.
    Returns (rows with exactly ROW_KEYS, meta). `scoring`: a resolved SkillScoring (default: the
    league's); `sources`/`fetched_at` can be injected (tests) and default to the database's."""
    reader = forecast_reader or legacy_forecast_reader
    if scoring is None:
        _, scoring = league_scoring(con, season)
    scoring.require_supported()
    fingerprint = scoring.fingerprint
    week_list = sorted({int(w) for w in weeks}) if weeks is not None else _all_weeks(con, season)
    meta = {"season": season, "weeks": week_list, "model_version": model_version, "scoring_fingerprint": fingerprint,
            "reader_exclusions": []}
    if not week_list:
        return [], meta
    if sources is None:
        sources = load_sources(con, scoring, [season])
    if fetched_at is None:
        from .schedule_intelligence import nfl_fetch_times
        fetched_at = nfl_fetch_times(con, season)["schedules"]
    fetched = fetched_at if isinstance(fetched_at, dict) else {season: fetched_at}

    forecasts, rexcl = reader(con, season, week_list, model_version=model_version, scoring_fingerprint=fingerprint)
    meta["reader_exclusions"] = list(rexcl)
    fc_by: dict[tuple, dict] = {}
    for f in forecasts:
        fc_by[(int(f["week"]), int(f["espn_player_id"]))] = f
    weeks_with_run = {w for w, _ in fc_by}
    cross = _crosswalk(con)
    kick, scheduled = _schedule(con, season)
    firsts = first_kickoffs(con, season)
    snaps = _espn_snapshots(con, season, week_list)
    roster_team: dict[tuple, str] = {}
    if sources.rosters is not None and sources.rosters.height:
        for r in sources.rosters.filter(pl.col("season") == season).iter_rows(named=True):
            roster_team[(r["week"], r["gsis_id"])] = r["team"]

    keys = sorted(k for k in set(snaps) | set(fc_by) if k[0] in week_list)
    recs: list[dict] = []
    for week, pid in keys:
        fc, cm = fc_by.get((week, pid)), cross.get(pid)
        position = (fc or {}).get("position") or (cm or {}).get("position")
        gsis = cm["gsis_id"] if cm and cm["ok"] else None
        team = ((fc or {}).get("nfl_team") or roster_team.get((week, gsis))) if gsis else None
        sched_k = kick.get((week, team)) if team else None
        fc_k = _utc((fc or {}).get("kickoff"))
        known = [x for x in (sched_k, fc_k) if x is not None]
        k = min(known) if known else None
        rec = dict.fromkeys(ROW_KEYS)
        rec.update(season=season, week=week, gsis_id=gsis, espn_player_id=pid, position=position, team=team,
                   kickoff=k, ours_origin=ESPN)
        rec["_fc_kickoff"] = fc_k if fc_k is not None else sched_k
        reason = None
        if position not in POSITIONS:
            reason = "unsupported_position"
        elif gsis is None:
            reason = "uncertain_id_mapping"
        elif team is None:
            reason = "team_unknown"
        elif k is None:
            reason = "bye" if (week, team) not in scheduled else "kickoff_unknown"
        if reason is None:
            pre = [(t, v) for t, v in snaps.get((week, pid), []) if t < k]
            if pre:
                t, v = max(pre)
                rec.update(espn_pred=v, espn_captured_at=t, espn_horizon_h=_hours(k, t))
            else:
                reason = "espn_not_pregame" if snaps.get((week, pid)) else "no_espn_snapshot"
        if reason is None and week not in weeks_with_run:
            reason = "no_independent_forecast_for_week"
        if reason is None and fc is not None:
            reason = _verify(fc, k=k, fk=firsts.get(week), model_version=model_version, fingerprint=fingerprint,
                             gsis=gsis)
            if reason is None:
                origin, ind, cond, consumer = _origin(fc)
                rec.update(ours_origin=origin, run_id=fc.get("run_id"), run_created_at=_utc(fc.get("run_created_at")))
                if origin != ESPN:
                    rec.update(ours_independent_pred=ind, ours_conditional_pred=cond, ours_consumer_pred=consumer,
                               ours_horizon_h=_hours(k, rec["run_created_at"]))
        rec["exclusion_reason"] = reason
        recs.append(rec)

    # outcome labels, with weekly_learning semantics, for every row that has an identity and a team
    if recs:
        frame = pl.DataFrame(
            {"season": [r["season"] for r in recs], "week": [r["week"] for r in recs],
             "gsis_id": [r["gsis_id"] for r in recs], "team": [r["team"] for r in recs],
             "kickoff": [r["_fc_kickoff"] for r in recs], "_i": list(range(len(recs)))},
            schema={"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "team": pl.String,
                    "kickoff": pl.Datetime("us", "UTC"), "_i": pl.Int64})
        labelled = wl.label_outcomes(frame, sources, fetched)
        for r in labelled.iter_rows(named=True):
            rec = recs[r["_i"]]
            rec["outcome_status"], rec["actual_points"] = r["outcome_status"], r["label"]
    for rec in recs:
        rec.pop("_fc_kickoff", None)
        if rec["exclusion_reason"] is None:
            status = rec["outcome_status"]
            if status not in wl.RESOLVED:
                rec["exclusion_reason"] = f"outcome_{status}"
        if rec["exclusion_reason"] is None and rec["ours_origin"] == ESPN:
            rec["ours_consumer_pred"] = rec["espn_pred"]     # the consumer sees ESPN's number: no model credit
        if rec["outcome_status"] not in wl.RESOLVED:
            rec["actual_points"] = None
    return [{k: r[k] for k in ROW_KEYS} for r in recs], meta


# ----------------------------------------------------------------------------- comparisons


def _scored(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["exclusion_reason"] is None]


def _resolved(r: dict) -> bool:
    return r["outcome_status"] in wl.RESOLVED


def assert_equal_population(rows: list[dict], ours: str) -> None:
    """Every scored row must carry the actual, ESPN's prediction and the model's prediction."""
    for r in rows:
        for c in ("actual_points", "espn_pred", ours):
            v = r[c]
            assert v is not None and math.isfinite(v), f"unequal population: {c} missing for {r['season']}w{r['week']} {r['espn_player_id']}"


def population(rows: list[dict], name: str, cfg: BenchmarkConfig) -> tuple[list[dict], str]:
    """(rows, ours column) of one comparison; ESPN is always `espn_pred`."""
    s = [r for r in _scored(rows) if _resolved(r)]
    if name == "pure_independent":
        return [r for r in s if r["ours_origin"] in (OURS, OURS_ZERO)], "ours_independent_pred"
    if name == "consumer_visible":
        return s, "ours_consumer_pred"
    if name == "conditional":
        return [r for r in s if r["outcome_status"] == wl.APPEARED and r["ours_origin"] == OURS
                and r["ours_conditional_pred"] is not None], "ours_conditional_pred"
    if name == "unconditional":
        return [r for r in s if r["ours_origin"] == OURS], "ours_independent_pred"
    if name == "horizon_matched":
        pure, col = population(rows, "pure_independent", cfg)
        return [r for r in pure if r["ours_horizon_h"] is not None and r["espn_horizon_h"] is not None
                and abs(r["ours_horizon_h"] - r["espn_horizon_h"]) <= cfg.horizon_window_h], col
    raise ValueError(f"unknown comparison {name}")


DEFINITIONS = {
    "pure_independent": "Rows where the model itself projects (origin ours or ours_zero), APPEARED + CONFIRMED_ABSENT "
                        "(absent = 0): unconditional expected points vs ESPN. ESPN-fallback rows are excluded.",
    "consumer_visible": "What the opt-in projection switch shows (projection_source.weekly) vs ESPN, every eligible "
                        "resolved row. Fallback rows show ESPN's number: zero difference, no model credit.",
    "conditional": "APPEARED only, origin ours: points-if-he-plays (projected_points) vs ESPN.",
    "unconditional": "APPEARED + CONFIRMED_ABSENT (absent = 0), origin ours: expected points vs ESPN. Same "
                     "players as `conditional` plus the confirmed absences.",
    "horizon_matched": "pure_independent rows whose two forecast horizons differ by at most the window "
                       "(horizon_window_h): both forecasts saw almost the same information time.",
}


def _group_stats(g: list[dict], col: str, cfg: BenchmarkConfig, need_n: int, consumer: bool) -> dict:
    n = len(g)
    model = [r for r in g if r["ours_origin"] != ESPN] if consumer else g
    weeks = len({(r["season"], r["week"]) for r in model})
    out = {"n": n, "n_model": len(model), "weeks_covered": weeks, "required_n": need_n,
           "required_weeks": cfg.min_weeks}
    if consumer:
        out["fallback_n"] = n - len(model)
        out["fallback_share"] = round((n - len(model)) / n, 4) if n else None
    if not g:
        out.update(ours=metrics(np.array([]), np.array([])), espn=metrics(np.array([]), np.array([])),
                   mae_diff=None, ci95=None)
    else:
        a = np.array([r["actual_points"] for r in g], dtype=float)
        o = np.array([r[col] for r in g], dtype=float)
        e = np.array([r["espn_pred"] for r in g], dtype=float)
        out["ours"], out["espn"] = metrics(a, o), metrics(a, e)
        df = pl.DataFrame({"label": a, "ours": o, "espn": e, "key": [r["season"] * 100 + r["week"] for r in g]})
        b = week_bootstrap_mae_diff(df, "ours", "espn", cfg.resamples, cfg.seed)
        out["mae_diff"], out["ci95"] = b["diff"], b["ci95"]
    out["verdict"], out["verdict_reason"] = verdict(out["n_model"], weeks, out["ci95"], need_n, cfg.min_weeks)
    return out


def verdict(n_model: int, weeks: int, ci95, need_n: int, need_weeks: int) -> tuple[str, str]:
    """The evidence gate. Never manufactures a result: thin data is insufficient_evidence."""
    if n_model < need_n:
        return "insufficient_evidence", f"{n_model} model-covered rows < {need_n} required"
    if weeks < need_weeks:
        return "insufficient_evidence", f"{weeks} distinct weeks < {need_weeks} required"
    if ci95 is None:
        return "insufficient_evidence", "no confidence interval"
    lo, hi = ci95
    if hi < 0:
        return "ours_better", "95% week-clustered CI of MAE(ours) - MAE(ESPN) is below 0"
    if lo > 0:
        return "espn_better", "95% week-clustered CI of MAE(ours) - MAE(ESPN) is above 0"
    return "no_significant_difference", "95% week-clustered CI of MAE(ours) - MAE(ESPN) includes 0"


def compare(rows: list[dict], name: str, cfg: BenchmarkConfig = BenchmarkConfig()) -> dict:
    need_n = PROMOTION_CRITERIA["espn_min_matched_per_position"]
    pop, col = population(rows, name, cfg)
    assert_equal_population(pop, col)
    consumer = name == "consumer_visible"
    by = {pos: _group_stats([r for r in pop if r["position"] == pos], col, cfg, need_n, consumer)
          for pos in POSITIONS}
    allg = _group_stats(pop, col, cfg, need_n, consumer)
    by["ALL"] = allg
    out = {"name": name, "definition": DEFINITIONS[name], "ours_column": col, "population_n": len(pop),
           "n": allg["n"], "n_model": allg["n_model"], "weeks_covered": allg["weeks_covered"],
           "verdict": allg["verdict"], "verdict_reason": allg["verdict_reason"], "by_position": by}
    if consumer:
        out["fallback_share"] = allg["fallback_share"]
    if name == "horizon_matched":
        ref, _ = population(rows, "pure_independent", cfg)
        out["window_h"] = cfg.horizon_window_h
        out["all_rows_reference"] = {"n": len(ref), "of_which_matched": len(pop),
                                     "share_matched": round(len(pop) / len(ref), 4) if ref else None}
    return out


# ----------------------------------------------------------------------------- diagnostics


def _count(rows: list[dict], field: str, order: Iterable[str] | None = None) -> dict:
    c: dict[str, int] = {}
    for r in rows:
        v = r[field]
        if v is not None:
            c[v] = c.get(v, 0) + 1
    keys = [k for k in (order or ()) if k in c] + sorted(k for k in c if k not in (order or ()))
    return {k: c[k] for k in keys}


def coverage(rows: list[dict]) -> dict:
    """Exclusion and outcome diagnostics, in total and per week and position."""
    cells = []
    for week in sorted({r["week"] for r in rows}):
        for pos in [*POSITIONS, None]:
            g = [r for r in rows if r["week"] == week and (r["position"] == pos if pos else r["position"] not in POSITIONS)]
            if not g:
                continue
            cells.append({"week": week, "position": pos or "other", "rows": len(g), "scored": len(_scored(g)),
                          "exclusions": _count(g, "exclusion_reason", EXCLUSIONS),
                          "outcomes": _count(g, "outcome_status", wl.OUTCOME_STATUSES)})
    origins = _count(_scored(rows), "ours_origin")
    return {"rows": len(rows), "scored": len(_scored(rows)), "by_exclusion_reason": _count(rows, "exclusion_reason", EXCLUSIONS),
            "by_outcome_status": _count(rows, "outcome_status", wl.OUTCOME_STATUSES), "scored_by_origin": origins,
            "by_week_position": cells}


# ----------------------------------------------------------------------------- scorecard


def scorecard(con, season: int, weeks: Iterable[int] | None = None, *, forecast_reader: ForecastReader | None = None,
              config: BenchmarkConfig | None = None, model_version: str = MODEL_VERSION, scoring=None,
              sources: Sources | None = None, fetched_at: datetime | dict | None = None) -> dict:
    """Read-only, deterministic scorecard (no wall-clock values): same inputs, same dict."""
    cfg = config or BenchmarkConfig()
    base = {"schema_version": SCHEMA_VERSION, "season": season, "model_version": model_version, "notes": NOTES,
            "params": {**asdict(cfg), "espn_min_matched_per_position": PROMOTION_CRITERIA["espn_min_matched_per_position"]}}
    try:
        rows, meta = evaluation_rows(con, season, weeks, forecast_reader=forecast_reader, model_version=model_version,
                                     scoring=scoring, sources=sources, fetched_at=fetched_at)
    except ValueError as err:     # unsupported league scoring: no labels can be computed
        return {**base, "available": False, "unavailable_reason": str(err), "weeks": [], "comparisons": {},
                "coverage": None, "reader_exclusions": []}
    base["params"]["scoring_fingerprint"] = meta["scoring_fingerprint"]
    return {**base, "available": True, "unavailable_reason": None, "weeks": meta["weeks"],
            "comparisons": {name: compare(rows, name, cfg) for name in COMPARISONS},
            "coverage": coverage(rows), "reader_exclusions": meta["reader_exclusions"]}


def to_json(card: dict) -> str:
    return json.dumps(card, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n"


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def record(card: dict, out_dir: Path, season: int, week: int) -> tuple[Path, bool]:
    """Write the scorecard once, named by the sha256 of its bytes. Identical inputs give the same
    file (nothing is written); changed inputs give a new file; an existing file is never replaced.
    Returns (path, created)."""
    text = to_json(card)
    path = Path(out_dir) / f"s{season}w{int(week):02d}-{content_sha(text)[:16]}.json"
    created = wl.write_once(path, text)
    if not created and path.read_text() != text:
        raise RuntimeError(f"{path} exists with different content (content sha collision or a damaged file); "
                           "move it away and rerun")
    return path, created

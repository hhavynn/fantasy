"""Research only: does the production model's teammate response predict what happens when a teammate's availability
changes? (issue #213, v1; docs/research/teammate_response.md)

Nothing in the app imports this module (tests/test_teammate_response.py); only scripts/teammate_response_research.py
does. It changes no forecast, table or artifact.

ifp-2.2.0 ships ifp-ctx-p1's teammate group for RB and WR (docs/research/ifp22_promotion.md): features that say how
much of the team's targets and carries in the player's recent games went to teammates unavailable this week
(`tm_*_vacated`), how much returning teammates reclaim (`tm_*_returning`) and the per-game volume he would gain from
it (`tm_*_gain_pg`). With every teammate available as before, all of them are 0 (roster_context._teammates).

The model's teammate response for a player-week is the model's point with his actual teammate inputs minus its point
with those inputs neutral (0), same fitted model, every other input unchanged. A what-if product would show exactly
that number, so it is evaluated before anything is built: on player-weeks where a teammate input is non-zero (a
teammate is out or returning), is the model's forecast closer to what happened than

  zero    the same model with the teammate inputs neutral (no teammate adjustment at all), and
  split   that zero forecast plus the player's own observational with/without split: his mean points in his earlier
          games of the season that had a teammate change minus his mean in those that did not (at least MIN_SPLIT_GAMES
          each, else no adjustment). Observational: teammates' absences are not random, so a split is a correlation,
          never a causal effect, and it is a baseline here only because a manager could compute it by hand.

DECISION_RULE is fixed here before the first run. QB and TE are out of scope (no shipped teammate group with evidence
for TE; QB scenario challengers were not adopted).

The scenario core (`scenario_point`) works on a stored run's verified full inputs (production_forecasts, #225 a): an
unchanged scenario reproduces the stored point exactly, and a change to an input the forecast did not know (imputed)
is refused with a reason, never computed.
"""

from __future__ import annotations

from collections import defaultdict
import math
import random

import numpy as np
import polars as pl

from .context_challenger import GROUPS
from .production_forecasts import reproduce

TR_VERSION = "teammate-response-0.1.0"
TR_STATUS = "research_only"
POSITIONS = ("RB", "WR")
TM_FEATURES = {p: tuple(GROUPS["tm"][p]) for p in POSITIONS}
NEUTRAL = 0.0
EPS = 1e-9
MIN_SPLIT_GAMES = 2
BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED = 2000, 213

DECISION_RULE = (
    "Per position (RB, WR), on evaluation player-weeks 2023 week 1 - 2025 week 18 with a non-zero teammate input, the "
    "model's teammate response passes only if MAE(model) - MAE(zero) and MAE(model) - MAE(split) both have a 95% "
    "paired bootstrap interval entirely below 0, under BOTH clusterings (by week and by player-season). Every position "
    "is reported whatever the outcome. A product what-if is considered only for a position that passes."
)


# ----------------------------------------------------------------------------- the response on feature rows


def neutral(rows: pl.DataFrame, position: str) -> pl.DataFrame:
    """The rows with the position's teammate inputs neutral (0) where known; an unknown (null) input stays unknown."""
    cols = [c for c in TM_FEATURES[position] if c in rows.columns]
    return rows.with_columns([pl.when(pl.col(c).is_null()).then(None).otherwise(pl.lit(NEUTRAL)).alias(c) for c in cols])


def event_mask(rows: pl.DataFrame, position: str) -> np.ndarray:
    """True where a known teammate input of the position is non-zero (a teammate out or returning)."""
    cols = [c for c in TM_FEATURES[position] if c in rows.columns]
    if not cols:
        return np.zeros(rows.height, dtype=bool)
    m = np.zeros(rows.height, dtype=bool)
    for c in cols:
        v = rows[c].cast(pl.Float64).fill_null(0.0).to_numpy()
        m |= np.abs(v) > EPS
    return m


def split_deltas(df: pl.DataFrame) -> pl.DataFrame:
    """Per row, the player's observational with/without split from his EARLIER games of the same season only: mean
    label in event games minus mean label in non-event games, when each has >= MIN_SPLIT_GAMES; else null (no
    adjustment). `df`: season, week, gsis_id, label, is_event."""
    out = []
    for (season, pid), g in df.sort("week").group_by(["season", "gsis_id"], maintain_order=True):
        ev, ne = [], []
        for week, y, is_ev in g.select("week", "label", "is_event").iter_rows():
            d = (sum(ev) / len(ev) - sum(ne) / len(ne)) if len(ev) >= MIN_SPLIT_GAMES and len(ne) >= MIN_SPLIT_GAMES else None
            out.append({"season": season, "gsis_id": pid, "week": week, "split_delta": d})
            if y is not None:                                   # a game played becomes history only after its week
                (ev if is_ev else ne).append(float(y))
    schema = {"season": pl.Int64, "gsis_id": pl.Utf8, "week": pl.Int64, "split_delta": pl.Float64}
    return pl.DataFrame(out, schema=schema)


# ----------------------------------------------------------------------------- evaluation


def _percentile(sorted_draws: list[float], q: float) -> float:
    pos = q * (len(sorted_draws) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_draws) - 1)
    return sorted_draws[lo] + (sorted_draws[hi] - sorted_draws[lo]) * (pos - lo)


def _mae_gap(rows: list[dict], a: str, b: str) -> dict:
    """MAE(a) - MAE(b) on the same rows, with 95% paired bootstrap intervals clustered by (season, week) and by
    (season, player)."""
    def clusters(key) -> list[list[float]]:
        cs: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rows:
            c = cs[key(r)]
            c[0] += abs(r[a] - r["label"])
            c[1] += abs(r[b] - r["label"])
            c[2] += 1
        return [cs[k] for k in sorted(cs)]

    def gap(cs) -> float | None:
        n = sum(c[2] for c in cs)
        return (sum(c[0] for c in cs) - sum(c[1] for c in cs)) / n if n else None

    out: dict = {"difference": None}
    for name, key in (("by_week", lambda r: (r["season"], r["week"])),
                      ("by_player_season", lambda r: (r["season"], r["gsis_id"]))):
        cs = clusters(key)
        out["difference"] = gap(cs)
        if out["difference"] is None:
            out[name] = {"ci95": None, "clusters": 0}
            continue
        rng = random.Random(BOOTSTRAP_SEED)
        draws = sorted(gap([cs[rng.randrange(len(cs))] for _ in cs]) for _ in range(BOOTSTRAP_RESAMPLES))
        out[name] = {"ci95": [_percentile(draws, 0.025), _percentile(draws, 0.975)], "clusters": len(cs)}
    return out


def _passes(gap: dict) -> bool:
    return all(gap[k]["ci95"] is not None and gap[k]["ci95"][1] < 0 for k in ("by_week", "by_player_season"))


def evaluate(preds: pl.DataFrame) -> dict:
    """`preds`: season, week, gsis_id, position, label, is_event, pred_model, pred_zero, split_delta. Event rows with a
    label and both forecasts are compared; the split forecast is pred_zero + split_delta (pred_zero where null)."""
    out: dict = {"version": TR_VERSION, "status": TR_STATUS, "decision_rule": DECISION_RULE,
                 "min_split_games": MIN_SPLIT_GAMES, "positions": {}}
    for pos in POSITIONS:
        p = preds.filter(pl.col("position") == pos)
        valid = p.filter(pl.col("label").is_not_null() & pl.col("pred_model").is_not_null()
                         & pl.col("pred_zero").is_not_null())
        ev = valid.filter(pl.col("is_event"))
        rows = [dict(r, pred_split=r["pred_zero"] + (r["split_delta"] or 0.0)) for r in ev.iter_rows(named=True)]
        if not rows:
            out["positions"][pos] = {"event_rows": 0, "decision": "no_data"}
            continue
        y = np.array([r["label"] for r in rows])
        resp = np.array([r["pred_model"] - r["pred_zero"] for r in rows])
        surprise = y - np.array([r["pred_zero"] for r in rows])
        nz = np.abs(resp) > EPS
        slope = float(np.dot(resp, surprise) / np.dot(resp, resp)) if np.dot(resp, resp) > 0 else None
        gaps = {"model_minus_zero": _mae_gap(rows, "pred_model", "pred_zero"),
                "model_minus_split": _mae_gap(rows, "pred_model", "pred_split")}
        diagnostics = {}
        if "pred_retrained" in ev.columns and all(r["pred_retrained"] is not None for r in rows):
            # Not deciding (added after the first results): the model retrained without the teammate group.
            diagnostics["model_minus_retrained"] = _mae_gap(rows, "pred_model", "pred_retrained")
            diagnostics["mae_retrained"] = float(np.mean([abs(r["pred_retrained"] - r["label"]) for r in rows]))
        out["positions"][pos] = {
            "rows": valid.height, "event_rows": len(rows), "event_share": len(rows) / valid.height,
            "players": len({(r["season"], r["gsis_id"]) for r in rows}),
            "weeks": len({(r["season"], r["week"]) for r in rows}),
            "split_available": sum(1 for r in rows if r["split_delta"] is not None),
            "mae": {k: float(np.mean([abs(r[f"pred_{k}"] - r["label"]) for r in rows])) for k in ("model", "zero", "split")},
            "response": {"mean": float(resp.mean()), "mean_abs": float(np.abs(resp).mean()),
                         "p90_abs": float(np.quantile(np.abs(resp), 0.9)),
                         # through the origin: how much of the response showed up in the result (1 = calibrated)
                         "calibration_slope": slope,
                         "sign_agreement": float(np.mean(np.sign(resp[nz]) == np.sign(surprise[nz]))) if nz.any() else None},
            "gaps": gaps,
            "non_event_identical": bool(np.allclose(
                valid.filter(~pl.col("is_event"))["pred_model"].to_numpy(),
                valid.filter(~pl.col("is_event"))["pred_zero"].to_numpy())),
            "decision": "pass" if all(_passes(g) for g in gaps.values()) else "fail",
            "diagnostics": diagnostics,
        }
    return out


# ----------------------------------------------------------------------------- the scenario core on stored inputs


def scenario_point(model: dict, inp: dict, changes: dict[str, float]) -> dict:
    """A stored row's point with some inputs changed, from its verified full inputs (production_forecasts) and model.
    No change reproduces the stored point exactly. Refused (`available: false` with a reason) when a changed column
    is not a model input or was unknown (imputed) in the forecast: an unknown is never filled in to make a scenario."""
    cols = list(model["columns"])
    base = reproduce(model, inp)
    if base is None:
        return {"available": False, "reason": "inputs_do_not_fit_the_model"}
    values = list(base["values"])
    for col, v in changes.items():
        if col not in cols:
            return {"available": False, "reason": "not_a_model_input", "column": col}
        i = cols.index(col)
        if inp["imputed"][i]:
            return {"available": False, "reason": "input_unknown", "column": col}
        values[i] = float(v)
    out = reproduce(model, {**inp, "input_values": values})
    return {"available": True, "baseline": base["points"], "points": out["points"],
            "delta": out["points"] - base["points"], "floored": bool(base["floored"] or out["floored"])}


def teammate_neutral(model: dict, inp: dict, position: str) -> dict:
    """The scenario 'every teammate available as before': the position's teammate inputs set to neutral."""
    return scenario_point(model, inp, {c: NEUTRAL for c in TM_FEATURES.get(position, ()) if c in model["columns"]})

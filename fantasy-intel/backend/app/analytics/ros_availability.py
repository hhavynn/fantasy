"""Rest-of-season P(plays) for players injured at the cutoff (research only, ros-avail-0.1.0).

Nothing in the app imports this module (tests/test_ros_availability.py). build_live, projection_source, the
model registry, approved artifacts and production runs are untouched. See docs/research/ros_availability.md.

The problem. A rest-of-season run (the projection store's `build_live(..., projected_from_week=W)`) gives
every target week T > W the participation model's *undesignated* p_play (`participation.predict_week(...,
feature_week=W)`): no week-T report exists, so a player ruled Out at W looks healthy from W+1 on.

The candidate (one, fixed before any evaluation run). For a player designated Out or Doubtful on the week-W
report, his p_play for his team's h-th game after W is the empirical return rate
`injury_workload.fit_return_rates` P(appears in the team's h-th game after W | Out/Doubtful at W), by position
and games already missed (pooled when a cell has < 30 episodes), with h > 4 using the h = 4 cell. The table is
refit per season on every episode from FIT_FIRST_SEASON through the previous season (its h >= 3 rates drift
upward). Every other player keeps the current p_play. A frequency, never a recovery date; no probability is
hand-set.

Leakage contract: the cutoff week's pregame report (rows modified at/after kickoff are dropped by
forecast_features.load_injuries), box scores strictly before W, and a table fitted only on earlier seasons.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json

import numpy as np
import polars as pl

from .forecast_features import Sources, key
from .injury_workload import (SKILL, UNAVAILABLE_DESIGNATIONS, _return_rate, fit_return_rates, injury_timeline,
                              missed_bucket)
from .participation import brier, log_loss, reliability

VERSION = "ros-avail-0.1.0"
SEED = 20261018
FIT_FIRST_SEASON = 2019            # first season with pregame-timestamped nflverse injury reports in the store
TEST_SEASONS = (2023, 2024)        # confirmation: the gates below are judged here, once
EXPLORATORY_SEASONS = (2025,)      # replay only: 2025+ reports carry no modification time; never gated
GATED_HORIZONS = (1, 2, 3, 4)      # h = the team's h-th game after the cutoff week (byes skipped)
DESCRIPTIVE_HORIZONS = (5, 6, 7, 8)    # the h = 4 cell reused, as production would; reported, not gated
TABLE_MAX_H = 4                    # fit_return_rates fits h = 1..4
CUTOFF_WEEKS = tuple(range(2, 18))
BINS = 10                          # equal-width reliability bins (participation.reliability)
BIN_MIN_N = 40                     # max bin gap counts bins with at least this many episodes
ROS_TOTAL_GAMES = 4                # ROS expected-points total: the team's next 4 games after the cutoff
SUBGROUP_MIN_N = 40
BOOTSTRAP_RESAMPLES = 1000

# Frozen BEFORE the first evaluation run (committed on its own; tests pin the hash). Known in advance from
# docs/research/injury_workload_handoff.md section 6: the h = 1 means (current 0.91 vs actual 0.39) and the
# fixed 2019-22 table's per-h test rates. The thresholds are set from the sampling noise of ~100-episode bins
# (sd of an observed rate ~0.05), not tuned to those numbers.
DECISION_RULES = {
    "population": "players (RB/WR/TE) designated Out or Doubtful on the week-W pregame report, W = 2..17, who have a "
                  "lookback appearance, are in the current ROS path's participation population for target week T "
                  "(latest roster row <= W is ACT/INA), and whose team's h-th game after W (week T) is final. "
                  "Outcome: appeared in week T (box-score row or offensive snaps).",
    "comparison": "p_current = participation.predict_week(sources, ipm-1.0.0, season, T, feature_week=W) (what "
                  "build_live gives today) vs p_candidate = the return-rate table refit per season on "
                  f"{FIT_FIRST_SEASON}..season-1. Identical rows. ipm-1.0.0 was trained on seasons that include "
                  "2023-24 (its held-out seasons are 2025-26), so the current path is in-sample there: conservative "
                  "for the candidate.",
    "gates_per_horizon": {
        "c1_brier": "Brier(candidate) - Brier(current) < 0 with a 95% bootstrap CI upper bound < 0 (pooled 2023-24; "
                    "1000 resamples of whole cutoff weeks, seed 20261018), at every h = 1..4",
        "c2_ece": "ECE(candidate) <= 0.06 (10 equal-width bins) at every h = 1..4",
        "c3_max_bin_gap": f"max |observed - mean predicted| over bins with n >= {BIN_MIN_N} <= 0.12 at every h = 1..4",
        "c4_ece_not_worse": "ECE(candidate) < ECE(current) at every h = 1..4",
        "c5_each_season": "Brier(candidate) < Brier(current) in 2023 and in 2024 separately, at every h = 1..4",
        "c6_subgroups": f"for every position (RB/WR/TE), missed bucket (0/1/2-3/4+) and designation (out/doubtful) "
                        f"with n >= {SUBGROUP_MIN_N} at an h = 1..4: Brier(candidate) <= Brier(current) + 0.01",
    },
    "gates_ros_totals": {
        "r1_mae": f"expected-points total over the team's next {ROS_TOTAL_GAMES} games (sum of p_h x conditional_h; "
                  "actual = points scored, 0 when he did not appear), episodes with all four games final and a "
                  "conditional projection for each: MAE(candidate) - MAE(current) < 0 with a 95% bootstrap CI upper "
                  "bound < 0 (cutoff-week resampling)",
        "r2_bias": "|bias(candidate)| <= |bias(current)| on the same totals",
        "conditional": "the ifp-2 ridge fitted walk-forward (challenger.train_rows before W, frozen ifp-2 config) on "
                       "week-W features against week T's opponent: identical under both paths, so only p_play moves",
    },
    "descriptive_only": ["h = 5..8 (h = 4 cell reused)", "2025 exploratory replay (ipm-1.0.0 out-of-sample, "
                         "untimestamped reports)", "a fixed 2019-22 table (no refit)", "log loss", "the live 2026 "
                         "week-6 cutoff example"],
    "outcome": "every gate passes -> propose_for_owner_review (a written proposal; nothing changes in production); "
               "any gate fails -> retain_current, with the failures listed",
    "never": "no change to build_live, projection_source.py, the registry, approved artifacts or production runs "
             "without owner approval; the merge coordinator is told first",
}
GATE_LIMITS = {"c2_ece": 0.06, "c3_max_bin_gap": 0.12, "c6_tolerance": 0.01}


def rules_sha() -> str:
    return hashlib.sha256(json.dumps({"rules": DECISION_RULES, "limits": GATE_LIMITS}, sort_keys=True)
                          .encode()).hexdigest()[:16]


# ros-avail-0.1.0 failed its gates (retain_current; docs/research/ros_availability.md). The next candidate is
# written down here BEFORE any data it will be judged on exists. Its design used what the 0.1.0 confirmation showed
# (post hoc), so 2019-2025 can no longer test it: only prospective weeks can. Not implemented or run here.
NEXT_CANDIDATE = {
    "version": "ros-avail-0.1.1",
    "changes": [
        "fit population = application population: an Out/Doubtful episode counts only if the player's latest weekly "
        "roster row <= W is ACT/INA (reserve-list players are withheld by build_live and never receive a ROS p_play; "
        "0.1.0 mixed them in and under-predicted returns)",
        "applied at h = 1 and h = 2 only; h >= 3 keeps the current p_play (0.1.0 lost to the current path there)",
        "refit at every cutoff on all episodes from 2019 through the cutoff (current season included)",
    ],
    "evaluation": "prospective only: a write-once pregame ledger from the 2026 week-7 cutoff to the end of the regular "
                  "season (current p_play, candidate p_play, both frozen before kickoff), scored after the season",
    "gates": {
        "p1_brier": "Brier(candidate) - Brier(current) < 0 with a cutoff-week bootstrap 95% CI upper < 0, at h = 1 and "
                    "at h = 2 separately",
        "p2_mean": "|mean candidate p - observed rate| <= 0.08 at h = 1 and at h = 2",
        "p3_totals": "expected points over the next two team games: MAE(candidate) <= MAE(current) (point estimate)",
    },
    "outcome": "all pass -> promotion proposal for owner review; otherwise retain_current",
}


def next_candidate_sha() -> str:
    return hashlib.sha256(json.dumps(NEXT_CANDIDATE, sort_keys=True).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- the candidate


def table_for_season(sources: Sources, season: int, first_season: int = FIT_FIRST_SEASON) -> dict:
    """The return-rate table a forecast in `season` may use: every Out/Doubtful episode of first_season ..
    season - 1 whose outcome game precedes the season (refit once per season)."""
    return fit_return_rates(sources, through_key=key(season, 1), seasons=tuple(range(first_season, season)))


def team_game_index(team_weeks: list[int], cutoff: int, target: int) -> int | None:
    """h for target week T: the number of the team's scheduled games in (W, T]; None when T is his bye."""
    if target not in team_weeks:
        return None
    return sum(1 for w in team_weeks if cutoff < w <= target)


def replacement_p(rates: dict, position: str, missed_team_games: int | None, h: int) -> float:
    if not rates:
        raise ValueError("no fitted return-rate table: keep the current p_play instead of guessing")
    return float(_return_rate(rates, {"position": position, "missed_team_games": missed_team_games},
                              min(h, TABLE_MAX_H)))


def ros_p_play(current: float | None, *, designation: str | None, position: str, missed_team_games: int | None,
               h: int, rates: dict) -> tuple[float | None, str]:
    """What a ROS row would carry under the candidate: the table for a player Out/Doubtful at the cutoff, the
    current p_play otherwise. Returns (p_play, source)."""
    if h >= 1 and (designation or "").strip().lower() in UNAVAILABLE_DESIGNATIONS and position in SKILL:
        return replacement_p(rates, position, missed_team_games, h), "return_rate_table"
    return current, "participation_undesignated"


# ----------------------------------------------------------------------------- evaluation rows


COHORT_SCHEMA = {"season": pl.Int64, "cutoff_week": pl.Int64, "key": pl.Int64, "gsis_id": pl.String,
                 "position": pl.String, "team": pl.String, "designation": pl.String, "missed_team_games": pl.Int64,
                 "missed_bucket": pl.String, "h": pl.Int64, "target_week": pl.Int64, "p_current": pl.Float64,
                 "p_candidate": pl.Float64, "appeared": pl.Int64, "points": pl.Float64}


def cohort(sources: Sources, season: int, current_p: Callable[[int, int], dict[str, float]], rates: dict, *,
           cutoffs=CUTOFF_WEEKS, horizons=GATED_HORIZONS + DESCRIPTIVE_HORIZONS) -> pl.DataFrame:
    """One row per (player Out/Doubtful at cutoff W, h) whose team's h-th game after W is final.
    `current_p(W, T)` -> {gsis: p_play} is the current ROS path for target week T at cutoff W; a player it does
    not cover (not in its population) is skipped for that T, so both paths always score identical rows."""
    games = sources.games.filter(pl.col("season") == season)
    sched: dict[str, list[int]] = {}
    final: set[tuple[str, int]] = set()
    for t, w, pf in games.select("team", "week", "points_for").sort("week").iter_rows():
        sched.setdefault(t, []).append(int(w))
        if pf is not None:
            final.add((t, int(w)))
    pan = sources.panel.filter(pl.col("season") == season)
    pts = {(int(w), g): float(p or 0.0) for w, g, p in pan.select("week", "gsis_id", "league_points").iter_rows()}
    rows = []
    for w in cutoffs:
        tl = injury_timeline(sources, season, w)
        hurt = tl.filter(pl.col("designation").is_in(list(UNAVAILABLE_DESIGNATIONS)) & pl.col("position").is_in(list(SKILL)))
        cur: dict[int, dict[str, float]] = {}
        for r in hurt.iter_rows(named=True):
            later = [x for x in sched.get(r["team"], []) if x > w]
            for h in horizons:
                if len(later) < h or (r["team"], later[h - 1]) not in final:
                    break
                t = later[h - 1]
                if t not in cur:
                    cur[t] = current_p(w, t)
                pc = cur[t].get(r["gsis_id"])
                if pc is None:
                    continue
                hit = (t, r["gsis_id"]) in pts
                rows.append({"season": season, "cutoff_week": w, "key": key(season, w), "gsis_id": r["gsis_id"],
                             "position": r["position"], "team": r["team"], "designation": r["designation"],
                             "missed_team_games": r["missed_team_games"],
                             "missed_bucket": missed_bucket(r["missed_team_games"]), "h": h, "target_week": t,
                             "p_current": float(pc),
                             "p_candidate": replacement_p(rates, r["position"], r["missed_team_games"], h),
                             "appeared": int(hit), "points": pts.get((t, r["gsis_id"]), 0.0)})
    return pl.DataFrame(rows, schema=COHORT_SCHEMA, orient="row") if rows else pl.DataFrame(schema=COHORT_SCHEMA)


# ----------------------------------------------------------------------------- metrics


def calibration(y: np.ndarray, p: np.ndarray) -> dict:
    if len(y) == 0:
        return {"n": 0}
    rel = reliability(y.astype(float), p.astype(float), bins=BINS, min_n=BIN_MIN_N)
    return {"n": int(len(y)), "observed": round(float(y.mean()), 4), "mean_p": round(float(p.mean()), 4),
            "brier": round(brier(y, p), 4), "log_loss": round(log_loss(y, p), 4), "ece": rel["ece"],
            "max_bin_gap": rel["max_gap_bins_n_ge_min"], "bins": [b for b in rel["bins"] if b["n"]]}


def cluster_diff(df: pl.DataFrame, a: str, b: str, target: str, *, loss: str = "squared",
                 resamples: int = BOOTSTRAP_RESAMPLES, seed: int = SEED) -> dict:
    """mean loss(a) - mean loss(b) against `target`, with a 95% CI resampling whole cutoff weeks (`key`)."""
    if df.height == 0:
        return {"diff": None, "ci95": None, "n": 0}
    f = (lambda e: e ** 2) if loss == "squared" else (lambda e: e.abs())
    d = df.with_columns(f(pl.col(target) - pl.col(a)).alias("_la"), f(pl.col(target) - pl.col(b)).alias("_lb"))
    by = d.group_by("key").agg(pl.col("_la").sum(), pl.col("_lb").sum(), pl.len().alias("n")).sort("key")
    la, lb, n = by["_la"].to_numpy(), by["_lb"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (la[idx].sum(1) - lb[idx].sum(1)) / n[idx].sum(1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": round(float((la.sum() - lb.sum()) / n.sum()), 4), "ci95": [round(float(lo), 4), round(float(hi), 4)],
            "n": int(n.sum())}


def _pair(g: pl.DataFrame) -> dict:
    y = g["appeared"].to_numpy()
    return {"current": calibration(y, g["p_current"].to_numpy()), "candidate": calibration(y, g["p_candidate"].to_numpy())}


def horizon_report(df: pl.DataFrame, h: int) -> dict:
    g = df.filter(pl.col("h") == h)
    out = {"h": h, **_pair(g), "brier_diff": cluster_diff(g, "p_candidate", "p_current", "appeared"),
           "by_season": {}, "subgroups": {}}
    for (s,), sg in g.group_by("season", maintain_order=True):
        out["by_season"][int(s)] = _pair(sg)
    for col in ("position", "missed_bucket", "designation"):
        for (v,), sg in g.group_by(col, maintain_order=True):
            if sg.height >= SUBGROUP_MIN_N:
                out["subgroups"][f"{col}:{v}"] = _pair(sg)
    return out


def ros_totals(df: pl.DataFrame) -> pl.DataFrame:
    """Per episode (season, cutoff, player): expected-points totals over the team's next ROS_TOTAL_GAMES games.
    Needs a `conditional` column (points if he appears); episodes missing any game or conditional are dropped."""
    g = df.filter(pl.col("h") <= ROS_TOTAL_GAMES)
    return (g.group_by("season", "cutoff_week", "key", "gsis_id", "position", "designation", "missed_bucket")
            .agg(pl.len().alias("_n"), pl.col("conditional").is_null().sum().alias("_miss"),
                 (pl.col("p_current") * pl.col("conditional")).sum().alias("ros_current"),
                 (pl.col("p_candidate") * pl.col("conditional")).sum().alias("ros_candidate"),
                 pl.col("points").sum().alias("ros_actual"))
            .filter((pl.col("_n") == ROS_TOTAL_GAMES) & (pl.col("_miss") == 0)).drop("_n", "_miss").sort("key", "gsis_id"))


def totals_report(t: pl.DataFrame) -> dict:
    if t.height == 0:
        return {"n": 0}

    def m(col):
        e = (t["ros_actual"] - t[col]).to_numpy()
        return {"mae": round(float(np.abs(e).mean()), 3), "rmse": round(float(np.sqrt((e ** 2).mean())), 3),
                "bias": round(float(e.mean()), 3), "mean_total": round(float(t[col].mean()), 3)}

    return {"n": t.height, "actual_mean_total": round(float(t["ros_actual"].mean()), 3), "current": m("ros_current"),
            "candidate": m("ros_candidate"),
            "mae_diff": cluster_diff(t, "ros_candidate", "ros_current", "ros_actual", loss="absolute"),
            "mse_diff": cluster_diff(t, "ros_candidate", "ros_current", "ros_actual")}


# ----------------------------------------------------------------------------- verdict


def gates(horizons: dict[int, dict], totals: dict) -> dict:
    """Apply DECISION_RULES to the confirmation reports (horizon_report per gated h, totals_report)."""
    lim = GATE_LIMITS
    res: dict[str, dict] = {}
    for h in GATED_HORIZONS:
        r = horizons[h]
        cur, cand, bd = r["current"], r["candidate"], r["brier_diff"]
        sub_bad = [k for k, v in r["subgroups"].items()
                   if v["candidate"]["brier"] > v["current"]["brier"] + lim["c6_tolerance"]]
        res[f"h{h}"] = {
            "c1_brier": bool(bd["diff"] is not None and bd["diff"] < 0 and bd["ci95"][1] < 0),
            "c2_ece": bool(cand["ece"] <= lim["c2_ece"]),
            "c3_max_bin_gap": bool(cand["max_bin_gap"] <= lim["c3_max_bin_gap"]),
            "c4_ece_not_worse": bool(cand["ece"] < cur["ece"]),
            "c5_each_season": bool(all(s in r["by_season"] and r["by_season"][s]["candidate"]["brier"]
                                       < r["by_season"][s]["current"]["brier"] for s in TEST_SEASONS)),
            "c6_subgroups": not sub_bad,
        }
        if sub_bad:
            res[f"h{h}"]["c6_failing_subgroups"] = sub_bad
    md = totals.get("mae_diff") or {}
    res["ros_totals"] = {
        "r1_mae": bool(md.get("diff") is not None and md["diff"] < 0 and md["ci95"][1] < 0),
        "r2_bias": bool(totals.get("n") and abs(totals["candidate"]["bias"]) <= abs(totals["current"]["bias"])),
    }
    failed = [f"{grp}.{c}" for grp, cs in res.items() for c, ok in cs.items() if isinstance(ok, bool) and not ok]
    return {"gates": res, "failed": failed, "rules_sha": rules_sha(),
            "outcome": "propose_for_owner_review" if not failed else "retain_current"}


__all__ = ["VERSION", "DECISION_RULES", "GATE_LIMITS", "rules_sha", "table_for_season", "team_game_index",
           "replacement_p", "ros_p_play", "cohort", "calibration", "cluster_diff", "horizon_report", "ros_totals",
           "totals_report", "gates"]

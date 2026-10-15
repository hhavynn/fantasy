"""Chronological (walk-forward) validation of the independent projection models.

Protocol (docs/independent_projection_engine.md, "Validation protocol"):
  1. Feature store: for every completed NFL week, point-in-time features for each player who
     appeared, built only from earlier weeks (forecast_features), plus the realized league
     points as the label.
  2. Walk-forward: for test week (S, W) every model is refit on store rows with (season, week)
     < (S, W) - expanding window, never a random row split - then predicts week W.
  3. Selection (`select`): ridge penalties, the primary model per position (B or C) and the
     prediction-interval bands are chosen on VALIDATION seasons only.
  4. Held-out (`evaluate`): the frozen selection is applied to later seasons that played no
     part in selection; PROMOTION_CRITERIA (fixed before the held-out run) decide the status.
  5. ESPN is compared only on matched player-weeks with a pregame snapshot captured strictly
     before the player's kickoff (same rule as projection_backtest). No match, no claim.

Everything returned is aggregate (no player-level rows), so reports are safe to print.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
import math

import numpy as np
import polars as pl

from .forecast_features import POSITIONS, Sources, features_for_week, key, played_targets
from .independent_projections import (LAMBDA_GRID, MIN_TRAIN_ROWS, MODEL_NAMES, MODEL_VERSION, RidgeModel,
                                      baseline_predictions, fit_ridge)

PROMOTION_CRITERIA = {
    "frozen_before_heldout": True,
    "heldout_min_observations_per_position": 400,
    "min_relative_mae_improvement_vs_best_trailing_baseline": 0.03,
    "require_bootstrap_ci_upper_below_zero": True,     # 95% CI of MAE(primary) - MAE(best baseline) < 0
    "bootstrap_resamples": 1000,
    "bootstrap_unit": "NFL week (all player-weeks of a week resampled together)",
    "interval_nominal_coverage": 0.80,
    "interval_coverage_tolerance": 0.05,
    "espn_min_matched_per_position": 300,
    "espn_rule": "Start/Sit readiness additionally requires primary MAE <= ESPN MAE on >= espn_min_matched "
                 "matched pregame observations per position; otherwise start_sit_ready is false.",
}
PAIR_POOL = {"QB": 24, "RB": 48, "WR": 60, "TE": 24}   # pregame relevance pool per position-week
CLOSE_PAIR_POINTS = 3.0


@dataclass(frozen=True)
class Protocol:
    first_feature_season: int = 2019          # warm-up only: provides features, never a label
    train_start_season: int = 2020
    validation_seasons: tuple[int, ...] = (2023, 2024)
    heldout_seasons: tuple[int, ...] = (2025, 2026)
    lambdas: tuple[float, ...] = LAMBDA_GRID
    seed: int = 20261014

    def check(self) -> None:
        if not (self.first_feature_season < self.train_start_season
                <= min(self.validation_seasons) <= max(self.validation_seasons) < min(self.heldout_seasons)):
            raise ValueError("protocol seasons must be ordered: warm-up < train <= validation < held-out")
        if set(self.validation_seasons) & set(self.heldout_seasons):
            raise ValueError("validation and held-out seasons overlap")


# ----------------------------------------------------------------------------- store


def completed_weeks(sources: Sources, seasons: list[int]) -> list[tuple[int, int]]:
    rows = sources.panel.filter(pl.col("season").is_in(seasons)).select("season", "week").unique()
    return sorted((int(s), int(w)) for s, w in rows.iter_rows())


def build_store(sources: Sources, weeks: list[tuple[int, int]]) -> pl.DataFrame:
    """Features (from strictly earlier weeks) + label for every appearance in `weeks`."""
    frames = []
    for season, week in weeks:
        targets = played_targets(sources, season, week)
        if targets.height == 0:
            continue
        feats = features_for_week(sources, season, week, targets.drop("league_points"))
        frames.append(feats.join(targets.select("gsis_id", pl.col("league_points").alias("label")),
                                 on="gsis_id", how="left"))
    if not frames:
        return pl.DataFrame()
    store = baseline_predictions(pl.concat(frames, how="diagonal_relaxed"))
    return store.with_columns((pl.col("season") * 100 + pl.col("week")).alias("key"))


# ----------------------------------------------------------------------------- walk-forward


def _train_rows(store: pl.DataFrame, k: int, position: str, train_start: int) -> pl.DataFrame:
    return store.filter((pl.col("key") < k) & (pl.col("season") >= train_start) & (pl.col("position") == position)
                        & (pl.col("games_recent") > 0) & pl.col("label").is_not_null())


def walk_forward(store: pl.DataFrame, test_weeks: list[tuple[int, int]], penalties: dict[str, dict[str, list[float]]],
                 train_start: int) -> pl.DataFrame:
    """Predictions for every store row in `test_weeks`. `penalties[pos][model]` lists the ridge
    penalties to evaluate; columns pred_<model>_<lam>. Training rows are strictly earlier weeks."""
    out = []
    for season, week in test_weeks:
        k = key(season, week)
        test = store.filter(pl.col("key") == k)
        for pos in POSITIONS:
            rows = test.filter(pl.col("position") == pos)
            if rows.height == 0:
                continue
            train = _train_rows(store, k, pos, train_start)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            cols = {}
            for model, lams in penalties.get(pos, {}).items():
                fitted = fit_ridge(train, pos, model, lams) if train.height >= MIN_TRAIN_ROWS else {}
                for lam in lams:
                    name = f"pred_{model}_{lam:g}"
                    if lam in fitted:
                        p = fitted[lam].predict(rows)
                        cols[name] = pl.Series(name, np.where(rows["games_recent"].to_numpy() > 0, p, np.nan))
                    else:
                        cols[name] = pl.Series(name, [None] * rows.height, dtype=pl.Float64)
            out.append(rows.with_columns([s.fill_nan(None) for s in cols.values()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- metrics


def metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    ok = ~(np.isnan(actual) | np.isnan(pred))
    a, p = actual[ok], pred[ok]
    if len(a) == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    err = a - p
    return {"n": int(len(a)), "mae": round(float(np.abs(err).mean()), 3),
            "rmse": round(float(math.sqrt((err ** 2).mean())), 3), "bias": round(float(err.mean()), 3)}


def _arr(df: pl.DataFrame, col: str) -> np.ndarray:
    return df[col].cast(pl.Float64).fill_null(np.nan).to_numpy()


def week_bootstrap_mae_diff(df: pl.DataFrame, a: str, b: str, resamples: int, seed: int) -> dict:
    """95% CI of MAE(a) - MAE(b) resampling whole weeks (player-weeks within a week are correlated)."""
    d = df.filter(pl.col(a).is_not_null() & pl.col(b).is_not_null())
    if d.height == 0:
        return {"diff": None, "ci95": None}
    d = d.with_columns((pl.col("label") - pl.col(a)).abs().alias("_ea"), (pl.col("label") - pl.col(b)).abs().alias("_eb"))
    by = d.group_by("key").agg(pl.col("_ea").sum(), pl.col("_eb").sum(), pl.len().alias("n")).sort("key")
    ea, eb, n = by["_ea"].to_numpy(), by["_eb"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (ea[idx].sum(1) - eb[idx].sum(1)) / n[idx].sum(1)
    point = (ea.sum() - eb.sum()) / n.sum()
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": round(float(point), 3), "ci95": [round(float(lo), 3), round(float(hi), 3)]}


def pair_decisions(df: pl.DataFrame, models: list[str], pool: dict[str, int] = PAIR_POOL,
                   restrict: set[tuple] | None = None) -> dict:
    """Same-position, same-week player pairs among the pregame relevance pool (top-N by the
    recent-average baseline, a pregame quantity identical for every model). For each model,
    start the higher projection; correct if that player outscored the other. Ties in actual
    points are skipped; ties in projection count half. Regret = points lost vs. the better
    player. `restrict` limits pairs to {(season, week, gsis_a, gsis_b)} (sorted ids)."""
    res = {m: {"pairs": 0, "correct": 0.0, "regret": 0.0} for m in models}
    close = {m: {"pairs": 0, "correct": 0.0, "regret": 0.0} for m in models}
    ties_skipped = 0
    for (season, week, pos), g in df.group_by(["season", "week", "position"], maintain_order=True):
        g = g.filter(pl.all_horizontal([pl.col(m).is_not_null() for m in models]))
        g = g.sort("pred_A1", descending=True).head(pool.get(pos, 0))
        ids, act = g["gsis_id"].to_list(), g["label"].to_numpy()
        preds = {m: g[m].to_numpy() for m in models}
        base = g["pred_A1"].to_numpy()
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if restrict is not None and (season, week, *sorted((ids[i], ids[j]))) not in restrict:
                    continue
                if act[i] == act[j]:
                    ties_skipped += 1
                    continue
                best = max(act[i], act[j])
                is_close = abs(base[i] - base[j]) <= CLOSE_PAIR_POINTS
                for m in models:
                    p = preds[m]
                    if p[i] == p[j]:
                        score, regret = 0.5, abs(act[i] - act[j]) / 2
                    else:
                        pick = i if p[i] > p[j] else j
                        score, regret = float(act[pick] == best), best - act[pick]
                    for bucket in ([res] + ([close] if is_close else [])):
                        bucket[m]["pairs"] += 1
                        bucket[m]["correct"] += score
                        bucket[m]["regret"] += regret

    def fmt(b):
        return {m: {"pairs": v["pairs"], "accuracy": round(v["correct"] / v["pairs"], 4) if v["pairs"] else None,
                    "mean_regret": round(v["regret"] / v["pairs"], 3) if v["pairs"] else None} for m, v in b.items()}
    return {"all": fmt(res), "close_calls": fmt(close), "actual_ties_skipped": ties_skipped,
            "pool": pool, "close_call_definition": f"recent-average baselines within {CLOSE_PAIR_POINTS:g} points"}


# ----------------------------------------------------------------------------- selection


def select(store: pl.DataFrame, protocol: Protocol) -> dict:
    """Choose penalties, the primary model and interval bands on validation seasons only."""
    protocol.check()
    weeks = sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season").is_in(protocol.validation_seasons))
                    .select("season", "week").unique().iter_rows()})
    grid = {p: {"B": list(protocol.lambdas), "C": list(protocol.lambdas)} for p in POSITIONS}
    preds = walk_forward(store, weeks, grid, protocol.train_start_season)
    chosen, table = {}, {}
    for pos in POSITIONS:
        rows = preds.filter(pl.col("position") == pos)
        common = rows.filter(pl.all_horizontal(
            [pl.col(f"pred_{m}_{lam:g}").is_not_null() for m in ("B", "C") for lam in protocol.lambdas]))
        a = _arr(common, "label")
        scores = {(m, lam): metrics(a, _arr(common, f"pred_{m}_{lam:g}"))["mae"]
                  for m in ("B", "C") for lam in protocol.lambdas}
        table[pos] = {f"{m}@{lam:g}": v for (m, lam), v in scores.items()}
        best = {m: min(protocol.lambdas, key=lambda lam: (scores[(m, lam)], -lam)) for m in ("B", "C")}
        # Pre-registered rule: primary = C unless B is at least as accurate on validation.
        primary = "C" if scores[("C", best["C"])] < scores[("B", best["B"])] else "B"
        col = f"pred_{primary}_{best[primary]:g}"
        chosen[pos] = {"lambda": {m: best[m] for m in ("B", "C")}, "primary": primary,
                       "validation_mae": {m: scores[(m, best[m])] for m in ("B", "C")},
                       "validation_n": common.height,
                       "intervals": interval_bands(common, col)}
    return {"model_version": MODEL_VERSION, "protocol": asdict(protocol), "validation_weeks": len(weeks),
            "positions": chosen, "grid_mae": table, "promotion_criteria": PROMOTION_CRITERIA}


def interval_bands(df: pl.DataFrame, col: str, nominal: float = 0.80) -> dict:
    """Residual (actual - prediction) quantiles by prediction tercile on validation rows."""
    d = df.filter(pl.col(col).is_not_null()).select(pl.col(col).alias("p"), "label")
    if d.height < 300:
        return {}
    cut1, cut2 = d["p"].quantile(1 / 3), d["p"].quantile(2 / 3)
    lo_q, hi_q = (1 - nominal) / 2, 1 - (1 - nominal) / 2
    bands = []
    for low, high in ((None, cut1), (cut1, cut2), (cut2, None)):
        b = d.filter((pl.lit(True) if low is None else pl.col("p") >= low) & (pl.lit(True) if high is None else pl.col("p") < high))
        r = (b["label"] - b["p"])
        bands.append({"low": None if low is None else round(float(low), 3),
                      "high": None if high is None else round(float(high), 3),
                      "q_low": round(float(r.quantile(lo_q)), 3), "q_high": round(float(r.quantile(hi_q)), 3),
                      "n": b.height})
    return {"nominal": nominal, "bands": bands, "source": "validation residuals"}


# ----------------------------------------------------------------------------- held-out


def frozen_penalties(selection: dict) -> dict:
    return {p: {m: [s["lambda"][m]] for m in ("B", "C")} for p, s in selection["positions"].items()}


def evaluate(store: pl.DataFrame, selection: dict, protocol: Protocol, espn: pl.DataFrame | None = None,
             roster_pairs: set[tuple] | None = None) -> dict:
    """Apply the frozen selection to held-out seasons and judge it against PROMOTION_CRITERIA."""
    from .independent_projections import interval_for

    protocol.check()
    sel_protocol = selection["protocol"]
    if tuple(sel_protocol["validation_seasons"]) != tuple(protocol.validation_seasons):
        raise ValueError("selection was made on different validation seasons")
    weeks = sorted({(int(s), int(w)) for s, w in store.filter(pl.col("season").is_in(protocol.heldout_seasons))
                    .select("season", "week").unique().iter_rows()})
    population = store.filter(pl.col("season").is_in(protocol.heldout_seasons))
    preds = walk_forward(store, weeks, frozen_penalties(selection), protocol.train_start_season)
    if preds.height == 0:
        return {"status": "insufficient_data", "reason": "no held-out weeks in the store", "positions": {}}
    renames = {}
    for pos, s in selection["positions"].items():
        for m in ("B", "C"):
            renames[(pos, m)] = f"pred_{m}_{s['lambda'][m]:g}"
    cols = []
    for m in ("B", "C"):
        expr = None
        for pos in POSITIONS:
            c = renames[(pos, m)]
            e = pl.when(pl.col("position") == pos).then(pl.col(c) if c in preds.columns else None)
            expr = e if expr is None else expr.when(pl.col("position") == pos).then(pl.col(c) if c in preds.columns else None)
        cols.append(expr.otherwise(None).alias(f"pred_{m}"))
    preds = preds.with_columns(cols)
    preds = preds.with_columns(pl.when(pl.col("position").is_in([p for p, s in selection["positions"].items()
                                                                  if s["primary"] == "C"]))
                               .then(pl.col("pred_C")).otherwise(pl.col("pred_B")).alias("pred_primary"))
    models = ["pred_A1", "pred_A2", "pred_B", "pred_C", "pred_primary"]
    rng_seed = protocol.seed
    out_pos, statuses = {}, {}
    for pos in POSITIONS:
        rows = preds.filter(pl.col("position") == pos)
        pop = population.filter(pl.col("position") == pos).height
        common = rows.filter(pl.all_horizontal([pl.col(m).is_not_null() for m in models]))
        a = _arr(common, "label")
        m_all = {MODEL_NAMES.get(m[5:], m[5:]): metrics(a, _arr(common, m)) for m in models}
        coverage = {MODEL_NAMES.get(m[5:], m[5:]): round(rows[m].is_not_null().sum() / pop, 4) if pop else None
                    for m in models}
        best_base = min(("pred_A1", "pred_A2"), key=lambda c: metrics(a, _arr(common, c))["mae"] or 1e9)
        boot = week_bootstrap_mae_diff(common, "pred_primary", best_base, PROMOTION_CRITERIA["bootstrap_resamples"],
                                       rng_seed)
        base_mae = metrics(a, _arr(common, best_base))["mae"]
        prim_mae = m_all["primary"]["mae"]
        rel = (base_mae - prim_mae) / base_mae if base_mae else None
        by_season = {int(s): {MODEL_NAMES.get(m[5:], m[5:]): metrics(_arr(g, "label"), _arr(g, m)) for m in models}
                     for (s,), g in common.group_by(["season"], maintain_order=True)}
        high = common.filter(pl.col("pred_primary") >= 10)
        low_vol = common.filter((pl.col("targets_pg") + pl.col("carries_pg")) < 5)
        cohort = {
            "primary_projection_ge_10": {MODEL_NAMES.get(m[5:], m[5:]): metrics(_arr(high, "label"), _arr(high, m))
                                         for m in models},
            "low_volume_lt5_opportunities_pg": {MODEL_NAMES.get(m[5:], m[5:]): metrics(_arr(low_vol, "label"), _arr(low_vol, m))
                                                for m in models},
            "low_sample_lt3_recent_games": {
                MODEL_NAMES.get(m[5:], m[5:]): metrics(_arr(common.filter(pl.col("games_recent") < 3), "label"),
                                                        _arr(common.filter(pl.col("games_recent") < 3), m)) for m in models},
            "team_changed": {MODEL_NAMES.get(m[5:], m[5:]): metrics(_arr(common.filter(pl.col("team_changed") == 1), "label"),
                                                                    _arr(common.filter(pl.col("team_changed") == 1), m))
                             for m in models},
        }
        ints = selection["positions"][pos]["intervals"]
        cover = None
        if ints:
            hits = 0
            for p_, y in zip(common["pred_primary"].to_list(), common["label"].to_list()):
                lo, hi = interval_for({pos: ints}, pos, p_)
                hits += lo <= y <= hi
            cover = round(hits / common.height, 4) if common.height else None
        out_pos[pos] = {"population_player_weeks": pop, "common_support_n": common.height,
                        "abstained_no_history": pop - rows["pred_A1"].is_not_null().sum(),
                        "coverage": coverage, "metrics": m_all, "by_season": by_season, "cohorts": cohort,
                        "best_trailing_baseline": MODEL_NAMES[best_base[5:]],
                        "primary_model": MODEL_NAMES[selection["positions"][pos]["primary"]],
                        "primary_minus_best_baseline_mae": boot, "relative_mae_improvement": None if rel is None else round(rel, 4),
                        "interval80_empirical_coverage": cover}
        statuses[pos] = status_for(out_pos[pos])
    pairs = pair_decisions(preds.filter(pl.all_horizontal([pl.col(m).is_not_null() for m in models])), models)
    report = {"model_version": MODEL_VERSION, "heldout_seasons": list(protocol.heldout_seasons),
              "heldout_weeks": len(weeks), "positions": out_pos, "status": statuses, "pair_decisions": pairs,
              "promotion_criteria": PROMOTION_CRITERIA}
    if roster_pairs is not None:
        report["league_roster_pairs"] = pair_decisions(
            preds.filter(pl.all_horizontal([pl.col(m).is_not_null() for m in models])), models,
            pool={p: 10_000 for p in POSITIONS}, restrict=roster_pairs)
    report["espn_comparison"] = espn_comparison(preds, espn)
    for pos, st in statuses.items():
        e = report["espn_comparison"]["by_position"].get(pos, {})
        st["start_sit_ready"] = bool(st["status"] == "validated" and e.get("criterion_met"))
        if not st["start_sit_ready"]:
            st["reasons"].append("ESPN comparison criterion not met: "
                                 f"{e.get('n', 0)} matched pregame observations "
                                 f"(need {PROMOTION_CRITERIA['espn_min_matched_per_position']})"
                                 if not e.get("criterion_met") else "not validated")
    return report


def status_for(pos_report: dict) -> dict:
    c = PROMOTION_CRITERIA
    reasons = []
    n = pos_report["common_support_n"]
    if n < c["heldout_min_observations_per_position"]:
        return {"status": "insufficient_data", "validated_for": None,
                "reasons": [f"{n} held-out observations < {c['heldout_min_observations_per_position']}"]}
    rel = pos_report["relative_mae_improvement"]
    ci = pos_report["primary_minus_best_baseline_mae"]["ci95"]
    ok_rel = rel is not None and rel >= c["min_relative_mae_improvement_vs_best_trailing_baseline"]
    ok_ci = ci is not None and ci[1] < 0
    if not ok_rel:
        reasons.append(f"relative MAE improvement {rel} < {c['min_relative_mae_improvement_vs_best_trailing_baseline']}")
    if not ok_ci:
        reasons.append(f"bootstrap 95% CI of MAE difference {ci} does not exclude 0")
    cover = pos_report["interval80_empirical_coverage"]
    intervals_ok = cover is not None and abs(cover - c["interval_nominal_coverage"]) <= c["interval_coverage_tolerance"]
    if ok_rel and ok_ci:
        return {"status": "validated", "intervals_published": intervals_ok, "reasons": reasons,
                "validated_for": (f"held-out MAE vs the best trailing-average baseline "
                                  f"({pos_report['best_trailing_baseline']}), conditional on the player appearing; "
                                  "NOT validated against ESPN")}
    return {"status": "experimental", "intervals_published": intervals_ok, "validated_for": None, "reasons": reasons}


# ----------------------------------------------------------------------------- ESPN


def espn_pregame(con, season: int) -> pl.DataFrame:
    """Latest ESPN projection captured strictly before the player's kickoff, per player-week,
    for high-confidence ESPN->GSIS mappings. Uses projection_backtest's kickoff/timestamp rules."""
    from ..db.connection import fetch_dicts, table_exists
    from .projection_backtest import _kickoff, _utc

    empty = pl.DataFrame(schema={"season": pl.Int64, "week": pl.Int64, "gsis_id": pl.String, "espn": pl.Float64})
    if con is None or not all(table_exists(con, t) for t in ("espn_projection_history", "player_id_map",
                                                             "nfl_schedules", "nfl_rosters_weekly")):
        return empty
    snaps = fetch_dicts(con, """
        SELECT h.season, h.scoring_period AS week, m.gsis_id, h.projected_points, h.synced_at
        FROM espn_projection_history h JOIN player_id_map m ON m.espn_player_id = h.espn_player_id
        WHERE h.season = ? AND m.match_confidence = 'high' AND m.gsis_id IS NOT NULL
          AND h.projected_points IS NOT NULL""", [season])
    teams = {(r["week"], r["gsis_id"]): r["team"] for r in fetch_dicts(
        con, "SELECT week, gsis_id, any_value(team) AS team FROM nfl_rosters_weekly WHERE season = ? GROUP BY 1, 2",
        [season])}
    kick = {}
    for g in fetch_dicts(con, "SELECT week, gameday, gametime, home_team, away_team FROM nfl_schedules "
                              "WHERE season = ? AND game_type = 'REG'", [season]):
        for t in (g["home_team"], g["away_team"]):
            kick[(g["week"], t)] = _kickoff(g)
    best: dict[tuple, tuple] = {}
    for r in snaps:
        team = teams.get((r["week"], r["gsis_id"]))
        ko = kick.get((r["week"], team))
        cap = _utc(r["synced_at"])
        if ko is None or cap is None or cap >= ko:
            continue
        k = (r["season"], r["week"], r["gsis_id"])
        if k not in best or cap > best[k][0]:
            best[k] = (cap, float(r["projected_points"]))
    if not best:
        return empty
    return pl.DataFrame({"season": [k[0] for k in best], "week": [k[1] for k in best],
                         "gsis_id": [k[2] for k in best], "espn": [v[1] for v in best.values()]},
                        schema=empty.schema)


def espn_comparison(preds: pl.DataFrame, espn: pl.DataFrame | None) -> dict:
    need = PROMOTION_CRITERIA["espn_min_matched_per_position"]
    if espn is None or espn.height == 0:
        return {"matched_observations": 0, "by_position": {p: {"n": 0, "criterion_met": False} for p in POSITIONS},
                "note": "No pregame ESPN snapshot matches a held-out player-week; no comparison is claimed."}
    j = preds.join(espn, on=["season", "week", "gsis_id"], how="inner").filter(pl.col("pred_primary").is_not_null())
    by = {}
    for pos in POSITIONS:
        g = j.filter(pl.col("position") == pos)
        a = _arr(g, "label")
        mp, me = metrics(a, _arr(g, "pred_primary")), metrics(a, _arr(g, "espn"))
        met = g.height >= need and mp["mae"] is not None and mp["mae"] <= me["mae"]
        by[pos] = {"n": g.height, "primary": mp, "espn": me, "criterion_met": bool(met)}
    return {"matched_observations": j.height, "by_position": by,
            "note": "Only player-weeks with an ESPN snapshot captured before kickoff and a completed actual."}


# ----------------------------------------------------------------------------- league rosters


def league_roster_pairs(con, season: int) -> set[tuple]:
    """Same-position player pairs that sat on the same fantasy roster in the same scoring period
    (contemporaneous lineup snapshots only; nothing is reconstructed)."""
    from ..db.connection import fetch_dicts, table_exists

    if con is None or not table_exists(con, "lineup_snapshots"):
        return set()
    rows = fetch_dicts(con, """
        SELECT s.season, s.scoring_period AS week, s.team_id, m.gsis_id, m.espn_position AS position
        FROM lineup_snapshots s JOIN player_id_map m ON m.espn_player_id = s.espn_player_id
        WHERE s.season = ? AND m.match_confidence = 'high' AND m.gsis_id IS NOT NULL
          AND m.espn_position IN ('QB', 'RB', 'WR', 'TE')""", [season])
    groups = defaultdict(list)
    for r in rows:
        groups[(r["season"], r["week"], r["team_id"], r["position"])].append(r["gsis_id"])
    out = set()
    for (s, w, _, _), ids in groups.items():
        ids = sorted(set(ids))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                out.add((s, w, ids[i], ids[j]))
    return out

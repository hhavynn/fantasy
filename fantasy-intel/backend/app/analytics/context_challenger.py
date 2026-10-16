"""Research only: QB- and teammate-context challenger (ifp-ctx) on top of the frozen ifp-2 spec.

Nothing in the app imports this module (tests/test_roster_context.py). ifp-1.0.0, ifp-2.0.0, the
model registry, production runs, ledgers, Start/Sit, trades and waivers are untouched.
See docs/research/qb_teammate_context.md.

Every candidate is ifp-2's own position spec (features, frozen penalty, 36-week recency weights)
plus ONE feature group from app.analytics.roster_context, fitted walk-forward by the same
challenger.fit_linear on the same training rows (challenger.train_rows: strictly earlier weeks,
labeled, >= 1 recent game). No penalty or hyperparameter is searched, so the only choice made on
data is which group (if any) a position adopts, and that choice uses the development window only.
Any effect on points is a fitted coefficient; nothing is a hand-set adjustment.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from .challenger import ChallengerConfig, fit_linear, train_rows
from .forecast_features import POSITIONS, key
from .independent_projections import MIN_TRAIN_ROWS, RidgeModel
from .roster_context import CONTEXT_DOCS, UNAVAILABLE

CTX_VERSION = "ifp-ctx-0.1.0"
CTX_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}

# Feature groups per position (fixed before any backtest). Few features on purpose.
GROUPS: dict[str, dict[str, list[str]]] = {
    "qb": {"QB": ["qb_is_expected", "qb_self_epa"],
           "RB": ["qb_epa_delta", "qb_cpoe_delta", "qb_changed"],
           "WR": ["qb_epa_delta", "qb_cpoe_delta", "qb_changed"],
           "TE": ["qb_epa_delta", "qb_cpoe_delta", "qb_changed"]},
    "tm": {"QB": ["tm_tgt_vacated", "tm_tgt_returning"],
           "RB": ["tm_car_gain_pg", "tm_tgt_gain_pg", "tm_car_vacated", "tm_car_returning"],
           "WR": ["tm_tgt_gain_pg", "tm_tgt_vacated", "tm_tgt_returning"],
           "TE": ["tm_tgt_gain_pg", "tm_tgt_vacated", "tm_tgt_returning"]},
    "env": {"QB": ["team_proe", "opp_pressure"],
            "RB": ["team_proe", "opp_pos_tgt_share"],
            "WR": ["team_proe", "opp_pressure", "opp_pos_tgt_share"],
            "TE": ["team_proe", "opp_pressure", "opp_pos_tgt_share"]},
}
CANDIDATES = {"A": ("qb",), "B": ("tm",), "C": ("qb", "tm"), "D": ("env",)}
GROUP_LABELS = {"qb": "QB context", "tm": "Teammate context", "env": "Offense/defense environment"}

for _g in GROUPS.values():
    for _fs in _g.values():
        assert all(f in CONTEXT_DOCS for f in _fs), _fs

# Pre-registered (committed with the code, before the first development run).
DECISION_RULES = {
    "selection": "development 2021-2022 only, per position: a candidate (A, B, C, D) is eligible if MAE(candidate) - "
                 "MAE(ifp2) <= -0.010 with a week-clustered bootstrap 95% CI upper bound < 0 and |bias| not worse by "
                 "more than 0.15; the eligible candidate with the lowest development MAE is adopted for that position, "
                 "otherwise the position keeps ifp2. The composite of the per-position choices is 'ctx'.",
    "confirmation": "2023-2024, run once, frozen composite vs ifp2 on identical rows, adopted positions only: (1) pooled "
                    "MAE diff < 0 with CI upper < 0; (2) no adopted position significantly worse (CI lower > 0); "
                    "(3) pooled RMSE not worse by > 0.5%; (4) |bias| not worse by > 0.15; (5) starters-pool MAE not "
                    "worse by > 1%; (6) Start/Sit pair accuracy not lower by > 0.5 pp; (7) mechanism check: the MAE "
                    "difference is < 0 on the cohort the adopted group targets (changed expected QB for qb; top target "
                    "competitor out or RB1 out for tm).",
    "outcome": "all pass -> 'prospective_shadow' (a recommendation to record frozen pregame forecasts; it changes "
               "nothing by itself); any fail, or nothing adopted -> 'retain_baseline'.",
    "exploratory": "2025-2026 replay is reported only: already inspected by earlier work, and 2025-26 injury reports "
                   "carry no modification timestamp (retrospective).",
    "never": "no promotion, no registry change; historical replays never establish promotion readiness",
}
SELECTION_MAX_DIFF = -0.010
BIAS_TOLERANCE = 0.15

# Separately pre-registered PROSPECTIVE candidate, written after the historical verdict
# ('retain_baseline') and before any prospective week was forecast. Historical windows cannot confirm it;
# only frozen pregame forecasts from 2026 week 7 onward can (docs/research/qb_teammate_context.md, section 8).
PROSPECTIVE_VERSION = "ifp-ctx-p1"
PROSPECTIVE_SPEC = {"QB": "A", "RB": "C", "WR": "C", "TE": "ifp2"}
PROSPECTIVE_RULES = {
    "evidence": "write-once pregame ledger rows only (forecast before the team's kickoff), scored after the week is "
                "fully final; ifp2 and the candidate are fitted in the same run on the same rows",
    "first_week": (2026, 7),
    "gate": "per position with a candidate: >= 8 fully final prospective weeks, >= 400 matched appearances, "
            "candidate - ifp2 MAE week-clustered 95% CI upper < 0, |bias| not worse by > 0.15, starters-pool MAE "
            "not worse (challenger.SHADOW_GATE thresholds)",
    "reported_not_gating": "RMSE, the changed-QB / teammate-out / returning cohorts, QB identification accuracy",
    "never": "passing the gate is evidence for an owner review only; no automatic promotion or registry change",
}


def candidate_features(cfg: ChallengerConfig, position: str, candidate: str | None) -> list[str]:
    """ifp-2's features for the position plus the candidate's groups (None/'ifp2' = ifp-2 itself)."""
    feats = list(cfg.features(position))
    for g in CANDIDATES.get(candidate or "", ()):
        feats += [f for f in GROUPS[g][position] if f not in feats]
    return feats


def fit_candidate(train: pl.DataFrame, cfg: ChallengerConfig, position: str, candidate: str | None,
                  target_key: int) -> RidgeModel:
    spec = cfg.positions[position]
    return fit_linear(train, position, candidate_features(cfg, position, candidate), spec["lam"],
                      half_life_weeks=spec.get("half_life_weeks"), target_key=target_key,
                      name=f"ctx_{candidate or 'ifp2'}")


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                 candidates: list[str] | tuple[str, ...] = tuple(CANDIDATES),
                 per_position: dict[str, str] | None = None) -> pl.DataFrame:
    """pred_ifp2 and pred_<candidate> per store row of `weeks` (null = abstention: no recent game or
    fewer than MIN_TRAIN_ROWS training rows). `per_position` adds pred_ctx (the composite: each
    position's adopted candidate, 'ifp2' = keep). Training rows strictly earlier (asserted)."""
    out = []
    for season, week in weeks:
        k = key(season, week)
        for pos in POSITIONS:
            rows = store.filter((pl.col("key") == k) & (pl.col("position") == pos))
            if rows.height == 0:
                continue
            train = train_rows(store, k, pos)
            assert train.height == 0 or train["key"].max() < k, "training row at or after the test week"
            ok = rows["games_recent"].fill_null(0).to_numpy() > 0
            names = ["ifp2", *candidates]
            if per_position is not None:
                names.append("ctx")
            cols = {}
            fitted: dict[str, np.ndarray] = {}
            for name in names:
                cand = per_position.get(pos, "ifp2") if name == "ctx" else name
                cand = None if cand == "ifp2" else cand
                if train.height < MIN_TRAIN_ROWS:
                    cols[f"pred_{name}"] = np.full(rows.height, np.nan)
                    continue
                tag = cand or "ifp2"
                if tag not in fitted:
                    fitted[tag] = fit_candidate(train, cfg, pos, cand, k).predict(rows)
                cols[f"pred_{name}"] = np.where(ok, fitted[tag], np.nan)
            out.append(rows.select("key", "gsis_id").with_columns(
                [pl.Series(c, v).fill_nan(None) for c, v in cols.items()]))
    return pl.concat(out, how="diagonal_relaxed") if out else pl.DataFrame()


# ----------------------------------------------------------------------------- cohorts


def cohorts() -> dict[str, pl.Expr]:
    """All pregame quantities except the two `eval_*` cohorts, which use the actual week-W starter
    (a postgame fact) to show how much of a QB change was recoverable before kickoff."""
    from .forecast_backtest import starters_pool_expr

    unavailable = pl.col("prev_starter_status").is_in(list(UNAVAILABLE))
    return {
        "same_qb": pl.col("qb_changed") == 0,
        "changed_qb": pl.col("qb_changed") == 1,
        "qb_injured_or_absent": unavailable,
        "backup_qb_expected": unavailable & pl.col("expected_qb_id").is_not_null(),
        "qb_uncertain_or_unknown": pl.col("qb_confidence").is_in(["uncertain", "unknown"]),
        "top_tgt_competitor_out": pl.col("top_tgt_comp_out") == 1,
        "top_tgt_competitor_returning": pl.col("top_tgt_comp_return") == 1,
        "rb1_out_rb_rows": (pl.col("rb1_out") == 1) & (pl.col("position") == "RB"),
        "low_volume_lt5_opps": (pl.col("targets_pg") + pl.col("carries_pg")) < 5,
        "starters_pool": starters_pool_expr(),
        "team_changed": (pl.col("team_changed") == 1) | (pl.col("prev_team_changed") == 1),
        "early_or_low_sample": (pl.col("week") <= 4) | (pl.col("games_recent") < 3),
        "eval_actual_qb_differs_from_recent": pl.col("actual_qb_id").is_not_null() & pl.col("recent_qb_id").is_not_null()
                                              & (pl.col("actual_qb_id") != pl.col("recent_qb_id")),
        "eval_expected_qb_wrong": pl.col("actual_qb_id").is_not_null() & pl.col("expected_qb_id").is_not_null()
                                  & (pl.col("actual_qb_id") != pl.col("expected_qb_id")),
    }


MECHANISM_COHORTS = {"qb": ("changed_qb",), "tm": ("top_tgt_competitor_out", "rb1_out_rb_rows"), "env": ()}


# ----------------------------------------------------------------------------- explanation


def explain_change(base: RidgeModel, ctx: RidgeModel, row: pl.DataFrame, groups: tuple[str, ...]) -> dict:
    """Explanation metadata for one row (a 1-row frame with features and roster_context columns).
    Reports both projections, the context features' contributions inside the challenger (points
    relative to the challenger's training-average player, NOT a causal effect of any teammate),
    the remaining difference, and the evidence behind the context."""
    b = float(base.predict(row)[0])
    c = float(ctx.predict(row)[0])
    contrib = ctx.contributions(row)[0]
    r = row.to_dicts()[0]
    factors = []
    for g in groups:
        feats = set(GROUPS[g][ctx.position])
        idx = [i for i, col in enumerate(ctx.columns) if col.split("__")[0] in feats]
        pts = float(sum(contrib[i] for i in idx))
        factors.append({"group": g, "label": GROUP_LABELS[g], "points_vs_training_average": round(pts, 2),
                        "inputs": {ctx.columns[i]: (int(r.get(ctx.columns[i].split("__")[0]) is None)
                                                    if ctx.columns[i].endswith("__missing")
                                                    else r.get(ctx.columns[i])) for i in idx}})
    qb = {k: r.get(k) for k in ("expected_qb_id", "qb_confidence", "qb_reason", "prev_starter_status",
                                "exp_qb_dropbacks", "qb_epa_delta", "qb_changed")}
    tm = {k: r.get(k) for k in ("tm_tgt_vacated", "tm_tgt_returning", "tm_car_vacated", "tm_car_returning",
                                "top_tgt_comp_out", "top_tgt_comp_return", "rb1_out")}
    return {
        "model": CTX_VERSION, "status": CTX_STATUS,
        "baseline_points": round(b, 2), "challenger_points": round(c, 2), "difference": round(c - b, 2),
        "factors": factors, "qb_context": qb, "teammate_context": tm,
        "uncertainty": {"qb_confidence": r.get("qb_confidence"), "report_used": r.get("ctx_report_used"),
                        "qb_sample_dropbacks": r.get("exp_qb_dropbacks")},
        "caveats": ["Research model, not production. Contributions are fitted associations relative to the "
                    "training-average player, not proof that a specific teammate or QB caused a number of points.",
                    "Conditional on the player appearing; availability (p_play) is not changed by this model."],
    }


__all__ = ["CTX_VERSION", "CTX_STATUS", "GROUPS", "CANDIDATES", "DECISION_RULES", "candidate_features",
           "fit_candidate", "walk_forward", "cohorts", "explain_change"]

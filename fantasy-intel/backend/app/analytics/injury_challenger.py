"""Research only: injury-aware workload handoff challenger (ifp-iwh) on top of the frozen ifp-2 spec.

Nothing in the app imports this module (tests/test_injury_workload.py). ifp-1.0.0, ifp-2.0.0, the model
registry, production runs, ledgers, Start/Sit, trades and waivers are untouched.
See docs/research/injury_workload_handoff.md.

The candidate is ifp-2's own position spec (features, frozen penalty, 36-week recency weights) plus ONE
small feature group from app.analytics.injury_workload, fitted walk-forward by the same
challenger.fit_linear on the same training rows (challenger.train_rows). No penalty or hyperparameter is
searched; the only data-driven choice is which positions adopt the group, made on the development window.
Any effect on points is a fitted coefficient; nothing is a hand-set adjustment.

Why a mean-oriented gate. The earlier teammate study (docs/research/qb_teammate_context.md section 7) found
that redistribution features fix the under-projection of players whose teammate is out (bias +0.7 to +1.8
-> ~0) and lower RMSE, but raise MAE on those cohorts: who realizes the vacated volume is right-skewed, and
MAE rewards the median. `expected_points` is a MEAN (p_play x E[points | appears]), so the rules below gate on
squared error and bias, and only guard MAE against material deterioration. They were written before the
first development run and are not changed after it.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from .challenger import SHADOW_GATE, ChallengerConfig, fit_linear, train_rows
from .forecast_features import POSITIONS, key
from .independent_projections import MIN_TRAIN_ROWS, RidgeModel

IWH_VERSION = "ifp-iwh-0.1.1"
# Versions (docs/research/injury_workload_handoff.md, sections 7 and 10):
#   ifp-iwh-0.1.0  handoff iwh-0.1.0. The only historically adjudicated version: development 2021-22 adopted RB,
#                  WR and TE; confirmation 2023-24 -> retain_baseline (failed c7 on the returning player). Its frozen
#                  adoption (frozen_adoption_ifp-iwh-0.1.0.json) and evaluation stand as written; the code that
#                  produced them is commit d6fed66 (merge of PR #103).
#   ifp-iwh-0.1.1  handoff iwh-0.1.1 (section 7.3 fix). Same features, same adoption, judged ONLY on prospective
#                  write-once pregame ledgers from 2026 week 7 (PROSPECTIVE_RULES); no historical window is re-run.
HISTORICAL_VERSION = "ifp-iwh-0.1.0"
IWH_STATUS = "research_only"
WINDOWS = {"development": (2021, 2022), "confirmation": (2023, 2024), "exploratory": (2025, 2026)}
SEED = 20261017

# Feature group per position (fixed before any backtest). Two features on purpose:
#   ho_exp_xfpl  expected league points of opportunity in week W under the handoff (who is available, each
#                player's established healthy claim, the team's own games without the absent players)
#   ho_shift_pg  expected (carries + targets) minus the player's raw last-5 average: positive for a backup whose
#                lead is out or a starter whose window holds injury-shortened games, negative for a replacement
#                whose starter is back. ifp-2 already sees the raw last-5 usage, so this is the correction.
GROUP = {"QB": [], "RB": ["ho_exp_xfpl", "ho_shift_pg"], "WR": ["ho_exp_xfpl", "ho_shift_pg"],
         "TE": ["ho_exp_xfpl", "ho_shift_pg"]}

DECISION_RULES = {
    "development": "2021-2022 only, per position (RB, WR, TE): the position adopts the handoff group if, versus ifp2 on "
                   "identical rows, (a) MSE(candidate) - MSE(ifp2) < 0 with a week-clustered bootstrap 95% CI upper bound "
                   "< 0, (b) MAE(candidate) - MAE(ifp2) <= +0.005, and (c) |bias| not worse by more than 0.15.",
    "confirmation": "2023-2024, run once, adopted positions pooled, frozen composite vs ifp2 on identical rows: (c1) MSE "
                    "diff < 0 with CI upper < 0; (c2) MAE diff <= +0.010; (c3) no adopted position with an MSE diff CI "
                    "lower bound > 0; (c4) |bias| not worse by > 0.15 per adopted position; (c5) starters-pool MAE not "
                    "worse by > 1% per adopted position; (c6) Start/Sit pair accuracy not lower by > 0.5 pp; (c7) "
                    "mechanism: on each targeted cohort with n >= 100 (lead out -> available teammates; lead returning "
                    "-> teammates; the returning player himself) the MSE diff is < 0 and |bias| does not grow.",
    "outcome": "all pass -> 'prospective_shadow' (a recommendation to record frozen pregame forecasts; changes nothing by "
               "itself); any fail, or nothing adopted -> 'retain_baseline'.",
    "exploratory": "2025-2026 replay reported only: 2025-26 injury reports carry no modification timestamp "
                   "(retrospective) and earlier studies already inspected those weeks.",
    "never": "no promotion, no registry change, no change to ifp-2.0.0; historical replays never establish promotion "
             "readiness",
}
DEV_MAX_MAE_DIFF = 0.005
CONF_MAX_MAE_DIFF = 0.010
BIAS_TOLERANCE = 0.15
MECHANISM_MIN_N = 100


def candidate_features(cfg: ChallengerConfig, position: str, adopt: bool) -> list[str]:
    feats = list(cfg.features(position))
    if adopt:
        feats += [f for f in GROUP[position] if f not in feats]
    return feats


def fit_candidate(train: pl.DataFrame, cfg: ChallengerConfig, position: str, adopt: bool, target_key: int) -> RidgeModel:
    spec = cfg.positions[position]
    return fit_linear(train, position, candidate_features(cfg, position, adopt), spec["lam"],
                      half_life_weeks=spec.get("half_life_weeks"), target_key=target_key,
                      name="iwh" if adopt else "ifp2")


def walk_forward(store: pl.DataFrame, weeks: list[tuple[int, int]], cfg: ChallengerConfig,
                 adopted: dict[str, bool] | None = None) -> pl.DataFrame:
    """pred_ifp2 and pred_iwh per store row of `weeks` (null = abstention). `adopted` (position -> bool) makes
    pred_iwh the composite: a position that did not adopt the group keeps ifp2's prediction. Training rows are
    strictly earlier weeks (asserted)."""
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
            if train.height < MIN_TRAIN_ROWS:
                base = cand = np.full(rows.height, np.nan)
            else:
                base = fit_candidate(train, cfg, pos, False, k).predict(rows)
                use = bool(GROUP[pos]) and (adopted is None or adopted.get(pos, False))
                cand = fit_candidate(train, cfg, pos, True, k).predict(rows) if use else base
            out.append(rows.select("key", "gsis_id").with_columns(
                pl.Series("pred_ifp2", np.where(ok, base, np.nan)).fill_nan(None),
                pl.Series("pred_iwh", np.where(ok, cand, np.nan)).fill_nan(None)))
    return pl.concat(out) if out else pl.DataFrame()


# ----------------------------------------------------------------------------- metrics


def week_bootstrap_diff(df: pl.DataFrame, a: str, b: str, *, loss: str = "squared", resamples: int = 1000,
                        seed: int = SEED) -> dict:
    """95% CI of mean loss(a) - mean loss(b), resampling whole NFL weeks. loss: squared | absolute."""
    d = df.filter(pl.col(a).is_not_null() & pl.col(b).is_not_null() & pl.col("label").is_not_null())
    if d.height == 0:
        return {"diff": None, "ci95": None, "n": 0}
    f = (lambda e: e ** 2) if loss == "squared" else (lambda e: e.abs())
    d = d.with_columns(f(pl.col("label") - pl.col(a)).alias("_la"), f(pl.col("label") - pl.col(b)).alias("_lb"))
    by = d.group_by("key").agg(pl.col("_la").sum(), pl.col("_lb").sum(), pl.len().alias("n")).sort("key")
    la, lb, n = by["_la"].to_numpy(), by["_lb"].to_numpy(), by["n"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n), size=(resamples, len(n)))
    diffs = (la[idx].sum(1) - lb[idx].sum(1)) / n[idx].sum(1)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"diff": float((la.sum() - lb.sum()) / n.sum()), "ci95": [float(lo), float(hi)], "n": int(n.sum())}


def point_metrics(df: pl.DataFrame, col: str) -> dict:
    d = df.filter(pl.col(col).is_not_null() & pl.col("label").is_not_null())
    if d.height == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    e = (d["label"] - d[col]).to_numpy()
    return {"n": int(len(e)), "mae": float(np.abs(e).mean()), "rmse": float(np.sqrt((e ** 2).mean())),
            "bias": float(e.mean())}


# ----------------------------------------------------------------------------- cohorts (pregame quantities)


def cohorts() -> dict[str, pl.Expr]:
    from .forecast_backtest import starters_pool_expr

    rb, wr, te = (pl.col("position") == p for p in ("RB", "WR", "TE"))
    lead_out = (pl.col("ho_lead_out") == 1) & (pl.col("ho_is_lead") == 0)
    lead_back = (pl.col("ho_lead_returning") == 1) & (pl.col("ho_is_lead") == 0)
    self_ret = pl.col("tl_status_class") == "available_returning"
    lead = pl.col("ho_is_lead") == 1
    missed = pl.col("tl_missed_team_games").fill_null(0)
    return {
        "rb_lead_out_teammates": rb & lead_out,
        "rb_backup_takeover": rb & lead_out & (pl.col("ho_car_share") >= 0.5),
        "rb_committee_after_lead_out": rb & lead_out & (pl.col("ho_car_share") >= 0.15) & (pl.col("ho_car_share") < 0.5),
        "rb_lead_returning_1_game": rb & lead & self_ret & (missed == 1),
        "rb_lead_returning_2plus": rb & lead & self_ret & (missed >= 2),
        "returning_questionable_or_limited": self_ret & ((pl.col("tl_designation") == "questionable")
                                                         | pl.col("tl_practice_status").fill_null("").str.to_lowercase()
                                                         .str.contains("limited")),
        "rb_backup_after_lead_returns": rb & lead_back,
        "wr1_out_teammates": wr & lead_out,
        "wr_after_wr1_returns": wr & lead_back,
        "te1_out_teammates": te & lead_out,
        "te_after_te1_returns": te & lead_back,
        "self_returning_any": self_ret,
        "long_absence_4plus": self_ret & (missed >= 4),
        "team_changed_during_absence": (pl.col("team_changed") == 1) & (pl.col("weeks_since_last") >= 2),
        "no_availability_change": (pl.col("ho_vacated").fill_null(0) == 0) & (pl.col("ho_returning").fill_null(0) == 0)
                                  & ~self_ret,
        "starters_pool": starters_pool_expr(),
    }


MECHANISM_COHORTS = ("rb_lead_out_teammates", "wr1_out_teammates", "te1_out_teammates", "rb_backup_after_lead_returns",
                     "wr_after_wr1_returns", "te_after_te1_returns", "self_returning_any")


def verdict(p: pl.DataFrame, adopted: list[str]) -> dict:
    """The pre-registered confirmation rules on `p` (identical rows, pred_ifp2 / pred_iwh, cohort columns)."""
    from .forecast_backtest import pair_decisions

    if not adopted:
        return {"adopted_positions": [], "outcome": "retain_baseline", "reason": "nothing adopted on development"}
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_iwh").is_not_null() & pl.col("pred_ifp2").is_not_null())
    mse = week_bootstrap_diff(rows, "pred_iwh", "pred_ifp2")
    mae = week_bootstrap_diff(rows, "pred_iwh", "pred_ifp2", loss="absolute")
    per = {}
    for pos in adopted:
        g = rows.filter(pl.col("position") == pos)
        sp = g.filter(cohorts()["starters_pool"])
        per[pos] = {"mse": week_bootstrap_diff(g, "pred_iwh", "pred_ifp2"),
                    "base": point_metrics(g, "pred_ifp2"), "cand": point_metrics(g, "pred_iwh"),
                    "starters_base": point_metrics(sp, "pred_ifp2"), "starters_cand": point_metrics(sp, "pred_iwh")}
    pairs = pair_decisions(rows, ["pred_iwh", "pred_ifp2"], exact=True)["all"]
    checks = {
        "c1_pooled_mse": mse["diff"] < 0 and mse["ci95"][1] < 0,
        "c2_pooled_mae": mae["diff"] <= CONF_MAX_MAE_DIFF,
        "c3_no_position_significantly_worse": all(v["mse"]["ci95"][0] <= 0 for v in per.values()),
        "c4_bias": all(abs(v["cand"]["bias"]) <= abs(v["base"]["bias"]) + BIAS_TOLERANCE for v in per.values()),
        "c5_starters_pool": all(v["starters_cand"]["mae"] <= v["starters_base"]["mae"] * 1.01 for v in per.values()
                                if v["starters_base"]["n"]),
        "c6_pairs": pairs["pred_iwh"]["accuracy"] >= pairs["pred_ifp2"]["accuracy"] - 0.005,
    }
    mech = {}
    flt = cohorts()
    for c in MECHANISM_COHORTS:
        g = rows.filter(flt[c].fill_null(False))
        if g.height >= MECHANISM_MIN_N:
            b, a = point_metrics(g, "pred_ifp2"), point_metrics(g, "pred_iwh")
            mech[c] = {"n": g.height, "mse_diff": a["rmse"] ** 2 - b["rmse"] ** 2, "bias": [b["bias"], a["bias"]],
                       "pass": a["rmse"] < b["rmse"] and abs(a["bias"]) <= abs(b["bias"])}
    checks["c7_mechanism"] = bool(mech) and all(v["pass"] for v in mech.values())
    return {"adopted_positions": adopted, "pooled_mse": mse, "pooled_mae": mae, "per_position": per,
            "pairs": {m: pairs[f"pred_{m}"] for m in ("iwh", "ifp2")}, "mechanism": mech, "checks": checks,
            "outcome": "prospective_shadow" if all(checks.values()) else "retain_baseline"}


# ----------------------------------------------------------------------------- prospective (ifp-iwh-0.1.1)

# Pre-registered after the historical verdict and before any prospective week was forecast or scored. The
# adoption is ifp-iwh-0.1.0's development result carried over unchanged: 2021-22 is not re-run for 0.1.1 (that
# would be a second selection on the same weeks), and the 2023-24 confirmation is not re-adjudicated.
PROSPECTIVE_FIRST_WEEK = (2026, 7)
PROSPECTIVE_ADOPTED = {"QB": False, "RB": True, "WR": True, "TE": True}
PROSPECTIVE_SELF_RETURNING = "self_returning_any"     # the cohort that failed c7 and that 0.1.1 targets
PROSPECTIVE_THRESHOLDS = {
    "min_prospective_weeks": SHADOW_GATE["min_prospective_weeks"],
    "min_matched_appearances_per_position": SHADOW_GATE["min_matched_appearances_per_position"],
    "mae_ci95_upper_below": SHADOW_GATE["mae_ci95_upper_below"],
    "bias_tolerance_points": SHADOW_GATE["bias_tolerance_points"],
    "starters_pool_mae_tolerance": SHADOW_GATE["starters_pool_mae_tolerance"],
    "mechanism_min_n": MECHANISM_MIN_N,
    "self_returning_min_n": 100,
}
PROSPECTIVE_RULES = {
    "evidence": "write-once pregame ledger rows only (written before the team's kickoff, after the week's injury report "
                "is ingested), scored once the week is fully final; ifp2 and ifp-iwh-0.1.1 fitted in the same run on the "
                "same training rows; identical rows (both predictions non-null, the player appeared)",
    "first_week": PROSPECTIVE_FIRST_WEEK,
    "adopted": "RB, WR, TE (ifp-iwh-0.1.0's development adoption, unchanged); QB keeps ifp2",
    "shadow_gate": "per adopted position (challenger.SHADOW_GATE): >= 8 fully final prospective weeks, >= 400 matched "
                   "appearances, iwh - ifp2 MAE week-clustered bootstrap 95% CI upper < 0, |bias| not worse by > 0.15, "
                   "starters-pool MAE not worse",
    "mse_bias_gate": "from DECISION_RULES['confirmation'], on the pooled adopted rows: (c1) MSE diff < 0 with CI upper < "
                     "0; (c3) no adopted position with an MSE diff CI lower bound > 0; (c4) per-position bias as above; "
                     "(c7) every mechanism cohort with n >= 100: MSE diff < 0 and |bias| does not grow",
    "self_returning": "the returning-player cohort (the c7 failure 0.1.1 fixes) must reach n >= 100 before the gate can be "
                      "met; below that the outcome is insufficient_data whatever the other checks say",
    "outcome": "meets_gate (every check passes) | insufficient_data (weeks, appearances or the returning cohort short) | "
               "fails_gate. meets_gate is evidence for an owner review only",
    "never": "no promotion, no registry change, no change to ifp-2.0.0; these rules are frozen with the first ledger "
             "and a later edit invalidates every ledger written under them",
}


def prospective_gate(p: pl.DataFrame) -> dict:
    """The pre-registered prospective gate on matched ledger rows `p` (label, position, pred_ifp2, pred_iwh, key and
    the frozen cohort columns). Every threshold comes from PROSPECTIVE_THRESHOLDS."""
    t = PROSPECTIVE_THRESHOLDS
    adopted = [pos for pos, a in PROSPECTIVE_ADOPTED.items() if a]
    rows = p.filter(pl.col("label").is_not_null() & pl.col("position").is_in(adopted)
                    & pl.col("pred_iwh").is_not_null() & pl.col("pred_ifp2").is_not_null())
    weeks = rows.select("key").n_unique()
    flt = cohorts()
    per = {}
    for pos in adopted:
        g = rows.filter(pl.col("position") == pos)
        sp = g.filter(flt["starters_pool"])
        mae = week_bootstrap_diff(g, "pred_iwh", "pred_ifp2", loss="absolute")
        mse = week_bootstrap_diff(g, "pred_iwh", "pred_ifp2")
        b, c = point_metrics(g, "pred_ifp2"), point_metrics(g, "pred_iwh")
        sb, sc = point_metrics(sp, "pred_ifp2"), point_metrics(sp, "pred_iwh")
        checks = {
            "weeks": weeks >= t["min_prospective_weeks"],
            "appearances": g.height >= t["min_matched_appearances_per_position"],
            "mae_ci_upper_below_0": mae["ci95"] is not None and mae["ci95"][1] < t["mae_ci95_upper_below"],
            "bias": g.height > 0 and abs(c["bias"]) <= abs(b["bias"]) + t["bias_tolerance_points"],
            "starters_pool": sb["n"] > 0 and sc["mae"] <= sb["mae"] + t["starters_pool_mae_tolerance"],
            "c3_mse_ci_lower_not_above_0": mse["ci95"] is not None and mse["ci95"][0] <= 0,
        }
        per[pos] = {"n": g.height, "base": b, "cand": c, "mae": mae, "mse": mse, "starters_base": sb,
                    "starters_cand": sc, "checks": checks}
    mse = week_bootstrap_diff(rows, "pred_iwh", "pred_ifp2")
    mech = {}
    for c in MECHANISM_COHORTS:
        g = rows.filter(flt[c].fill_null(False))
        if g.height == 0:
            continue
        b, a = point_metrics(g, "pred_ifp2"), point_metrics(g, "pred_iwh")
        mech[c] = {"n": g.height, "mse_diff": a["rmse"] ** 2 - b["rmse"] ** 2, "bias": [b["bias"], a["bias"]],
                   "gating": g.height >= t["mechanism_min_n"],
                   "pass": a["rmse"] < b["rmse"] and abs(a["bias"]) <= abs(b["bias"])}
    ret_n = mech.get(PROSPECTIVE_SELF_RETURNING, {}).get("n", 0)
    pooled = {"c1_pooled_mse": mse["ci95"] is not None and mse["diff"] < 0 and mse["ci95"][1] < 0,
              "c7_mechanism": all(v["pass"] for v in mech.values() if v["gating"]),
              "self_returning_n": ret_n >= t["self_returning_min_n"]}
    sufficient = (pooled["self_returning_n"] and all(v["checks"]["weeks"] and v["checks"]["appearances"]
                                                     for v in per.values()))
    passed = all(pooled.values()) and all(all(v["checks"].values()) for v in per.values())
    return {"version": IWH_VERSION, "weeks": weeks, "adopted_positions": adopted, "per_position": per,
            "pooled_mse": mse, "mechanism": mech, "pooled_checks": pooled,
            "outcome": "meets_gate" if passed else "insufficient_data" if not sufficient else "fails_gate"}


__all__ = ["IWH_VERSION", "IWH_STATUS", "GROUP", "DECISION_RULES", "PROSPECTIVE_RULES", "PROSPECTIVE_ADOPTED",
           "walk_forward", "cohorts", "verdict", "prospective_gate", "week_bootstrap_diff", "point_metrics"]

"""Research only: the pre-registered prospective shadow of ifp-iwh-0.1.1 (docs/research/injury_workload_handoff.md
section 10). Nothing in the app imports this module (tests/test_injury_workload.py).

The rules, thresholds and gate below were written after the historical verdict on ifp-iwh-0.1.0 and before any
prospective week was forecast or scored. scripts/injury_workload_research.py `predict` freezes them write-once
with the first ledger (hashing this module's gate and the challenger's cohorts), and `prospective` applies
`prospective_gate` to matched ledger rows. Passing is evidence for an owner review only.
"""

from __future__ import annotations

import polars as pl

from .challenger import SHADOW_GATE
from .injury_challenger import (IWH_VERSION, MECHANISM_COHORTS, MECHANISM_MIN_N, cohorts, point_metrics,
                                week_bootstrap_diff)

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


__all__ = ["PROSPECTIVE_FIRST_WEEK", "PROSPECTIVE_ADOPTED", "PROSPECTIVE_THRESHOLDS", "PROSPECTIVE_RULES",
           "prospective_gate"]

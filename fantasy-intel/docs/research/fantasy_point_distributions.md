# Calibrated fantasy-point distributions (ifpd-0.1.0, research only)

Date: 2026-10-16. Data: local nflverse history 2019-2025 and live 2026 weeks 1-5 (112 fully final
weeks), the same feature store, rows and scoring fingerprint (`e165b59686110c7d`) as the ifp-2
report. Code:

* `backend/app/analytics/point_distributions.py` (research module, never imported by the app)
* `scripts/point_distribution_research.py`
* tests: `backend/tests/test_point_distributions.py`
* prospective ledger (added 2026-10-18, section 17): `scripts/distribution_ledger.py`, tests
  `backend/tests/test_distribution_ledger.py`

**Answer.** Yes, statistically defensible floors and ceilings can sit on top of the existing
projections without touching them. The method is simple. Take the realized scores of the 300
earlier player-weeks whose projection was closest to this one, at the same position. Scale them so
their average equals our projection exactly. Read off the percentiles. Zero-point games stay at
exactly zero, the touchdown-driven right skew is kept, and the projection stays the mean of the
distribution (it is never relabeled a median).

* **Conditional ranges (points if he plays) pass the pre-registered bar at every position** on the
  2023-24 confirmation run. P10, P50 and P90 rates are 0.09-0.11, 0.47-0.49 and 0.10-0.11, and 80%
  coverage is 0.80-0.81. They hold on the already-inspected 2025-26 replay.
* **They beat the existing published bands** on every proper score for RB, WR and TE. CRPS is
  0.02-0.04 points lower, with week-clustered 95% CIs below zero in all three windows. The existing
  bands get 80% coverage on average by being too wide for low projections and predicting too few
  zero-point games above them.
* **Unconditional ranges** (including the chance he sits) are calibrated at all four positions when
  combined with `ipm-1.0.0` through an exact mixture: absence rates within 0.016, coverage
  0.79-0.82. They are publishable in research terms for **WR and TE only**, because QB and RB
  participation is still `experimental`.
* **A player's range is set almost entirely by his projection.** Quantile regression with player
  volatility inputs (recent scoring SD, aDOT, carries, targets) was worse than the mean-only method
  in every window. At the same projection, the most volatile third of players has a realized 10-90
  range only 5-9% wider than the steadiest third (QB: none). "Same 15 points, different range"
  mostly comes from appearance risk, not from a measurable boom/bust trait.
* **Risk-aware Start/Sit does not help.** A pre-registered rule ("start the player more likely to
  reach the points you need") made no improvement in a controlled scenario analysis. For the
  underdog it cost about 1 point per changed decision.

**Recommendation.** Do not ship percentile cards yet. WR and TE conditional and unconditional
ranges, and RB and QB conditional ranges, are ready for a *future*, separately approved and
reversible integration. They must carry the semantics below and keep the QB restriction (see
section 9). Nothing in production changed: no mean, artifact, ledger, API field, ESPN/Ours switch,
Start/Sit, trade or waiver logic.

## 1. Audit: what exists today

| Item | Behavior |
|---|---|
| ifp-1.0.0 80% interval | `forecast_backtest.interval_bands`: residual (actual − prediction) 10th/90th percentiles in three prediction terciles. Built from 2023-24 validation residuals. `independent_projections.interval_for` adds them to the projection and **floors the low end at 0**. |
| Publication rule | `PROMOTION_CRITERIA`: published only if held-out coverage is in [0.75, 0.85]. |
| Withholding | **QB withheld** (held-out coverage 74.9%). RB 79.3%, WR 80.1%, TE 80.0% published. |
| ifp-2.0.0 (shadow) | Same tercile method on development residuals. **QB withheld** by policy (confirmation 74.0%). `SHADOW_GATE.interval_coverage` stays `insufficient_data` for QB. |
| Consumers | Only `GET /api/projections/independent` (`interval_80`) and the shadow ledger. Start/Sit, Matchup, the "Ours" switch, trades, waivers and the frontend never read an interval. |
| Median / percentiles / P(threshold) | None anywhere. |

The rebuilt pipeline reproduces the published held-out coverage exactly: 74.9% / 79.3% / 80.1% /
80.0%.

Two properties of the existing bands matter here:

* **They change the mean.** Flooring and uncentered band residuals shift the implied mean: TE
  +0.19, QB −0.19 and RB +0.08 points on development.
* **Their 80% is an average of two errors.** Measured with an exact treatment of the point mass at
  0 (section 3), 17-22% of RB/WR/TE outcomes fall in the bands' bottom decile, not 10%. In the low
  tercile the band covers every zero by flooring. Above it, the band puts the 10th percentile
  above 0 while 10-15% of those players still score 0.

## 2. Statistical meanings (Phase 1)

| Field | Meaning |
|---|---|
| `conditional_mean` | E[points \| appears], the existing `projected_points` (ifp-1.0.0 model C), unchanged |
| `p_play` | P(appears), the existing `ipm-1.0.0` estimate. The PR #54 isotonic layer stays shadow-only and is not used. |
| `expected_points` | `p_play × conditional_mean`. Exact, a mixture identity for **means only** |
| `conditional_p10/p50/p90` | Percentiles of points given an appearance. `p50` is a median and is generally below the mean (right skew). |
| `unconditional_p10/p50/p90` | Percentiles of the mixture: 0 points with probability 1 − p_play, otherwise a conditional draw. **Not** `p_play × conditional_pN`. |
| `prob_at_least_t` | P(points ≥ t), conditional or unconditional (t = 10, 15, 20) |
| `prob_below_5` | P(points < 5): "an unusually low result" |

**Representation.** A conditional distribution is a non-decreasing quantile function at 99 knots
(τ = 0.01 … 0.99), linear between knots and constant beyond them. Negative values are allowed (QB
interceptions, fumbles), and a flat stretch is a point mass (e.g. exactly 0).

**Mixture.** The unconditional CDF is F(x) = (1 − p)·1[x ≥ 0] + p·F_c(x). Its quantile at level u
is the generalized inverse:

* Q_c(u/p) while u ≤ p·F_c(0⁻), the negative outcomes;
* exactly 0 while u ≤ (1 − p) + p·F_c(0), the absence mass plus any conditional mass at 0;
* Q_c((u − (1 − p))/p) above that.

**Missing inputs.** A missing `p_play` yields no unconditional numbers; it is never assumed to be 1.
A withheld position yields no percentiles at all, while `expected_points` still follows the mean
identity.

## 3. Protocol

* **Mean being dressed.** Frozen ifp-1.0.0 model C (`selection.json` penalties), refit walk-forward
  every week on strictly earlier appearances from 2020. Rows and n are identical to the ifp-2
  windows: development 13,372; confirmation 13,345 (C MAE 4.031); exploratory 8,645.
* **Residual pools are walk-forward.** For forecast week W, every method is fitted only on
  out-of-sample forecasts and outcomes of weeks before W (from 2020). This is asserted in code and
  tested by corrupting every label at or after W.
* **Windows** (as in ifp-2):
  * development 2021-22: all choices;
  * confirmation 2023-24: one run, statuses;
  * exploratory 2025 + 2026 weeks 1-5: already inspected by earlier work, reported only.
* **Only fully final weeks.** Every scheduled game has a final score and every scheduled team has
  synced box scores. Nonappearance labels come from the game-day roster (ACT/INA) of such weeks
  only, so an unplayed game never becomes a zero.
* **Atoms are scored exactly.** Tail rates use the randomized PIT (uniform on [F(y⁻), F(y)]), which
  is exact when 21-40% of RB/WR/TE appearances score exactly 0. `coverage80` = P(0.1 < PIT ≤ 0.9).
  `inclusive_coverage80` is the plain share of outcomes inside [P10, P90], which a point mass at P10
  pushes above 80% by construction.
* **Scores.**
  * CRPS: 99-quantile approximation, 2 × mean pinball loss.
  * Pinball loss at P10/P50/P90.
  * PIT decile deviation.
  * Threshold-probability Brier and reliability.
  * Week-clustered bootstrap 95% CIs (1,000 resamples, seed 20261014).
* **Uncertainty assumptions.** No finite-sample conformal guarantee is claimed. The method is an
  empirical, expanding-window recalibration. Its coverage holds only if next week's forecast errors
  resemble earlier weeks' errors at the same projection level. That is the assumption the
  confirmation and replay windows test.

**Pre-registered rules** (`SELECTION_RULE`, `VALIDATION_CRITERIA`, `UNCONDITIONAL_CRITERIA`, in
code):

* **Selection** (development only, per position):
  * eligible: 80% coverage in [0.75, 0.85] and P10/P90 tail rates each in [0.07, 0.13];
  * choice: lowest CRPS;
  * tie-break: a simpler method wins within 0.5% CRPS.
* **Validation** (confirmation, run once), all of:
  * n ≥ 400;
  * coverage in [0.75, 0.85];
  * P10 and P90 rates in [0.07, 0.13];
  * P50 rate in [0.45, 0.55];
  * starters-pool coverage in [0.75, 0.85];
  * each projection-tercile coverage in [0.72, 0.88];
  * CRPS not more than 0.5% worse than the existing bands (walk-forward) on the same rows.
* **Unconditional:**
  * the conditional distribution is validated;
  * `ipm-1.0.0` participation is `validated` for the position;
  * on the participation held-out window (2025-26), coverage is in [0.75, 0.85] and the predicted
    absence rate is within 0.02 of the observed rate.

**Candidate rounds** (development results only; no later window was inspected before the final
freeze):

| Round | Added | Development choice | Why superseded |
|---|---|---|---|
| 1 | A, N, B, C@k, F@k, D (k ∈ {300, 1000, 3000}) | QB B, RB C@300, WR C@1000, TE C@300 | Centred residuals put probability on impossible negative scores for low projections. A constant shift moves the cluster of zeros off 0. |
| 2 | z-variants: floor at the pool's support, mean-preserving shift; randomized-PIT rates | QB Bz, RB C@300, WR Cz@3000, TE C@300 | The PIT showed the zero mass still under-predicted: 16-20% of outcomes in the bottom decile. |
| 3 | R@k (scaled local outcomes), Dr; diagnostic L@k (unscaled, own mean) | **QB Bz, RB/WR/TE R@300** | final, frozen in `distribution_selection.json` |

The round-1 run computed later-window metrics into its report file. They were not read before
round 3 was frozen. A later code edit re-froze the selection from identical development inputs, and
the choice was unchanged (`choice_changed: false` is recorded).

## 4. Methods

| | Method | Definition |
|---|---|---|
| A | existing bands | Tercile residual quantiles, floored at 0, refit walk-forward (also evaluated as published) |
| N | normal | mean ± position residual SD × z (symmetric) |
| B | position residuals | All pool residuals of the position, centred |
| Bz | B + support floor | Censored at L, then shifted so the mean is exact. L = 0 when < 2% of pool outcomes are negative, else the pool's 1st percentile: −1.24 for QB, 0 otherwise. |
| C@k | magnitude-conditioned | Residuals of the k pool rows nearest in projection rank, centred; upper-edge widening only |
| Cz@k | C + support floor | |
| F@k, Fz@k | rolling recalibration | C with 36-week half-life recency weights |
| D | linear quantile regression | Projection + pregame volatility inputs (QB: scoring SD, carries; RB: scoring SD, targets; WR/TE: scoring SD, aDOT) at 25 levels, rearranged (sorted) so quantiles never cross, centred |
| Dz, Dr | D + floor / D + scaling | |
| **R@k** | **scaled local outcomes** | Outcome quantiles of the k pool rows nearest in projection, multiplied by mean/local mean (shifted if the local mean < 0.1). Mean exact, zeros stay 0, skew kept, wider automatically above the pool's range. |
| L@k | diagnostic only | R without the scaling: the local outcome distribution with its own mean, never eligible. Measures what keeping our mean costs. |
| E | quantile gradient boosting | **Not fitted** (section 10) |

## 5. Results by window (pooled over positions)

CRPS and pinball in league points (lower is better). "vs A" is the CRPS difference against the
existing bands on identical rows, with a week-clustered 95% CI.

| Window | n | Method | CRPS | vs A (95% CI) | Coverage | P10 rate | P50 rate | P90 rate | Width |
|---|---|---|---|---|---|---|---|---|---|
| Development | 13,372 | A existing | 2.907 | | 0.724 | 0.183 | 0.554 | 0.093 | 12.95 |
| | | D quantile regr. | 2.931 | +0.024 [+0.018, +0.031] | 0.821 | 0.090 | 0.496 | 0.089 | 13.55 |
| | | **R@300 (chosen)** | **2.880** | **−0.027 [−0.034, −0.019]** | 0.806 | 0.098 | 0.516 | 0.096 | 13.47 |
| | | L@300 (own mean) | 2.885 | −0.021 [−0.030, −0.012] | 0.810 | 0.097 | 0.511 | 0.093 | 13.24 |
| Confirmation | 13,345 | A existing | 2.794 | | 0.725 | 0.175 | 0.535 | 0.100 | 12.24 |
| | | N normal | 3.060 | +0.266 [+0.251, +0.281] | 0.850 | 0.052 | 0.633 | 0.098 | 14.97 |
| | | B position | 2.972 | +0.179 [+0.169, +0.189] | 0.816 | 0.090 | 0.509 | 0.094 | 13.78 |
| | | D quantile regr. | 2.811 | +0.018 [+0.011, +0.024] | 0.821 | 0.082 | 0.484 | 0.097 | 12.82 |
| | | **R@300** | **2.768** | **−0.026 [−0.032, −0.019]** | 0.809 | 0.092 | 0.492 | 0.099 | 12.84 |
| Exploratory | 8,645 | A existing | 2.819 | | 0.723 | 0.180 | 0.547 | 0.098 | 12.11 |
| | | **R@300** | **2.790** | **−0.030 [−0.037, −0.022]** | 0.800 | 0.098 | 0.504 | 0.102 | 12.59 |

The symmetric normal is clearly worst, with the median too high (P50 rate 0.63-0.65). Skew is
real: a P50 that equals the mean is wrong about half the time in one direction only. Keeping our
own mean (R) instead of each neighborhood's own mean (L) costs at most 0.006 CRPS on development,
and R is better on confirmation RB.

## 6. Position-specific evaluation (confirmation 2023-24, frozen selection, run once)

| Pos | Method | n | Coverage (inclusive) | P10 / P50 / P90 rates | Width | CRPS vs A (95% CI) | Starters coverage | Tercile coverage (low / mid / high) | Status |
|---|---|---|---|---|---|---|---|---|---|
| QB | Bz | 1,330 | 0.795 (0.795) | 0.092 / 0.474 / 0.113 | 18.9 | +0.004 [−0.018, +0.026] | 0.784 | 0.819 / 0.797 / 0.770 | **validated** |
| RB | R@300 | 3,398 | 0.813 (0.853) | 0.089 / 0.493 / 0.098 | 12.8 | −0.023 [−0.031, −0.014] | 0.818 | 0.791 / 0.845 / 0.802 | **validated** |
| WR | R@300 | 5,409 | 0.813 (0.865) | 0.091 / 0.493 / 0.096 | 13.6 | −0.020 [−0.030, −0.010] | 0.809 | 0.800 / 0.833 / 0.804 | **validated** |
| TE | R@300 | 3,208 | 0.801 (0.881) | 0.095 / 0.490 / 0.104 | 8.7 | −0.036 [−0.046, −0.027] | 0.798 | 0.774 / 0.832 / 0.797 | **validated** |

The existing bands on the same rows: coverage 0.748 / 0.757 / 0.728 / 0.679 and P10 rates 0.135 /
0.151 / 0.174 / 0.219. As published (validation-window bands, in-sample here), their inclusive
coverage is 0.765 / 0.794 / 0.796 / 0.797.

Exploratory replay (2025-26, not a fresh test). Every position still passes every criterion:

| Pos | n | Coverage | P10 / P50 / P90 rates | CRPS vs A (95% CI) |
|---|---|---|---|---|
| QB | 834 | 0.776 | 0.122 / 0.493 / 0.102 | +0.000 [−0.025, +0.027] |
| RB | 2,196 | 0.810 | 0.089 / 0.504 / 0.102 | −0.033 [−0.044, −0.022] |
| WR | 3,477 | 0.807 | 0.092 / 0.505 / 0.101 | −0.026 [−0.037, −0.015] |
| TE | 2,138 | 0.791 | 0.104 / 0.504 / 0.105 | −0.032 [−0.044, −0.019] |

### Cohorts (confirmation; exploratory agrees in direction unless noted)

Coverage of the chosen method, with the existing bands in parentheses:

| Cohort | QB | RB | WR | TE |
|---|---|---|---|---|
| Starters pool (top 24/48/60/24 by recent average) | 0.784 (0.781) | 0.818 (0.789) | 0.809 (0.785) | 0.797 (0.740) |
| After an injury exit | 0.853 (0.838), n 34 | 0.839 (0.758), n 65 | 0.813 (0.768) | 0.853 (0.737), n 58 |
| Returning after ≥ 2 weeks out | 0.770 (0.640) | 0.791 (0.734) | 0.773 (0.705) | 0.808 (0.712) |
| After a role drop | 0.741 (0.624), n 58 | 0.825 (0.777) | 0.822 (0.659) | 0.795 (0.624) |
| Late season (week 13+) | 0.773 (0.728) | 0.793 (0.748) | 0.800 (0.722) | 0.807 (0.691) |
| Late season with a role change | 0.786 (0.712) | 0.778 (0.759) | 0.806 (0.692) | 0.806 (0.697) |
| < 3 recent games | 0.747 (0.662) | 0.798 (0.701) | 0.811 (0.686) | 0.801 (0.641) |
| **Team changed** | **0.691** (0.558), n 55 | **0.719** (0.746) | **0.733** (0.687) | 0.820 (0.687) |

Known weak spots:

* **Team changes.** The right tail is too thin: RB/WR P90 exceeded 20% / 11% of the time. Exploratory
  QB is worse (0.564, n 39).
* **WR after an injury exit (exploratory, n 105).** P90 exceeded 16%.
* **QB lower tail in the exploratory replay.** P10 rate 0.122 overall and 0.141 in the starters
  pool: QB floors were too optimistic.

## 7. Threshold probabilities (Phase 5)

P(points ≥ t | appears), mean predicted vs observed:

| Pos | ≥ 10 | ≥ 15 | ≥ 20 | < 5 | Brier ≥ 15 |
|---|---|---|---|---|---|
| QB | 0.622 / 0.620 | 0.406 / 0.423 | 0.220 / 0.239 | 0.201 / 0.222 | 0.204 |
| RB | 0.281 / 0.281 | 0.163 / 0.156 | 0.082 / 0.080 | 0.534 / 0.536 | 0.098 |
| WR | 0.273 / 0.260 | 0.152 / 0.149 | 0.075 / 0.074 | 0.550 / 0.551 | 0.099 |
| TE | 0.138 / 0.139 | 0.053 / 0.060 | 0.022 / 0.024 | 0.707 / 0.703 | 0.047 |

Confirmation. Exploratory is within 0.02 for RB/TE, and WR ≥ 10 is over-predicted by 0.02.

QB under-predicts both tails slightly: ≥ 20 by 0.02-0.03 and < 5 by 0.02-0.05. QB outcomes are
more extreme than the position-level residual distribution says.

## 8. Unconditional distributions (Phase 4)

Roster rows (game-day ACT/INA, with a lookback game):

* label = points, or a confirmed 0 when he did not appear;
* p = walk-forward `ipm-1.0.0` (frozen penalties);
* conditional mean = the same walk-forward model C.

The conditional appearance distribution comes from earlier appearance rows only. Nonappearance
rows never enter a residual pool (tested).

| Window | Pos | n | Absence predicted / observed | P(≤ 0) predicted / observed | Coverage (inclusive) | P10 / P50 / P90 rates | Naive p × conditional P10..P90 inclusive coverage |
|---|---|---|---|---|---|---|---|
| Held-out 2025-26 | QB | 1,544 | 0.459 / 0.451 | 0.500 / 0.503 | 0.814 (0.863) | 0.089 / 0.491 / 0.098 | 0.696 |
| | RB | 2,593 | 0.167 / 0.151 | 0.352 / 0.341 | 0.817 (0.875) | 0.087 / 0.497 / 0.096 | 0.836 |
| | WR | 3,975 | 0.132 / 0.130 | 0.396 / 0.394 | 0.808 (0.878) | 0.093 / 0.504 / 0.099 | 0.848 |
| | TE | 2,424 | 0.121 / 0.124 | 0.473 / 0.465 | 0.792 (0.879) | 0.101 / 0.498 / 0.107 | 0.862 |
| 2023-24 | all | 16,305 | within 0.009 | within 0.018 | 0.803-0.818 | | |

The 2023-24 rows are the participation model's validation window.

* **The mixture is calibrated** at every position, including QB, where nearly half the rows are
  backups. Scaling conditional percentiles by p_play, the forbidden shortcut, covers only 70% for
  QBs, with 19% of outcomes below its "P10".
* **Unconditional status: WR, TE `validated`. QB, RB `withheld`.** `ipm-1.0.0` participation is
  `experimental` there (calibration bar). The pre-registered rule defers to that status even though
  the mixture's own coverage is fine.
* Unconditional P(≥ 15): QB 0.229 vs 0.248 observed, RB 0.136 / 0.135, WR 0.123 / 0.112, TE 0.054
  / 0.059 (held-out).

## 9. Pairwise comparisons and dependence (Phase 5)

Residual correlation (actual − projection) between players of the same week (starters pool,
2023-26):

| Relation | Pairs | Correlation | Notable position pairs |
|---|---|---|---|
| Same NFL team | 20,794 | +0.070 | **QB-WR +0.31, QB-TE +0.29**, QB-RB +0.08, RB-RB −0.05, WR-WR +0.01 |
| Opponents (same game) | 23,795 | +0.046 | QB-QB +0.17, QB-TE +0.12, QB-WR +0.08, WR-WR +0.08 |
| Different games | 668,721 | −0.000 | all within ±0.007 |

**P(A outscores B)** for same-position starters-pool pairs, computed assuming independence:

* Different games (192k pairs): pick accuracy 65.2%, slightly underconfident (predicted 0.92 →
  0.94 observed).
* Same team (3,758 pairs): **miscalibrated**. Predicted 0.15 → observed 0.08, and predicted 0.25 →
  observed 0.18; teammates competing for the same touches separate more than independence implies.

Therefore:

* "P(A > B)" may only be labeled as an *independence-assumed* comparison, and only for players in
  different games.
* Same-team and same-game pairs, QB stacks and any lineup-win probability need a dependence model
  that does not exist. No lineup-win probability is computed or claimed.

## 10. Decision utility (Phase 7, controlled scenario analysis)

No genuine pregame win probabilities exist, so this is a scenario analysis, not a historical
win-rate claim. The rule was fixed before outcomes (`DECISION_RULE`):

* **Population.** Same-position, same-week starters-pool pairs whose unconditional expected points
  are within 1.5 (43,335 close calls, 2023-26 roster rows).
* **Needs.** Points needed from the slot:
  * favorite = 40th percentile of 2020-22 starters' outcomes (QB 14.6, RB 7.6, WR 8.4, TE 5.9);
  * underdog = 85th percentile (QB 25.8, RB 20.2, WR 20.6, TE 15.7).
* **Rules.** The EV rule starts the higher expected points. The risk rule starts the higher
  unconditional P(points ≥ need).

| Scenario | Rules disagree | Success: risk − EV on disagreements (95% CI) | Points: risk − EV | Predicted gain |
|---|---|---|---|---|
| Favorite | 3,877 | −0.020 [−0.054, +0.016] | −0.36 [−0.86, +0.21] | +0.010 |
| Underdog | 7,825 | −0.015 [−0.031, +0.001] | **−0.95 [−1.37, −0.57]** | +0.009 |
| Underdog, width ratio ≥ 1.25 | 161 | −0.012 [−0.078, +0.072] | | |
| Sensitivity: D (player volatility) underdog | 5,802 | −0.008 [−0.024, +0.006] | −0.64 [−1.09, −0.23] | +0.012 |

When projections are this close, the predicted difference in P(reaching the need) is about 1
percentage point. The realized effect is zero or slightly negative, and the underdog rule gives up
a point per changed decision. **Rejected:** do not add a risk-preference toggle to Start/Sit.

## 11. Does a player's own volatility matter? (the "11-19 vs wider" question)

The test: within the same projection decile, compare the most and least volatile thirds of players
by pregame scoring SD.

| Pos | Scoring SD (low / high third) | Realized 10-90 range (low / high) | Ratio |
|---|---|---|---|
| QB | 3.5 / 9.3 | 19.3 / 18.4 | 0.95 |
| RB | 2.3 / 6.5 | 11.9 / 12.9 | 1.08 |
| WR | 2.4 / 7.0 | 12.0 / 13.1 | 1.09 |
| TE | 1.6 / 5.0 | 8.1 / 8.8 | 1.10 |

Confirmation window. Exploratory ratios: 0.98 / 1.05 / 1.09 / 0.98.

Recent volatility nearly triples between the two thirds, but realized ranges move by under 10%,
which is why D lost to R. With the evidence available, two players at the same projection have
about the same conditional range. The defensible way to show a "steadier" player is through:

* a higher projection (a range that is wider in points but proportionally similar);
* a higher p_play (less mass at 0).

It is not a boom/bust label. A future study could try touchdown dependence or a role-certainty
input, prospectively.

## 12. Rejected methods

| Method | Why |
|---|---|
| N symmetric normal | Worst CRPS (+0.27 vs A); median wrong (P50 rate 0.63-0.65), lower tail impossible |
| B position residuals | +0.18 CRPS: ignores that spread grows with the projection |
| A existing bands | Calibrated only on average, shifts the mean, no full distribution. Kept as the baseline; QB as published stays withheld. |
| C@k, F@k centred local residuals | Zeros pushed off 0: P10 rate 0.15-0.22 for RB/WR/TE |
| F rolling recalibration (36-week half-life) | Never better than its unweighted counterpart (±0.002 CRPS on development). Drift at this scale is not detectable. |
| Bz, Cz, Fz, Dz support floor + shift | Better CRPS than C but zero mass still too small (P10 rate 0.13-0.20); QB Bz is the exception and is chosen |
| D, Dr quantile regression with volatility inputs | Worse than R in every window (D +0.018 CRPS vs A on confirmation); the volatility inputs carry little signal (section 11) |
| E quantile gradient boosting | **Not fitted.** Reasons below this table. |
| Risk-aware Start/Sit rule | Section 10 |
| Independent P(A > B) for teammates or opponents | Section 9 |

Reasons E was not fitted:

* Prior HGB mean-forecast research was negative: worse than ridge, with errors correlated
  0.98-0.99 with it.
* The quantile problem here needs only the projection: R uses one input and beats D, which adds
  four.
* Calibrating 99 quantile levels per position at this sample size (1-5k rows per position-season)
  invites tail overfitting.
* It would need a new dependency group.

## 13. Frozen research artifact

`point_distributions_ifpd-0.1.0_<content-hash>.json` (schema `point-distribution-artifact/v1`,
~260 KB) is written once to the git-ignored `data/models/research/point_distributions/`. It is never
read by the app. It contains:

* per position, the chosen method's residual-quantile table over a grid of projections (0.5-point
  steps; interpolation keeps the mean exact), fitted on every out-of-sample residual through 2026
  week 5;
* the status and reasons, and the unconditional status;
* every pre-registered rule;
* the input fingerprint, scoring fingerprint and `selection.json` hash.

Rebuilding from identical inputs reproduces the same bytes and hash; this is tested, and was
checked on real data (hash `5a05e3f55e99bdd1`).

`artifact_summary(artifact, position, conditional_mean, p_play)` returns the consumer-shaped
research summary. Percentiles are `null` unless the position is validated, unconditional ones need
an approved p_play, and each withheld field carries its reason.

Illustration, 2026 week 5 (pregame inputs; realized score shown afterwards):

| Player | Projection | p_play | Conditional P10 / P50 / P90 | P(≥ 15) | Unconditional P10 / P50 / P90 | Scored |
|---|---|---|---|---|---|---|
| Drake London (WR) | 15.0 | 0.99 | 4.4 / 13.5 / 28.0 | 0.43 | 4.1 / 13.3 / 28.0 | 18.8 |
| Nico Collins (WR) | 14.9 | 0.97 | 4.4 / 13.5 / 27.7 | 0.43 | 3.3 / 13.0 / 27.6 | 10.6 |
| DeVonta Smith (WR, inactive) | 13.6 | 0.00 | 3.8 / 12.5 / 26.2 | 0.38 | 0 / 0 / 0 | 0 |
| Dalton Schultz (TE) | 9.1 | 0.99 | 1.9 / 8.2 / 18.3 | 0.17 | 1.8 / 8.1 / 18.2 | 6.0 |
| Brock Purdy (QB) | 18.0 | 0.94 | 8.4 / 17.3 / 28.4 | 0.63 | withheld (QB participation experimental) | 16.2 |

## 14. Recommendation on product integration

**Not yet, and not by default.**

Ready for a future, separately approved integration that can be rolled back on its own:

* WR and TE conditional and unconditional ranges;
* RB conditional ranges.

Any such integration must:

* label the projection as the **mean** and P50 as the **median**;
* say "if he plays" for conditional ranges;
* show unconditional ranges only where participation is validated;
* never present a percentile as a guaranteed floor or ceiling;
* offer pairwise "chance to outscore" only for players in different games, labeled as assuming
  independence.

**QB:** keep the existing restriction. The new QB distribution passes the conditional bar on
confirmation, but its lower tail was too optimistic in the 2025-26 replay (P10 rate 0.122 overall,
0.141 among starters), and QB participation is `experimental`. Lift it only after prospective weeks
confirm it.

Before any product use, accumulate frozen pregame distributions alongside the weekly learning cycle
and re-check coverage on prospective weeks (2026 week 6 onward), as the ifp-2 shadow gate does for
means. That ledger and its weekly coverage check now run automatically (section 17).

## 15. Reproduce

    cd backend
    uv run python ../scripts/sync_nfl_history.py --seasons 2019-2025
    # live DB: 2026 nflverse sync with the league's skill scoring (see ifp2 report)
    uv run python ../scripts/backtest_independent_projections.py select
    uv run python ../scripts/backtest_independent_projections.py participation-select
    uv run python ../scripts/weekly_learning.py sync-exits --history-seasons 2019-2025 && \
        uv run python ../scripts/weekly_learning.py sync-exits
    uv run python ../scripts/point_distribution_research.py run          # ~7 min (first run builds caches)
    uv run python ../scripts/point_distribution_research.py artifact     # re-check: same hash
    uv run python ../scripts/point_distribution_research.py examples

Guards:

* `run` refuses a `selection.json` made under another scoring fingerprint, and a
  `participation_selection.json` whose `ifp_selection_sha` does not match.
* Cached frames, the frozen selection, the report and the artifact all carry an input fingerprint.
  It covers content hashes of the panel, games, injuries, exits, rosters and defense frames; the
  fully final weeks; scoring; polars/numpy versions; the source of the five modules involved; and
  both selection hashes.
* A selection frozen from other inputs is never silently refit (`--refreeze` is explicit and
  recorded). `artifact` refuses a stale report.
* Writes are atomic (temp file + rename). The artifact is write-once under its content hash.

## 16. Limitations

* **Empirical recalibration, no formal guarantee.** Coverage assumes this week's errors look like
  earlier weeks' errors at the same projection level. A rule or usage shift (e.g. a new kickoff
  rule raising scoring) would show up as drift; the expanding window adapts slowly.
* **Conditioning is on the projection only.** Cohorts with information the projection lacks remain
  under-dispersed: team changes, WR returns from injury, and the QB lower tail.
* **The QB distribution is position-level** (Bz). It ignores that rushing QBs and pocket passers
  may differ; D tested that through carries and found no CRPS gain.
* **Unconditional QB/RB ranges** inherit `ipm-1.0.0`'s experimental calibration and its lack of
  team-level coherence (two backup QBs of one team can both get a high p_play).
* **Dependence is measured, not modeled.**
* **Scoring scope.** Only QB/RB/WR/TE under this league's scoring (fingerprint `e165b59686110c7d`).
  K and D/ST have no distribution.

## 17. Frozen pregame ledger and prospective coverage (added 2026-10-18)

Section 14's precondition is now automated by `scripts/distribution_ledger.py`. It is research output only: nothing
in the app, the API or the frontend reads it, and no projection, artifact, ledger or table of the production models
changes.

**Freeze (write-once, before kickoff).** For week W the script reads the production weekly ledger
(`data/models/weekly/ledger/s<S>w<WW>/`, written by `learning_cycle.py`'s `predict` stage). Per player it takes the
latest pregame row created before his kickoff, the rule `weekly_learning.frozen_rows` uses, and keeps it only while
his kickoff is still ahead of the freeze. Each row's two frozen means are dressed with the pinned artifact (R@300 for
RB/WR/TE, Bz for QB):

* `ifp-1.0.0` (the weekly champion, model C refit on earlier weeks): the mean these distributions were developed
  and validated on (primary);
* `ifp-2.0.0` (the production challenger): what the product shows (secondary, never part of the validation).

Stored per row and mean: the 99-knot conditional quantile function, conditional P10/P50/P90, and unconditional
P10/P50/P90 with the row's `ipm-1.0.0` p_play where the artifact approves it (WR/TE).

Provenance recorded in the file:

* the pin, and the artifact file, its content hash and sha256;
* the trained-through key (always before W);
* the weekly-ledger run ids, created times and file sha256s;
* a sha256 of the rows.

One file per week: `<ifp-dir>/research/point_distributions/ledger/weeks/s<S>w<WW>-pregame.json` (git-ignored,
about 0.5 MB). It is never rewritten. A prospective week whose games all start without one is recorded under
`missed/` and never backfilled.

**The pin.** Every week uses the same artifact until someone re-pins on purpose (`distribution_ledger.py pin
--artifact FILE --reason TEXT`, write-once under `pins/`). The first freeze pins the newest artifact trained only on
weeks before the week being frozen. Nothing is fitted here. Without an artifact the stage warns and freezes nothing:
run `point_distribution_research.py run` once (section 15). The artifact's league-scoring fingerprint must match the
live database's.

Using one fixed artifact differs from the walk-forward evaluation in sections 5-6, which refit the residual pool every
week. It is the stricter prospective test: the frozen method is not refit while it is being judged. A refit is an
explicit, recorded re-pin.

**Scoring (weekly).** Once a frozen week is fully final (`weekly_learning.week_finality`; outcomes from
`weekly_learning.label_outcomes`, so a game not yet played never becomes a zero), the ledger's own frozen quantiles are
scored per mean model and position. It reuses the research functions:

* randomized-PIT `calibration` (exact with the point mass at 0): coverage80, P10/P50/P90 rates, width, plus CRPS and a
  week-clustered 95% interval for coverage80;
* `unconditional_block` for unconditional ranges (WR/TE only).

Each block is compared with the pre-registered bounds (`VALIDATION_CRITERIA`, `UNCONDITIONAL_CRITERIA`, at least 400
rows). The result is `insufficient_data`, `within_bounds` or `outside_bounds` with reasons. That label is a monitoring
statement and decides nothing: QB stays restricted (it is scored only to see whether prospective weeks confirm its
lower tail), RB is scored conditional-only, and lifting any restriction or shipping any percentile still needs a
separate approval. Output: `ledger/scores.json`, recomputed each cycle so stat corrections are picked up.

| Job (existing) | When | Runs |
|---|---|---|
| `learning_cycle.py`, stage `dist_ledger` after `ctx_shadow` | Tue/Wed/Thu 07:00 PT, right after `predict` froze the week's forecast | `distribution_ledger.py cycle --week <target>`: score final weeks, record a missed week, freeze the target week |
| `refresh_all.py` evidence stage | Thu/Sun/Mon/Tue plus every pregame-guard run | `distribution_ledger.py freeze --week <upcoming>`: a retry (a no-op once frozen) |

Both are non-fatal: a failure is a warning in the cycle report or the refresh log, and exit 3 (no artifact, missed
week) is informational. `--no-dist-ledger` skips the cycle stage.

Notes:

* The first prospective week is 2026 week 6. That week's games began before this automation existed, so
  `missed/s2026w06.json` is expected.
* A dry run on the real 2026 week-6 forecast (scratch copy of the data, freeze time 2026-10-15 12:00 UTC) froze all
  436 rows with an artifact trained through 2026 week 5 (`852219ee4d73d2b3`). The hash differs from section 13's
  because the inputs have changed since, which is why the pin records the exact file.

```
cd backend
uv run python ../scripts/distribution_ledger.py status
uv run python ../scripts/distribution_ledger.py freeze --week W --now 2026-10-25T12:00Z   # decide only
uv run python ../scripts/distribution_ledger.py score
```

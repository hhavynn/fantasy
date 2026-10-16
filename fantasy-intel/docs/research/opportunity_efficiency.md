# Opportunity + efficiency decomposition (ifp-oe, research only)

Date: 2026-10-15. Data: local nflverse history 2019-2025 and live 2026 weeks 1-5, with the same
feature store, rows and scoring fingerprint (`e165b59686110c7d`) as the ifp-2 report. Code:

* `backend/app/analytics/opportunity_efficiency.py`
* `scripts/opportunity_efficiency_research.py`
* tests: `backend/tests/test_opportunity_efficiency.py`

**Answer.** Separating next-week points into predicted opportunity (expected league points) plus a
regularized efficiency residual does **not** improve forecasts over the frozen ifp-2 challenger. The
best candidate (OE_D, with a stronger penalty on efficiency) gains 0.008 points of MAE on development
(95% CI [−0.014, −0.002]). The gain does not hold on the 2023-2026 replay (+0.003 [−0.001, +0.007]),
and Start/Sit regret does not improve. Under the pre-registered rule the outcome is **negative**, so
no artifact is written. ifp-1.0.0, ifp-2.0.0, the weekly pipeline, the ledgers and the "Ours"
selector are unchanged.

The decomposition adds no information because:

* **The efficiency residual is essentially unpredictable from pregame data.** Out-of-sample R² is
  0.003-0.007.
* **Any regularized efficiency forecast collapses toward a per-position constant.** Its standard
  deviation is about 0.35 points, against a target standard deviation of 4.0.
* **Everything the decomposition can learn is already inside the direct ridge.**

## 1. How this differs from earlier opportunity-first work

| Earlier experiment | What it did | Result |
|---|---|---|
| ifp-1.0.0 model B ("opportunity") | one target (points), usage features only | worse than C |
| ifp-2 `O_XHPE` ("opportunity-first") | one target (points); raw production features dropped | rejected on development |
| ifp-2 group `X` (TD vs yardage efficiency) | one target; efficiency split as features | no gain (4.211 vs 4.215) |
| **This study** | **two targets**: realized xFP and points − xFP, a ridge for each, forecast = sum | below |

Ridge regression is linear in its target. With identical features, penalty, recency weights and
training rows, the opportunity model plus the efficiency model is exactly the direct model. This is
proven in `test_summed_forecast_keeps_mean_semantics_and_equals_direct_ridge_on_identical_rows` and
confirmed on real data: OE_C reproduces ifp-2 to three decimals on every window.

A two-target model can therefore differ from the direct model through only three channels. Each
pre-registered candidate isolates one:

| Candidate | Channel | Definition |
|---|---|---|
| OE_C | training population only | ifp-2 features for both components; one shared penalty |
| OE_D | penalty | ifp-2 features; separate penalties, efficiency penalty from {0.3 … 30} and ≥ the opportunity penalty |
| OE_E | features | opportunity: ifp-2 features without raw-production averages; efficiency: player's shrunk yardage/TD efficiency, xFP volume, opponent and team context, home |

Common to all candidates:

* QB is decomposed too.
* Recency weighting and penalties follow ifp-2's per-position settings.
* Penalties are chosen on 2021-22 only.
* Fitting uses squared loss throughout (mean semantics; no median-seeking loss).
* Components are unfloored; only the summed forecast is floored at 0, exactly as the direct model
  floors its own prediction.

## 2. Targets and missing data

* `t_opp`: expected league points of the target game. The league's weights are applied to nflverse
  `ff_opportunity` expected counts for that game's realized opportunities. This is the same `xfpl`
  the feature pipeline uses for past games, so it is in the label's units.
* `t_eff = label − t_opp`. It is negative whenever a player under-performs his opportunity
  (allowed, tested).

**Scoring compatibility.** The league scores 14 skill items:

* 11 have an nflverse expected-count counterpart and enter `t_opp`: passing/rushing/receiving
  yards, TDs and 2-point conversions, receptions, interceptions.
* 3 do not and land in `t_eff`: fumbles lost (−2), special-teams TDs (+6) and fumble-recovery TDs
  (+6).

**Coverage.** nflverse has no `ff_opportunity` row for 21-23% of appearances in every season (2020:
20.9%, 2026: 23.4%). Every one of these 9,309 appearances had **zero carries, targets and pass
attempts**, in a team-week that nflverse does cover. Their mean league points are 0.015; the rare
nonzero values are fumble or return scoring.

Their expected opportunity points are therefore a measured zero, not an unknown. The target rule
reflects this, and it is the same rule `load_panel` already applies to red-zone counts:

* `t_opp = 0` with source `zero_opportunity` only when the player had no opportunity AND the team's
  game is covered.
* Any other absence stays null in both targets and is excluded from both components' training. Two
  cases count as an absence:
  * opportunities with no expected counts;
  * a team-week that nflverse does not cover.
* On the current data this leaves 0% missing. The coverage table by season and position is in the
  report, with nflverse and zero-opportunity counts. For example, 2025 has 2,134 nflverse rows and
  642 zero-opportunity rows for WR, and 1,129 and 550 for TE.

**The naive rule was run first and is kept as an ablation.** It treated every absent xFP as missing
and trained only on players who touched the ball. Under it, every candidate was far worse:

* Development MAE 4.55-4.59 against 4.202, bias −0.90.
* On the zero-opportunity rows, bias −3.4 against −2.05.

This is a large selection effect: excluding structural zeros teaches the components that every
player touches the ball. The corrected rule was defined from a coverage audit, not from outcomes,
and the candidates and decision rules were left unchanged.

**Upstream in-sample bias.** nflverse's expected-points models are fitted by nflverse on historical
play-by-play, so earlier seasons' `t_opp` (and the `xfpl` features) are partly in-sample for that
upstream model. This can only flatter the opportunity component's historical accuracy; it is not a
reason the decomposition failed.

**Abstention.** A component with fewer than 200 training rows abstains, and the sum then abstains
too: there is never a fallback to one component or to another model (tested). Coverage was 97.7%
for every model including ifp-2 (rows without any recent game abstain everywhere), so all
comparisons use identical rows.

## 3. Component validity

These numbers are out-of-sample on the same rows as the points comparison. "Naive" means using
`xfpl_eb` directly as the opportunity forecast.

| | Dev OE_C | Dev OE_D | Dev OE_E | 2023-26 OE_D |
|---|---|---|---|---|
| Opportunity MAE (naive `xfpl_eb`) | 3.308 (4.087) | 3.308 | 3.347 | 3.136 (3.895) |
| Opportunity R² / bias | 0.593 / −0.17 | 0.593 / −0.17 | 0.585 / −0.22 | 0.602 / −0.03 |
| Efficiency MAE (predicting 0) | 2.575 (2.516) | 2.528 (2.516) | 2.527 | 2.440 (2.428) |
| Efficiency R² / bias | −0.003 / −0.03 | 0.004 / −0.02 | 0.007 / −0.01 | 0.003 / +0.07 |
| SD of efficiency forecast (target SD) | 0.56 (4.03) | 0.37 | 0.35 | 0.33 (3.94) |
| Correlation of component errors | −0.06 | −0.06 | −0.06 | −0.04 |
| Var(sum of errors) / (sum of variances) | 0.94 | 0.94 | 0.95 | 0.96 |

What the table shows:

* **Opportunity is predictable.** R² is about 0.6, and the model beats the naive estimators by about
  0.8 points of MAE.
* **Efficiency is not predictable.** No efficiency model beats a constant: R² ≤ 0.007, and MAE is
  worse than predicting zero, because the residual distribution is skewed.
* **Combining the components does not amplify error.** Errors are slightly negatively correlated,
  so the sum's variance is 4-6% below the sum of the variances.
* The decomposition supplies no separate signal. It reproduces the direct regression, with an
  efficiency term that the data shrinks to nearly a constant.

**Where should efficiency shrink?** Development, combined with OE_D's opportunity forecast. Every
method is scored on the same 13,372 rows: a known target, an opportunity forecast, and a value from
every method.

| Efficiency forecast | Efficiency MAE | Bias | Points MAE |
|---|---|---|---|
| Zero | 2.516 | −0.18 | 4.234 |
| Position baseline (recency-weighted mean residual) | 2.521 | −0.01 | 4.197 |
| Player history (shrunk yardage + TD over expected) | 2.765 | −0.05 | 4.299 |
| Position baseline + player history | 2.776 | +0.12 | 4.276 |
| OE_D ridge (penalty 0.3-30) | 2.528 | −0.02 | 4.194 |
| OE_E ridge | 2.527 | −0.01 | 4.195 |

What the shrinkage study shows:

* Shrinking to zero under-projects, because league points have positive non-opportunity scoring.
* The position baseline is about as good as any model.
* A player's own efficiency history makes forecasts worse: past over-performance is mostly noise.
  There is no evidence for a player or "superstar" efficiency bonus.

## 4. Backtest (identical rows, week-clustered CIs vs ifp-2)

**Pooled MAE / RMSE / bias:**

| Window | n | C | ifp-2 | OE_C | OE_D | OE_E |
|---|---|---|---|---|---|---|
| Development 2021-22 | 13,372 | 4.216 | 4.202 / 5.872 / −0.21 | 4.202 (±0.000) | 4.194 / 5.867 / −0.20 (−0.008 [−0.014, −0.002]) | 4.232 (+0.029 [+0.014, +0.046]) |
| 2023-24, inspected | 13,345 | 4.031 | 4.016 | 4.016 | 4.021 (+0.005 [−0.000, +0.009]) | 4.041 (+0.025) |
| 2025-26, inspected | 8,645 | 4.068 | 4.050 | 4.050 | 4.051 (+0.001 [−0.006, +0.008]) | 4.070 (+0.020) |
| 2023-26 pooled | 21,990 | 4.045 | 4.030 / 5.745 / +0.015 | 4.030 | 4.033 / 5.748 / +0.030 (+0.003 [−0.001, +0.007]) | 4.052 (+0.023 [+0.012, +0.034]) |

**By position, OE_D − ifp-2:**

* Development:
  * QB +0.003.
  * RB −0.018 [−0.033, −0.006].
  * WR −0.005.
  * TE −0.007.
* 2023-26:
  * QB −0.001.
  * RB +0.000 [−0.009, +0.008].
  * WR +0.002.
  * TE +0.009 [+0.001, +0.019].

The RB development gain, the most motivated case, does not replicate.

**Start/Sit pairs, accuracy / mean regret:**

| Window | ifp-2 | OE_D | OE_E |
|---|---|---|---|
| Development | 65.37% / 2.523 | 65.43% / 2.519 | 65.11% / 2.555 |
| 2023-26 | 65.47% / 2.492 | 65.41% / 2.500 | 65.25% / 2.515 |

**Cohorts, 2023-26 pooled.** MAE, with bias in parentheses; [CI] is OE_D − ifp-2.

| Cohort | n | ifp-2 | OE_D | OE_E |
|---|---|---|---|---|
| Starters pool | 9,204 | 5.794 (+0.17) | 5.799 (+0.22) [−0.001, +0.012] | 5.812 (+0.33) |
| RB starters pool | 2,832 | 5.582 | 5.593 [−0.003, +0.023] | 5.606 |
| High volume | 1,772 | 6.690 | 6.696 | 6.736 |
| Injury return | 778 | 4.317 | 4.330 [−0.008, +0.033] | 4.362 |
| Role change (role drop or team change) | 5,936 | 3.505 (−0.12) | 3.522 (−0.17) [+0.012, +0.024] | 3.534 |
| Early season (weeks 1-4) | 6,085 | 4.116 | 4.126 [+0.001, +0.018] | 4.150 |
| After a touchdown boom | 535 | 6.169 (+0.23) | 6.153 (+0.33) | 6.094 (+0.99) |
| Low volume (< 5 opportunities) | 14,164 | 3.075 | 3.075 | 3.090 |

Restraining the efficiency features (OE_E) makes the model react less to touchdown booms, with a
lower MAE there, but it under-projects them by about a point and is worse overall. Role changes and
early-season weeks are significantly worse for OE_D: separate penalties loosen the opportunity model
where usage is least stable.

## 5. Decision (`OE_DECISION_RULES`)

* OE_D was selected (lowest development MAE).
* It has a development gain (CI upper bound −0.002) but no decision gain (regret 2.519 vs 2.523,
  under the 1% bar).
* On the 2023-26 replay its pooled MAE is worse (+0.003). That fires a rejection, so the outcome is
  **negative**.
* OE_C equals ifp-2 by construction. OE_E is worse everywhere.
* No research artifact is serialized. The artifact format (`ResearchArtifact`, schema
  `opportunity-efficiency-research/v1`, status `research_only`) is implemented and tested: the
  production loader refuses it, and the format refuses any other status.
* No prospective evidence exists for this method: 2026 week 6 is not final, and the method was
  never added to the frozen weekly pipeline. Nothing here is confirmation evidence, and none is
  claimed.

**Recommendation:** do not continue this decomposition. In particular, do not escalate to a
structural simulator because of it: the bottleneck is that pregame data cannot predict efficiency,
which a finer simulator would not fix. The one transferable lesson is about the data: absent
nflverse xFP for a zero-opportunity appearance is a measured zero. Any future work that uses
game-level xFP as a target must handle that, or it will inherit a large selection bias.

## 6. What changes, and what does not

Research only:

* the module, its script, its tests and this report;
* the two isolation-test allow-lists, which name the new module;
* a test asserting that nothing in `app/` imports it.

Unchanged:

* ifp-1.0.0 and ifp-2.0.0 artifacts and config;
* production projections, the weekly pipeline and ledgers;
* the ESPN/Ours switch, Start/Sit, the API, schedules and dependencies.

Reproduce (about 2.5 minutes on 4 workers; needs the play-by-play exits from
`weekly_learning.py sync-exits`):

```
cd backend
uv run python ../scripts/opportunity_efficiency_research.py run
```

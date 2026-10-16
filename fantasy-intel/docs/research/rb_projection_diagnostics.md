# RB projection diagnostics and pre-registered RB candidates (research only)

Date: 2026-10-16. Status: **research, shadow only.** Nothing here is promoted, read by a consumer, or
changes `ifp-1.0.0`, `ifp-2.0.0`, `MODEL_VERSION`, any artifact or any forecast. Code:
`backend/app/analytics/rb_research.py`, `scripts/rb_research.py`, tests `backend/tests/test_rb_research.py`.

> **Status of this file.** Sections 1-4 (prior art, data, development diagnostics, preregistration) were
> committed *before* any candidate grid or any confirmation-window result was run. Sections 5-7 are added
> afterwards, in later commits, and never edit the preregistration block.

## 1. What was already tried for RBs (and why it did not work)

All four earlier studies use the same feature store, the same scoring fingerprint
(`e165b59686110c7d`) and week-clustered bootstrap CIs. Numbers are RB rows only unless stated.

| Study (status) | What it tried | RB result | Why it failed or stayed small |
|---|---|---|---|
| **ifp-2** (merged, shadow) `docs/research/ifp2_weekly_learning_report.md` | Healthy-game usage, previous-season prior, game-class flags, empirical-Bayes established workload, 36-week recency weights; Huber loss; touchdown vs yardage efficiency split (group X); team-volume context (K) | confirmation 2023-24: -0.009 vs C [-0.022, +0.004]; exploratory 2025-26: -0.030 [-0.049, -0.010] | Gain is ~0.2-0.7%. Huber lowers MAE only by targeting the median (bias +0.2 to +0.6, rejected). X and K: no gain (4.211 / 4.216 vs 4.215). After an injury exit the challenger removes the bias but is no more accurate. |
| **Adaptive workload** (`research/adaptive-workload-state`, rejected) | Exposure-weighted established workload (injury-shortened games count as a fraction of a game), adaptive recent-vs-established blend | vs ifp-2, 2023-26: AW_E +0.001, AW_B +0.004 [-0.001, +0.009] | ifp-2's healthy-games-only `xfpl_eb` is already close to the exposure-correct estimate (exits are ~3% of games). Persistent declines (n >= 2 games below role) help (-0.03) and persistent breakouts hurt (+0.04); the two cancel. |
| **Opportunity + efficiency** (`research/opportunity-efficiency-decomposition`, rejected) | Two ridge targets (expected points, points minus expected points), separate penalties | OE_D development -0.018 [-0.033, -0.006]; 2023-26 +0.000 [-0.008, +0.008] | The efficiency residual is unpredictable (out-of-sample R^2 0.003-0.007); a regularised efficiency forecast collapses to a constant; a player's own efficiency history made forecasts *worse*. The RB development gain did not replicate. |
| **Shallow gradient boosting** (`research/hgb-projection-challenger`, rejected) | Depth-3 HistGradientBoosting on ifp-2's features, 8 configurations | development +0.034 [-0.003, +0.074]; 2023-26 +0.007 [-0.019, +0.031] | Errors correlate 0.98-0.99 with ridge; worse for role drops and team changes (the flags carry little weight in trees); an additive (depth-1) model is clearly worse, so there is no large nonlinearity or interaction ridge misses. |
| First-read share / depth charts (`research/first-read-depth-chart-signals`) | FTN first-read target share, depth-chart tier changes | RB -0.012 to -0.020 from first-read share only; depth *changes* rejected | Small gain from a new data source, still pre-prospective; not an RB-specific model. |
| Point distributions (merged, `docs/research/fantasy_point_distributions.md`) | Conditional ranges | n/a | Documents that **21-40% of RB/WR/TE appearances score exactly 0**; this matters for the mean-versus-median finding below. |

Production context from `docs/independent_projection_engine.md`: RB is the only position whose held-out
result does not beat the better trailing baseline (2025-26: context C 4.10 vs season average 4.07; CI of the
difference [-0.09, +0.12]); the "Known biases" section says low-volume RBs and players with fewer than 3
recent games are worse than simple averages.

What this study does *not* repeat: touchdown/yardage efficiency features (group X), healthy-game windows
(H, U, R, E, Q groups), exposure weighting, regime blending, Huber/robust loss, boosted trees, an
opportunity/efficiency two-target model, recency half-lives.

## 2. Data, protocol and commands

* **nflverse history** 2019-2025 built once with the existing script (feature warm-up 2019, training from
  2020, exactly `forecast_backtest.Protocol`), plus **2026 weeks 1-5** from a *separate* nflverse-only
  database written by the existing `sync_nfl.py` pointed at a scratch file (no league data; the 2026 sync
  stops at week 5), plus play-by-play injury evidence from `weekly_learning.py sync-exits`:

  ```
  cd backend
  NFL_HISTORY_DB_PATH=<scratch>/nfl_history.duckdb uv run python ../scripts/sync_nfl_history.py --seasons 2019-2025
  FANTASY_DB_PATH=<scratch>/nfl_live_2026.duckdb uv run python ../scripts/sync_nfl.py
  uv run python ../scripts/weekly_learning.py sync-exits --history-seasons 2019-2025 --history-db ... --out <scratch>/models/weekly
  uv run python ../scripts/weekly_learning.py sync-exits --season 2026 --live-db ... --out <scratch>/models/weekly
  uv run python ../scripts/rb_research.py prepare | diagnose | develop | confirm   (flags: --live-db --exits-dir --out)
  ```

  The history database was opened read-only after the single build. League scoring is rebuilt from the
  league's ESPN scoring items (no live league database exists here); its fingerprint equals the production
  `e165b59686110c7d` and `prepare` asserts it. ESPN projections are never an input.
* **Store**: 42,745 player-games, 11,165 RB rows (2020: 1,784; 2021: 1,801; 2022: 1,840; 2023: 1,737;
  2024: 1,737; 2025: 1,749; 2026 weeks 1-5: 517). `prepare` re-runs `forecast_backtest.select` (ifp-1.0.0's
  own validation on 2023-24) and asserts RB primary = C at penalty 0.03 (validation MAE 4.015), matching the
  documented ifp-1.0.0 numbers.
* **Models compared, all walk-forward** (each test week is predicted by models fitted on strictly earlier
  weeks only; training rows have at least one recent game and start in 2020): A1 recent average, A2 season
  average, **C** = ifp-1.0.0 context ridge at its frozen penalty, **ifp-2** = frozen RB spec
  (`challenger_config.json`: groups FEQ, 36-week half-life, penalty 0.01).
* **Population and rows**: conditional on the RB appearing (box score or snaps, the repository's
  definition). Every comparison uses the *identical* rows on which every model produced a forecast.
* **Windows**: *development* 2021-2022 (all diagnostics and every hyperparameter choice);
  *confirmation* 2023, 2024, 2025 and 2026 weeks 1-5, one run of the frozen candidates.

**Honest limits of the confirmation window.** Every season 2023-2026w5 was already inspected by earlier
studies for the *reference* models (ifp-1 selection used 2023-24; ifp-1 held-out was 2025-26w5; ifp-2
confirmed on 2023-24), so the reference RB results were known before this study, including that C lost to
the season average on 2025. What is *new* is that none of the three candidates below existed, none was fitted
or evaluated on these seasons, and all choices were frozen on 2021-22. Ridge C's penalty was chosen on
2023-24, which favours C there. There is no fresh holdout in the data: 2026 week 6 has not finished, and
prospective frozen pregame forecasts from week 6 onward are the only clean evidence left.

## 3. Diagnostics (development window 2021-2022 only)

`rb_research.py diagnose`. 3,641 RB appearances, 3,557 on which A1, A2, C and ifp-2 all forecast
(98%; the rest have no game in the lookback window and every model abstains).

**Pooled development MAE / RMSE / bias (mean(actual - forecast))**

| Model | MAE | RMSE | Bias |
|---|---|---|---|
| A1 recent average | 4.511 | 6.422 | -0.12 |
| A2 season average | 4.402 | 6.386 | +0.19 |
| **C** (ifp-1.0.0) | **4.315** | 6.020 | -0.17 |
| ifp-2 | 4.296 | 6.021 | -0.12 |

C - A2 = -0.086 [-0.162, -0.014]; C - A1 = -0.196 [-0.275, -0.125]; ifp-2 - C = -0.020 [-0.039, -0.002].
By season: 2021 A2 4.496 / C 4.446; 2022 A2 4.309 / C 4.188. **On development, ridge beats the season
average overall.** The question is where it does not.

**Where ridge C loses to the season average (C - A2, 95% week-clustered CI).** Only rows with pregame
quantities are used to define a segment; `outcome:` rows describe the game being forecast and are
diagnostics only.

| Segment | n | A1 | A2 | C | ifp-2 | C - A2 | Bias C / A2 |
|---|---|---|---|---|---|---|---|
| rush volume: carries/g < 5 | 1,775 | 2.69 | 2.69 | 2.81 | 2.79 | **+0.122 [+0.050, +0.186]** | -0.19 / +0.46 |
| rush volume: carries/g 5-10 | 707 | 5.23 | 5.17 | 4.94 | 4.93 | -0.224 [-0.408, -0.061] | +0.15 / +0.36 |
| rush volume: carries/g 10-15 | 661 | 6.73 | 6.58 | 6.18 | 6.17 | -0.407 [-0.616, -0.209] | +0.03 / -0.16 |
| rush volume: carries/g >= 15 | 414 | 7.57 | 6.95 | 6.71 | 6.70 | -0.230 [-0.431, -0.013] | -0.94 / -0.69 |
| target volume: targets/g < 1.5 | 1,801 | 3.02 | 2.99 | 3.10 | 3.06 | **+0.105 [+0.035, +0.170]** | +0.02 / +0.81 |
| target volume: targets/g >= 3.5 | 626 | 7.00 | 6.58 | 6.25 | 6.25 | -0.324 [-0.584, -0.079] | -0.52 / -1.00 |
| team rush share < 0.15 | 1,564 | 2.49 | 2.51 | 2.63 | 2.60 | **+0.114 [+0.043, +0.183]** | -0.16 / +0.43 |
| team rush share >= 0.35 | 1,176 | 6.97 | 6.67 | 6.34 | 6.33 | -0.324 [-0.478, -0.167] | -0.12 / -0.10 |
| healthy snap share < 0.30 | 1,760 | 2.79 | 2.80 | 2.90 | 2.88 | **+0.099 [+0.025, +0.165]** | -0.14 / +0.51 |
| snap share 0.30-0.60 | 1,246 | 5.66 | 5.59 | 5.29 | 5.26 | -0.303 [-0.456, -0.153] | -0.09 / -0.09 |
| no inside-5 carries | 1,683 | 2.86 | 2.87 | 2.96 | 2.94 | **+0.095 [+0.010, +0.178]** | -0.12 / +0.55 |
| inside-5 carries/g > 0 | 1,874 | 5.99 | 5.78 | 5.53 | 5.52 | -0.249 [-0.362, -0.144] | -0.21 / -0.13 |
| red-zone opportunities/g >= 3 | 514 | 7.71 | 7.12 | 6.77 | 6.75 | -0.347 [-0.521, -0.147] | -0.77 / -1.03 |
| receiving back (targets/g >= 3, carries/g < 10) | 316 | 5.62 | 5.70 | 5.24 | 5.21 | -0.463 [-0.789, -0.146] | +0.16 / -0.50 |
| early-down back (carries/g >= 10, targets/g < 3) | 504 | 6.59 | 6.50 | 6.07 | 6.04 | -0.431 [-0.619, -0.243] | +0.15 / +0.29 |
| last game a role drop | 277 | 5.02 | 4.03 | 4.16 | 3.89 | +0.126 [-0.133, +0.363] | -0.74 / -0.28 |
| team change (this or last season) | 763 | 4.24 | 4.12 | 4.11 | 4.03 | -0.020 [-0.190, +0.155] | -0.18 / +0.33 |
| last game an injury exit | 74 | 6.04 | 5.71 | 5.32 | 5.45 | -0.397 [-1.019, +0.275] | +0.60 / -0.80 |
| >= 3 weeks since last game | 315 | 3.50 | 3.48 | 3.46 | 3.42 | -0.018 [-0.214, +0.183] | -0.50 / -0.64 |
| fewer than 3 recent games | 161 | 3.81 | 3.81 | 3.50 | 3.50 | -0.310 [-0.644, +0.030] | -0.03 / +0.88 |
| early season (weeks 1-4) | 808 | 4.76 | 4.63 | 4.40 | 4.33 | -0.227 [-0.471, +0.015] | -0.19 / -0.18 |
| mid season (weeks 5-12) | 1,539 | 4.47 | 4.36 | 4.34 | 4.34 | -0.017 [-0.105, +0.065] | -0.09 / +0.41 |
| recent TDs above expectation | 769 | 6.05 | 5.62 | 5.25 | 5.21 | -0.366 [-0.523, -0.205] | -0.57 / -0.83 |
| recent TDs below expectation | 714 | 5.34 | 5.33 | 5.16 | 5.13 | -0.179 [-0.325, -0.032] | +0.32 / +0.90 |
| **starters pool (top 48 by A1)** | 1,728 | 6.44 | 6.21 | 5.88 | 5.87 | **-0.332 [-0.457, -0.215]** | -0.23 / -0.37 |
| high-volume backs (carries + targets /g >= 15) | 815 | 7.37 | 6.93 | 6.63 | 6.64 | -0.293 [-0.479, -0.105] | -0.50 / -0.57 |
| *outcome: scored a touchdown* | 759 | 8.22 | 8.28 | 7.85 | 7.87 | -0.428 [-0.598, -0.225] | +7.22 / +7.34 |
| *outcome: no touchdown* | 2,798 | 3.50 | 3.35 | 3.36 | 3.33 | +0.007 [-0.074, +0.085] | -2.17 / -1.75 |
| *outcome: <= 40% snap share* | 2,223 | 3.24 | 3.14 | 3.27 | 3.23 | +0.131 [+0.072, +0.193] | -1.40 / -0.84 |
| *outcome: zero carries and zero targets* | 661 | 1.73 | 1.41 | 2.18 | 2.07 | +0.767 [+0.625, +0.904] | -2.13 / -1.36 |

**Finding 1: the loss is one segment, low-volume backs.** Ridge loses to the season average exactly where a
back has few opportunities (carries/g < 5, targets/g < 1.5, snap share < 0.30, no inside-5 carries, rush share
< 0.15): about half the rows, each with C - A2 of +0.10 to +0.12 and a CI above 0. Everywhere with real volume
(5+ carries/g, 3.5+ targets/g, 30%+ snaps, any goal-line work, the starters pool) ridge wins by 0.2-0.5 points
with CIs below 0. Role-drop rows (n = 277) lean toward A2 but the CI includes 0, and ifp-2 already recovers
that segment (3.89 vs A2 4.03).

**Finding 2: by opportunities per game (carries + targets, last <= 5 games) the loss is a mean-versus-median
effect.**

| Opp/g | n | Mean / median outcome | Zero-touch share | Mean forecast A2 / C | Slope of outcome on forecast A2 / C | MAE A2 / C | C - A2 |
|---|---|---|---|---|---|---|---|
| 0-2 | 859 | 1.42 / 0.0 | 53% | 0.87 / 1.60 | 0.39 / 0.58 | 1.59 / 1.84 | **+0.252 [+0.176, +0.325]** |
| 2-4 | 409 | 2.91 / 1.4 | 24% | 2.56 / 3.30 | 0.17 / 0.41 | 3.01 / 3.07 | +0.057 [-0.086, +0.218] |
| 4-6 | 369 | 4.94 / 2.8 | 12% | 4.02 / 4.68 | 0.40 / 0.98 | 4.07 / 4.05 | -0.025 [-0.232, +0.170] |
| 6-9 | 427 | 5.96 / 4.0 | 9% | 5.65 / 6.20 | 0.48 / 0.97 | 4.54 / 4.51 | -0.026 [-0.206, +0.161] |
| 9-12 | 355 | 8.41 / 6.9 | 4.5% | 8.30 / 8.28 | 0.44 / 1.12 | 5.53 / 5.25 | -0.278 [-0.582, +0.022] |
| 12-16 | 436 | 10.68 / 9.4 | 2.1% | 10.94 / 10.75 | 0.17 / 0.95 | 6.45 / 5.81 | **-0.639 [-0.935, -0.370]** |
| 16+ | 702 | 14.66 / 13.4 | 0.1% | 15.14 / 15.09 | 0.49 / 0.77 | 6.90 / 6.69 | -0.212 [-0.398, -0.016] |

In the lowest tier more than half of appearances have **no carry and no target** (the outcome median is 0).
The season average (mean forecast 0.87) *under*-forecasts the mean outcome (1.42) by 0.55 and still has the
lower MAE because absolute error rewards a forecast near the median of a spike-at-zero distribution, while ridge
(1.60) is an unbiased-to-slightly-high estimate of the mean. Ridge's calibration slope in this tier (0.58) is
well below 1, so its forecast spreads players more than their outcomes spread, but the MAE gap is mostly
a loss function artefact on low-stakes players. It is not a failure to rank starters: pair decisions and the
starters pool favour ridge.

**Finding 3: touchdown variance.** 21% of RB appearances score a touchdown; touchdown points are 18% of the
variance of RB points. Replacing each game's touchdown points by nflverse expected touchdown points removes
only ~10% of every model's MAE (A2 9.3%, C 10.7%), so touchdown luck is real but not the dominant error source.
Games with a touchdown have MAE ~7.9 and mean error +7.2 for *every* model (nobody predicts the touchdown);
no-touchdown games have mean error -2.2. Is there TD-regression signal ridge misses? In the high-volume group
(8+ opportunities/g, n = 1,612) the error by quintile of recent touchdown-minus-expectation is
+0.37 / +0.27 / +0.57 / -1.45 / -0.42: the pattern is not monotone, and adding the touchdown/yardage
efficiency group (X) to C moves development MAE by +0.006 [-0.005, +0.019] (RB). **No touchdown-regression
candidate is pre-registered**: the evidence points to noise, consistent with the opportunity/efficiency study.

**Finding 4: injury and role segments are small and not where C loses.** Injury-exit rows (n = 74) and
returns (n = 315) show C near or better than A2 with wide CIs; ifp-2 and the adaptive-workload study already
covered healthy-game workload. Early-season rows (weeks 1-4) favour C.

**Finding 5: a pregame fact C has no input for.** C and A2 both under-project RBs whose team has another RB
designated Out or Doubtful on the week-W report (n = 492 of 3,557): bias +0.61 for C and +1.22 for A2
(actual higher than forecast), rising to +1.92 / +2.45 when the missing teammate carried 8+ times per game
(n = 204). C beats A2 there (-0.11), but neither knows the teammate is out. Prior studies and the participation
model never feed teammate availability to the *conditional* points model.

**Where C also fails to beat A2 on development.** Nowhere with CI above 0 except the low-volume segments
above; the 2025 loss reported for held-out data is therefore not visible in 2021-22 aggregates, and this
study does not look at 2025 before the preregistered run.

<!-- PREREG-START -->
## 4. Preregistration (committed before any candidate grid or confirmation result)

**Candidates (at most three, one hypothesis each).** All are RB-only. Everything is point-in-time: features
come from games strictly before the target week (plus the target week's pregame injury report for H2); every
refit uses only weeks strictly before the test week (expanding window from 2020, as ifp-1.0.0). A candidate
abstains exactly where C abstains (no game in the last five). Hyperparameters come from the fixed grids below,
chosen once on the 2021-2022 window by lowest MAE on the rows where every grid point, C and A2 forecast (ties
to the smaller value). No other search is run.

* **H1 `vol_shrink`: ridge is over-dispersed for low-volume backs, so shrink toward the season average by
  volume.** Forecast = w * C + (1 - w) * A2 with w = clip((carries/g + targets/g over the last <= 5 games) /
  tau, 0, 1). If A2 abstains the forecast is C. Grid: tau in {4, 8, 12}. No refit (C and A2 as above).
* **H2 `tm_transfer`: a teammate RB who is Out or Doubtful transfers opportunity to the back.** Ridge on exactly
  model C's RB features plus `tm_out_carries_pg`, `tm_out_targets_pg`, `tm_out_xfpl_pg`: the summed last-<=5-game
  carries, targets and expected league points per game of every OTHER RB whose last game was for the same team
  within the previous 6 NFL weeks (season boundary = 18 weeks) and who is designated Out or Doubtful on the
  week-W nflverse injury report (rows modified at or after kickoff are dropped at load time where the report
  has `date_modified`). 0 when nobody qualifies. Squared loss, no recency weights, floor at 0. Grid: ridge
  penalty in {0.01, 0.03, 0.1}.
* **H3 `hurdle`: the zero-touch spike is a separate, strongly nonlinear event.** Forecast = P(touch) * E[points |
  touch] + (1 - P(touch)) * z0. P(touch) = L2 logistic regression (the repository's `participation.fit_logit`)
  of "had at least one carry or target" on model C's RB features; E[points | touch] = ridge (penalty 0.03, C's
  frozen value) on C's features fitted on touched rows only; z0 = mean points of untouched training rows. The
  labels are past games' box scores (training rows are strictly earlier weeks). Grid: logistic penalty in
  {0.001, 0.01, 0.1}.

**Not pre-registered, on purpose.** A touchdown-regression candidate (Finding 3), healthy-game windows (done by
ifp-2), and anything chosen because one player improves.

**Evaluation (confirmation window, run once).** Test rows: RB appearances 2023, 2024, 2025 and 2026 weeks 1-5
on which A1, A2, C, ifp-2 and all three candidates forecast. Reported: MAE, RMSE, bias; the RB starters pool
(`forecast_backtest.starters_pool_expr`, top 48 by A1); high-volume backs (carries/g + targets/g >= 15);
injury returns (last game an injury exit or >= 3 weeks since the last game); low volume (< 4 opportunities/g);
team changes; early season; rows with an unavailable teammate; Start/Sit pairwise accuracy and regret
(`forecast_backtest.pair_decisions`, RB pool 48); week-clustered bootstrap (1,000 resamples, seed 20261016)
CIs of the MAE difference against C, against A2, and against ifp-2, pooled, by season and by cohort. Because
three candidates are compared, the decision uses the Bonferroni-adjusted interval (alpha = 0.05 / 3,
percentiles 0.83 and 99.17); plain 95% intervals are reported alongside.

**Decision rule (a candidate is `supported_for_shadow_tracking` only if ALL hold; otherwise `not_supported`):**

1. pooled MAE(candidate) - MAE(C) <= -0.02 points AND the adjusted CI upper bound < 0;
2. the difference is < 0 in at least 3 of the 4 sub-windows (2023, 2024, 2025, 2026 weeks 1-5);
3. pooled RMSE no worse than C by more than 0.5%;
4. |bias| no larger than C's by more than 0.15 points;
5. starters-pool MAE no worse than C;
6. Start/Sit pair accuracy no lower than C's by more than 0.5 percentage points;
7. no guard cohort (high-volume backs, injury returns, team changes, low volume) where the candidate is
   significantly worse than C (adjusted CI lower bound > 0).

A candidate is described as *beating the season average* only if the adjusted CI of candidate - A2 is below 0.
A pass means "worth tracking on frozen pregame forecasts"; it is never a promotion. Promotion would need the
existing `PROMOTION_CRITERIA` (3% over the better trailing baseline), prospective frozen weeks and explicit
approval. Negative results will be reported plainly.

**Disclosures made before the grids were run.** While forming H2 on the *development window only*, C with
the three teammate features was fitted once at penalties 0.03 and 0.1 (development MAE 4.271 and 4.282 vs C
4.315; difference -0.044 [-0.059, -0.030] at 0.03), and the touchdown/yardage group X was fitted on RB at
0.03 and 0.1 (no gain). No confirmation-window row was used for any of this. The teammate feature depends on
injury reports: nflverse rows for 2025 and 2026 carry no `date_modified`, so the pregame filter cannot be
verified there (2023-24 can); the report therefore also gives the 2023-24-only result of H2 separately.
<!-- PREREG-END -->

## 5. Development selection (2021-2022)

`rb_research.py develop` ran the three fixed grids on 2021-2022 only (3,557 common-support rows, 36 weeks),
then wrote the frozen configuration once (bound to the SHA-256 of the preregistration block above and to the
fingerprint of every input frame; `confirm` re-checks both and refuses a second run). Development numbers
cannot confirm anything; they only fix the grid points.

| Candidate | Grid (development MAE) | Frozen | Dev MAE (C 4.315) | Candidate - C [95% CI] | RMSE | Bias |
|---|---|---|---|---|---|---|
| `vol_shrink` | tau 4: 4.249, 8: 4.235, **12: 4.226** | tau = 12 (top of the grid) | 4.226 | -0.089 [-0.116, -0.063] | 6.051 (C 6.020) | +0.13 (C -0.17) |
| `tm_transfer` | lambda **0.01: 4.268**, 0.03: 4.271, 0.1: 4.283 | lambda = 0.01 | 4.268 | -0.048 [-0.063, -0.033] | 5.995 | -0.12 |
| `hurdle` | logit lambda 0.001: 4.2958, 0.01: 4.2959, 0.1: 4.2958 | 0.001 (flat) | 4.296 | -0.020 [-0.027, -0.012] | 6.014 | -0.14 |

Reference on the same rows: A2 4.402, ifp-2 4.296 (ifp-2 - C -0.020), so on development all three
candidates beat the season average (adjusted CIs below 0) and `tm_transfer`, `vol_shrink` beat ifp-2.

Things to read before the confirmation numbers:

* `vol_shrink` buys its MAE by moving toward the season average, which is a lower-variance but *biased*
  forecast: bias flips from -0.17 to +0.13, RMSE rises 0.5% (6.020 to 6.051, beyond the 0.5% guard) and the
  starters-pool MAE is no better (5.882 vs 5.880). Its gain is concentrated in the low-volume cohort
  (-0.196 [-0.275, -0.120]) and is zero in the starters pool and high-volume backs by construction (weight 1
  above 12 opportunities per game). The selected tau sits on the grid edge, so a larger tau might do better;
  the preregistered grid is not widened.
* `tm_transfer` improves pooled development MAE, but **not in the subset the hypothesis names**: with a
  teammate Out or Doubtful (n = 492) its MAE is 4.870 against C's 4.818 (+0.052 [-0.089, +0.174]), while the
  gain is in low-volume rows (-0.078 [-0.120, -0.036]) and early-season weeks (-0.028). Its bias there
  falls from +0.61 to -0.15, but the error does not. The pooled rule is what was preregistered, so that is
  what decides; the mechanism check is reported next to it.
* `hurdle` ties ifp-2 on development (4.296) and is flat in its penalty.
* All development decisions list `c2_consistency` as failed only because two seasons cannot satisfy the
  "3 of 4 sub-windows" rule; it is not informative here.

## 6. Confirmation results (2023-2026 weeks 1-5, one run)

`rb_research.py confirm` ran once (the report file is write-once and the command refuses to run again; it
also refuses if the preregistration text or any input frame changed since `develop`). 5,740 RB appearances,
**5,594 common-support rows** in 59 weeks (146 abstain: no game in the lookback; identical for every model);
2023: 1,703, 2024: 1,695, 2025: 1,705, 2026 weeks 1-5: 491.

**Pooled MAE / RMSE / bias (mean(actual - forecast)), identical rows**

| Model | MAE | RMSE | Bias | Mean forecast (outcome mean 6.959) |
|---|---|---|---|---|
| A1 recent average | 4.134 | 6.074 | +0.01 | 6.952 |
| A2 season average | 4.074 | 6.034 | +0.17 | 6.787 |
| **C** (ifp-1.0.0) | 4.047 | 5.762 | -0.05 | 7.006 |
| ifp-2 | 4.030 | 5.756 | -0.00 | 6.961 |
| `vol_shrink` (tau 12) | **3.944** | 5.780 | **+0.26** | 6.702 |
| `tm_transfer` (lambda 0.01) | 4.025 | 5.741 | -0.05 | 7.006 |
| `hurdle` (logit lambda 0.001) | 4.029 | 5.755 | -0.02 | 6.978 |

**Differences in MAE (week-clustered bootstrap, 1,000 resamples, seed 20261016)**

| Comparison | Difference | 95% CI | Bonferroni CI (3 candidates) |
|---|---|---|---|
| C - A2 (the reference gap) | -0.026 | [-0.083, +0.029] | [-0.098, +0.043] |
| ifp-2 - C | -0.017 | [-0.028, -0.006] | [-0.031, -0.003] |
| `vol_shrink` - C | **-0.103** | [-0.123, -0.084] | [-0.128, -0.081] |
| `tm_transfer` - C | **-0.022** | [-0.037, -0.006] | [-0.040, -0.003] |
| `hurdle` - C | **-0.018** | [-0.024, -0.013] | [-0.025, -0.012] |
| `vol_shrink` - A2 | -0.129 | [-0.191, -0.076] | [-0.201, -0.066] |
| `tm_transfer` - A2 | -0.049 | [-0.114, +0.008] | [-0.124, +0.021] |
| `hurdle` - A2 | -0.045 | [-0.102, +0.009] | [-0.116, +0.023] |
| `vol_shrink` - ifp-2 | -0.086 | [-0.106, -0.067] | [-0.109, -0.060] |
| `tm_transfer` - ifp-2 | -0.005 | [-0.023, +0.013] | [-0.025, +0.017] |
| `hurdle` - ifp-2 | -0.001 | [-0.013, +0.010] | [-0.016, +0.012] |

By sub-window (candidate - C, 95% CI): 2023 `vol_shrink` -0.114 [-0.143, -0.084], `tm_transfer` -0.005
[-0.027, +0.020], `hurdle` -0.016 [-0.027, -0.005]; 2024 -0.055 [-0.086, -0.025], -0.026 [-0.060, +0.008], -0.014
[-0.024, -0.006]; 2025 -0.117 [-0.158, -0.075], -0.039 [-0.052, -0.022], -0.021 [-0.031, -0.010]; 2026 weeks 1-5
-0.183 [-0.240, -0.138], -0.007 [-0.059, +0.048], -0.030 [-0.043, -0.022]. By-season MAE of A2 / C: 2023
4.047 / 3.969, 2024 4.099 / 4.061, **2025 4.012 / 4.081**, 2026 4.290 / 4.152.

**Cohorts** (MAE; in brackets the Bonferroni-adjusted CI of candidate - C)

| Cohort | n | A2 | C | ifp-2 | `vol_shrink` | `tm_transfer` | `hurdle` |
|---|---|---|---|---|---|---|---|
| starters pool (top 48 by A1) | 2,832 | 5.869 | 5.603 | 5.582 | 5.606 [-0.011, +0.017] | 5.578 [-0.045, -0.004] | 5.602 [-0.009, +0.006] |
| high-volume backs | 1,402 | 6.518 | 6.290 | 6.262 | 6.290 (identical by construction) | 6.283 [-0.029, +0.011] | 6.270 [-0.033, -0.008] |
| injury returns | 450 | 3.380 | 3.364 | 3.362 | 3.180 [-0.281, -0.084] | 3.332 [-0.108, +0.057] | 3.353 [-0.038, +0.015] |
| low volume (< 4 opp/g) | 1,986 | 1.776 | 1.979 | 1.962 | 1.762 [-0.274, -0.163] | 1.965 [-0.050, +0.027] | 1.926 [-0.067, -0.039] |
| team changes | 1,097 | 3.872 | 3.799 | 3.740 | 3.740 [-0.114, -0.010] | 3.779 [-0.061, +0.028] | 3.788 [-0.025, +0.004] |
| early season (weeks 1-4) | 1,551 | 4.194 | 4.131 | 4.105 | 3.975 [-0.205, -0.113] | 4.131 [-0.030, +0.037] | 4.106 [-0.036, -0.012] |
| a teammate RB Out/Doubtful | 781 | 4.204 | 4.065 | 4.068 | 3.977 [-0.171, +0.002] | **4.202 [+0.004, +0.254]** | 4.052 [-0.036, +0.009] |

**Start/Sit (pairs among the top-48 RBs, 66,330 pairs, 22,475 close calls):** pick accuracy A1 66.4%, A2 67.7%,
C 68.8%, ifp-2 69.0%, `vol_shrink` 68.8%, `tm_transfer` 69.1%, `hurdle` 68.8%; mean regret 2.426 / 2.270 /
2.141 / 2.119 / 2.145 / 2.107 / 2.145; close calls 55.2 / 59.2 / 61.4 / 61.9 / 61.3 / 62.0 / 61.4%. None of the
three candidates moves pair accuracy by more than 0.3 points.

**Decision rule outcome (pre-registered, c1-c7):**

| Candidate | Verdict | Failed checks |
|---|---|---|
| `vol_shrink` | **not_supported** | c4 bias: +0.257 vs C -0.047 (limit 0.197); c5 starters-pool MAE 5.606 vs 5.603 |
| `tm_transfer` | **supported_for_shadow_tracking** (marginally) | none: -0.022 against the -0.02 floor, 4 of 4 sub-windows, RMSE -0.4%, starters pool -0.025, pairs +0.3 points |
| `hurdle` | **not_supported** | c1: -0.018 is smaller than the -0.02 floor (its adjusted CI is below 0 and every other check passes) |

### Post-hoc checks (descriptive, after the single run; they select nothing and change no verdict)

`rb_research.py posthoc` on the same window.

**The development diagnostics replicate out of sample.** C - A2 by volume: carries/g < 5 **+0.161 [+0.113,
+0.215]** (n = 2,699), snap share < 0.30 +0.185 [+0.135, +0.243], no inside-5 carries +0.193 [+0.150, +0.242],
team rush share < 0.15 +0.181 [+0.130, +0.235], fewer than 3 recent games **+0.455 [+0.216, +0.768]**;
with volume it flips: carries/g 10-15 -0.238 [-0.391, -0.103], >= 15 -0.290 [-0.523, -0.087], starters pool
**-0.266 [-0.387, -0.163]**, high-volume backs -0.228 [-0.406, -0.082], recent TDs above expectation -0.404
[-0.607, -0.232]. By opportunities per game: 0-2 (n = 1,307, 57% with no carry or target, outcome median 0)
+0.282 [+0.232, +0.335]; 2-9 about +0.05 to +0.09 (CIs include 0); 9-12 -0.367, 12-16 -0.379, 16+ -0.204
(all CIs below 0). Pooled C - A2 is only -0.026 [-0.083, +0.029], and in **2025 C loses to the season average
(+0.069 [+0.011, +0.118])**, the same pattern the held-out report in `independent_projection_engine.md` showed:
the aggregate gap between C and A2 is a weighted sum of "ridge wins on volume" and "ridge loses on
low-volume backs", and the second term dominates in a season with many low-volume appearances.

**`vol_shrink`: the biggest MAE gain is a loss-function effect, not a better forecast.** The mean forecast
falls to 6.702 against an outcome mean of 6.959 (C: 7.006, ifp-2: 6.961): among low-volume backs (outcome mean
1.78) bias goes from -0.16 (C) to +0.46, close to the season average's +0.55. MAE rewards forecasting near the
median of a distribution with a spike at zero, so `vol_shrink` buys MAE by systematically under-forecasting
the mean. It leaves the starters pool (+0.003) and pair decisions (68.8% vs 68.8%) untouched. It is useful
only if the reported metric is MAE on all appearances; it is the wrong direction for `expected_points`
(`p_play * projected_points`), which needs a mean.

**`tm_transfer`: the gain is real but not where the hypothesis put it.**

| Check | Result |
|---|---|
| Placebo: same model, the three teammate columns shuffled within each week | placebo - C +0.002 [-0.000, +0.004]; `tm_transfer` - placebo -0.024 [-0.039, -0.009]. The gain is not a ridge regularisation artefact. |
| Teammate Out/Doubtful rows (n = 781) | bias drops from +0.91 (C; A2 +1.45) to +0.02, but **MAE rises +0.137 [+0.038, +0.230]** (2023 +0.286, 2024 +0.071, 2025 +0.018, 2026 +0.297). With a teammate carrying 8+ per game (n = 338): bias +1.85 (C) to +0.01, MAE 4.023 to 4.329. |
| Rows with no unavailable teammate (n = 4,813) | **-0.048 [-0.055, -0.041]**; placebo +0.002. All of the pooled gain is here. |
| Timestamp-verified seasons 2023-24 (n = 3,398) | -0.016 [-0.036, +0.007] (Bonferroni CI includes 0) |
| 2025-26 (no `date_modified`, n = 2,196) | -0.032 [-0.049, -0.011] |
| Against ifp-2 / the season average | -0.005 [-0.023, +0.013] / -0.049 [-0.114, +0.008]: no evidence it beats either |

Reading: knowing that a back's teammate is *not* absent removes a contamination term from the ridge (14% of
rows have a teammate out and are bumped up), which sharpens the baseline for the other 86%; for the 14% the
teammate features fix the *mean* (bias to ~0) but widen the error because the size of the bump is uncertain
(a skewed outcome again: mean fixed, MAE worse). The larger gain in 2025-26 comes from seasons whose injury
reports cannot be checked against kickoff, which is why the result is reported with that caveat and not
relied on.

**`hurdle`** is a consistent small gain (-0.016, -0.014, -0.021, -0.030 across the four windows) concentrated in
low-volume backs (-0.053) and indistinguishable from ifp-2 (-0.001 [-0.013, +0.010]); it is neither biased
nor worse anywhere. It is below the pre-set effect floor and has no decision value (pairs 68.76% vs 68.82%).

## 7. Verdict, limitations and open questions

**Verdict (plain).**

1. **Where ridge loses to the season average:** on RBs with little volume (under ~4 carries + targets per
   game, under 30% snaps, no goal-line work, fewer than 3 recent games), about a third of appearances. There the
   outcome is dominated by a spike at zero (57% of the lowest tier have no touch) and a forecast near the
   median beats a forecast at the mean on MAE; ridge is also over-dispersed there (development calibration slope 0.4-0.6). With
   volume ridge beats the season average by 0.2-0.6 points with CIs below zero, in both windows. The
   2025 aggregate loss to the season average is this effect, not a failure on starters.
2. **No RB-specific candidate is a clear improvement.** The only one passing the preregistered rule,
   `tm_transfer`, does so at the effect floor (-0.022 vs -0.02, 0.5% of MAE), its gain does not come from
   the teammate-out mechanism that motivated it, its timestamp-verified slice is not significant, and it
   is not distinguishable from ifp-2. `vol_shrink` improves MAE by 2.5% but only by biasing forecasts down
   (fails the bias and starters guards) and changes no decision. `hurdle` is a 0.4% gain equal to ifp-2.
3. **Nothing is promoted and nothing in production changed.** No candidate approaches the existing 3% bar over
   the better trailing baseline, and ridge C itself does not beat the season average on RB with a CI
   excluding 0 (-0.026 [-0.083, +0.029]); the existing `experimental` RB status stands.
4. **Do not repeat** touchdown regression (no signal on development, group X confirmed null), healthy-game windows
   and exposure weighting (ifp-2, adaptive workload), or non-linear models on the same features (HGB).

**Recommendations (none acted on).** (a) If MAE over all appearances is the headline, report RB accuracy
split at ~4 opportunities per game: the pooled number mixes two regimes. (b) Do not adopt `vol_shrink`-style
shrinkage for `expected_points`; if wanted for a "typical outcome" display it is a median-style number and
should be labelled so. (c) `tm_transfer`: do not add a third shadow model on this evidence; if the owner wants
it, first pin down the 2025-26 injury-report timing (snapshot the report before kickoff in the weekly
pipeline, as `source_provenance` already does for the weekly cycle) and pre-register the teammate features
*added to the ifp-2 specification*, evaluated on prospective weeks 6 onward only.

**Limitations.**

* No fresh holdout: 2023-2026 week 5 had been inspected for the reference models (ridge C's penalty was chosen on
  2023-24, which favours C there). The candidates were frozen on 2021-22 and run once, but a sceptic can
  discount any confirmation on already-seen seasons; prospective frozen forecasts from 2026 week 6 are the
  only clean test.
* Conditional-on-appearing population (as ifp-1.0.0); availability is the participation model's job, and
  `tm_transfer` partly duplicates what that model sees (teammates ahead designated Out/Doubtful).
* Teammate availability uses nflverse injury reports; 2025-26 rows have no `date_modified` so their pregame
  status cannot be verified. The teammate definition (last game for the team within 6 weeks, Out or
  Doubtful) is a fixed choice, not tuned; Questionable players, IR/PUP players without a listing and role
  vacancies from trades are not captured.
* `vol_shrink` hit the top of its preregistered grid (tau = 12) on development; the grid was not widened.
* MAE is the preregistered headline and rewards under-forecasting skewed distributions; RMSE and bias are
  guards, not objectives. Three candidates, one confirmation: the Bonferroni CIs control the family-wise
  rate for the c1 check only.
* ESPN was not compared (no pregame ESPN snapshots exist for completed weeks) and is never an input.

**Open questions.** Why does knowing "no teammate is out" help the other 86% of rows (is it a cleaner baseline,
or a proxy for team health/stability)? Does the teammate feature add anything to ifp-2's richer feature set?
Should the RB headline metric be MAE (median-friendly) or squared error (mean-friendly) given that
`expected_points` multiplies the forecast by a probability? Would a median-style companion forecast for
low-volume backs be useful to the Start/Sit page at all, given it changes no pairwise decision?

**Reproduce** (about a minute of compute after the downloads, modest memory): the commands in section 2, then
`prepare`, `diagnose`, `develop`, `confirm`, `posthoc`. The development and confirmation reports are
`diagnostics.json`, `develop.json`, `frozen.json`, `confirmation_report.json`, `posthoc.json` under `--out`
(git-ignored, not committed). The history, 2026 and play-by-play inputs live outside the repository.

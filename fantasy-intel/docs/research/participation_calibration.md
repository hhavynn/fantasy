# Participation calibration research (ipm-cal-0.1.0, shadow only)

**Question.** Does a post-hoc calibration layer on top of the existing participation model
`ipm-1.0.0` give better P(appears), and does that turn into better unconditional fantasy-point
forecasts (`expected_points = P(appears) x conditional points`)?

**Answer.** For **QB** only: isotonic calibration, refit every week on an expanding window of earlier
out-of-sample predictions. It improves log loss and Brier with week-clustered 95% CIs below 0 in
the development seasons, in each development season separately, and in both diagnostic seasons
(log loss), and it removes most of the known mid-range overconfidence (ECE 0.046 -> 0.015 in
development, 0.042 -> 0.019 in the diagnostic seasons). It does **not** improve fantasy-point
accuracy: unconditional MAE is slightly worse (+0.010 points, within the pre-registered 0.5%
tolerance), RMSE is a little better in development and flat in the diagnostic seasons, and it is
slightly worse for QBs coming off an injury exit or a reserve list (small cohorts, same direction in
both periods). **Platt scaling (plain or shrunk) never helps**, and **no method passes for RB, WR or
TE**: their base probabilities are kept.

**Recommendation.** Keep `ipm-1.0.0` unchanged. Keep collecting the QB isotonic map as a *shadow*
candidate (artifact below) and re-test it on prospective weeks; do not deploy it. It makes QB
probabilities more honest, but it does not yet make QB projections more accurate.

## Data and reproduction

nflverse 2019-2025 (`sync_nfl_history.py`) and 2026 weeks 1-5 (`sync_nfl.py`, nflreadpy 0.1.5),
league scoring = the connected league's skill scoring (full PPR, 4-pt passing TD; fingerprint as
in `selection.json`). The rebuilt pipeline reproduces the documented `ipm-1.0.0` results exactly
(penalties QB 0.003 / RB 0.0001 / WR 0.0001 / TE 0.001; QB held-out n 1,544, Brier 0.112, log loss
0.355, ECE 0.042), so the substrate matches the production research.

    cd backend
    uv run python ../scripts/backtest_independent_projections.py select && ... participation-select
    uv run python ../scripts/participation_calibration.py run

Outputs (git-ignored): `data/models/research/participation_calibration_report.json` (every table
below, aggregates only), `oos_base_<hash>.parquet` (cached out-of-sample base predictions) and the
shadow artifact `participation_calibration_ipm-cal-0.1.0.json`. Runtime ~25 s (base walk-forward
~21 s, calibration walk-forward ~5 s).

## Audit: existing baseline (`ipm-1.0.0`, held-out 2025 + 2026 weeks 1-5)

| Pos | n | Appeared | Brier | Log loss | ECE | Max bin gap | Uncond. MAE / RMSE / bias (expected points) | Status |
|---|---|---|---|---|---|---|---|---|
| QB | 1,544 | 54.9% | 0.1116 | 0.3549 | 0.042 | 0.065 | 4.326 / 6.489 / +0.115 | experimental |
| RB | 2,593 | 84.9% | 0.0707 | 0.2338 | 0.025 | 0.037 | 3.592 / 5.492 / +0.019 | experimental |
| WR | 3,975 | 87.0% | 0.0555 | 0.1980 | 0.012 | 0.031 | 3.713 / 5.469 / -0.157 | validated |
| TE | 2,424 | 87.6% | 0.0535 | 0.1771 | 0.015 | 0.055 | 2.738 / 4.356 / +0.068 | validated |

Appearance rate by pregame designation (same rows):

| Pos | Out/Doubtful n (appeared) | Questionable | No designation | Returning from reserve | After injury exit |
|---|---|---|---|---|---|
| QB | 56 (0%) | 38 (24%) | 1450 (58%) | 33 (36%) | 73 (33%) |
| RB | 90 (0%) | 105 (71%) | 2398 (89%) | 48 (81%) | 82 (68%) |
| WR | 200 (0%) | 194 (69%) | 3581 (93%) | 79 (76%) | 216 (60%) |
| TE | 90 (0%) | 77 (70%) | 2257 (92%) | 54 (57%) | 95 (51%) |

Existing criteria (`PARTICIPATION_CRITERIA`, unchanged): >= 400 held-out rows, Brier below the best
pregame baseline with a CI below 0, ECE <= 0.02, no bin with n >= 100 off by more than 0.08;
expected points need participation `validated` plus a >= 3% unconditional MAE gain. QB and RB fail
only the calibration bar. QB's base reliability shows the problem calibration targets: 0.1-0.2
predicted came true ~25% of the time (underconfident for backups), 0.4-0.6 came true ~36-43%
(overconfident in the middle), the extremes are fine.

Audit notes:

* Live inference (`participation.predict_week`) and serialization (`ParticipationArtifact`) apply
  the raw logistic probability; there is no calibration step anywhere today.
* `participation_features` sets every designation flag to 0 for a team with **no** week-W report
  as soon as the injury table exists, so "no report published" looks like "healthy". The model is
  unchanged here; the research cohorts below keep the two apart (`no_team_injury_report`).

## Protocol and leakage safeguards

1. **Out-of-sample base predictions only.** For every week j from 2021 on, the frozen `ipm-1.0.0`
   penalty is refit on participation rows with key < j and predicts week j
   (`participation.walk_forward`). In-sample fitted probabilities are never calibrated (tested:
   each cached prediction equals a model fitted only on earlier weeks, and differs from an
   in-sample fit).
2. **Calibration from earlier weeks only.** For test week k, every map is fitted on out-of-sample
   predictions with key < k (asserted). Flipping every outcome at or after k leaves week k's
   calibrated probabilities unchanged (tested).
3. **Windows.** `expanding` (all out-of-sample weeks since 2021) or `rolling` (last 36 weeks).
4. **Fitting, selection and evaluation are separate.** Maps are fitted per week; the selection rule
   reads the development seasons (2023-2024, the seasons `ipm-1.0.0` itself used for its penalty)
   only; 2025-2026 are reported as **diagnostics**: they were inspected by the `ipm-1.0.0` work and
   are not an untouched holdout.
5. **Small candidate set.** A uncalibrated, B Platt, C isotonic (blocks >= 50 rows, edge rate
   outside the data range, no extrapolation), D Platt with a Gaussian prior on the identity map
   (precision 25), each with two windows. < 300 calibration rows or a single outcome class: the base
   probability is kept. Clipping to [1e-4, 1 - 1e-4] as in `participation.log_loss`. No
   hand-tuned designation probabilities: every method is a 1-D map of the base probability.
6. **Pre-registered selection rule** (`SELECTION_RULES`): >= 400 development rows; week-clustered
   bootstrap 95% CI of both the log-loss and Brier differences below 0; unconditional MAE not worse
   than the base by more than 0.5%; log loss better than the base in every development season.
   Lowest development log loss among passing candidates, else the base.

## Results

All comparisons are on identical player-weeks (every candidate and the conditional estimate
present); CIs resample whole NFL weeks (1,000 resamples). Unconditional points = calibrated
P(appears) x the same walk-forward conditional `ifp-1.0.0` estimate; actual = league points, 0 if
he did not appear. Fallback rate to the base probability: 0% (every position always had >= 300
earlier calibration rows).
#### Development seasons 2023-2024 (selection)


**QB** (n = 2,494 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.1215 | 0.3806 | 0.046 | 0.096 | - | - | 4.098 / 6.117 / +0.216 | - |
| B Platt (expanding) | 0.1215 | 0.3810 | 0.046 | 0.132 | +0.0004 [-0.0007, +0.0015] | -0.0000 [-0.0003, +0.0003] | 4.121 / 6.126 / +0.228 | +0.0238 [+0.0200, +0.0277] |
| B Platt (rolling 36 wk) | 0.1217 | 0.3816 | 0.046 | 0.121 | +0.0010 [-0.0009, +0.0029] | +0.0002 [-0.0003, +0.0008] | 4.137 / 6.131 / +0.209 | +0.0395 [+0.0314, +0.0474] |
| D shrunk Platt (expanding) | 0.1215 | 0.3810 | 0.046 | 0.114 | +0.0004 [-0.0007, +0.0014] | -0.0000 [-0.0003, +0.0003] | 4.121 / 6.126 / +0.228 | +0.0232 [+0.0195, +0.0271] |
| D shrunk Platt (rolling) | 0.1216 | 0.3815 | 0.046 | 0.121 | +0.0009 [-0.0009, +0.0028] | +0.0002 [-0.0003, +0.0007] | 4.135 / 6.130 / +0.211 | +0.0378 [+0.0301, +0.0453] |
| C isotonic (expanding) | 0.1189 | 0.3718 | 0.015 | 0.025 | -0.0088 [-0.0145, -0.0032] | -0.0026 [-0.0044, -0.0006] | 4.108 / 6.102 / +0.214 | +0.0107 [-0.0048, +0.0261] |
| C isotonic (rolling) | 0.1193 | 0.3729 | 0.014 | 0.037 | -0.0077 [-0.0136, -0.0022] | -0.0022 [-0.0041, -0.0001] | 4.125 / 6.109 / +0.194 | +0.0272 [+0.0125, +0.0419] |

**RB** (n = 4,026 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0799 | 0.2580 | 0.014 | 0.060 | - | - | 3.583 / 5.357 / -0.021 | - |
| B Platt (expanding) | 0.0799 | 0.2586 | 0.016 | 0.048 | +0.0006 [+0.0001, +0.0011] | +0.0000 [-0.0001, +0.0001] | 3.580 / 5.357 / -0.007 | -0.0025 [-0.0035, -0.0016] |
| B Platt (rolling 36 wk) | 0.0800 | 0.2583 | 0.015 | 0.053 | +0.0003 [-0.0003, +0.0010] | +0.0001 [-0.0000, +0.0003] | 3.583 / 5.357 / -0.023 | +0.0004 [-0.0014, +0.0020] |
| D shrunk Platt (expanding) | 0.0799 | 0.2586 | 0.016 | 0.049 | +0.0006 [+0.0001, +0.0011] | +0.0000 [-0.0001, +0.0001] | 3.580 / 5.357 / -0.007 | -0.0026 [-0.0036, -0.0017] |
| D shrunk Platt (rolling) | 0.0800 | 0.2583 | 0.015 | 0.057 | +0.0003 [-0.0003, +0.0009] | +0.0001 [-0.0000, +0.0003] | 3.583 / 5.357 / -0.023 | +0.0004 [-0.0014, +0.0020] |
| C isotonic (expanding) | 0.0800 | 0.2575 | 0.011 | 0.032 | -0.0005 [-0.0022, +0.0013] | +0.0001 [-0.0004, +0.0005] | 3.582 / 5.354 / -0.028 | -0.0007 [-0.0046, +0.0033] |
| C isotonic (rolling) | 0.0802 | 0.2580 | 0.007 | 0.018 | -0.0000 [-0.0019, +0.0021] | +0.0003 [-0.0002, +0.0008] | 3.585 / 5.355 / -0.043 | +0.0025 [-0.0014, +0.0062] |

**WR** (n = 6,171 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0589 | 0.2010 | 0.015 | 0.113 | - | - | 3.862 / 5.691 / -0.043 | - |
| B Platt (expanding) | 0.0589 | 0.2017 | 0.017 | 0.144 | +0.0008 [-0.0000, +0.0016] | +0.0000 [-0.0003, +0.0004] | 3.867 / 5.693 / -0.044 | +0.0051 [+0.0037, +0.0065] |
| B Platt (rolling 36 wk) | 0.0589 | 0.2014 | 0.015 | 0.131 | +0.0004 [-0.0001, +0.0010] | +0.0001 [-0.0001, +0.0003] | 3.865 / 5.692 / -0.047 | +0.0027 [+0.0017, +0.0038] |
| D shrunk Platt (expanding) | 0.0589 | 0.2016 | 0.016 | 0.143 | +0.0006 [-0.0001, +0.0014] | +0.0000 [-0.0003, +0.0003] | 3.867 / 5.693 / -0.043 | +0.0043 [+0.0031, +0.0056] |
| D shrunk Platt (rolling) | 0.0589 | 0.2013 | 0.015 | 0.136 | +0.0004 [-0.0001, +0.0009] | +0.0001 [-0.0001, +0.0002] | 3.865 / 5.692 / -0.046 | +0.0023 [+0.0014, +0.0033] |
| C isotonic (expanding) | 0.0586 | 0.2001 | 0.010 | 0.068 | -0.0009 [-0.0025, +0.0007] | -0.0003 [-0.0008, +0.0002] | 3.867 / 5.690 / -0.058 | +0.0044 [+0.0025, +0.0064] |
| C isotonic (rolling) | 0.0585 | 0.1998 | 0.009 | 0.083 | -0.0012 [-0.0033, +0.0008] | -0.0004 [-0.0008, +0.0000] | 3.864 / 5.689 / -0.060 | +0.0020 [+0.0001, +0.0042] |

**TE** (n = 3,614 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0471 | 0.1678 | 0.009 | 0.022 | - | - | 2.595 / 4.040 / +0.060 | - |
| B Platt (expanding) | 0.0471 | 0.1684 | 0.012 | 0.028 | +0.0006 [-0.0006, +0.0019] | -0.0000 [-0.0004, +0.0004] | 2.599 / 4.040 / +0.058 | +0.0036 [+0.0022, +0.0049] |
| B Platt (rolling 36 wk) | 0.0470 | 0.1683 | 0.012 | 0.038 | +0.0005 [-0.0003, +0.0015] | -0.0001 [-0.0004, +0.0003] | 2.598 / 4.040 / +0.059 | +0.0021 [+0.0008, +0.0032] |
| D shrunk Platt (expanding) | 0.0471 | 0.1683 | 0.013 | 0.031 | +0.0005 [-0.0006, +0.0016] | -0.0000 [-0.0003, +0.0003] | 2.598 / 4.040 / +0.060 | +0.0028 [+0.0016, +0.0039] |
| D shrunk Platt (rolling) | 0.0470 | 0.1683 | 0.011 | 0.035 | +0.0004 [-0.0003, +0.0013] | -0.0001 [-0.0003, +0.0002] | 2.597 / 4.040 / +0.060 | +0.0014 [+0.0003, +0.0023] |
| C isotonic (expanding) | 0.0472 | 0.1662 | 0.007 | 0.007 | -0.0016 [-0.0055, +0.0011] | +0.0001 [-0.0004, +0.0006] | 2.599 / 4.039 / +0.053 | +0.0040 [+0.0022, +0.0058] |
| C isotonic (rolling) | 0.0472 | 0.1664 | 0.004 | 0.003 | -0.0014 [-0.0050, +0.0013] | +0.0001 [-0.0004, +0.0006] | 2.597 / 4.039 / +0.055 | +0.0020 [+0.0006, +0.0035] |

#### Diagnostic seasons 2025 + 2026 wk 1-5 (already inspected; never used to choose)


**QB** (n = 1,544 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.1116 | 0.3549 | 0.042 | 0.065 | - | - | 4.326 / 6.489 / +0.115 | - |
| B Platt (expanding) | 0.1118 | 0.3554 | 0.044 | 0.066 | +0.0005 [-0.0008, +0.0018] | +0.0001 [-0.0002, +0.0005] | 4.353 / 6.495 / +0.103 | +0.0270 [+0.0236, +0.0301] |
| B Platt (rolling 36 wk) | 0.1119 | 0.3551 | 0.041 | 0.052 | +0.0002 [-0.0012, +0.0016] | +0.0003 [-0.0001, +0.0008] | 4.359 / 6.491 / +0.065 | +0.0334 [+0.0294, +0.0375] |
| D shrunk Platt (expanding) | 0.1118 | 0.3554 | 0.044 | 0.066 | +0.0005 [-0.0008, +0.0018] | +0.0001 [-0.0002, +0.0005] | 4.352 / 6.495 / +0.104 | +0.0264 [+0.0231, +0.0294] |
| D shrunk Platt (rolling) | 0.1119 | 0.3551 | 0.042 | 0.053 | +0.0002 [-0.0011, +0.0015] | +0.0003 [-0.0001, +0.0007] | 4.357 / 6.491 / +0.070 | +0.0314 [+0.0277, +0.0352] |
| C isotonic (expanding) | 0.1092 | 0.3471 | 0.019 | 0.038 | -0.0078 [-0.0148, -0.0008] | -0.0024 [-0.0046, +0.0001] | 4.336 / 6.492 / +0.059 | +0.0096 [-0.0163, +0.0349] |
| C isotonic (rolling) | 0.1091 | 0.3477 | 0.018 | 0.021 | -0.0072 [-0.0161, +0.0038] | -0.0025 [-0.0050, +0.0002] | 4.341 / 6.491 / +0.003 | +0.0146 [-0.0135, +0.0427] |

**RB** (n = 2,593 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0707 | 0.2338 | 0.025 | 0.037 | - | - | 3.592 / 5.492 / +0.019 | - |
| B Platt (expanding) | 0.0707 | 0.2337 | 0.023 | 0.031 | -0.0001 [-0.0003, +0.0002] | -0.0000 [-0.0001, +0.0001] | 3.593 / 5.492 / +0.016 | +0.0012 [+0.0006, +0.0019] |
| B Platt (rolling 36 wk) | 0.0707 | 0.2330 | 0.024 | 0.054 | -0.0008 [-0.0020, +0.0004] | +0.0001 [-0.0002, +0.0003] | 3.600 / 5.493 / -0.011 | +0.0081 [+0.0058, +0.0101] |
| D shrunk Platt (expanding) | 0.0707 | 0.2337 | 0.023 | 0.031 | -0.0001 [-0.0003, +0.0002] | -0.0000 [-0.0001, +0.0000] | 3.593 / 5.492 / +0.016 | +0.0012 [+0.0005, +0.0018] |
| D shrunk Platt (rolling) | 0.0707 | 0.2330 | 0.024 | 0.052 | -0.0008 [-0.0020, +0.0004] | +0.0001 [-0.0002, +0.0003] | 3.600 / 5.493 / -0.011 | +0.0079 [+0.0057, +0.0098] |
| C isotonic (expanding) | 0.0704 | 0.2327 | 0.016 | 0.088 | -0.0010 [-0.0042, +0.0037] | -0.0003 [-0.0007, +0.0002] | 3.596 / 5.488 / -0.006 | +0.0034 [-0.0006, +0.0070] |
| C isotonic (rolling) | 0.0705 | 0.2311 | 0.013 | 0.013 | -0.0027 [-0.0077, +0.0034] | -0.0002 [-0.0005, +0.0001] | 3.601 / 5.489 / -0.032 | +0.0090 [+0.0055, +0.0127] |

**WR** (n = 3,975 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0555 | 0.1980 | 0.012 | 0.031 | - | - | 3.713 / 5.469 / -0.157 | - |
| B Platt (expanding) | 0.0555 | 0.1981 | 0.013 | 0.034 | +0.0001 [-0.0003, +0.0005] | -0.0000 [-0.0002, +0.0002] | 3.715 / 5.470 / -0.158 | +0.0024 [+0.0016, +0.0030] |
| B Platt (rolling 36 wk) | 0.0556 | 0.1981 | 0.013 | 0.036 | +0.0001 [-0.0002, +0.0004] | +0.0001 [-0.0001, +0.0002] | 3.712 / 5.470 / -0.158 | -0.0008 [-0.0013, -0.0001] |
| D shrunk Platt (expanding) | 0.0555 | 0.1981 | 0.013 | 0.034 | +0.0001 [-0.0003, +0.0005] | -0.0000 [-0.0002, +0.0002] | 3.715 / 5.470 / -0.158 | +0.0022 [+0.0014, +0.0027] |
| D shrunk Platt (rolling) | 0.0556 | 0.1981 | 0.013 | 0.036 | +0.0001 [-0.0002, +0.0004] | +0.0000 [-0.0001, +0.0001] | 3.712 / 5.470 / -0.158 | -0.0005 [-0.0011, +0.0001] |
| C isotonic (expanding) | 0.0559 | 0.1994 | 0.009 | 0.009 | +0.0014 [-0.0009, +0.0047] | +0.0003 [-0.0001, +0.0008] | 3.716 / 5.469 / -0.170 | +0.0030 [+0.0009, +0.0050] |
| C isotonic (rolling) | 0.0560 | 0.2000 | 0.009 | 0.089 | +0.0019 [-0.0008, +0.0066] | +0.0004 [+0.0001, +0.0009] | 3.712 / 5.469 / -0.169 | -0.0004 [-0.0026, +0.0021] |

**TE** (n = 2,424 identical player-weeks)

| Candidate | Brier | Log loss | ECE | Max bin gap (n>=100) | Δ log loss vs A (95% CI) | Δ Brier vs A (95% CI) | Uncond. MAE / RMSE / bias | Δ uncond. MAE vs A (95% CI) |
|---|---|---|---|---|---|---|---|---|
| A uncalibrated | 0.0535 | 0.1771 | 0.015 | 0.055 | - | - | 2.738 / 4.356 / +0.068 | - |
| B Platt (expanding) | 0.0537 | 0.1779 | 0.016 | 0.059 | +0.0008 [-0.0000, +0.0018] | +0.0001 [-0.0002, +0.0005] | 2.740 / 4.355 / +0.067 | +0.0021 [+0.0015, +0.0026] |
| B Platt (rolling 36 wk) | 0.0538 | 0.1775 | 0.016 | 0.062 | +0.0005 [-0.0000, +0.0011] | +0.0002 [+0.0001, +0.0004] | 2.738 / 4.356 / +0.065 | +0.0003 [-0.0003, +0.0008] |
| D shrunk Platt (expanding) | 0.0536 | 0.1778 | 0.017 | 0.059 | +0.0007 [-0.0001, +0.0015] | +0.0001 [-0.0002, +0.0004] | 2.740 / 4.355 / +0.067 | +0.0018 [+0.0013, +0.0023] |
| D shrunk Platt (rolling) | 0.0537 | 0.1775 | 0.017 | 0.067 | +0.0004 [-0.0001, +0.0009] | +0.0002 [+0.0001, +0.0003] | 2.738 / 4.356 / +0.065 | +0.0004 [-0.0001, +0.0008] |
| C isotonic (expanding) | 0.0531 | 0.1744 | 0.014 | 0.060 | -0.0026 [-0.0045, -0.0009] | -0.0005 [-0.0011, +0.0002] | 2.738 / 4.353 / +0.064 | +0.0001 [-0.0021, +0.0019] |
| C isotonic (rolling) | 0.0533 | 0.1739 | 0.010 | 0.037 | -0.0032 [-0.0052, -0.0010] | -0.0002 [-0.0006, +0.0002] | 2.737 / 4.355 / +0.060 | -0.0010 [-0.0032, +0.0009] |

#### QB cohorts: A uncalibrated vs C isotonic (expanding)

| Cohort | Dev n | Dev played | Dev Brier A -> C | Dev log loss A -> C | Diag n | Diag played | Diag Brier A -> C | Diag log loss A -> C |
|---|---|---|---|---|---|---|---|---|
| designated_questionable | 75 | 37% | 0.1715 -> 0.1590 | 0.5078 -> 0.4835 | 38 | 24% | 0.1601 -> 0.1528 | 0.5043 -> 0.5380 |
| designated_doubtful_or_out | 50 | 0% | 0.0024 -> 0.0044 | 0.0274 -> 0.0267 | 56 | 0% | 0.0009 -> 0.0019 | 0.0188 -> 0.0134 |
| returning_from_reserve | 97 | 22% | 0.1081 -> 0.1143 | 0.3546 -> 0.3748 | 33 | 36% | 0.1361 -> 0.1363 | 0.4248 -> 0.4285 |
| prior_injury_exit | 133 | 29% | 0.1138 -> 0.1181 | 0.3574 -> 0.3674 | 73 | 33% | 0.0988 -> 0.1110 | 0.3176 -> 0.3457 |
| week_1 | 124 | 56% | 0.1864 -> 0.1924 | 0.5483 -> 0.5634 | 127 | 52% | 0.1361 -> 0.1314 | 0.4339 -> 0.4246 |
| early_season_weeks_1_4 | 536 | 56% | 0.1253 -> 0.1232 | 0.3935 -> 0.3873 | 533 | 54% | 0.1069 -> 0.1030 | 0.3425 -> 0.3304 |
| midseason_weeks_5_13 | 1202 | 54% | 0.1158 -> 0.1124 | 0.3634 -> 0.3514 | 645 | 55% | 0.1131 -> 0.1111 | 0.3572 -> 0.3481 |
| late_season_weeks_14_plus | 756 | 53% | 0.1279 -> 0.1262 | 0.3987 -> 0.3932 | 366 | 55% | 0.1158 -> 0.1149 | 0.3689 -> 0.3695 |
| low_history_lt3_games | 347 | 22% | 0.1224 -> 0.1210 | 0.3877 -> 0.3796 | 175 | 26% | 0.1694 -> 0.1635 | 0.5201 -> 0.5230 |
| no_designation_report_published | 2364 | 56% | 0.1223 -> 0.1200 | 0.3838 -> 0.3754 | 1450 | 58% | 0.1146 -> 0.1122 | 0.3640 -> 0.3550 |
| no_team_injury_report | 5 | 20% | 0.1687 -> 0.1504 | 0.4917 -> 0.4610 | 0 | - | - | - |

#### QB reliability (development), A -> C

| Bin | A n | A predicted | A observed | C n | C predicted | C observed |
|---|---|---|---|---|---|---|
| 0.0-0.1 | 364 | 0.056 | 0.069 | 243 | 0.032 | 0.037 |
| 0.1-0.2 | 379 | 0.149 | 0.245 | 225 | 0.172 | 0.160 |
| 0.2-0.3 | 231 | 0.244 | 0.268 | 564 | 0.246 | 0.266 |
| 0.3-0.4 | 159 | 0.347 | 0.258 | 229 | 0.334 | 0.314 |
| 0.4-0.5 | 123 | 0.447 | 0.358 | 102 | 0.451 | 0.461 |
| 0.5-0.6 | 93 | 0.554 | 0.430 | 65 | 0.543 | 0.523 |
| 0.6-0.7 | 87 | 0.644 | 0.563 | 48 | 0.660 | 0.688 |
| 0.7-0.8 | 94 | 0.756 | 0.734 | 75 | 0.744 | 0.787 |
| 0.8-0.9 | 177 | 0.857 | 0.876 | 178 | 0.862 | 0.888 |
| 0.9-1.0 | 787 | 0.955 | 0.977 | 765 | 0.971 | 0.979 |


## Interpretation

* **Why Platt fails.** QB miscalibration is not a sigmoid distortion: the model is underconfident
  for backups (0.1-0.2) and overconfident in the middle (0.3-0.7) at the same time. A monotone
  2-parameter logistic map cannot fix both, so it only adds noise. Isotonic can.
* **Probabilities vs points.** Better probabilities did not give better QB fantasy points: the
  calibrated expected points have slightly worse MAE and about equal RMSE. Expected points is a
  mean forecast (it minimizes squared error, not absolute error), and the biggest probability
  gains come from backups whose expected points are small either way. The decision-relevant gain is
  therefore small.
* **Cohorts.** Questionable QBs: better in development, mixed in the diagnostic seasons. QBs after
  an injury exit or returning from a reserve list: slightly worse in both periods (n 33-133). Week 1:
  mixed. No cohort is large enough to claim an effect on its own.
* **RB / WR / TE.** Isotonic lowers ECE for all three but its log-loss and Brier CIs include 0;
  Platt is neutral or worse. Their base probabilities stay.

## Shadow artifact

`participation_calibration_ipm-cal-0.1.0.json` (`participation-calibration/v1`, `status: shadow`):
base model identity (`ipm-1.0.0`, penalties, conditional spec, participation-selection sha), per
position the selected map (QB isotonic with 31 knots, every block >= 50 rows; RB/WR/TE method
`none`), training cutoff (last out-of-sample week, 2026 week 5), calibration sample counts (QB
6,440, RB 10,930, WR 16,299, TE 9,644), scoring fingerprint, data hash and the development
selection. `CalibrationArtifact.from_dict` refuses any status other than `shadow`. Nothing loads
it: not the API, not `weekly_learning`, not ESPN/Ours, not Start/Sit (tested).

## Limitations

* The diagnostic seasons were seen before; only prospective weeks are new evidence.
* The base penalty was chosen on the same development seasons the calibration selection reads.
* Week-clustered CIs treat weeks as independent; season-level drift is only checked by the
  per-season rule.
* Calibration is per position, not per cohort; designation-specific miscalibration is only
  reported, never fitted (by design).

# Rest-of-season availability for players injured at the cutoff (ros-avail-0.1.0, research only)

**Question.** A rest-of-season (ROS) run (`independent_projection_store.build_live(..., projected_from_week=W)`) gives
every future week the participation model's *undesignated* p_play (`participation.predict_week(..., feature_week=W)`).
A player ruled Out at W therefore looks healthy from W+1 on. Live example, 2026 cutoff week 6: Saquon Barkley
p 0.93 and Breece Hall p 0.96 for week 7. Would the empirical return-rate table (`injury_workload.fit_return_rates`,
PR hhavynn/fantasy#103) be a better p_play for those players, at horizons 1-4 and in ROS expected-points totals?

**Answer: not as pre-registered. The verdict is `retain_current`, so there is no promotion proposal.** Production is
unchanged.

* **h = 1 (next team game): the table is far better.** Brier 0.496 -> 0.246, diff -0.250 [-0.297, -0.194], better
  in both seasons and in every subgroup. It still fails the calibration gate narrowly: ECE 0.068 > 0.06, because it
  under-predicts (mean 0.34 vs observed 0.41).
* **h = 2: better Brier** (-0.048 [-0.088, -0.007]). It fails ECE (0.129), max bin gap (0.144) and one subgroup
  (missed 2-3 games).
* **h = 3 and 4: worse than the current path** (Brier +0.025 and +0.020). Observed return rates (0.78, 0.79) are far
  above the table (0.60, 0.63). The current path's 0.91 is closer.
* **ROS totals (next 4 games): MAE -0.37 [-1.17, +0.48], not significant.** The bias flips from -5.4 (current
  over-projects) to +5.6 (candidate under-projects).
* **Why (diagnosed after the verdict; changes nothing above).**
  * The table was fitted on every Out/Doubtful episode. 12-24% of those players were already on a reserve list
    at W. They almost never return within two games, and `build_live` withholds them anyway, so they never
    receive a ROS p_play. Mixing them in pulls the table down.
  * Even with matched populations, return rates at h >= 2 rose steadily from 2019 to 2024 (h = 3: 0.59 -> 0.80).
    A table pooled over earlier seasons lags that trend.
* **Exploratory 2025 replay (not gated): the candidate wins everywhere,** including ROS totals (MAE -3.97
  [-5.08, -2.88]). 2025 return rates fell back toward the 2019-22 level. The h >= 3 rates move too much from
  season to season for a pooled table to be reliable there.

**Next.** `ros-avail-0.1.1` is pre-registered in code (`ros_availability.NEXT_CANDIDATE`, hash pinned in tests).
It changes three things: the fit population matches the application population, the table applies at h = 1-2 only,
and the table is refit at every cutoff. Its design used the 0.1.0 confirmation, so it can only be judged on
prospective weeks (a write-once pregame ledger from the 2026 week-7 cutoff). That is a proposed follow-up, not done
here.

Code:

* `backend/app/analytics/ros_availability.py`: candidate, evaluation rows, metrics, frozen gates, `NEXT_CANDIDATE`.
* `scripts/ros_availability_research.py`: `evaluate` (confirmation + exploratory) and `live` (2026 week-6 cutoff).
* `backend/tests/test_ros_availability.py`.

Nothing in the app imports the module (tested). `build_live`, `projection_source.py`, the model registry, approved
artifacts, production runs and participation `ipm-1.0.0` are untouched.

## 1. Pre-registration

`DECISION_RULES` and `GATE_LIMITS` were committed on their own (commit `9f16505`, rules hash `db1ad0df05ec5216`, pinned
in `test_rules_are_frozen`) before the first evaluation run. Two things were known in advance from
`injury_workload_handoff.md` section 6: the h = 1 means, and the per-h rates of a fixed 2019-22 table on 2023-24. The
limits were set from the sampling noise of ~100-episode bins (sd of an observed rate ~0.05), not from those numbers.

**Population.** RB/WR/TE designated Out or Doubtful on the week-W pregame report, W = 2..17. Each player must have a
lookback appearance and be in the current path's participation population (latest roster row <= W is ACT/INA). His
team's h-th game after W (byes skipped) must be final. The outcome is whether he appeared.

**Comparison.** On identical rows:

* `p_current`: `predict_week(sources, ipm-1.0.0, season, T, feature_week=W)`, exactly what `build_live` gives today.
* `p_candidate`: the return-rate table, refit per season on 2019..season-1, with h > 4 using the h = 4 cell.

`ipm-1.0.0` was trained on seasons that include 2023-24 (its held-out seasons are 2025-26), so the current path is
in-sample in the confirmation. That is conservative for the candidate.

**Gates.** Each gate holds at every h = 1..4 (pooled 2023-24):

| Gate | Rule |
|---|---|
| c1 | Brier diff < 0, cutoff-week bootstrap 95% CI upper < 0 |
| c2 | ECE(candidate) <= 0.06 (10 equal-width bins) |
| c3 | max bin gap (bins n >= 40) <= 0.12 |
| c4 | ECE(candidate) < ECE(current) |
| c5 | Brier(candidate) < Brier(current) in 2023 and in 2024 separately |
| c6 | every position / missed bucket / designation subgroup with n >= 40: Brier(candidate) <= Brier(current) + 0.01 |
| r1 | ROS total over the next 4 team games (sum of p_h x ifp-2 conditional_h; actual 0 when he did not play): MAE diff CI upper < 0 |
| r2 | \|bias(candidate)\| <= \|bias(current)\| on those totals |

The conditional projection for the totals is the ifp-2 ridge, fitted walk-forward (frozen config, rows before W) on
week-W features against week T's opponent. It is identical under both paths, so only p_play moves.

## 2. Results

**Confirmation 2023-24** (run once). Pairs are current / candidate:

| h | n | Observed | Mean p | Brier | Brier diff [95% CI] | ECE | Max bin gap |
|---|---|---|---|---|---|---|---|
| 1 | 445 | 0.409 | 0.908 / 0.341 | 0.496 / **0.246** | **-0.250 [-0.297, -0.194]** | 0.499 / 0.068 | 0.544 / 0.090 |
| 2 | 412 | 0.653 | 0.909 / 0.525 | 0.290 / **0.242** | **-0.048 [-0.088, -0.007]** | 0.258 / 0.129 | 0.284 / 0.144 |
| 3 | 375 | 0.779 | 0.909 / 0.598 | **0.181** / 0.206 | +0.025 [-0.002, +0.051] | **0.136** / 0.180 | 0.139 / 0.218 |
| 4 | 346 | 0.786 | 0.910 / 0.628 | **0.173** / 0.193 | +0.020 [-0.012, +0.048] | **0.130** / 0.159 | 0.168 / 0.206 |
| 5* | 317 | 0.786 | 0.911 / 0.628 | 0.169 / 0.193 | +0.024 | 0.129 / 0.157 | |
| 6* | 289 | 0.775 | 0.914 / 0.630 | 0.176 / 0.196 | +0.020 | 0.139 / 0.146 | |
| 7* | 253 | 0.787 | 0.914 / 0.631 | 0.166 / 0.192 | +0.026 | 0.128 / 0.155 | |
| 8* | 219 | 0.817 | 0.914 / 0.633 | 0.144 / 0.184 | +0.040 | 0.103 / 0.185 | |

\* Descriptive only (h = 4 cell reused, as production would). Log loss at h = 1: 1.856 / 0.685.

| Gate | h = 1 | h = 2 | h = 3 | h = 4 |
|---|---|---|---|---|
| c1 Brier CI | pass | pass | fail | fail |
| c2 ECE <= 0.06 | **fail** (0.068) | fail | fail | fail |
| c3 max bin gap | pass | fail | fail | fail |
| c4 ECE < current | pass | pass | fail | fail |
| c5 each season | pass | pass | fail | fail |
| c6 subgroups | pass | fail (missed 2-3: 0.198 -> 0.251) | fail (all) | fail (all) |

**ROS totals** (next 4 team games, n = 346 episodes, actual mean 19.2 points): r1 and r2 both fail.

| | Mean total | MAE | RMSE | Bias (actual - predicted) |
|---|---|---|---|---|
| Current | 24.6 | 11.84 | 16.66 | -5.40 |
| Candidate | 13.7 | 11.46 | 17.21 | +5.57 |
| Diff | | **-0.37 [-1.17, +0.48]** | MSE +18.4 [-20.6, +61.3] | |

By position (MAE current -> candidate):

| Position | n | MAE | Diff CI |
|---|---|---|---|
| RB | 104 | 13.24 -> 11.88 | [-3.61, +1.14] |
| WR | 151 | 12.33 -> 12.61 | [-1.34, +1.95] |
| TE | 91 | 9.41 -> 9.08 | [-2.04, +1.56] |

**Outcome: `retain_current`.** No promotion proposal is written.

**Exploratory 2025 replay** (`ipm-1.0.0` out-of-sample; the reports carry no timestamps; not gated):

| h | n | Observed | Mean p cur / cand | Brier cur / cand | Brier diff [95% CI] | ECE cur / cand |
|---|---|---|---|---|---|---|
| 1 | 291 | 0.409 | 0.896 / 0.348 | 0.491 / 0.243 | -0.249 [-0.318, -0.175] | 0.497 / 0.061 |
| 2 | 275 | 0.618 | 0.897 / 0.538 | 0.313 / 0.245 | -0.069 [-0.116, -0.021] | 0.285 / 0.080 |
| 3 | 257 | 0.685 | 0.896 / 0.611 | 0.249 / 0.219 | -0.030 [-0.065, +0.005] | 0.216 / 0.074 |
| 4 | 242 | 0.665 | 0.895 / 0.639 | 0.264 / 0.222 | -0.042 [-0.071, -0.010] | 0.236 / 0.026 |

ROS totals 2025 (n = 242, actual mean 12.0):

* MAE 14.16 -> 10.19, diff -3.97 [-5.08, -2.88].
* Bias -10.76 -> -0.77.

**A fixed 2019-22 table instead of the per-season refit** makes almost no difference: mean p 0.341 / 0.521 / 0.592 /
0.618 and Brier 0.245 / 0.242 / 0.208 / 0.197 at h = 1..4. Adding one season moves a 4-5 season pool very little.

## 3. Why it failed (post hoc; changes nothing above)

**The fit population is not the population the table would be applied to.** `fit_return_rates` counts every
Out/Doubtful episode. Some of those players are already on a reserve list (RES; a few CUT/EXE) by week W. Those
players are withheld by `build_live` and never get a ROS p_play, and they almost never play in the next two games:

| Seasons | h | Reserve at W: n, return rate | ACT/INA at W: n, return rate |
|---|---|---|---|
| 2019-22 | 1 | 142, 0.014 | 1,006, 0.381 |
| 2019-22 | 2 | 136, 0.022 | 958, 0.582 |
| 2019-22 | 3 | 122, 0.164 | 906, 0.640 |
| 2019-22 | 4 | 113, 0.274 | 848, 0.657 |
| 2023-24 | 1 | 127, 0.118 | 448, 0.406 |
| 2023-24 | 3 | 102, 0.235 | 379, 0.784 |

The reserve share of episodes grew from 8% (2019) to 24% (2024), so the bias of the pooled table grows with it.

**Return rates at h >= 2 trend upward, then dip** (ACT/INA population, share appearing in the team's h-th game after
W):

| Season | h = 1 | h = 2 | h = 3 | h = 4 |
|---|---|---|---|---|
| 2019 | 0.312 | 0.506 | 0.586 | 0.606 |
| 2020 | 0.406 | 0.565 | 0.630 | 0.635 |
| 2021 | 0.437 | 0.646 | 0.651 | 0.684 |
| 2022 | 0.377 | 0.615 | 0.691 | 0.697 |
| 2023 | 0.386 | 0.635 | 0.765 | 0.778 |
| 2024 | 0.425 | 0.676 | 0.802 | 0.804 |
| 2025 | 0.401 | 0.612 | 0.687 | 0.673 |
| 2026 (weeks 1-6) | 0.419 | 0.750 | 0.846 | 0.833 |

* **h = 1 is stable** (0.31-0.44) and is where the current path is worst (0.91).
* **h >= 3 swings by 0.1-0.15 between seasons.** The current path's flat ~0.91 happened to sit close to the 2023-24
  rates (0.78-0.80) and far from the 2025 rates (0.67-0.69). Neither path is calibrated there.

The candidate also isn't monotone in h when a cell falls back to a pooled cell. For example, Breece Hall gets
0.400 / 0.556 / 0.526 / 0.618 for h = 1..4.

## 4. Live example (2026, cutoff week 6; table refit on 2019-2025)

22 RB/WR/TE were Out/Doubtful on the week-6 report. 15 are in the current path's population for their next four
games (the rest are reserve or not ACT/INA).

| | h = 1 | h = 2 | h = 3 | h = 4 |
|---|---|---|---|---|
| Mean p, current / candidate | 0.953 / 0.344 | 0.953 / 0.528 | 0.953 / 0.612 | 0.953 / 0.641 |
| Saquon Barkley (missed 1; weeks 7, 8, 9, 11) | 0.927 / 0.399 | 0.927 / 0.599 | 0.927 / 0.636 | 0.927 / 0.604 |
| Breece Hall (missed 2; weeks 7-10) | 0.962 / 0.400 | 0.962 / 0.556 | 0.962 / 0.526 | 0.962 / 0.618 |

## 5. What is live, what is next

| Item | Status |
|---|---|
| ROS p_play for players Out/Doubtful at the cutoff | **unchanged**: still the undesignated `ipm-1.0.0` p_play (labeled experimental) |
| ros-avail-0.1.0 (table, refit per season, h = 1..4) | `retain_current` (failed c2-c6, r1, r2) |
| ros-avail-0.1.1 (`NEXT_CANDIDATE`) | pre-registered for prospective evaluation only; not implemented or run |

Proposed next work, by expected value:

1. **Prospective shadow of ros-avail-0.1.1.** A write-once pregame ledger from the 2026 week-7 cutoff, scored after the
   season with the gates in `NEXT_CANDIDATE` (h = 1 and 2 only). It needs `fit_return_rates` to accept the roster-status
   filter.
2. **Re-project after Friday's final injury report** (`injury_workload_handoff.md` section 9, item 3). The production run
   freezes mid-week. A player Questionable on Wednesday and Out on Friday never gets an Out designation in that run, so
   any table keyed on the cutoff designation fires less often live than in this replay. The replay reads final pregame
   reports.
3. **Designation source in production** (only if a candidate passes). Use the horizon-0 participation designations
   (nflverse week-W report; ESPN status fallback for teams without one). The ESPN-fallback branch has no historical
   replay.

Promotion of any version needs the owner's approval. The merge coordinator session is told before any production
code (`build_live`, `projection_source.py`, the registry, approved artifacts, production runs) changes.

## Reproduce

```
cd backend
uv run python ../scripts/ros_availability_research.py evaluate   # ~145 s; writes <ifp-dir>/research/ros_availability/
uv run python ../scripts/ros_availability_research.py live
uv run pytest -q tests/test_ros_availability.py
```

Data: nflverse 2019-2025 (history DB) and 2026 weeks 1-6 (live DB), participation artifact `ipm-1.0.0`, league scoring
of the connected league. Outputs (git-ignored): `evaluation.json`, `cohort.parquet` (every scored row),
`live_2026w6.parquet`, and the cached ifp-2 training store.

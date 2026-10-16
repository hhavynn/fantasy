# Shallow gradient boosting vs ridge (ifp-hgb, research only)

Date: 2026-10-15. Data: local nflverse history 2019-2025 and live 2026 weeks 1-5, with the same
feature store, rows and scoring fingerprint (`e165b59686110c7d`) as the ifp-2 report. Code:

* `backend/app/analytics/hgb_research.py`
* `scripts/hgb_research.py`
* tests: `backend/tests/test_hgb_research.py`

**Answer: retain ridge.** A small, pre-registered family of shallow
`HistGradientBoostingRegressor` models on ifp-2's own features is:

* **worse than the frozen ifp-2 ridge on development** (2021-22): +0.025 MAE, CI
  [+0.003, +0.045]. All 8 configurations lose at every position.
* **not better on the already-inspected 2023-26 replay**: +0.004 MAE, CI [−0.009, +0.016].
* **worse on Start/Sit decisions**: pair accuracy 65.29% vs 65.47%; close-call regret 2.903 vs
  2.868.

Its errors correlate 0.98-0.99 with ridge's, so a convex ensemble is not justified under the
pre-registered rule and was not built. The model is roughly 30× larger and 20× slower to train.

The configuration was frozen before any replay. Its first pregame research forecast (2026 week 6)
is in a write-once research ledger, as prospective evidence only. Nothing is promoted, and ridge
stays the model everywhere.

## 1. Audit and dependency design

**Same footing as ridge.** HGB uses exactly what the ridge models use:

* ifp-2's features, in ifp-2's order.
* ifp-2's training rows (`challenger.train_rows`: strictly earlier weeks, labeled, at least one
  recent game, from 2020).
* ifp-2's 36-week recency weights (QB unweighted).
* The same availability conditioning: no forecast without a recent game. Coverage is 97.5%,
  identical for every model, and every comparison uses identical rows (`common_rows`).
* Squared loss, so predictions are means, as `expected_points = P(appears) × points` needs.
* The same floor at 0.

**Missing data.**

* nflverse gaps stay NaN, and HGB routes them per split.
* The existing indicators (`xfp_missing`, `snap_missing`, `rz_missing`) stay features.
* Measured zeros stay zero.
* Target-game snaps, injuries, xFP and points are never features (tested).

The missing-source indicators carry no extra information for trees: removing or shuffling them
changes MAE by 0.000, because the NaN routing already encodes them. No unstable missingness
pattern was found.

**Correlated features.** Many of the 33-35 features are near-duplicates: raw and healthy usage,
several point averages, the established workload and the previous-season prior. Trees split among
them arbitrarily, so the group-level importance below is the meaningful unit, not single features.

**Dependencies.**

* scikit-learn 1.9.1 (+ scipy, threadpoolctl) is in a new `research` dependency group, included by
  `dev`. `uv.lock` gains those three packages only.
* Vercel installs with `uv sync --no-dev`, so the hosted bundle never gets them (`uv export
  --no-dev` lists none).
* The module imports scikit-learn lazily inside its fitting functions.
* A test asserts that importing `app.main`, and even `hgb_research` itself, loads neither sklearn
  nor scipy, and that no other app file mentions them.
* No XGBoost, LightGBM or CatBoost.

**Artifacts without pickle.** Fitted trees are exported to an explicit JSON schema
(`hgb-research-artifact/v1`):

* Each node stores value, feature index, threshold, missing-goes-left, children and the leaf flag.
* `predict_trees` evaluates them with numpy alone, and is tested to reproduce scikit-learn exactly
  (max difference 0.0 on real data).
* Each artifact records its training cutoff, frozen-config hash, training-data hash, scoring
  fingerprint and scikit-learn version, and is sealed with a content hash.
* `from_dict` refuses a wrong schema, any status other than `research_only`, or edited content.
* The production loader refuses it by schema.
* Nothing picks "the newest model on disk": the ledger names the artifact hash it used.

## 2. Pre-registered search (development 2021-22 only)

* Fixed: squared loss, max_depth 3, L2 1.0, learning rate 0.05, no early stopping (it would use a
  random validation split), random_state 0.
* Searched: max_leaf_nodes {7, 15} × min_samples_leaf {50, 200} × max_iter {150, 300}, so 8
  configurations.
* Each configuration is refit every week on strictly earlier rows (identical cutoffs to ridge).
* The configuration (and any ensemble weight) is chosen on unrounded development MAE. The decision
  rules are applied to unrounded values; the tables show them rounded.
* Only fully final, fully ingested weeks are used (every week in the current data is).

| Position | ifp-2 ridge | Best HGB (chosen) | Worst HGB |
|---|---|---|---|
| QB | 6.008 | 6.051 (7 leaves, min leaf 50, 150 iterations) | 6.123 |
| RB | 4.296 | 4.329 (7, 200, 150) | 4.360 |
| WR | 4.438 | 4.456 (15, 200, 150) | 4.468 |
| TE | 2.990 | 3.010 (15, 200, 300) | 3.034 |

Larger leaves (200 rows) were chosen for RB, WR and TE: the data reward heavy smoothing.

Frozen configuration `dd0a657cf9c034f3` was written once at 23:11 UTC, before any 2023-26 HGB
prediction and before the week-6 forecast.

**Ensemble decision.** Development errors of ridge and HGB correlate at 0.981, above the
pre-registered 0.97 threshold for "complementary". No ensemble weights were fitted. The 2023-26
correlation is 0.986.

## 3. Results

**MAE / RMSE / bias on identical rows, with week-clustered CI of HGB − ifp-2:**

| Window | n | C (ifp-1) | ifp-2 | HGB | Additive HGB (depth 1) |
|---|---|---|---|---|---|
| Development 2021-22 | 13,372 | 4.216 | 4.202 / 5.872 / −0.21 | 4.228 / 5.901 / −0.21 (+0.025 [+0.003, +0.045]) | — |
| 2023-24, inspected | 13,345 | 4.031 | 4.016 / 5.716 / +0.05 | 4.018 / 5.711 / +0.00 (+0.002 [−0.014, +0.019]) | 4.035 (+0.019) |
| 2025-26, inspected | 8,645 | 4.068 | 4.050 / 5.790 / −0.03 | 4.057 / 5.800 / −0.03 (+0.007 [−0.013, +0.023]) | 4.081 (+0.031) |
| 2023-26 pooled | 21,990 | 4.045 | 4.030 / 5.745 / +0.02 | 4.034 / 5.746 / −0.01 (+0.004 [−0.009, +0.016]) | 4.053 (+0.024 [+0.012, +0.035]) |

**By position, HGB − ifp-2, with CIs:**

| Position | Development | 2023-26 |
|---|---|---|
| QB | +0.042 [−0.059, +0.146] | −0.033 [−0.107, +0.044] |
| RB | +0.034 [−0.003, +0.074] | +0.007 [−0.019, +0.031] |
| WR | +0.018 [−0.010, +0.042] | +0.002 [−0.015, +0.020] |
| TE | +0.020 [−0.008, +0.049] | +0.019 [−0.004, +0.041] |

No position is significantly worse in the replay, and none is better.

**Decisions** (2023-26 pairs, same 48-RB / 60-WR / 24-QB / 24-TE pools):

| | ifp-2 | HGB |
|---|---|---|
| Pair accuracy | 65.47% | 65.29% |
| Mean regret | 2.492 | 2.515 |
| Close calls (79,970 pairs): accuracy | 60.42% | 60.10% |
| Close calls: regret | 2.868 | 2.903 |

**Cohorts, 2023-26 pooled.** MAE, with bias in parentheses and the HGB − ifp-2 CI.

| Cohort | n | ifp-2 | HGB | Additive |
|---|---|---|---|---|
| Starters pool | 9,204 | 5.794 (+0.17) | 5.818 (+0.06) [−0.003, +0.048] | 5.818 |
| High-volume starters (healthy xFP ≥ 15) | 1,686 | 6.737 (+0.22) | 6.781 (+0.01) [−0.029, +0.111] | 6.741 |
| Injury return | 778 | 4.317 (−0.03) | 4.307 (+0.19) [−0.105, +0.084] | 4.303 |
| Repeated exits | 302 | 4.023 | 4.004 | 4.015 |
| Role drop (last game `role_low`) | 1,682 | 3.120 (−0.11) | 3.199 (−0.33) [+0.040, +0.119] | 3.250 |
| Team changed | 4,753 | 3.585 (−0.14) | 3.618 (−0.23) [+0.001, +0.063] | 3.650 |
| Breakout (recent xFP > 1.4 × established) | 225 | 4.663 | 4.737 [−0.057, +0.210] | 4.740 |
| Early season (weeks 1-4) | 6,085 | 4.116 | 4.110 [−0.030, +0.017] | 4.129 |
| Limited history (< 3 games) | 1,032 | 3.059 | 2.993 [−0.107, −0.022] | 3.044 |

The pattern is consistent: HGB helps where ridge's linear extrapolation is poor (sparse history,
QB). It loses clearly where ridge's explicit flags already encode the structure: role drops and
team changes, both significantly worse, and both over-projected. The development cohorts show the
same signs (role drop +0.064, team change +0.057; repeated exits −0.185 [−0.343, −0.028]).

## 4. What the model learned

**Out-of-sample permutation importance, 2023-26** (MAE increase when a feature group is shuffled
within each week; every comparison on identical rows):

| Group | QB | RB | WR | TE | All |
|---|---|---|---|---|---|
| Production (point averages) | 1.053 | 0.970 | 0.725 | 0.360 | 0.730 |
| Usage (opportunity) | 0.214 | 0.799 | 0.521 | 0.546 | 0.567 |
| Established workload / prior | — | 0.194 | 0.459 | 0.333 | 0.315 |
| Trend / role | 0.286 | 0.055 | 0.068 | 0.066 | 0.085 |
| Matchup / team | 0.046 | 0.003 | 0.012 | 0.006 | 0.011 |
| Game-class flags | — | 0.005 | 0.000 | 0.000 | 0.001 |
| Missing indicators | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |

Two corrections were made before these numbers:

* A first version of the pooled column mixed positions without a group, such as QB without flags,
  into the baseline. Now every row is compared with its own unpermuted prediction.
* Following a Codex review, values are shuffled only among forecastable rows: players without a
  recent game neither donate nor receive values.

**Ablation: refit without the group, development MAE increase:**

| Group removed | QB | RB | WR | TE |
|---|---|---|---|---|
| Production | +0.070 | +0.024 | +0.050 | +0.013 |
| Trend / role | +0.057 | +0.048 | +0.013 | +0.022 |
| Matchup / team | +0.034 | +0.004 | +0.009 | +0.027 |
| Usage | −0.019 | +0.016 | +0.009 | +0.025 |
| Established / prior | — | +0.004 | +0.009 | +0.012 |
| Game-class flags | — | +0.001 | −0.001 | 0.000 |

Correlated groups substitute for each other, so refit ablations are much smaller than permutation
importances. The game-class flags that give ridge its largest cohort gains (role drops −3.5%) carry
almost no weight in the trees. With minimum leaves of 50-200 rows, a flag set on 3-8% of rows
rarely wins a split against the continuous usage features. This is the most plausible reason HGB
loses on role drops and team changes.

**Interactions vs nonlinearity.** The additive model (depth-1 trees: nonlinear, no interactions)
is clearly worse than both ridge and depth-3 HGB (2023-26: 4.053 vs 4.030 and 4.034). So:

* Per-feature nonlinearity alone costs accuracy relative to the linear fit: binning noise on smooth
  relationships.
* Interactions recover most, but not all, of that loss.

There is no evidence of a large nonlinear or interaction effect that ridge misses. The motivating
interactions are already linear-ish in these features, or too rare to learn at this sample size:

* status × starter role;
* rush share × offense volume;
* prior × team change.

Importance does not establish causation, and none of the above is training-set importance.

**Case studies** (2023-26 rows where |HGB − ridge| ≥ 3 points; there are 239 of 21,990, and HGB is
closer in 56% of them). The largest disagreements are mostly QB backups and spot starters, where
trees split on snap share and production:

| Player-week | Context | ifp-2 | HGB | Actual | Closer |
|---|---|---|---|---|---|
| Jalen Hurts 2025 wk 1 | 9.4 carries/g, last season's established role | 14.4 | 20.0 | 24.3 | HGB |
| Mitchell Trubisky 2024 wk 2 | team changed, 63% snaps | 9.7 | 3.7 | −0.2 | HGB |
| Deshaun Watson 2023 wk 7 | 98% snaps, returning | 16.3 | 10.7 | −1.8 | HGB |
| Tyrod Taylor 2025 wk 7 | 50% snaps, spot starter | 13.0 | 7.0 | 2.1 | HGB |
| Mason Rudolph 2024 wk 15 | 97% snaps, new starter | 14.1 | 7.9 | 15.8 | ridge |
| Marcus Mariota 2025 wk 3 | 51% snaps, 3 recent games | 12.5 | 6.5 | 20.3 | ridge |
| Tim Boyle 2023 wk 11 | 1 recent game, team changed | 0.1 | 5.4 | −0.6 | ridge |
| George Kittle 2023 wk 4 | 82% snaps, 6.4 targets/g | 10.7 | 15.7 | 1.9 | ridge |

The trees react sharply to snap-share and role context for backup QBs, in both directions. Their
large moves are right only slightly more often than not, which is why the pooled MAE does not
move.

## 5. Cost

| | Ridge (ifp-2) | HGB |
|---|---|---|
| Fit per position-week | ~0.05 s | ~1.2-1.9 s single-threaded (300-400 s per 4-position, 36-week walk-forward) |
| Inference for a week | ~7 ms (WR) | ~26 ms for 386 rows (numpy JSON trees) |
| Artifact | 28.6 KB (ifp-1, all positions) | 921 KB JSON (150-300 trees per position) |
| Dependencies | numpy | scikit-learn + scipy (research group) to fit; numpy to predict |

The development search, diagnostic replay, ablation and cases take about 15 minutes on 4 CPUs.
The complexity buys no accuracy.

## 6. Decision (`HGB_DECISION_RULES`) and prospective plan

* Development gain: **no** (CI [+0.003, +0.045] is above 0).
* 2023-26 replay: pooled MAE is worse (+0.004), so outcome **`retain_ridge`**. Other guards:
  * passed: no position significantly worse; RMSE; bias; starters pool within 1% (+0.4%); pair
    accuracy within 0.5 pp (−0.18 pp); close-call regret within 2% (+1.2%);
  * still worse on the starters pool, pair accuracy and close-call regret, just within tolerance.
* Recommendation: **retain ridge** for every position. Do not add HGB to the weekly pipeline or the
  "Ours" selector, and do not build a ridge/HGB ensemble.

**Prospective evidence (frozen, never backdated).**

* `hgb_research.py predict --week 6` ran at 2026-10-15 23:12 UTC, before the first week-6 kickoff
  (00:15 UTC).
* It trained the frozen configuration on all final weeks before week 6.
* It wrote these files once:
  * artifact `ifp-hgb-0.1.0-s2026w06-a5d6bac88c1f9154.json`;
  * ledger `s2026w06-pregame.json`, with 436 roster players and 413 forecasts: the same population
    the weekly pipeline froze.
* A rerun cannot overwrite either file. Rows of teams that have already kicked off are excluded.
* Artifacts are now stored at one deterministic path per week (`ifp-hgb-0.1.0-s{season}w{week}.json`).
  A rerun after an interrupted or concurrent run loads that stored artifact instead of fitting another,
  and the ledger names the file it used. The week-6 artifact above predates this naming.
* The ledger is standalone (`data/models/research/hgb/ledger/`, git-ignored) rather than part of
  the weekly ledger. Adding a third model to that ledger's run schema would change frozen run
  hashes and the weekly evaluator's inputs, which this PR must not touch.

If anyone wants more evidence before closing the question:

1. Run `predict` before each Thursday kickoff for 8+ weeks.
2. Join the ledger rows to the weekly ledger's champion and challenger rows (same `gsis_id`, season
   and week).
3. Score the result with the existing finality labels.

The historical results give no reason to expect a different answer. Historical comparisons alone
never establish promotion readiness.

## 7. What changes, and what does not

Research only:

* the module, its script, its tests and this report;
* a `research` dependency group (lockfile: +scikit-learn, scipy, threadpoolctl);
* one README note.

Unchanged:

* ridge in production, ifp-1.0.0 and ifp-2.0.0 artifacts and config;
* the weekly pipeline and its ledgers;
* the ESPN/Ours switch, Start/Sit, trades, waivers, matchups, the API and its runtime
  dependencies, and schedules.

Reproduce (needs `uv sync` with dev groups, and the play-by-play exits from `weekly_learning.py
sync-exits`):

```
cd backend
uv run python ../scripts/hgb_research.py develop      # ~5 min; writes the frozen config once
uv run python ../scripts/hgb_research.py diagnose     # ~3 min
uv run python ../scripts/hgb_research.py ablate       # ~5 min
uv run python ../scripts/hgb_research.py cases
uv run python ../scripts/hgb_research.py predict --week 7   # before Thursday's kickoff
```

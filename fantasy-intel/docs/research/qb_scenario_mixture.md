# QB scenario mixture (`ifp-ctx-p2`, research only)

**Question.** p1's QB context (`ifp-ctx-p1`, [qb_teammate_context.md](qb_teammate_context.md)) cut QB
MAE by 0.15-0.29 in every window but was rejected because QB |bias| got worse by more than 0.15 in
development. Can we keep the QB signal and remove that bias? This study tests two fixes:

* give week 1 (and a team's first game) real starter evidence instead of "unknown";
* replace p1's single committed starter with calibrated start probabilities over the plausible QBs,
  then project each scenario and mix.

**Short answer: don't adopt. The QB part needs prospective evidence.**

* **Where p1's bias comes from** (section 1):
  * **Week 1, consistently.** When the starter is unknown, the imputed flag inflates every week-1 QB,
    and backups most (bias -11 to -12 points on backups who appear). This costs -0.12 to -0.16 of
    bias in **every** window.
  * **A 2021-22-only overshoot on expected starters.** p1 gives expected starters about 0.7 points
    more than ifp-2. Development's starters were already slightly overprojected, so bias moves another
    -0.11. In 2023-26 those starters were underprojected, so the same shift *helps*.
  * **Not uncertain starters or teammates.** Uncertain starters and identification misses cost MAE,
    not bias. Teammate features change QB bias by under 0.01.
* **The start-probability model is well calibrated, and better than p1's evidence classes** (section 3).
  * Brier 0.142 vs 0.170, log loss 0.30 vs 0.36, top-1 accuracy 90.7% vs 88.6% (2021-26, walk-forward).
  * Reliability is within about 0.05 in every bin.
  * Week 1 / first game: top-1 85% vs 48%, Brier 0.21 vs 0.58.
* **The QB scenario model (S2) is the best QB result so far** (section 4).
  * QB MAE: -0.264 [-0.383, -0.149] in development, -0.340 [-0.451, -0.207] in confirmation and
    -0.372 [-0.479, -0.274] in the 2025-26 replay. About 1.3-2.1x p1's gain, with lower RMSE in every
    window.
  * Bias improves on p1 in all three windows. In confirmation it is better than ifp-2's (+0.032 vs +0.167).
  * The week-1 backup failure is gone: bias on week-1 backups is -4.7 / -5.8 / +0.4, against p1's
    -12.1 / -11.1 / -5.3.
* **It still fails the pre-registered development bias guard, by 0.035.** QB bias is -0.710 against a
  limit of |-0.525| + 0.15 = 0.675.
  * What is left is the same 2021-22 starter overshoot (section 5). It changes sign across windows.
  * So by the rule, QB keeps ifp-2, and the frozen p2 composite adopts only RB (S2 + teammates).
  * That composite fails confirmation the way p1's did: pooled MAE CI crosses 0, and the teammate
    mechanism check fails. **Outcome: `retain_baseline`.**
* **Recommendation.** Do not adopt p2, and change nothing in production. S2 at QB is worth a separately
  pre-registered prospective shadow, gated on bias exactly like p1. It must not be re-selected on these
  historical windows (section 7).

Code (new files only):
* `scripts/qb_scenarios.py`: candidates, start model, P(start | appears), scenario columns, mixing,
  walk-forward.
* `scripts/qb_scenario_research.py`: `decompose`, `build`, `calibration`, `develop`, `evaluate`.
* `backend/tests/test_qb_scenarios.py`.

Outputs go to `data/models/research/qb_scenarios/` (git-ignored), with its own copy of the feature store.
p1 is untouched: its definition, features, frozen selection, artifacts, ledgers and `research/context/`
outputs. So are `learning_cycle.py`, launchd, the registry, live projection code and the app. Nothing in
`app/` imports p2 (tested).

## 1. Where p1's QB bias comes from (all windows)

Reproduced exactly from `context_research.py develop` / `evaluate`: QB MAE -0.146 / -0.159 / -0.291 and
development bias -0.525 to -0.792. Bias is actual minus projected, so negative means overprojection. Every
cell's share is `sum(actual - projected) / N(window)`, so the shares add up to the window's bias.

**By season phase** (change in bias share, p1 minus ifp-2):

| Window | ifp-2 bias | p1 bias | Change | Week 1 | Weeks 2-4 | Weeks 5+ |
|---|---|---|---|---|---|---|
| Development 2021-22 (n = 1,280) | -0.525 | -0.792 | **-0.267** | **-0.155** | -0.087 | -0.025 |
| Confirmation 2023-24 (n = 1,330) | +0.167 | +0.171 | +0.004 | **-0.117** | -0.010 | +0.131 |
| Exploratory 2025-26 (n = 834) | -0.074 | -0.167 | -0.093 | **-0.149** | +0.026 | +0.030 |

**By role** (p1's expected QB vs the realized starter; change in bias share):

| Cell | Dev | Conf | Expl | What p1 does |
|---|---|---|---|---|
| Expected, started | **-0.533** | -0.427 | -0.354 | starters +0.5-0.7 points vs ifp-2 |
| Not expected, did not start (relief) | +0.257 | +0.400 | +0.333 | relief appearances lowered (correct) |
| Not expected, started (identification miss) | +0.179 | +0.171 | +0.102 | the real starter underprojected further |
| Expected, did not start | -0.015 | -0.023 | -0.025 | |
| Week 1 (unknown), starter | -0.130 | -0.076 | -0.124 | imputed flag inflates everyone |
| Week 1 (unknown), backup who appeared | -0.025 | -0.041 | -0.025 | bias -12.1 / -11.1 / -5.3 points per row |

Reading:

* **Mostly a reallocation.** p1 moves points from non-expected QBs to expected QBs. In weeks 2+ that is
  roughly bias-neutral: +0.12 in confirmation, +0.06 in the replay.
* **Development's overshoot.** In development it nets to -0.11, because ifp-2 already slightly
  overprojected starters (-0.15) and p1 pushed them to -0.88.
* **Week 1 is the one consistently negative piece:** -0.12 to -0.16 in all three windows. It is the
  failure mode the earlier study diagnosed: with no expected starter, `qb_is_expected` is imputed with
  the training mean.
* **Uncertain starters are not the bias source.** p1's `uncertain` class *reduces* the bias share
  (+0.05 / +0.09 / +0.02).
* **Identification misses cost MAE instead.** Their MAE share is +0.06 / +0.07 / +0.07.
* **Teammate effects are about zero on QB bias** (C vs A: -0.786 vs -0.792 in development).

## 2. Method (fixed in `qb_scenarios.py` and committed before any p2 points backtest)

**Candidates (pregame only).**
* **Team with a game this season:** every QB with a game or passing stats for the team this season
  before W. Add rostered QBs (ACT/INA in the latest roster week before W) and the previous starter.
  Anyone who is gone (another team, or no longer on the roster) is dropped.
* **Team's first game (week 1):** the QBs on the team's **week-W roster**, using team membership only.
  * The ACT vs INA game-day distinction is not used. Practice-squad and reserve QBs are excluded.
  * This is the one new pregame input relative to p1, which used roster weeks before W only. Team
    membership on game week is public before kickoff. The inactive designation is not used.
* Coverage: the realized starter is a candidate in 99.5% of team-weeks, and 100% of first games
  (2020-26, 3,388 team-weeks, 2.7 candidates on average).

**Start model.**
* A conditional logit: a softmax within the team-week, so each team-week's probabilities sum to 1.
* **Features** (17 in total):
  * previous starter, season start share, last season's start share for the team, last season's
    dropbacks;
  * first-game interactions of those two;
  * relief attempts last game, takeover;
  * p1's expected QB (overall and by class), p1's alternative;
  * Questionable / Doubtful / Out-or-inactive;
  * shrunk EPA relative to the replacement prior, no history.
* **Fitting:**
  * standardized features, ridge 1.0 on the coefficients (a separation guard only, never tuned);
  * Newton steps, refitted walk-forward every week on team-weeks from 2020 strictly before W (asserted);
  * the label is the realized starter of those past games.
* p1's evidence classes are inputs, so the logit calibrates them rather than replacing them.

**From start probability to points.**
* **QB rows.** The QB projection is ifp-2's spec plus `s_start` and `qb_self_epa`.
  * In **training**, `s_start` is whether the QB actually started (past weeks only).
  * For the label population (QBs who appeared), the scenario weight is
    pi = P(start | appears) = p / (p + (1 - p) r). Here r is the walk-forward relief-appearance rate of
    non-starters, by probability rank in the team-week. Appearing is itself evidence of starting: a
    p = 0.05 backup has pi of about 0.3.
  * The forecast is `pi x f(start) + (1 - pi) x f(no start)`.
  * If a team-week has no candidates at all, ifp-2's projection is used, never an imputed flag.
* **RB/WR/TE rows.** `qb_epa_delta`, `qb_cpoe_delta` and `qb_changed` (p1's definitions) are computed
  for the **realized** starter in training.
  * The forecast mixes the projections under the two most likely starters, weighted by their
    renormalized probabilities.
* **Why an explicit mixture.** The ridge is linear, so the mixture equals plugging in expected feature
  values. The 0 floor is the only nonlinearity, so the mixture is done explicitly.

**Candidates and rules.**
* **S2:** ifp-2 + the scenario QB group.
* **SC2:** S2 + p1's teammate group, unchanged.
* **S2h:** an ablation, reported only. It is the same fit with a hard top-1 starter.
* **Selection** (development 2021-22) uses exactly p1's rule:
  * MAE diff <= -0.010, with a week-clustered CI upper bound < 0;
  * |bias| not worse than ifp-2's by more than 0.15.
* **Confirmation** (2023-24) uses p1's checks c1-c7.
* 2025-26 is reported only. Its injury reports carry no timestamp.

**Disclosed fix after the pre-registration commit.**
* The first development run named the scenario candidate `A2`. The feature store already has a baseline
  column `pred_A2`, which won the join, so the report scored the store's baseline instead of the
  candidate.
* The candidates were renamed (S2 / SC2 / S2h), and a guard now refuses any prediction column that
  collides with a store column.
* The bad frozen selection was deleted and development re-run. The re-run selection was identical: QB
  ifp-2, RB SC2, WR ifp-2, TE ifp-2.
* No feature, constant or rule changed.

## 3. Start-probability calibration (walk-forward, no points labels)

Team-week multi-class Brier and log loss (a realized starter outside the candidate set counts as
probability 0). "p1 classes" turns p1's evidence classes into probabilities walk-forward:
* the expected QB gets his class's start rate in earlier team-weeks;
* the rest of the probability is split equally among the other candidates;
* unknown -> uniform.

| Window | Team-weeks | Brier p2 / p1 classes / p1 hard | Log loss p2 / p1 classes | Top-1 p2 / p1 |
|---|---|---|---|---|
| Development | 1,086 | **0.156** / 0.178 / 0.232 | **0.353** / 0.396 | **90.2%** / 88.6% |
| Confirmation | 1,088 | **0.145** / 0.167 / 0.224 | **0.289** / 0.337 | **90.1%** / 88.4% |
| Exploratory | 702 | **0.117** / 0.161 / 0.217 | **0.252** / 0.328 | **92.5%** / 89.0% |
| First game only (192) | | **0.206** / 0.585 | **0.364** / 0.901 | **84.9%** / 48.4% |
| All 2021-26 | 2,876 | **0.142** / 0.170 / 0.225 | **0.304** / 0.357 | **90.7%** / 88.6% |

(First game pools the three windows. Source: `calibration.json`.)

**Reliability, all 2021-26 (candidate level, n = 7,651).**

| p_start bin | n | Mean p | Started |
|---|---|---|---|
| [0, 0.02) | 2,317 | 0.012 | 0.010 |
| [0.02, 0.10) | 1,916 | 0.041 | 0.041 |
| [0.10, 0.30) | 376 | 0.176 | 0.221 |
| [0.30, 0.50) | 205 | 0.391 | 0.415 |
| [0.50, 0.70) | 203 | 0.593 | 0.562 |
| [0.70, 0.90) | 319 | 0.831 | 0.787 |
| [0.90, 0.98) | 1,486 | 0.956 | 0.951 |
| [0.98, 1] | 829 | 0.987 | 0.983 |

p2 is slightly overconfident at 0.7-0.9 and slightly underconfident at 0.1-0.3. Per window the gaps stay
within about 0.09.

**p1's expected QB by class** (all windows): mean p2 probability vs observed start rate.

| Class | n | p2 | Observed |
|---|---|---|---|
| Supported | 2,334 | 0.958 | 0.960 |
| Probable | 145 | 0.710 | 0.662 |
| Uncertain | 205 | 0.540 | 0.581 |

**P(start | appears) on QBs who appeared:** Brier 0.048 (n = 3,580). The [0.90, 0.98) bin is slightly
overconfident: 0.956 projected vs 0.911 observed.

## 4. Points results (identical rows, week-clustered 95% CIs)

**QB rows** (MAE / bias; difference vs ifp-2 MAE with CI):

| Window | ifp-2 | p1 (A) | p2 S2 (mixture) | S2h (hard top-1, ablation) |
|---|---|---|---|---|
| Development 2021-22 (n = 1,280) | 6.008 / -0.525 | 5.862 / -0.792, -0.146 [-0.273, -0.003] | 5.745 / -0.710, **-0.264 [-0.383, -0.149]** | 5.815 / -0.141, -0.193 [-0.360, -0.024] |
| Confirmation 2023-24 (n = 1,330) | 6.056 / +0.167 | 5.897 / +0.171, -0.159 [-0.292, -0.015] | 5.716 / **+0.032**, **-0.340 [-0.451, -0.207]** | 5.766 / +0.513, -0.290 [-0.474, -0.093] |
| Exploratory 2025-26 (n = 834) | 6.552 / -0.074 | 6.261 / -0.167, -0.291 [-0.406, -0.184] | 6.180 / -0.162, **-0.372 [-0.479, -0.274]** | 6.153 / +0.146, -0.399 [-0.551, -0.259] |
| QB RMSE dev / conf / expl | 7.544 / 7.600 / 8.139 | 7.410 / 7.438 / 7.822 | **7.294 / 7.273 / 7.775** | 7.499 / 7.432 / 7.839 |

**Bias guard** (|bias| <= |ifp-2 bias| + 0.15; pre-registered on development, reported elsewhere):

| | Development | Confirmation | Exploratory | Weeks 1-4, conf |
|---|---|---|---|---|
| p1 A | fail (0.792 > 0.675) | pass | pass | fail (-0.83 vs -0.26) |
| p2 S2 | **fail (0.710 > 0.675)** | pass (better than ifp-2) | pass | fail (-0.73 vs -0.26) |
| S2h | pass | **fail** (0.513 > 0.317) | pass | pass |

S2h looks good on development bias only through cancellation. It underprojects backups who start (bias
+6.9) and overprojects starters, and the two offset on development. In confirmation the same ablation
fails the guard. The mixture is what calibrates the backup rows: across development's 275 non-expected QB
rows the mean residual is -0.16 under S2, against +2.0 under S2h.

**QB cohorts** (MAE; ifp-2 -> p1 -> S2):

| Cohort | Development | Confirmation | Exploratory |
|---|---|---|---|
| Changed expected QB | 6.50 -> 5.38 -> **5.19** (n = 189) | 6.79 -> 5.93 -> **5.51** (230) | 7.09 -> 5.90 -> **5.67** (130) |
| Week 1 | 6.86 -> 6.32 -> **5.92** (67) | 5.63 -> 6.63 -> 5.78 (69) | 7.32 -> 7.08 -> **6.87** (66) |
| Uncertain / unknown expected QB | 6.32 -> 6.35 -> **5.92** (151) | 6.09 -> 6.73 -> **5.85** (167) | 6.98 -> 6.81 -> **6.67** (121) |
| Expected QB wrong (evaluation only) | 7.03 -> 7.67 -> 7.40 (104) | 6.30 -> 7.13 -> 6.46 (108) | 7.36 -> 8.56 -> 8.27 (50) |
| Same QB | 5.86 -> 5.92 -> 5.84 | 5.92 -> 5.84 -> 5.76 | 6.36 -> 6.25 -> 6.21 |
| Starters pool | 6.21 -> 6.18 -> 6.09 | 6.30 -> 6.24 -> 6.17 | 6.78 -> 6.59 -> 6.51 |

* **Uncertain / unknown QBs.** p1 was significantly *worse* than ifp-2 on this cohort in confirmation
  (CI [+0.07, +1.01]). S2 is better than both (CI [-0.77, +0.10]).
* **Identification misses** still cost MAE. Against p1's penalty, the mixture's is about half in
  development, about a fifth in confirmation, and three quarters in the replay.
* **Week 1.** S2 fixes the backup overprojection in every window. The week-1 *starter* level is still
  noisy: in confirmation, week-1 starters are overprojected (bias -2.1 vs ifp-2 -0.5), and week-1 MAE is
  5.78 vs ifp-2's 5.63.

**Other positions.**
* The receivers' scenario QB group adds nothing over ifp-2 (S2 at RB/WR/TE is within ±0.004 in every
  window), the same as p1's A.
* SC2 tracks p1's C (the teammate group): RB -0.029, WR -0.014 in development, then RB -0.017, WR -0.021
  in confirmation.

## 5. What is left of the bias in S2 (development)

| Cell | ifp-2 share | S2 share | Change |
|---|---|---|---|
| Expected, started | -0.106 | -0.583 | **-0.477** |
| Not expected, relief appearance | -0.592 | -0.271 | +0.321 |
| Not expected, started | +0.169 | +0.237 | +0.068 |
| Week 1 (all) | +0.082 | -0.003 | -0.085 |
| Total | -0.525 | -0.710 | -0.185 |

The week-1 piece now moves bias *toward* zero relative to ifp-2. The remaining failure is development's
starter overshoot, the same one p1 has (-0.533).

It reverses in the other windows. On expected starters who started, ifp-2's bias is +1.08 and S2's is
+0.43 in confirmation. In the replay they are +0.58 and +0.10.

One explanation fits but is **not tested**:
* The start indicator ties a starter's projection to the pooled level of starters in the training data.
* QB rows carry no recency weighting (`challenger_config.json`: no `half_life_weeks` for QB).
* So early-2021 forecasts lean on 2020's high-scoring season.

A QB recency weight is the natural candidate. It would have to be pre-registered and judged prospectively,
not tuned on these windows.

## 6. Verdict under the pre-registered rules

**Development selection** (`frozen_selection_ifp-ctx-p2.json`, write-once):

| Position | S2 | SC2 | Adopted |
|---|---|---|---|
| QB | -0.264 [-0.383, -0.149], bias -0.710 (limit 0.675) | -0.266 [-0.395, -0.145], bias -0.713 | **ifp-2** (both fail the bias guard) |
| RB | +0.002 | -0.029 [-0.051, -0.008], bias -0.092 | **SC2** |
| WR | +0.002 | -0.014 [-0.029, **+0.001**] | **ifp-2** (CI) |
| TE | +0.004 | +0.005 | **ifp-2** |

**Confirmation 2023-24** (adopted position: RB).

| Check | Result |
|---|---|
| c1 pooled MAE (RB -0.017 [-0.036, +0.007]) | **fail** |
| c2 no adopted position significantly worse | pass |
| c3 RMSE (5.676 -> 5.652) | pass |
| c4 bias (-0.020 -> -0.027) | pass |
| c5 starters pool | pass |
| c6 Start/Sit pairs (68.98% -> 69.24%) | pass |
| c7 mechanism (RB: top target competitor out +0.135, RB1 out +0.098, changed QB -0.003) | **fail** |

Outcome: **`retain_baseline`**. The frozen composite's pooled difference is -0.004 in confirmation and
-0.008 in the replay.

## 7. Recommendation

**Do not adopt `ifp-ctx-p2`. No production, registry or automation change.**

**The QB scenario model needs prospective evidence.** It is the strongest QB-context result so far:
* better MAE than p1 in all three windows, about 1.3-2.1x p1's gain, with every CI below 0;
* better RMSE in every window;
* bias better than p1 everywhere, and better than ifp-2 in confirmation;
* the diagnosed week-1 failure fixed.

But it fails the development bias guard. Its residual bias is a starter-level shift that changes sign by
window. And confirmation 2023-24 has now been evaluated by this study family three times, after a
decomposition that looked at every window. Historical replay cannot settle it.

**Suggested next step** (not done here, because it touches the shadow automation another session owns):
* Pre-register a QB-only prospective candidate (QB S2; RB/WR/TE ifp-2, or p1's choices) with p1's gate.
  That means at least 8 final prospective weeks and at least 400 matched appearances, a CI upper bound
  below 0, |bias| within 0.15 and a starters pool not worse.
* Add a write-once pregame ledger for it, next to p1's.
* Report the start-probability calibration prospectively as well. Week-W roster snapshots would make the
  week-1 candidate rule verifiable.

## Reproduce

```
cd backend
uv run python ../scripts/context_research.py build && uv run python ../scripts/context_research.py develop \
  && uv run python ../scripts/context_research.py evaluate      # p1, unchanged (writes research/context/)
uv run python ../scripts/qb_scenario_research.py decompose      # section 1
uv run python ../scripts/qb_scenario_research.py calibration    # section 3
uv run python ../scripts/qb_scenario_research.py develop        # writes the frozen p2 selection once
uv run python ../scripts/qb_scenario_research.py evaluate       # sections 4-6
uv run pytest -q tests/test_qb_scenarios.py
```

All numbers above come from a snapshot of `fantasy.duckdb` / `nfl_history.duckdb` taken on 2026-10-17,
with the same store fingerprint as p1's re-run. Runtime: about 70 seconds to build the candidates and
under 30 seconds per command afterwards.
